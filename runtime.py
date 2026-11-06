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
                self.store.add_event("warning", "نشست ذخیره‌شده یکی از حساب‌ها قابل بازیابی نبود؛ احتمالاً باید QR دوباره اسکن شود.",
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

    async def _start_account_context(self, account: dict[str, Any]):
        account_id = str(account["id"])
        existing = self.telegram_sessions.get(account_id)
        if existing and existing.started:
            return await existing.status()
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
            await self._ensure_shared_browser()
            for account in accounts:
                try:
                    await self._start_account_context(account)
                except Exception as exc:
                    await asyncio.to_thread(self.store.update_account, account["id"], {"status": "error"})
                    await asyncio.to_thread(self.store.add_event, "error",
                                            f"راه‌اندازی حساب {account.get('label', account['id'])} ناموفق بود: {exc}",
                                            account.get("owner_id", "owner"))
            self.started = True
            self._status.update(service="online" if self.browser and self.browser.is_connected() else "degraded",
                                browser_started=bool(self.browser and self.browser.is_connected()))
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
        await asyncio.to_thread(self.store.delete_runtime_state, self._session_key(account_id))
        if account_id == self.primary_account_id:
            # Remove the legacy single-account key too; never restore a deleted login
            # into a newly-created owner account after the next server restart.
            await asyncio.to_thread(self.store.delete_runtime_state, "telegram_web_storage_state")
            # Never silently switch the owner's default sender to an admin's private account.
            self.primary_account_id = None
            self.telegram = None

    async def restart_telegram_account(self, account_id: str):
        account = await asyncio.to_thread(self.store.get_account, account_id)
        if not account:
            raise TelegramWebError("حساب پیدا نشد")
        telegram = self.telegram_sessions.get(account_id)
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
                    state = await telegram.status()
                    connected = bool(state.get("connected"))
                    account = await asyncio.to_thread(self.store.get_account, account_id) or {}
                    new_status = "connected" if connected else "pending"
                    if account.get("status") != new_status:
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
                self._status.update(service="online" if browser_alive else "degraded",
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
            await asyncio.sleep(8)

    async def _browser_supervisor_loop(self):
        while True:
            try:
                alive = bool(self.browser and self.browser.is_connected())
                if not alive and time.monotonic() - self._last_browser_restart > 25:
                    self._last_browser_restart = time.monotonic()
                    await self._restart_shared_browser()
                else:
                    accounts = await asyncio.to_thread(self.store.list_accounts)
                    for account in accounts:
                        account_id = account["id"]
                        telegram = self.telegram_sessions.get(account_id)
                        if telegram is None or not telegram.started or not telegram.context:
                            try:
                                await self._start_account_context(account)
                            except Exception as account_exc:
                                await asyncio.to_thread(self.store.update_account, account_id, {"status": "error"})
                                log.warning("account context restart failed (%s): %s", account_id, account_exc)
            except Exception as exc:
                self._status.update(service="degraded", browser_started=False, last_error=str(exc)[:500])
                log.exception("browser supervisor failed")
            await asyncio.sleep(12)

    async def _restart_shared_browser(self):
        log.warning("Chromium disconnected; restoring Telegram contexts")
        accounts = await asyncio.to_thread(self.store.list_accounts)
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
            for account in accounts:
                try:
                    await self._start_account_context(account)
                except Exception as exc:
                    await asyncio.to_thread(self.store.update_account, account["id"], {"status": "error"})
                    log.warning("Could not restore Telegram context %s: %s", account.get("id"), exc)
            await asyncio.to_thread(self.store.add_event, "warning", "مرورگر پس از قطع‌شدن دوباره راه‌اندازی شد")
        except Exception as exc:
            self._status.update(service="error", browser_started=False, last_error=str(exc)[:500])

    async def _scheduler_loop(self):
        min_interval = int(os.getenv("MIN_JOB_INTERVAL_SECONDS", "60"))
        while True:
            try:
                jobs = await asyncio.to_thread(self.store.list_jobs, True, None)
                now = utcnow()
                for job in jobs:
                    # Resolve and validate the account before checking the due time.
                    # This makes a revoked admin consent pause delegated jobs on the next
                    # scheduler tick, not only when their next execution becomes due.
                    account_id = job.get("account_id")
                    if not account_id and job.get("owner_id", "owner") == "owner":
                        account_id = self.primary_account_id
                    account = await asyncio.to_thread(self.store.get_account, account_id) if account_id else None
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
                    if not telegram:
                        continue
                    state = await telegram.status()
                    if not state.get("connected"):
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
                await asyncio.sleep(5)
            except Exception as exc:
                log.exception("scheduler loop failed")
                try:
                    await asyncio.to_thread(self.store.add_event, "error", f"خطای موتور زمان‌بندی: {exc}")
                except Exception:
                    pass
                await asyncio.sleep(5)

    def get_telegram(self, account_id: str | None = None) -> TelegramWeb | None:
        selected = account_id or self.primary_account_id
        return self.telegram_sessions.get(selected or "")

    def wait_for_browser(self, timeout: float = 55, require_connected: bool = True,
                         account_id: str | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + max(1, timeout)
        last_state: dict[str, Any] = {}
        selected = account_id or self.primary_account_id
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
            raise TimeoutError("سرویس بیدار شد، اما این حساب تلگرام متصل نیست؛ QR را اسکن کن و دوباره تلاش کن")
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

    def status(self, owner_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            result = dict(self._status)
        result["storage"] = self.store.backend
        accounts = result.get("accounts", [])
        if owner_id is not None:
            accounts = [a for a in accounts if a.get("owner_id") == owner_id]
        result["accounts"] = accounts
        result["telegram_connected"] = any(a.get("connected") for a in accounts)
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
