from __future__ import annotations

import asyncio
import logging
import os
import threading
import json
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine
from cryptography.fernet import Fernet, InvalidToken

from store import Store, parse_dt, utcnow
from telegram_web import TelegramWeb, TelegramWebError

log = logging.getLogger("meow.runtime")


class CloudRuntime:
    def __init__(self):
        self.data_dir = Path(os.getenv("DATA_DIR", "/tmp/meow-data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir)
        self._fernet = None
        if self.store.backend == "supabase":
            encryption_key = os.getenv("PROFILE_ENCRYPTION_KEY", "").strip()
            if not encryption_key:
                raise RuntimeError("PROFILE_ENCRYPTION_KEY تنظیم نشده؛ برای ذخیره امن نشست تلگرام در Supabase لازم است")
            try:
                self._fernet = Fernet(encryption_key.encode("ascii"))
            except Exception as exc:
                raise RuntimeError("PROFILE_ENCRYPTION_KEY معتبر نیست؛ یک کلید Fernet تولید کن") from exc
        self._last_state_save = 0.0
        self._state_save_interval = max(30, int(os.getenv("SESSION_SAVE_INTERVAL_SECONDS", "60")))
        self._last_db_heartbeat = 0.0
        self._db_heartbeat_interval = max(120, int(os.getenv("SUPABASE_HEARTBEAT_INTERVAL_SECONDS", "240")))
        self._last_browser_restart = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.telegram: TelegramWeb | None = None
        self.started = False
        self._lock = threading.RLock()
        self._status: dict[str, Any] = {
            "service": "starting", "telegram_connected": False,
            "browser_started": False, "last_heartbeat": None, "last_error": "",
        }

    def start(self):
        with self._lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._thread_main, name="meow-async-runtime", daemon=True)
            self.thread.start()

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        loop.create_task(self._boot())
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    async def _boot(self):
        try:
            await self._start_telegram_browser()
        except Exception as exc:
            self._status.update(service="degraded", browser_started=False, last_error=str(exc)[:500])
            log.exception("initial browser boot failed; supervisor will retry")
            try:
                await asyncio.to_thread(self.store.add_event, "error", f"راه‌اندازی اولیه مرورگر ناموفق بود: {exc}")
            except Exception:
                pass
        asyncio.create_task(self._scheduler_loop(), name="scheduler")
        asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        asyncio.create_task(self._browser_supervisor_loop(), name="browser-supervisor")

    async def _start_telegram_browser(self):
        saved_state = await asyncio.to_thread(self._load_browser_state)
        telegram = TelegramWeb(
            None,
            url=os.getenv("TELEGRAM_WEB_URL", "https://web.telegram.org/k/"),
            headless=os.getenv("BROWSER_HEADLESS", "true").lower() == "true",
            storage_state=saved_state,
        )
        await telegram.start()
        self.telegram = telegram
        self.started = True
        self._status.update(service="online", browser_started=True, last_error="")
        await asyncio.to_thread(self.store.add_event, "success", "مرورگر Telegram Web اجرا شد؛ برای اولین ورود QR را اسکن کن.")

    async def _browser_supervisor_loop(self):
        while True:
            try:
                browser_alive = bool(self.telegram and self.telegram.started and self.telegram.browser and self.telegram.browser.is_connected())
                if not browser_alive and time.monotonic() - self._last_browser_restart > 30:
                    self._last_browser_restart = time.monotonic()
                    await self._restart_telegram_browser()
            except Exception as exc:
                self._status.update(service="degraded", browser_started=False, last_error=str(exc)[:500])
                log.exception("browser supervisor failed")
            await asyncio.sleep(10)

    def _load_browser_state(self) -> dict[str, Any] | None:
        if not self._fernet:
            return None
        encrypted = self.store.get_runtime_state("telegram_web_storage_state")
        if not encrypted:
            return None
        try:
            raw = self._fernet.decrypt(encrypted.encode("ascii"))
            state = json.loads(raw.decode("utf-8"))
            return state if isinstance(state, dict) else None
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            log.warning("Saved Telegram Web state could not be decrypted/parsed: %s", exc)
            self.store.add_event("warning", "نشست ذخیره‌شده تلگرام قابل بازیابی نبود؛ احتمالاً باید QR دوباره اسکن شود.")
            return None

    async def _save_browser_state(self):
        if not self.telegram or not self._fernet:
            return
        state = await self.telegram.export_storage_state()
        raw = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet.encrypt(raw).decode("ascii")
        await asyncio.to_thread(self.store.set_runtime_state, "telegram_web_storage_state", encrypted)
        self._last_state_save = time.monotonic()
        self._status["session_saved_at"] = utcnow().isoformat()

    async def _heartbeat_loop(self):
        while True:
            try:
                state = await self.telegram.status() if self.telegram else {"connected": False}
                connected = bool(state.get("connected"))
                browser_alive = bool(self.telegram and self.telegram.started and self.telegram.browser and self.telegram.browser.is_connected())
                self._status.update(
                    service="online" if browser_alive else "degraded", browser_started=browser_alive,
                    telegram_connected=connected,
                    last_heartbeat=utcnow().isoformat(), last_error=state.get("last_error", ""),
                )

                # Periodic Supabase heartbeat keeps runtime metadata current even when no jobs run.
                if self.store.backend == "supabase" and time.monotonic() - self._last_db_heartbeat >= self._db_heartbeat_interval:
                    try:
                        await asyncio.to_thread(self.store.set_runtime_state, "service_heartbeat", utcnow().isoformat())
                        self._last_db_heartbeat = time.monotonic()
                    except Exception as db_exc:
                        self._status["storage_error"] = str(db_exc)[:300]
                        log.exception("Supabase heartbeat failed")

                # Persist Telegram auth state only after a logged-in state is detected.
                if connected and self._fernet and time.monotonic() - self._last_state_save >= self._state_save_interval:
                    try:
                        await self._save_browser_state()
                    except Exception as save_exc:
                        self._status["state_save_error"] = str(save_exc)[:300]
                        log.exception("browser state save failed")

            except Exception as exc:
                self._status.update(last_error=str(exc)[:500])
                log.exception("heartbeat failed")
            await asyncio.sleep(8)

    async def _restart_telegram_browser(self):
        log.warning("Telegram Web browser is not alive; attempting restart")
        try:
            if self.telegram:
                try:
                    await self.telegram.stop()
                except Exception:
                    pass
            await self._start_telegram_browser()
            await asyncio.to_thread(self.store.add_event, "warning", "مرورگر تلگرام پس از قطع‌شدن به‌صورت خودکار راه‌اندازی شد")
        except Exception as exc:
            self._status.update(service="error", browser_started=False, last_error=str(exc)[:500])
            log.exception("Telegram Web auto-restart failed")

    async def _scheduler_loop(self):
        min_interval = int(os.getenv("MIN_JOB_INTERVAL_SECONDS", "60"))
        while True:
            try:
                state = await self.telegram.status() if self.telegram else {"connected": False}
                if state.get("connected"):
                    jobs = await asyncio.to_thread(self.store.list_jobs, True)
                    now = utcnow()
                    for job in jobs:
                        due = parse_dt(job.get("next_run_at"))
                        if due and due <= now:
                            interval = int(job.get("interval_seconds") or 0)
                            if interval < min_interval:
                                self.store.set_job_error(job["id"], f"فاصله اجرا کمتر از حد مجاز سرور است ({min_interval}s)")
                                continue
                            claimed = await asyncio.to_thread(self.store.claim_due_job, job)
                            if not claimed:
                                continue
                            try:
                                if job["action"] == "send":
                                    await self.telegram.send_message(job["target"], job["payload"])
                                    msg = f"پیام زمان‌بندی‌شده ارسال شد: {job['name']} → {job['target']}"
                                elif job["action"] == "click":
                                    await self.telegram.click_button(job["target"], job["payload"])
                                    msg = f"روی دکمه زمان‌بندی‌شده کلیک شد: {job['name']} → {job['target']}"
                                else:
                                    raise TelegramWebError("نوع عملیات ناشناخته است")
                                await asyncio.to_thread(self.store.add_event, "success", msg)
                            except Exception as exc:
                                await asyncio.to_thread(self.store.set_job_error, job["id"], str(exc))
                                await asyncio.to_thread(self.store.add_event, "error", f"اجرای {job['name']} ناموفق بود: {exc}")
                await asyncio.sleep(5)
            except Exception as exc:
                log.exception("scheduler loop failed")
                try:
                    await asyncio.to_thread(self.store.add_event, "error", f"خطای موتور زمان‌بندی: {exc}")
                except Exception:
                    pass
                await asyncio.sleep(4)

    def wait_for_browser(self, timeout: float = 55, require_connected: bool = True) -> dict[str, Any]:
        """Wait for cold-start browser boot; useful for a webhook that woke a sleeping Free service."""
        deadline = time.monotonic() + max(1, timeout)
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            telegram = self.telegram
            if telegram and self.loop and self.loop.is_running() and telegram.started:
                try:
                    last_state = self.submit(telegram.status(), timeout=10)
                    if not require_connected or last_state.get("connected"):
                        return last_state
                except Exception as exc:
                    last_state = {"connected": False, "last_error": str(exc)}
            time.sleep(1)
        if require_connected and not last_state.get("connected"):
            raise TimeoutError("سرویس بیدار شد، اما تلگرام هنوز متصل نیست؛ اگر اولین ورود است QR را اسکن کن و درخواست را دوباره بفرست")
        raise TimeoutError("موتور مرورگر در زمان مقرر آماده نشد؛ چند لحظه دیگر درخواست را تکرار کن")

    def submit(self, coro: Coroutine, timeout: float = 30):
        if not self.loop:
            coro.close()
            raise RuntimeError("موتور مرورگر هنوز راه‌اندازی نشده است")
        future: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError("زمان اجرای عملیات مرورگر تمام شد") from exc

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._status)
        result["storage"] = self.store.backend
        result["telegram_connected"] = result.get("telegram_connected", False)
        try:
            jobs = self.store.list_jobs()
            result["jobs_total"] = len(jobs)
            result["jobs_active"] = sum(1 for job in jobs if job.get("status") == "active")
        except Exception as exc:
            result["storage_error"] = str(exc)[:300]
            result["jobs_total"] = result.get("jobs_total", 0)
            result["jobs_active"] = result.get("jobs_active", 0)
        return result

    def shutdown(self):
        if self.loop and self.telegram:
            try:
                if self._fernet:
                    self.submit(self._save_browser_state(), timeout=20)
                self.submit(self.telegram.stop(), timeout=20)
            except Exception:
                log.exception("browser shutdown failed")
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
