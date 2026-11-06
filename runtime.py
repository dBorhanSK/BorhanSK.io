from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine

from cryptography.fernet import Fernet, InvalidToken
from playwright.async_api import Browser, async_playwright

from store import Store, parse_dt, utcnow
from telegram_web import TelegramWeb, TelegramWebError

log = logging.getLogger("borhan.runtime")


def owner_can_access_account(account: dict[str, Any] | None) -> bool:
    """The owner may inspect/manage own accounts and admin accounts with explicit consent."""
    return bool(account and (str(account.get("owner_id", "")) == "owner" or account.get("owner_access")))


def job_account_is_authorized(job: dict[str, Any], account: dict[str, Any] | None) -> bool:
    """Admins can automate only their own accounts; owner delegation must remain enabled."""
    if not account:
        return False
    job_owner = str(job.get("owner_id", "owner"))
    account_owner = str(account.get("owner_id", ""))
    return account_owner == job_owner or (
        job_owner == "owner" and account_owner != "owner" and bool(account.get("owner_access"))
    )


class CloudRuntime:
    """One Chromium process, isolated Telegram Web contexts and encrypted account state."""

    def __init__(self):
        self.data_dir = Path(os.getenv("DATA_DIR", "/tmp/borhan-sk-data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir)
        self._fernet: Fernet | None = None
        encryption_key = os.getenv("PROFILE_ENCRYPTION_KEY", "").strip()
        if self.store.backend == "supabase" and not encryption_key:
            raise RuntimeError("PROFILE_ENCRYPTION_KEY برای ذخیره رمزگذاری‌شده نشست‌ها در Supabase لازم است")
        if encryption_key:
            try:
                self._fernet = Fernet(encryption_key.encode("ascii"))
            except Exception as exc:
                raise RuntimeError("PROFILE_ENCRYPTION_KEY معتبر نیست؛ کلید Fernet تولید کن") from exc

        self._state_save_interval = max(30, int(os.getenv("SESSION_SAVE_INTERVAL_SECONDS", "60")))
        self._last_state_save: dict[str, float] = {}
        self._last_db_heartbeat = 0.0
        self._db_heartbeat_interval = max(120, int(os.getenv("SUPABASE_HEARTBEAT_INTERVAL_SECONDS", "240")))
        self._last_browser_restart = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.playwright = None
        self.browser: Browser | None = None
        self.telegram_sessions: dict[str, TelegramWeb] = {}
        self._account_start_locks: dict[str, asyncio.Lock] = {}
        self._account_start_guard = threading.RLock()
        self._account_starting: set[str] = set()
        # Render Free has 512 MB RAM. Keep only one Telegram Web context loaded
        # by default; all other account sessions remain encrypted in Supabase and
        # are restored on demand. Paid instances can raise this setting.
        try:
            requested_contexts = int(os.getenv("MAX_TELEGRAM_CONTEXTS", "1"))
        except (TypeError, ValueError):
            requested_contexts = 1
        self._max_telegram_contexts = max(1, min(4, requested_contexts))
        self._context_capacity_lock: asyncio.Lock | None = None
        self._context_meta_lock = threading.RLock()
        self._account_last_used: dict[str, float] = {}
        self._account_pin_until: dict[str, float] = {}
        self._background_futures: set[Future] = set()
        self._auth_state_guard = threading.RLock()
        self._auth_operations: dict[str, dict[str, Any]] = {}
        self._qr_state_guard = threading.RLock()
        self._qr_cache: dict[str, tuple[bytes, float]] = {}
        self._qr_refreshing: set[str] = set()
        self._login_state_guard = threading.RLock()
        self._login_state_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._login_state_refreshing: set[str] = set()
        self.telegram: TelegramWeb | None = None  # Backwards compatibility for older routes.
        self.primary_account_id: str | None = None
        self.started = False
        self._lock = threading.RLock()
        self._status: dict[str, Any] = {
            "service": "starting", "browser_started": False, "telegram_connected": False,
            "last_heartbeat": None, "last_error": "", "session_saved_at": None,
        }

    def start(self):
        with self._lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._thread_main, name="borhan-async-runtime", daemon=True)
            self.thread.start()

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        loop.create_task(self._boot(), name="runtime-boot")
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    async def _ensure_shared_browser(self):
        if self.browser and self.browser.is_connected():
            return
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=os.getenv("BROWSER_HEADLESS", "true").lower() == "true",
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-background-networking", "--disable-default-apps", "--disable-sync",
                  "--disable-extensions", "--disable-component-update", "--disable-breakpad",
                  "--renderer-process-limit=2", "--metrics-recording-only", "--mute-audio",
                  "--no-first-run", "--no-default-browser-check"],
        )

    def _session_key(self, account_id: str) -> str:
        return f"telegram_web_storage_state:{account_id}"

    def _load_browser_state(self, account_id: str) -> dict[str, Any] | None:
        if not self._fernet:
            return None
        encrypted = self.store.get_runtime_state(self._session_key(account_id))
        # Migrate the original single-account build's session exactly once.
        # Moving the ciphertext and deleting the legacy key prevents a deleted
        # account's old login from being silently restored into a future account.
        if not encrypted and account_id == self.primary_account_id:
            encrypted = self.store.get_runtime_state("telegram_web_storage_state")
            if encrypted:
                self.store.set_runtime_state(self._session_key(account_id), encrypted)
                self.store.delete_runtime_state("telegram_web_storage_state")
        if not encrypted:
            return None
        try:
            raw = self._fernet.decrypt(encrypted.encode("ascii"))
            import json as _json
            state = _json.loads(raw.decode("utf-8"))
            return state if isinstance(state, dict) else None
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            log.warning("Saved Telegram account state could not be restored (%s)", account_id)
            try:
                self.store.add_event("warning", "نشست ذخیره‌شده یکی از حساب‌ها قابل بازیابی نبود؛ ورود دوباره با شماره تلفن یا QR لازم است.",
                                     self.store.get_account(account_id).get("owner_id", "owner"))
            except Exception:
                pass
            return None

    async def _save_browser_state(self, account_id: str):
        telegram = self.telegram_sessions.get(account_id)
        if not telegram or not telegram.started or not self._fernet:
            return
        state = await telegram.export_storage_state()
        raw = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet.encrypt(raw).decode("ascii")
        await asyncio.to_thread(self.store.set_runtime_state, self._session_key(account_id), encrypted)
        self._last_state_save[account_id] = time.monotonic()
        self._status["session_saved_at"] = utcnow().isoformat()

    def pin_account(self, account_id: str, seconds: int = 600):
        """Keep a context resident while an interactive login or QR flow is in progress."""
        account_id = str(account_id or "").strip()
        if not account_id:
            return
        now = time.monotonic()
        with self._context_meta_lock:
            self._account_last_used[account_id] = now
            self._account_pin_until[account_id] = max(
                self._account_pin_until.get(account_id, 0.0), now + max(30, seconds)
            )

    def unpin_account(self, account_id: str):
        with self._context_meta_lock:
            self._account_pin_until.pop(str(account_id), None)

    async def _evict_one_context(self, exclude_account_id: str) -> bool:
        """Persist and close the least-recently-used idle account context."""
        now = time.monotonic()
        with self._context_meta_lock:
            candidates = []
            for account_id, telegram in self.telegram_sessions.items():
                if account_id == exclude_account_id or not telegram or not telegram.started:
                    continue
                if self._account_pin_until.get(account_id, 0.0) > now:
                    continue
                if telegram._action_lock.locked():
                    continue
                if account_id in self._account_starting:
                    continue
                candidates.append((self._account_last_used.get(account_id, 0.0), account_id, telegram))
        if not candidates:
            return False

        _, account_id, telegram = min(candidates, key=lambda item: item[0])
        # Never discard a logged-in session on disk-backed deployments. Supabase
        # runtime state is encrypted with PROFILE_ENCRYPTION_KEY before eviction.
        if self._fernet:
            try:
                await asyncio.wait_for(self._save_browser_state(account_id), timeout=12)
            except Exception:
                log.exception("could not persist Telegram state before context eviction (%s)", account_id)
                return False
        else:
            try:
                state = await asyncio.wait_for(telegram.status(), timeout=3)
            except Exception:
                state = {"connected": False}
            if state.get("connected"):
                log.warning("context capacity reached; refusing to evict account %s without encrypted persistence", account_id)
                return False

        try:
            await asyncio.wait_for(telegram.stop(), timeout=8)
        except Exception:
            log.exception("could not close idle Telegram context (%s)", account_id)
            return False
        self.telegram_sessions.pop(account_id, None)
        if self.telegram is telegram:
            self.telegram = None
        with self._context_meta_lock:
            self._account_last_used.pop(account_id, None)
        log.info("released idle Telegram context to stay within memory limit: %s", account_id)
        return True

    async def _start_account_context(self, account: dict[str, Any]):
        account_id = str(account["id"])
        lock = self._account_start_locks.setdefault(account_id, asyncio.Lock())
        if self._context_capacity_lock is None:
            self._context_capacity_lock = asyncio.Lock()
        async with lock:
            existing = self.telegram_sessions.get(account_id)
            if existing and existing.started:
                with self._context_meta_lock:
                    self._account_last_used[account_id] = time.monotonic()
                return await existing.status()
            async with self._context_capacity_lock:
                existing = self.telegram_sessions.get(account_id)
                if existing and existing.started:
                    with self._context_meta_lock:
                        self._account_last_used[account_id] = time.monotonic()
                    return await existing.status()
                while len(self.telegram_sessions) >= self._max_telegram_contexts:
                    evicted = await self._evict_one_context(account_id)
                    if not evicted:
                        raise TelegramWebError(
                            "ظرفیت حافظه برای نشست دیگری کافی نیست؛ ورود فعال حساب دیگر را تمام کن و دوباره تلاش کن."
                        )
                await self._ensure_shared_browser()
                saved_state = await asyncio.to_thread(self._load_browser_state, account_id)
                telegram = TelegramWeb(
                    url=os.getenv("TELEGRAM_WEB_URL", "https://web.telegram.org/k/"),
                    headless=os.getenv("BROWSER_HEADLESS", "true").lower() == "true",
                    storage_state=saved_state,
                    shared_browser=self.browser,
                )
                try:
                    await telegram.start()
                except Exception:
                    try:
                        await telegram.stop()
                    except Exception:
                        pass
                    raise
                self.telegram_sessions[account_id] = telegram
                with self._context_meta_lock:
                    self._account_last_used[account_id] = time.monotonic()
                if self.primary_account_id is None and account.get("owner_id") == "owner":
                    self.primary_account_id = account_id
                self.telegram = self.telegram_sessions.get(self.primary_account_id) if self.primary_account_id else None
                await asyncio.to_thread(self.store.update_account, account_id, {"status": "pending"})
                return await telegram.status()

    async def _boot(self):
        try:
            accounts = await asyncio.to_thread(self.store.list_accounts)
            owner_default = next((a for a in accounts if a.get("owner_id") == "owner"), None)
            if not owner_default:
                owner_default = await asyncio.to_thread(self.store.create_account, "owner", "telegram", "تلگرام اصلی")
                accounts.append(owner_default)
            self.primary_account_id = owner_default["id"]

            # Do not launch Chromium during web-service boot. It is the largest
            # memory consumer and is not needed to serve the dashboard. Telegram
            # contexts start only when the user opens an account or a job is due.
            self.started = True
            self._status.update(service="online", browser_started=False,
                                telegram_connected=False, last_error="")
            log.info("dashboard runtime ready; Telegram browser will start on demand")
        except Exception as exc:
            self._status.update(service="degraded", browser_started=False, last_error=str(exc)[:500])
            log.exception("initial runtime boot failed")
        asyncio.create_task(self._scheduler_loop(), name="scheduler")
        asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        asyncio.create_task(self._browser_supervisor_loop(), name="browser-supervisor")

    async def add_telegram_account(self, account: dict[str, Any]):
        try:
            state = await self._start_account_context(account)
            owner_id = account.get("owner_id", "owner")
            status = "connected" if state.get("connected") else "pending"
            await asyncio.to_thread(self.store.update_account, account["id"], {"status": status})
            await asyncio.to_thread(self.store.add_event, "info", f"نشست {account.get('label', 'تلگرام')} ساخته شد", owner_id)
            return state
        except Exception as exc:
            await asyncio.to_thread(self.store.update_account, account["id"], {"status": "error"})
            raise exc

    async def remove_telegram_account(self, account_id: str):
        telegram = self.telegram_sessions.get(account_id)
        if telegram:
            try:
                if self._fernet and telegram.started:
                    await self._save_browser_state(account_id)
                await telegram.stop()
            except Exception:
                log.exception("failed to stop account context %s", account_id)
        self.telegram_sessions.pop(account_id, None)
        with self._account_start_guard:
            self._account_starting.discard(account_id)
        with self._context_meta_lock:
            self._account_last_used.pop(account_id, None)
            self._account_pin_until.pop(account_id, None)
        with self._qr_state_guard:
            self._qr_cache.pop(account_id, None)
        with self._login_state_guard:
            self._login_state_cache.pop(account_id, None)
            self._login_state_refreshing.discard(account_id)
        with self._auth_state_guard:
            self._auth_operations.pop(account_id, None)
        await asyncio.to_thread(self.store.delete_runtime_state, self._session_key(account_id))
        if account_id == self.primary_account_id:
            # Remove the legacy single-account key too; never restore a deleted login
            # into a newly-created owner account after the next server restart.
            await asyncio.to_thread(self.store.delete_runtime_state, "telegram_web_storage_state")
            # Never silently switch the owner's default sender to an admin's private account.
            self.primary_account_id = None
            self.telegram = None

    async def restart_telegram_account(self, account_id: str):
        self.pin_account(account_id, 180)
        account = await asyncio.to_thread(self.store.get_account, account_id)
        if not account:
            raise TelegramWebError("حساب پیدا نشد")
        telegram = self.telegram_sessions.get(account_id)
        with self._qr_state_guard:
            self._qr_cache.pop(account_id, None)
        with self._login_state_guard:
            self._login_state_cache.pop(account_id, None)
            self._login_state_refreshing.discard(account_id)
        with self._auth_state_guard:
            self._auth_operations.pop(account_id, None)
        if telegram:
            if self._fernet and telegram.started:
                try:
                    await self._save_browser_state(account_id)
                except Exception:
                    log.exception("session save failed before restart")
            await telegram.stop()
            self.telegram_sessions.pop(account_id, None)
        return await self._start_account_context(account)

    async def _heartbeat_loop(self):
        while True:
            try:
                rows = []
                for account_id, telegram in list(self.telegram_sessions.items()):
                    account = await asyncio.to_thread(self.store.get_account, account_id) or {}
                    probe_timed_out = False
                    try:
                        state = await asyncio.wait_for(telegram.status(), timeout=2.0)
                    except asyncio.TimeoutError:
                        # A login/QR operation owns this page. Preserve persisted status
                        # and do not let a diagnostic probe interrupt an active workflow.
                        probe_timed_out = True
                        state = {"connected": account.get("status") == "connected",
                                 "last_error": ""}
                    connected = bool(state.get("connected"))
                    new_status = "connected" if connected else "pending"
                    if not probe_timed_out and account.get("status") != new_status:
                        await asyncio.to_thread(self.store.update_account, account_id, {"status": new_status})
                    rows.append({"id": account_id, "label": account.get("label", "Telegram"),
                                 "platform": "telegram", "owner_id": account.get("owner_id", "owner"),
                                 "connected": connected, "needs_qr": not connected,
                                 "last_error": state.get("last_error", "")})
                    if connected and self._fernet and time.monotonic() - self._last_state_save.get(account_id, 0) >= self._state_save_interval:
                        try:
                            await self._save_browser_state(account_id)
                        except Exception as exc:
                            self._status["state_save_error"] = str(exc)[:300]
                            log.exception("session state save failed")

                browser_alive = bool(self.browser and self.browser.is_connected())
                # Chromium is intentionally lazy-loaded on Render Free. A stopped
                # browser while no Telegram account is open is normal, not a degraded
                # web service. Only the runtime loop/startup health determines service
                # liveness; browser_started remains a separate status signal.
                runtime_alive = bool(self.started and self.loop and self.loop.is_running())
                with self._lock:
                    self._status.update(service="online" if runtime_alive else "degraded",
                                        browser_started=browser_alive,
                                        telegram_connected=any(a["connected"] for a in rows),
                                        accounts=rows, last_heartbeat=utcnow().isoformat())

                if self.store.backend == "supabase" and time.monotonic() - self._last_db_heartbeat >= self._db_heartbeat_interval:
                    try:
                        await asyncio.to_thread(self.store.set_runtime_state, "service_heartbeat", utcnow().isoformat())
                        self._last_db_heartbeat = time.monotonic()
                    except Exception as exc:
                        self._status["storage_error"] = str(exc)[:300]
                        log.exception("Supabase heartbeat failed")
            except Exception as exc:
                self._status.update(last_error=str(exc)[:500])
                log.exception("runtime heartbeat failed")
            # Dashboard health does not need a hot loop; slower checks reduce
            # Chromium DOM work and database pressure on the free instance.
            await asyncio.sleep(20)

    async def _browser_supervisor_loop(self):
        while True:
            try:
                # A browser that has not been started yet is intentional: secondary
                # accounts are lazy-loaded. Only recover a browser that actually died.
                browser_was_started = self.browser is not None
                alive = bool(self.browser and self.browser.is_connected())
                if browser_was_started and not alive and time.monotonic() - self._last_browser_restart > 25:
                    self._last_browser_restart = time.monotonic()
                    await self._restart_shared_browser()
                elif alive:
                    for account_id, telegram in list(self.telegram_sessions.items()):
                        if telegram is not None and telegram.started and telegram.context:
                            continue
                        account = await asyncio.to_thread(self.store.get_account, account_id)
                        if account:
                            try:
                                await self._start_account_context(account)
                            except Exception as account_exc:
                                await asyncio.to_thread(self.store.update_account, account_id, {"status": "error"})
                                log.warning("loaded account context restart failed (%s): %s", account_id, account_exc)
            except Exception as exc:
                self._status.update(service="degraded", browser_started=False, last_error=str(exc)[:500])
                log.exception("browser supervisor failed")
            await asyncio.sleep(12)

    async def _restart_shared_browser(self):
        log.warning("Chromium disconnected; restoring active Telegram contexts")
        active_ids = list(self.telegram_sessions.keys())
        active_accounts = []
        for account_id in active_ids:
            account = await asyncio.to_thread(self.store.get_account, account_id)
            if account:
                active_accounts.append(account)
        for telegram in list(self.telegram_sessions.values()):
            try:
                await telegram.stop()
            except Exception:
                pass
        self.telegram_sessions.clear()
        try:
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass
            await self._ensure_shared_browser()
            for account in active_accounts:
                try:
                    await self._start_account_context(account)
                except Exception as exc:
                    await asyncio.to_thread(self.store.update_account, account["id"], {"status": "error"})
                    log.warning("Could not restore active Telegram context %s: %s", account.get("id"), exc)
            await asyncio.to_thread(self.store.add_event, "warning", "مرورگر پس از قطع‌شدن دوباره راه‌اندازی شد")
        except Exception as exc:
            self._status.update(service="error", browser_started=False, last_error=str(exc)[:500])

    async def _scheduler_loop(self):
        min_interval = int(os.getenv("MIN_JOB_INTERVAL_SECONDS", "60"))
        try:
            poll_seconds = max(8, min(60, int(os.getenv("SCHEDULER_POLL_SECONDS", "15"))))
        except (TypeError, ValueError):
            poll_seconds = 15
        while True:
            try:
                jobs = await asyncio.to_thread(self.store.list_jobs, True, None)
                now = utcnow()
                # Avoid repeated Supabase lookups and Playwright DOM checks when many
                # due jobs share the same Telegram account in one scheduler pass.
                account_cache: dict[str, dict[str, Any] | None] = {}
                telegram_state_cache: dict[str, bool] = {}
                for job in jobs:
                    # Resolve and validate the account before checking the due time.
                    # This makes a revoked admin consent pause delegated jobs on the next
                    # scheduler tick, not only when their next execution becomes due.
                    account_id = job.get("account_id")
                    if not account_id and job.get("owner_id", "owner") == "owner":
                        account_id = self.primary_account_id
                    if account_id:
                        if account_id not in account_cache:
                            account_cache[account_id] = await asyncio.to_thread(self.store.get_account, account_id)
                        account = account_cache[account_id]
                    else:
                        account = None
                    job_owner = str(job.get("owner_id", "owner"))
                    account_owner = str((account or {}).get("owner_id", ""))
                    if not job_account_is_authorized(job, account):
                        try:
                            await asyncio.to_thread(self.store.set_job_status, job["id"], "paused", job_owner)
                            await asyncio.to_thread(self.store.set_job_error, job["id"],
                                                    "اجرای زمان‌بندی متوقف شد: حساب حذف شده یا مجوز مالک لغو شده است.")
                            message = f"زمان‌بندی «{job.get('name', job['id'])}» به‌دلیل نبود مجوز متوقف شد."
                            await asyncio.to_thread(self.store.add_event, "warning", message, job_owner)
                            if account_owner and account_owner != job_owner:
                                await asyncio.to_thread(self.store.add_event, "warning", message, account_owner)
                        except Exception:
                            log.exception("could not pause unauthorized scheduled job %s", job.get("id"))
                        continue
                    due = parse_dt(job.get("next_run_at"))
                    if not due or due > now:
                        continue
                    interval = int(job.get("interval_seconds") or 0)
                    if interval < min_interval:
                        await asyncio.to_thread(self.store.set_job_error, job["id"], f"فاصله اجرا کمتر از حد مجاز سرور است ({min_interval}s)")
                        continue
                    telegram = self.telegram_sessions.get(account_id or "")
                    if (not telegram or not telegram.started) and account:
                        try:
                            await self._start_account_context(account)
                            telegram = self.telegram_sessions.get(account_id or "")
                        except Exception as start_exc:
                            log.warning("could not load scheduled account %s: %s", account_id, start_exc)
                            continue
                    if not telegram:
                        continue
                    if account_id not in telegram_state_cache:
                        try:
                            state = await asyncio.wait_for(telegram.status(), timeout=5.0)
                            telegram_state_cache[account_id or ""] = bool(state.get("connected"))
                        except asyncio.TimeoutError:
                            log.warning("status probe timed out for scheduled account %s", account_id)
                            telegram_state_cache[account_id or ""] = False
                    if not telegram_state_cache.get(account_id or "", False):
                        continue
                    claimed = await asyncio.to_thread(self.store.claim_due_job, job)
                    if not claimed:
                        continue
                    owner_id = job.get("owner_id", "owner")
                    try:
                        if job["action"] == "send":
                            await telegram.send_message(job["target"], job["payload"])
                            msg = f"پیام زمان‌بندی‌شده ارسال شد: {job['name']} → {job['target']}"
                        elif job["action"] == "click":
                            await telegram.click_button(job["target"], job["payload"])
                            msg = f"روی دکمه زمان‌بندی‌شده کلیک شد: {job['name']} → {job['target']}"
                        else:
                            raise TelegramWebError("نوع عملیات ناشناخته است")
                        await asyncio.to_thread(self.store.add_event, "success", msg, owner_id)
                        if account_owner and account_owner != owner_id:
                            await asyncio.to_thread(self.store.add_event, "success", msg, account_owner)
                    except Exception as exc:
                        await asyncio.to_thread(self.store.set_job_error, job["id"], str(exc))
                        error_message = f"اجرای {job['name']} ناموفق بود: {exc}"
                        await asyncio.to_thread(self.store.add_event, "error", error_message, owner_id)
                        if account_owner and account_owner != owner_id:
                            await asyncio.to_thread(self.store.add_event, "error", error_message, account_owner)
                await asyncio.sleep(poll_seconds)
            except Exception as exc:
                log.exception("scheduler loop failed")
                try:
                    await asyncio.to_thread(self.store.add_event, "error", f"خطای موتور زمان‌بندی: {exc}")
                except Exception:
                    pass
                await asyncio.sleep(min(poll_seconds, 10))

    def schedule_account_start(self, account: dict[str, Any]) -> bool:
        """Start an account context once, outside the HTTP request thread."""
        account_id = str(account.get("id", "")).strip()
        # A short startup pin protects the context while the login UI is loading.
        # Active phone/code/2FA polling extends it only while authentication is in progress.
        self.pin_account(account_id, 180)
        if not account_id:
            return False
        telegram = self.telegram_sessions.get(account_id)
        if telegram and telegram.started:
            return False
        with self._account_start_guard:
            if account_id in self._account_starting:
                return False
            self._account_starting.add(account_id)

        async def _start():
            try:
                await self.add_telegram_account(account)
            except Exception:
                log.exception("scheduled Telegram context startup failed (%s)", account_id)
            finally:
                with self._account_start_guard:
                    self._account_starting.discard(account_id)

        try:
            self.schedule(_start())
            return True
        except Exception:
            with self._account_start_guard:
                self._account_starting.discard(account_id)
            return False

    def get_telegram(self, account_id: str | None = None) -> TelegramWeb | None:
        selected = account_id or self.primary_account_id
        telegram = self.telegram_sessions.get(selected or "")
        if telegram:
            with self._context_meta_lock:
                self._account_last_used[selected] = time.monotonic()
        return telegram

    def wait_for_browser(self, timeout: float = 55, require_connected: bool = True,
                         account_id: str | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + max(1, timeout)
        last_state: dict[str, Any] = {}
        selected = account_id or self.primary_account_id
        if selected:
            account = self.store.get_account(selected)
            if account and (not self.get_telegram(selected) or not self.get_telegram(selected).started):
                self.pin_account(selected, max(60, int(timeout) + 30))
                self.schedule_account_start(account)
        while time.monotonic() < deadline:
            telegram = self.get_telegram(selected)
            if telegram and self.loop and self.loop.is_running() and telegram.started:
                try:
                    last_state = self.submit(telegram.status(), timeout=10)
                    if not require_connected or last_state.get("connected"):
                        return last_state
                except Exception as exc:
                    last_state = {"connected": False, "last_error": str(exc)}
            time.sleep(1)
        if require_connected and not last_state.get("connected"):
            raise TimeoutError("سرویس بیدار شد، اما این حساب تلگرام متصل نیست؛ با شماره تلفن یا QR وارد شو و دوباره تلاش کن")
        raise TimeoutError("موتور مرورگر در زمان مقرر آماده نشد؛ درخواست را دوباره بفرست")

    def submit(self, coro: Coroutine, timeout: float = 30):
        if not self.loop or not self.loop.is_running():
            coro.close()
            raise RuntimeError("موتور مرورگر هنوز آماده نیست")
        future: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError("زمان اجرای عملیات مرورگر تمام شد") from exc

    def schedule(self, coro: Coroutine) -> Future:
        """Schedule a long browser operation without tying it to an HTTP request."""
        if not self.loop or not self.loop.is_running():
            coro.close()
            raise RuntimeError("موتور مرورگر هنوز آماده نیست")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        self._background_futures.add(future)

        def _done(done: Future):
            self._background_futures.discard(done)
            try:
                done.result()
            except Exception:
                log.exception("background Telegram task failed")
        future.add_done_callback(_done)
        return future

    def get_auth_operation_state(self, account_id: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._auth_state_guard:
            state = self._auth_operations.get(account_id)
            if not state:
                return None
            if state.get("expires_at", 0) <= now:
                self._auth_operations.pop(account_id, None)
                return None
            return {key: value for key, value in state.items() if key != "expires_at"}

    def get_cached_login_state(self, account_id: str, max_age_seconds: float = 4.0) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._login_state_guard:
            entry = self._login_state_cache.get(account_id)
            if not entry:
                return None
            state, created = entry
            if now - created > max_age_seconds:
                return None
            return dict(state)

    def schedule_login_state_refresh(self, account_id: str) -> bool:
        """Refresh Telegram's UI state in the runtime loop, never inside an HTTP worker."""
        with self._login_state_guard:
            if account_id in self._login_state_refreshing:
                return False
            self._login_state_refreshing.add(account_id)

        async def _refresh():
            try:
                telegram = self.telegram_sessions.get(account_id)
                if not telegram or not telegram.started:
                    state = {"stage": "starting", "connected": False,
                             "message": "در حال آماده‌سازی مرورگر تلگرام…"}
                else:
                    state = await telegram.login_state()
                    if state.get("connected"):
                        cached = self.get_cached_login_state(account_id, max_age_seconds=3600)
                        if not cached or not cached.get("connected"):
                            await self.persist_account_state(account_id)
                safe = {
                    "stage": str(state.get("stage", "loading")),
                    "connected": bool(state.get("connected")),
                    "message": str(state.get("message", ""))[:240],
                }
                with self._login_state_guard:
                    self._login_state_cache[account_id] = (safe, time.monotonic())
            except Exception:
                log.exception("Telegram login-state refresh failed for account %s", account_id)
                with self._login_state_guard:
                    self._login_state_cache[account_id] = ({
                        "stage": "unavailable", "connected": False,
                        "message": "تلگرام فعلاً پاسخ نمی‌دهد؛ چند لحظه دیگر دوباره تلاش کن.",
                    }, time.monotonic())
            finally:
                with self._login_state_guard:
                    self._login_state_refreshing.discard(account_id)

        try:
            self.schedule(_refresh())
            return True
        except Exception:
            with self._login_state_guard:
                self._login_state_refreshing.discard(account_id)
            return False

    def schedule_auth_operation(self, account_id: str, operation: str, coro: Coroutine) -> bool:
        """Run phone/code/2FA browser work off-request to avoid Render gateway timeouts."""
        self.pin_account(account_id, 300)
        messages = {
            "phone": "در حال ارسال درخواست ورود به تلگرام…",
            "code": "در حال بررسی کد ورود…",
            "password": "در حال بررسی رمز دومرحله‌ای…",
        }
        with self._qr_state_guard:
            self._qr_cache.pop(account_id, None)
        with self._login_state_guard:
            self._login_state_cache.pop(account_id, None)
        with self._auth_state_guard:
            current = self._auth_operations.get(account_id) or {}
            if current.get("stage") == "processing" and current.get("expires_at", 0) > time.monotonic():
                coro.close()
                return False
            self._auth_operations[account_id] = {
                "stage": "processing", "connected": False,
                "message": messages.get(operation, "در حال انجام عملیات ورود…"),
                "expires_at": time.monotonic() + 90,
            }

        async def _run():
            try:
                result = await coro
                safe = {
                    "stage": str(result.get("stage", "loading")),
                    "connected": bool(result.get("connected")),
                    "message": str(result.get("message", ""))[:240],
                }
                if safe["connected"]:
                    await self.persist_account_state(account_id)
                    account = await asyncio.to_thread(self.store.get_account, account_id)
                    if account:
                        await asyncio.to_thread(
                            self.store.add_event, "success",
                            f"حساب تلگرام «{account.get('label', 'تلگرام')}» متصل شد.",
                            account.get("owner_id", "owner"),
                        )
                with self._auth_state_guard:
                    self._auth_operations[account_id] = {
                        **safe, "expires_at": time.monotonic() + (600 if safe["stage"] in {"code", "password", "error"} else 60),
                    }
                if safe["connected"]:
                    # Once login is complete, make this context eligible for LRU
                    # eviction; its encrypted storage state has already been saved.
                    self.unpin_account(account_id)
                elif safe["stage"] in {"code", "password"}:
                    self.pin_account(account_id, 300)
                elif safe["stage"] == "error":
                    self.pin_account(account_id, 180)
                with self._login_state_guard:
                    self._login_state_cache[account_id] = (safe, time.monotonic())
            except TelegramWebError as exc:
                safe = {"stage": "error", "connected": False, "message": str(exc)[:240]}
                self.pin_account(account_id, 180)
                with self._auth_state_guard:
                    self._auth_operations[account_id] = {**safe, "expires_at": time.monotonic() + 30}
                with self._login_state_guard:
                    self._login_state_cache[account_id] = (safe, time.monotonic())
            except Exception:
                log.exception("Telegram %s operation failed for account %s", operation, account_id)
                self.pin_account(account_id, 180)
                safe = {"stage": "error", "connected": False,
                        "message": "ارتباط با تلگرام کامل نشد؛ وضعیت را بررسی و در صورت نیاز دوباره تلاش کن."}
                with self._auth_state_guard:
                    self._auth_operations[account_id] = {**safe, "expires_at": time.monotonic() + 30}
                with self._login_state_guard:
                    self._login_state_cache[account_id] = (safe, time.monotonic())
            finally:
                with self._qr_state_guard:
                    self._qr_cache.pop(account_id, None)

        try:
            self.schedule(_run())
            return True
        except Exception:
            try:
                coro.close()
            except Exception:
                pass
            with self._auth_state_guard:
                self._auth_operations[account_id] = {
                    "stage": "unavailable", "connected": False,
                    "message": "موتور تلگرام آماده نیست؛ چند لحظه بعد دوباره تلاش کن.",
                    "expires_at": time.monotonic() + 15,
                }
            return False

    def get_cached_qr(self, account_id: str, max_age_seconds: float = 12.0) -> bytes | None:
        now = time.monotonic()
        with self._qr_state_guard:
            entry = self._qr_cache.get(account_id)
            if not entry:
                return None
            image, created = entry
            if now - created > max_age_seconds:
                self._qr_cache.pop(account_id, None)
                return None
            return image

    def schedule_qr_refresh(self, account_id: str) -> bool:
        self.pin_account(account_id, 180)
        with self._qr_state_guard:
            if account_id in self._qr_refreshing:
                return False
            self._qr_refreshing.add(account_id)

        async def _refresh():
            try:
                telegram = self.telegram_sessions.get(account_id)
                if not telegram or not telegram.started:
                    return
                state = await telegram.prepare_qr_login()
                safe_state = {
                    "stage": str(state.get("stage", "loading")),
                    "connected": bool(state.get("connected")),
                    "message": str(state.get("message", ""))[:240],
                }
                with self._login_state_guard:
                    self._login_state_cache[account_id] = (safe_state, time.monotonic())
                if state.get("connected"):
                    with self._qr_state_guard:
                        self._qr_cache.pop(account_id, None)
                    await self.persist_account_state(account_id)
                    return
                if state.get("stage") != "choose_method":
                    # Do not cache a screenshot of the phone/code/password form as a QR.
                    with self._qr_state_guard:
                        self._qr_cache.pop(account_id, None)
                    return
                image = await telegram.screenshot()
                active_auth = self.get_auth_operation_state(account_id)
                if active_auth and active_auth.get("stage") == "processing":
                    return
                if self.telegram_sessions.get(account_id) is not telegram:
                    return
                # Re-check after screenshotting so a phone/code operation cannot leave
                # a stale QR image cached behind the login-method switch.
                latest = await telegram.login_state()
                safe_latest = {
                    "stage": str(latest.get("stage", "loading")),
                    "connected": bool(latest.get("connected")),
                    "message": str(latest.get("message", ""))[:240],
                }
                with self._login_state_guard:
                    self._login_state_cache[account_id] = (safe_latest, time.monotonic())
                if latest.get("connected"):
                    with self._qr_state_guard:
                        self._qr_cache.pop(account_id, None)
                    await self.persist_account_state(account_id)
                    return
                if latest.get("stage") != "choose_method":
                    return
                with self._qr_state_guard:
                    self._qr_cache[account_id] = (image, time.monotonic())
            except Exception:
                log.exception("QR refresh failed for account %s", account_id)
            finally:
                with self._qr_state_guard:
                    self._qr_refreshing.discard(account_id)

        try:
            self.schedule(_refresh())
            return True
        except Exception:
            with self._qr_state_guard:
                self._qr_refreshing.discard(account_id)
            return False

    async def persist_account_state(self, account_id: str) -> bool:
        telegram = self.telegram_sessions.get(account_id)
        if not telegram or not telegram.started:
            return False
        if self._fernet:
            await self._save_browser_state(account_id)
        account = await asyncio.to_thread(self.store.get_account, account_id)
        if account:
            await asyncio.to_thread(self.store.update_account, account_id, {"status": "connected"})
        return True

    def status(self, owner_id: str | None = None, include_jobs: bool = True) -> dict[str, Any]:
        with self._lock:
            result = dict(self._status)
        result["storage"] = self.store.backend
        accounts = result.get("accounts", [])
        if owner_id is not None:
            accounts = [a for a in accounts if a.get("owner_id") == owner_id]
        result["accounts"] = accounts
        result["telegram_connected"] = any(a.get("connected") for a in accounts)
        if include_jobs:
            try:
                jobs = self.store.list_jobs(owner_id=owner_id)
                result["jobs_total"] = len(jobs)
                result["jobs_active"] = sum(1 for job in jobs if job.get("status") == "active")
            except Exception as exc:
                result["storage_error"] = str(exc)[:300]
        return result

    def shutdown(self):
        if self.loop and self.loop.is_running():
            try:
                async def _close_all():
                    for account_id, telegram in list(self.telegram_sessions.items()):
                        try:
                            await self._save_browser_state(account_id)
                        except Exception:
                            pass
                        try:
                            await telegram.stop()
                        except Exception:
                            pass
                    if self.browser:
                        await self.browser.close()
                    if self.playwright:
                        await self.playwright.stop()
                self.submit(_close_all(), timeout=35)
            except Exception:
                log.exception("runtime shutdown failed")
            self.loop.call_soon_threadsafe(self.loop.stop)
