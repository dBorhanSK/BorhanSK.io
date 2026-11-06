from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine

from store import Store, parse_dt, utcnow
from telegram_web import TelegramWeb, TelegramWebError

log = logging.getLogger("meow.runtime")


class CloudRuntime:
    def __init__(self):
        self.data_dir = Path(os.getenv("DATA_DIR", "./data"))
        self.profile_dir = self.data_dir / "telegram-profile"
        self.store = Store(self.data_dir)
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
            self.telegram = TelegramWeb(
                self.profile_dir,
                url=os.getenv("TELEGRAM_WEB_URL", "https://web.telegram.org/k/"),
                headless=os.getenv("BROWSER_HEADLESS", "true").lower() == "true",
            )
            await self.telegram.start()
            self.started = True
            self.store.add_event("success", "مرورگر Telegram Web اجرا شد؛ برای اولین ورود QR را اسکن کن.")
            self._status["service"] = "online"
            asyncio.create_task(self._scheduler_loop(), name="scheduler")
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        except Exception as exc:
            self._status.update(service="error", last_error=str(exc)[:500])
            log.exception("runtime boot failed")
            try:
                self.store.add_event("error", f"راه‌اندازی مرورگر ناموفق بود: {exc}")
            except Exception:
                pass

    async def _heartbeat_loop(self):
        while True:
            try:
                state = await self.telegram.status() if self.telegram else {"connected": False}
                self._status.update(
                    service="online", browser_started=bool(self.telegram and self.telegram.started),
                    telegram_connected=bool(state.get("connected")),
                    last_heartbeat=utcnow().isoformat(), last_error=state.get("last_error", ""),
                )
            except Exception as exc:
                self._status.update(last_error=str(exc)[:500])
                log.exception("heartbeat failed")
            await asyncio.sleep(8)

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
                self.submit(self.telegram.stop(), timeout=20)
            except Exception:
                log.exception("browser shutdown failed")
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
