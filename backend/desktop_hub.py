"""Per-account Telegram Desktop cloud workspaces.

No Telethon, TDLib, developer API ID/hash or HTTP bot API are used. Each account
runs the official Telegram Desktop binary with its own encrypted `-workdir` on
an isolated Xvfb display. x11vnc + websockify + noVNC provide a browser desktop.
The noVNC WebSocket is served directly by the Oracle API hostname (not proxied by
Netlify); Netlify only proxies ordinary /api requests.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from store import Store

log = logging.getLogger("borhan.desktop")


class HubError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class DesktopSession:
    display: str
    vnc_port: int
    xvfb: subprocess.Popen
    wm: subprocess.Popen
    telegram: subprocess.Popen
    vnc: subprocess.Popen
    log_files: list[Any]


class DesktopHub:
    READY_PREFIX = "desktop_ready:"
    AUTO_PREFIX = "desktop_autostart:"

    def __init__(self, store: Store):
        self.store = store
        self.data_dir = Path(os.getenv("TELEGRAM_DATA_DIR", "/data/telegram"))
        self.upload_dir = Path(os.getenv("TELEGRAM_UPLOAD_DIR", "/data/uploads"))
        self.log_dir = Path(os.getenv("APP_LOG_DIR", "/data/logs"))
        self.token_file = Path(os.getenv("VNC_TOKEN_FILE", "/data/vnc_tokens.txt"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.touch(exist_ok=True)
        self.api_host = os.getenv("API_HOST", "").strip()
        self.api_scheme = os.getenv("API_SCHEME", "https").strip()
        self.width = max(1024, min(1920, int(os.getenv("DESKTOP_WIDTH", "1440"))))
        self.height = max(700, min(1200, int(os.getenv("DESKTOP_HEIGHT", "900"))))
        self.max_accounts = max(1, min(8, int(os.getenv("MAX_DESKTOP_ACCOUNTS", "2"))))
        self.sessions: dict[str, DesktopSession] = {}
        self.tokens: dict[str, tuple[int, float, str]] = {}
        self.lock = threading.RLock()
        self.action_locks: dict[str, threading.Lock] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.last_error: dict[str, str] = {}
        self._screen_lock = threading.Lock()
        self._watch_retry_at: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        required = ("telegram-desktop", "Xvfb", "xdpyinfo", "openbox", "x11vnc", "xdotool", "wmctrl", "xclip", "tesseract", "websockify")
        return all(shutil.which(x) for x in required) and bool(self.api_host)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._main, name="borhan-desktop-loop", daemon=True)
        self.thread.start()

    def _main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        loop.create_task(self._boot())
        loop.run_forever()

    async def _boot(self):
        self.ready.set()
        if not self.configured:
            log.error("Desktop engine incomplete: install Telegram Desktop, Xvfb, Openbox, x11vnc, xdotool, wmctrl, websockify and set API_HOST")
            return
        # No VNC auth token survives a backend restart.
        self.tokens.clear()
        self._write_tokens()
        asyncio.create_task(self._token_gc())
        try:
            accounts = await asyncio.to_thread(self.store.list_accounts)
            for account in accounts:
                auto = await asyncio.to_thread(self.store.get_runtime_state, self.AUTO_PREFIX + account["id"])
                if auto == "1":
                    try:
                        await self.open_account(account["id"])
                    except Exception as exc:
                        self.last_error[account["id"]] = str(exc)[:220]
                        self._watch_retry_at[account["id"]] = time.monotonic() + 90
                        log.exception("failed to restore Telegram Desktop profile %s", account["id"])
                    await asyncio.sleep(1.0)
        except Exception:
            log.exception("desktop restore failed")
        asyncio.create_task(self._watch_sessions())

    async def _watch_sessions(self):
        """Restart crashed account desktops with a cooldown; never fake activity."""
        while True:
            try:
                accounts = await asyncio.to_thread(self.store.list_accounts)
                for account in accounts:
                    aid = account["id"]
                    auto = await asyncio.to_thread(self.store.get_runtime_state, self.AUTO_PREFIX + aid)
                    if auto != "1" or self.running(aid) or time.monotonic() < self._watch_retry_at.get(aid, 0):
                        continue
                    self._watch_retry_at[aid] = time.monotonic() + 90
                    try:
                        await self.open_account(aid)
                        self.last_error.pop(aid, None)
                        await asyncio.to_thread(self.store.update_account, aid, last_error=None)
                    except Exception as exc:
                        self.last_error[aid] = str(exc)[:220]
                        await asyncio.to_thread(self.store.update_account, aid, status="error", last_error=self.last_error[aid])
                        log.exception("automatic Telegram Desktop restart failed for %s", aid)
            except Exception:
                log.exception("desktop watchdog tick failed")
            await asyncio.sleep(20)

    async def _token_gc(self):
        while True:
            await asyncio.sleep(60)
            with self.lock:
                now = time.time()
                self.tokens = {k: v for k, v in self.tokens.items() if v[1] > now}
                self._write_tokens()

    def _write_tokens(self):
        tmp = self.token_file.with_suffix(".tmp")
        lines = [f"{token}: 127.0.0.1:{port}" for token, (port, expiry, _account_id) in self.tokens.items() if expiry > time.time()]
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.token_file)

    def call(self, coro, timeout: float = 45):
        if not self.loop:
            try:
                coro.close()
            except Exception:
                pass
            raise HubError("موتور دسکتاپ هنوز آماده نیست؛ چند ثانیه بعد تلاش کن.", 503)
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            fut.cancel()
            raise HubError("عملیات دسکتاپ بیش از حد طول کشید؛ دوباره تلاش کن.", 504)
        except HubError:
            raise
        except Exception as exc:
            log.exception("desktop action failed")
            raise HubError(str(exc)[:220] or "خطای دسکتاپ", 500)

    def profile_path(self, account_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", account_id):
            raise HubError("شناسهٔ حساب نامعتبر است", 400)
        path = (self.data_dir / account_id).resolve()
        if self.data_dir.resolve() not in path.parents:
            raise HubError("مسیر حساب نامعتبر است", 400)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    def download_dir(self, account_id: str) -> Path:
        return self.profile_path(account_id) / "home" / "Downloads"

    @staticmethod
    def _alive(proc: subprocess.Popen | None) -> bool:
        return bool(proc and proc.poll() is None)

    def _session(self, account_id: str) -> DesktopSession | None:
        with self.lock:
            s = self.sessions.get(account_id)
            if s and not all(self._alive(p) for p in (s.xvfb, s.telegram, s.vnc)):
                self.sessions.pop(account_id, None)
                self.tokens = {k: v for k, v in self.tokens.items() if v[2] != account_id}
                self._write_tokens()
                self._stop_session(s)
                return None
            return s

    def running(self, account_id: str) -> bool:
        return self._session(account_id) is not None

    def is_connected(self, account_id: str) -> bool:
        return self.running(account_id) and self.store.get_runtime_state(self.READY_PREFIX + account_id) == "1"

    def stage_of(self, account_id: str) -> str:
        if not self.running(account_id):
            return "idle"
        return "connected" if self.is_connected(account_id) else "desktop_login"

    def _free_slot(self) -> int:
        occupied = {int(s.display[1:]) - 100 for s in self.sessions.values()}
        for slot in range(self.max_accounts):
            if slot not in occupied:
                return slot
        raise HubError(f"حداکثر {self.max_accounts} محیط دسکتاپ هم‌زمان فعال است. برای افزایش، RAM ماشین Oracle را بررسی و MAX_DESKTOP_ACCOUNTS را تغییر بده.", 429)

    def _log(self, name: str):
        f = open(self.log_dir / name, "ab", buffering=0)
        return f

    def _popen(self, args, env, log_file):
        return subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=log_file,
                                stderr=subprocess.STDOUT)

    def _start_sync(self, account_id: str):
        if not self.configured:
            raise HubError("موتور Telegram Desktop آماده نیست؛ API_HOST و سرویس‌های Xvfb/Openbox/VNC را بررسی کن.", 503)
        with self.lock:
            old = self._session(account_id)
            if old:
                return {"running": True, "ready": self.is_connected(account_id), "display": old.display}
            slot = self._free_slot()
            display = f":{100 + slot}"
            vnc_port = 5901 + slot
            workdir = self.profile_path(account_id)
            display_number = int(display[1:])
            probe_env = os.environ.copy(); probe_env["DISPLAY"] = display
            alive_display = subprocess.run(["xdpyinfo", "-display", display], env=probe_env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2).returncode == 0
            if not alive_display:
                for stale in (Path(f"/tmp/.X{display_number}-lock"), Path(f"/tmp/.X11-unix/X{display_number}")):
                    try: stale.unlink()
                    except FileNotFoundError: pass
                    except OSError: log.warning("could not remove stale X display file %s", stale)
            env = os.environ.copy()
            env.update({"DISPLAY": display, "QT_X11_NO_MITSHM": "1", "LIBGL_ALWAYS_SOFTWARE": "1",
                        "QT_QUICK_BACKEND": "software", "QT_OPENGL": "software",
                        "XDG_CONFIG_HOME": str(workdir / ".config"),
                        "XDG_CACHE_HOME": str(workdir / ".cache"), "XDG_RUNTIME_DIR": str(workdir / ".runtime"),
                        "XDG_DOWNLOAD_DIR": str(workdir / "home" / "Downloads"), "HOME": str(workdir / "home")})
            for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "XDG_DOWNLOAD_DIR"):
                Path(env[key]).mkdir(exist_ok=True, parents=True, mode=0o700)
                try: Path(env[key]).chmod(0o700)
                except OSError: pass
            user_dirs = Path(env["XDG_CONFIG_HOME"]) / "user-dirs.dirs"
            if not user_dirs.exists():
                user_dirs.write_text('XDG_DOWNLOAD_DIR="$HOME/Downloads"\n', encoding="utf-8")
                user_dirs.chmod(0o600)
            logs: list[Any] = []
            started: list[subprocess.Popen] = []
            try:
                xvfb_log = self._log(f"xvfb-{account_id}.log"); logs.append(xvfb_log)
                xvfb = self._popen(["Xvfb", display, "-screen", "0", f"{self.width}x{self.height}x24",
                                    "-nolisten", "tcp", "-ac", "+extension", "RANDR"], env, xvfb_log); started.append(xvfb)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if xvfb.poll() is not None:
                        raise HubError(f"Xvfb برای حساب {account_id} اجرا نشد؛ لاگ را بررسی کن.", 503)
                    check = subprocess.run(["xdpyinfo", "-display", display], env=env,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
                    if check.returncode == 0:
                        break
                    time.sleep(0.25)
                else:
                    raise HubError("نمایش مجازی دسکتاپ آماده نشد.", 503)
                wm_log = self._log(f"openbox-{account_id}.log"); logs.append(wm_log)
                wm = self._popen(["openbox", "--sm-disable"], env, wm_log); started.append(wm)
                time.sleep(0.35)
                tg_log = self._log(f"telegram-{account_id}.log"); logs.append(tg_log)
                telegram = self._popen(["telegram-desktop", "-workdir", str(workdir)], env, tg_log); started.append(telegram)
                vnc_log = self._log(f"x11vnc-{account_id}.log"); logs.append(vnc_log)
                vnc = self._popen(["x11vnc", "-display", display, "-forever", "-shared", "-nopw", "-localhost",
                                   "-rfbport", str(vnc_port), "-noxdamage", "-repeat", "-quiet"], env, vnc_log); started.append(vnc)
                time.sleep(0.4)
                if telegram.poll() is not None:
                    raise HubError("Telegram Desktop بلافاصله بسته شد؛ لاگ telegram را بررسی کن.", 503)
                session = DesktopSession(display, vnc_port, xvfb, wm, telegram, vnc, logs)
                self.sessions[account_id] = session
                self.store.set_runtime_state(self.AUTO_PREFIX + account_id, "1")
                return {"running": True, "ready": self.is_connected(account_id), "display": display}
            except Exception:
                for proc in reversed(started):
                    try: proc.terminate()
                    except Exception: pass
                for f in logs:
                    try: f.close()
                    except Exception: pass
                raise

    async def open_account(self, account_id: str):
        return await asyncio.to_thread(self._start_sync, account_id)

    def _stop_session(self, s: DesktopSession):
        for proc in (s.vnc, s.telegram, s.wm, s.xvfb):
            if self._alive(proc):
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
        for f in s.log_files:
            try: f.close()
            except Exception: pass

    def _window(self, account_id: str) -> tuple[DesktopSession, str]:
        s = self._session(account_id)
        if not s:
            raise HubError("Telegram Desktop این حساب اجرا نیست؛ ابتدا دسکتاپ را باز کن.", 409)
        env = os.environ.copy(); env["DISPLAY"] = s.display
        r = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "Telegram"], env=env,
                           capture_output=True, text=True, timeout=3)
        ids = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        # Telegram can expose transient popups as separate X11 windows. Select
        # the largest visible Telegram window (normally the main chat window).
        candidates = []
        for wid in ids:
            try:
                raw = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid], env=env,
                                     capture_output=True, text=True, check=True, timeout=3).stdout
                dims = {k.lower(): int(v) for line in raw.splitlines() if "=" in line
                        for k, v in [line.split("=", 1)] if k in {"X", "Y", "WIDTH", "HEIGHT"}}
                area = dims.get("width", 0) * dims.get("height", 0)
                if area:
                    candidates.append((area, wid))
            except (subprocess.SubprocessError, ValueError):
                continue
        if not candidates:
            r = subprocess.run(["wmctrl", "-lp"], env=env, capture_output=True, text=True, timeout=3)
            ids = [line.split()[0] for line in r.stdout.splitlines()
                   if len(line.split()) >= 5 and "Telegram" in " ".join(line.split()[4:])]
            if ids:
                candidates = [(1, ids[0])]
        if not candidates:
            raise HubError("پنجرهٔ Telegram Desktop هنوز آماده نشده؛ چند ثانیه دیگر تلاش کن.", 503)
        window = max(candidates, key=lambda row: row[0])[1]
        # Make the main chat and message composer occupy the virtual display.
        subprocess.run(["wmctrl", "-ir", window, "-b", "add,maximized_vert,maximized_horz"],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return s, window

    @staticmethod
    def _normalize_target(target: str) -> str:
        target = str(target or "").strip()
        if len(target) > 160:
            raise HubError("مقصد بیش از حد طولانی است.")
        candidate = target if "://" in target else ("https://" + target if target.startswith(("t.me/", "telegram.me/")) else "")
        if candidate:
            from urllib.parse import urlparse
            parsed = urlparse(candidate)
            if parsed.hostname and parsed.hostname.lower() in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
                parts = [part for part in parsed.path.split("/") if part]
                if not parts:
                    raise HubError("لینک تلگرام مقصد مشخصی ندارد.")
                if parts[0].startswith("+") or parts[0] == "joinchat":
                    raise HubError("لینک دعوت خصوصی را ابتدا دستی در Telegram Desktop باز و عضو شو؛ سپس نام چت را برای اتوماسیون وارد کن.")
                return "@" + parts[1] if parts[0] == "s" and len(parts) > 1 else "@" + parts[0]
        return target

    def _geometry(self, account_id: str, window: str) -> dict[str, int]:
        s = self._session(account_id)
        if not s:
            raise HubError("جلسهٔ دسکتاپ از دسترس خارج شده است.", 409)
        env = os.environ.copy(); env["DISPLAY"] = s.display
        out = subprocess.run(["xdotool", "getwindowgeometry", "--shell", window], env=env,
                             capture_output=True, text=True, check=True, timeout=3).stdout
        vals = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k in {"X", "Y", "WIDTH", "HEIGHT"}:
                    vals[k.lower()] = int(v)
        if not all(k in vals for k in ("x", "y", "width", "height")):
            raise HubError("اندازهٔ پنجرهٔ تلگرام قابل دریافت نیست.", 500)
        return vals

    async def mark_ready(self, account_id: str):
        if not self.running(account_id):
            await self.open_account(account_id)
        self.store.set_runtime_state(self.READY_PREFIX + account_id, "1")
        from datetime import datetime, timezone
        self.store.update_account(account_id, status="connected", last_seen_at=datetime.now(timezone.utc).isoformat())
        self.store.add_event("success", f"ورود Telegram Desktop برای حساب {account_id} توسط صاحب حساب تأیید شد.",
                             (self.store.get_account(account_id) or {}).get("owner_id", "owner"))
        return {"ready": True}

    def issue_view_token(self, account_id: str) -> str:
        s = self._session(account_id)
        if not s:
            raise HubError("ابتدا Telegram Desktop این حساب را باز کن.", 409)
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.tokens = {k: v for k, v in self.tokens.items() if v[1] > now}
            self.tokens[token] = (s.vnc_port, now + 600, account_id)
            self._write_tokens()
        if not self.api_host:
            raise HubError("API_HOST روی سرور تنظیم نشده است.", 503)
        # websockify reads its bearer token from the *WebSocket request* query,
        # not from the outer vnc.html page query. noVNC appends this `path` value
        # to the websocket URL, so keep token inside path=websockify?token=...
        query = urlencode({"autoconnect": "true", "resize": "scale", "host": self.api_host,
                           "port": "443", "encrypt": "true", "path": f"websockify?token={token}",
                           "reconnect": "true", "view_only": "false"})
        return f"{self.api_scheme}://{self.api_host}/vnc.html?{query}"

    async def revoke_view_tokens(self, account_id: str):
        """Invalidate every existing view token and reset the VNC endpoint.

        Restarting x11vnc disconnects already-established viewers as well as making
        previously issued bearer tokens unusable.
        """
        with self.lock:
            session = self._session(account_id)
            self.tokens = {k: v for k, v in self.tokens.items() if v[2] != account_id}
            self._write_tokens()
            if not session:
                return {"ok": True, "running": False}
            if self._alive(session.vnc):
                try:
                    session.vnc.terminate()
                    session.vnc.wait(timeout=3)
                except Exception:
                    try: session.vnc.kill()
                    except Exception: pass
            env = os.environ.copy(); env["DISPLAY"] = session.display
            vnc_log = self._log(f"x11vnc-{account_id}.log")
            session.log_files.append(vnc_log)
            session.vnc = self._popen(["x11vnc", "-display", session.display, "-forever", "-shared", "-nopw", "-localhost",
                                       "-rfbport", str(session.vnc_port), "-noxdamage", "-repeat", "-quiet"], env, vnc_log)
            time.sleep(0.25)
            if not self._alive(session.vnc):
                raise HubError("بازنشانی دسترسی دسکتاپ ناموفق بود؛ لاگ x11vnc را بررسی کن.", 503)
        return {"ok": True, "running": True}

    async def remove(self, account_id: str, revoke: bool = True):
        with self.lock:
            session = self.sessions.pop(account_id, None)
            self.tokens = {k: v for k, v in self.tokens.items() if v[2] != account_id}
            self._write_tokens()
            if session:
                self._stop_session(session)
        if revoke:
            shutil.rmtree(self.data_dir / account_id, ignore_errors=True)
            shutil.rmtree(self.upload_dir / account_id, ignore_errors=True)
            self.store.delete_runtime_state(self.READY_PREFIX + account_id)
            self.store.delete_runtime_state(self.AUTO_PREFIX + account_id)
        return {"ok": True}

    async def close_account(self, account_id: str):
        await self.remove(account_id, revoke=False)
        self.store.set_runtime_state(self.AUTO_PREFIX + account_id, "0")
        self.store.delete_runtime_state(self.READY_PREFIX + account_id)
        self.store.update_account(account_id, status="pending")
        return {"ok": True, "closed": True, "profile_kept": True}

    async def disconnect_keep(self, account_id: str):
        was_ready = self.is_connected(account_id)
        await self.remove(account_id, revoke=False)
        result = await self.open_account(account_id)
        if was_ready:
            self.store.set_runtime_state(self.READY_PREFIX + account_id, "1")
        return result

    def _env_for(self, account_id: str):
        s = self._session(account_id)
        if not s:
            raise HubError("محیط Telegram Desktop فعال نیست.", 409)
        env = os.environ.copy(); env["DISPLAY"] = s.display
        return s, env

    def _xdotool(self, account_id: str, *args: str):
        _, env = self._env_for(account_id)
        p = subprocess.run(["xdotool", *map(str, args)], env=env, capture_output=True, text=True, timeout=8)
        if p.returncode:
            raise HubError((p.stderr.strip() or "خطای کنترل Telegram Desktop")[:200], 400)
        return p.stdout

    def _paste_text(self, account_id: str, text: str):
        s, env = self._env_for(account_id)
        p = subprocess.Popen(["xclip", "-selection", "clipboard", "-loops", "1"], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert p.stdin is not None
        p.stdin.write(str(text).encode("utf-8")); p.stdin.close()
        time.sleep(0.08)
        self._xdotool(account_id, "key", "ctrl+v")
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired: p.kill()

    def _account_action_lock(self, account_id: str):
        with self.lock:
            return self.action_locks.setdefault(account_id, threading.Lock())

    async def send_text(self, account_id: str, target: str, text: str):
        return await asyncio.to_thread(self._send_text_locked, account_id, target, text)

    def _send_text_locked(self, account_id: str, target: str, text: str):
        with self._account_action_lock(account_id):
            return self._send_text_sync(account_id, target, text)

    def _send_text_sync(self, account_id: str, target: str, text: str):
        target, text = self._normalize_target(target), str(text or "")
        if not target or len(target) > 160 or not text or len(text) > 4000:
            raise HubError("مقصد یا متن پیام نامعتبر است.")
        _, window = self._window(account_id)
        self._xdotool(account_id, "windowactivate", "--sync", window)
        self._xdotool(account_id, "key", "ctrl+k")
        time.sleep(0.3)
        self._paste_text(account_id, target)
        time.sleep(1.0)
        self._xdotool(account_id, "key", "Return")
        time.sleep(1.0)
        geom = self._geometry(account_id, window)
        self._xdotool(account_id, "mousemove", str(geom["x"] + int(geom["width"] * .55)),
                       str(geom["y"] + geom["height"] - 42), "click", "1")
        self._paste_text(account_id, text)
        self._xdotool(account_id, "key", "Return")
        return {"ok": True, "method": "telegram-desktop-ui"}

    async def click_text(self, account_id: str, target: str, button_text: str):
        return await asyncio.to_thread(self._click_text_locked, account_id, target, button_text)

    def _click_text_locked(self, account_id: str, target: str, button_text: str):
        with self._account_action_lock(account_id):
            return self._click_text_sync(account_id, target, button_text)

    def _click_text_sync(self, account_id: str, target: str, button_text: str):
        import mss
        import pytesseract
        from PIL import Image
        from pytesseract import Output
        needle = " ".join(str(button_text or "").casefold().split())
        target = self._normalize_target(target)
        if not target or len(needle) < 2 or len(needle) > 120:
            raise HubError("مقصد یا نام دکمه نامعتبر است.")
        s, window = self._window(account_id)
        self._xdotool(account_id, "windowactivate", "--sync", window)
        self._xdotool(account_id, "key", "ctrl+k"); time.sleep(0.3)
        self._paste_text(account_id, str(target)); time.sleep(1.0)
        self._xdotool(account_id, "key", "Return"); time.sleep(1.2)
        with self._screen_lock:
            old_display = os.environ.get("DISPLAY")
            os.environ["DISPLAY"] = s.display
            try:
                with mss.mss() as sct:
                    shot = sct.grab({"left": 0, "top": 0, "width": self.width, "height": self.height})
                    image = Image.frombytes("RGB", shot.size, shot.rgb)
                data = pytesseract.image_to_data(image, output_type=Output.DICT, lang="fas+eng")
            finally:
                if old_display is None:
                    os.environ.pop("DISPLAY", None)
                else:
                    os.environ["DISPLAY"] = old_display
        # Tesseract usually reports one word per row. Join words belonging to
        # the same visual line so multi-word inline-keyboard buttons can match.
        lines: dict[tuple[int, int, int], list[tuple[str, int, int, int, int]]] = {}
        texts = data.get("text", [])
        for i, raw in enumerate(texts):
            word = " ".join(str(raw).casefold().split())
            if not word:
                continue
            key = (int(data.get("block_num", [0] * len(texts))[i]),
                   int(data.get("par_num", [0] * len(texts))[i]),
                   int(data.get("line_num", [i] * len(texts))[i]))
            lines.setdefault(key, []).append((word, int(data["left"][i]), int(data["top"][i]),
                                                int(data["width"][i]), int(data["height"][i])))
        matches = []
        from difflib import SequenceMatcher
        for words in lines.values():
            words.sort(key=lambda item: item[1])
            line_text = " ".join(item[0] for item in words)
            normalized = " ".join(line_text.split())
            ratio = SequenceMatcher(None, needle, normalized).ratio()
            if needle == normalized or needle in normalized or ratio >= 0.84:
                x = min(item[1] for item in words)
                y = min(item[2] for item in words)
                right = max(item[1] + item[3] for item in words)
                bottom = max(item[2] + item[4] for item in words)
                if y > int(self.height * .48):
                    matches.append((ratio, y, x, right - x, bottom - y))
        if not matches:
            raise HubError("متن دکمه در بخش پایین صفحه پیدا نشد؛ دکمه باید روی صفحه قابل مشاهده باشد.", 404)
        _ratio, y, x, w, h = sorted(matches, key=lambda r: (r[0], r[1]), reverse=True)[0]
        self._xdotool(account_id, "mousemove", str(x + w // 2), str(y + h // 2), "click", "1")
        return {"ok": True, "method": "desktop-ocr-best-effort"}

    def shutdown(self):
        """Stop every desktop child process on graceful container shutdown."""
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            self._stop_session(session)
        loop = self.loop
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
