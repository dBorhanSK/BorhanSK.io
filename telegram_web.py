from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

log = logging.getLogger("meow.telegram_web")


class TelegramWebError(RuntimeError):
    pass


class TelegramWeb:
    """Automates the official Telegram Web UI; no user API_ID/API_HASH is requested.

    Authentication prefers Telegram Web's phone/code/2FA flow; QR remains an optional
    fallback. Browser storage state is exported to encrypted Supabase runtime storage.
    The temporary browser cache itself lives on the ephemeral Render filesystem.
    """

    def __init__(self, profile_dir: str | Path | None = None, url: str | None = None,
                 headless: bool = True, storage_state: dict[str, Any] | None = None,
                 shared_browser: Browser | None = None):
        # Each account gets a fully isolated BrowserContext. The Chromium process is shared.
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.url = url or os.getenv("TELEGRAM_WEB_URL", "https://web.telegram.org/k/")
        self.headless = headless
        self.initial_storage_state = storage_state
        self.playwright = None
        self.browser: Browser | None = shared_browser
        self._owns_browser = shared_browser is None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.last_error = ""
        self.started = False
        self._action_lock = asyncio.Lock()

    async def start(self):
        if self._owns_browser:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-background-networking", "--disable-default-apps", "--disable-sync",
                      "--disable-extensions", "--disable-component-update", "--disable-breakpad",
                      "--renderer-process-limit=2", "--metrics-recording-only", "--mute-audio",
                      "--no-first-run", "--no-default-browser-check"],
            )
        if not self.browser or not self.browser.is_connected():
            raise TelegramWebError("مرورگر مشترک آماده نیست")
        context_args = {"viewport": {"width": 1120, "height": 760}, "locale": "en-US"}
        if self.initial_storage_state:
            context_args["storage_state"] = self.initial_storage_state
        try:
            self.context = await self.browser.new_context(**context_args)
        except Exception as exc:
            log.warning("Saved Telegram Web state could not be restored; starting a fresh session: %s", exc)
            context_args.pop("storage_state", None)
            self.context = await self.browser.new_context(**context_args)
            self.last_error = "نشست ذخیره‌شده بازیابی نشد؛ احتمالاً باید QR دوباره اسکن شود."
        try:
            # Intercepting every Telegram Web request adds Python callback overhead
            # during startup. Keep the fast native request path by default; deployments
            # may opt into blocking media/fonts when bandwidth matters more than speed.
            # On Render Free, fonts and video/audio media consume extra bandwidth and
            # memory but are not needed for text automation or login. Allow override.
            block_media_default = "true" if (os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL")) else "false"
            if os.getenv("TELEGRAM_BLOCK_MEDIA", block_media_default).strip().lower() in {"1", "true", "yes", "on"}:
                await self.context.route("**/*", self._lightweight_resource_route)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(7000)
            self.page.on("pageerror", lambda exc: log.warning("Telegram Web page error: %s", exc))
            await self.page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
            # Telegram Web keeps network activity alive (including sockets), so
            # networkidle is an unreliable and expensive readiness signal. The app
            # polls login_state asynchronously while the web UI hydrates.
            try:
                await self.page.locator("body").wait_for(state="attached", timeout=5000)
            except Exception:
                pass
            self.started = True
            log.info("Telegram Web account context opened")
        except Exception:
            await self.stop()
            raise

    async def _lightweight_resource_route(self, route):
        try:
            if route.request.resource_type in {"font", "media"}:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    async def export_storage_state(self) -> dict[str, Any]:
        """Return cookies/localStorage/IndexedDB for encrypted persistence in Supabase."""
        if not self.context:
            raise TelegramWebError("مرورگر آماده نیست")
        try:
            return await self.context.storage_state(indexed_db=True)
        except TypeError:
            # Compatibility fallback for older Playwright, though requirements pin a recent version.
            return await self.context.storage_state()

    async def stop(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            if self._owns_browser:
                try:
                    if self.browser:
                        await self.browser.close()
                finally:
                    if self.playwright:
                        await self.playwright.stop()
            self.context = None
            self.page = None
            self.started = False

    async def _ensure_page(self) -> Page:
        if not self.started or not self.context or not self.browser or not self.browser.is_connected():
            raise TelegramWebError("مرورگر هنوز آماده نیست یا اتصال Chromium قطع شده است")
        if self.page is None or self.page.is_closed():
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            await self.page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        return self.page

    async def status(self) -> dict[str, Any]:
        async with self._action_lock:
            return await self._status_unlocked()

    async def _status_unlocked(self) -> dict[str, Any]:
        try:
            page = await self._ensure_page()
            composer_count = await self._visible_count(
                'div.input-message-input[contenteditable="true"], '
                'div[contenteditable="true"][data-placeholder]'
            )
            chat_shell_count = await self._visible_count("#column-left, .chatlist, .chat-list, .ChatList")
            body = " ".join((await page.locator("body").inner_text(timeout=1200)).lower().split())
            auth_screen = any(hint in body for hint in (
                "log in by qr", "log in by phone", "scan the qr", "scan qr code",
                "qr code to log in", "enter your phone number", "phone number to continue",
                "enter the code", "enter login code", "two-step verification",
                "enter your password", "password hint",
            ))
            connected = not auth_screen and (composer_count > 0 or chat_shell_count > 0)
            return {
                "connected": bool(connected),
                "needs_qr": not bool(connected),
                "url": page.url,
                "last_error": self.last_error,
            }
        except Exception as exc:
            self.last_error = str(exc)[:500]
            return {"connected": False, "needs_qr": True, "url": self.url, "last_error": self.last_error}

    @staticmethod
    def _normalise_phone(phone: str) -> str:
        # Telegram expects E.164: + followed by 7-15 digits.
        raw = str(phone or "").strip()
        chars = []
        for ch in raw:
            if ch == "+":
                chars.append(ch)
            else:
                try:
                    chars.append(str(unicodedata.digit(ch)))
                except (TypeError, ValueError):
                    if ch.isspace() or ch in "()-.":
                        continue
                    raise TelegramWebError("شماره تلفن فقط باید شامل رقم و علامت + باشد")
        value = "".join(chars)
        if value.startswith("00"):
            value = "+" + value[2:]
        if not value.startswith("+") or value.count("+") != 1:
            raise TelegramWebError("شماره را با + و کد کشور وارد کن؛ مثال: +491234567890")
        digits = value[1:]
        if not digits.isdigit() or not 7 <= len(digits) <= 15:
            raise TelegramWebError("شماره باید با کد کشور وارد شود؛ مثال: +491234567890")
        return "+" + digits

    async def _visible_count(self, selector: str, limit: int = 12) -> int:
        """Count visible matches only; Telegram sometimes keeps hidden app DOM on its login page."""
        page = await self._ensure_page()
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), limit)
        except Exception:
            return 0
        visible = 0
        for index in range(count):
            try:
                if await locator.nth(index).is_visible():
                    visible += 1
            except Exception:
                continue
        return visible

    async def _visible_inputs(self, selectors: list[str]):
        page = await self._ensure_page()
        found = []
        seen = set()
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(await locator.count(), 12)
                for index in range(count):
                    item = locator.nth(index)
                    if not await item.is_visible():
                        continue
                    box = await item.bounding_box()
                    rect = tuple(round(float(box[k]), 1) for k in ("x", "y", "width", "height")) if box else None
                    identity = await item.evaluate("el => [el.tagName, el.id, el.name, el.placeholder].join('|')")
                    key = (identity, rect)
                    if key not in seen:
                        seen.add(key)
                        found.append(item)
            except Exception:
                continue
        return found

    async def _click_auth_action(self, words: tuple[str, ...]) -> bool:
        """Click a visible Telegram auth action without relying on translated CSS classes."""
        page = await self._ensure_page()
        selectors = 'button, [role="button"], a, .btn-primary, .Button'
        locator = page.locator(selectors)
        try:
            count = min(await locator.count(), 80)
        except Exception:
            return False
        for index in range(count):
            item = locator.nth(index)
            try:
                if not await item.is_visible():
                    continue
                text = " ".join((await item.inner_text(timeout=800)).lower().split())
                if not text:
                    text = " ".join(str(await item.get_attribute(attr) or "").lower()
                                    for attr in ("aria-label", "title", "data-testid"))
                if not text:
                    continue
                wants_phone_method = any(word in {"log in by phone", "use phone", "phone number"} for word in words)
                wants_qr_method = any(word in {"log in by qr", "use qr code", "show qr code"} for word in words)
                is_phone_method = ("phone" in text or "phone number" in text) and any(
                    marker in text for marker in ("log in", "login", "use", "number")
                )
                is_qr_method = "qr" in text and any(
                    marker in text for marker in ("log in", "login", "use", "show", "code")
                )
                if is_qr_method:
                    if not wants_qr_method:
                        continue
                    await item.click(timeout=2500)
                    return True
                if is_phone_method:
                    if not wants_phone_method:
                        continue
                    await item.click(timeout=2500)
                    return True
                if any(word in text for word in words):
                    await item.click(timeout=2500)
                    return True
            except Exception:
                continue
        return False

    async def login_state(self) -> dict[str, Any]:
        """Return a safe auth state, serialized with other page interactions."""
        async with self._action_lock:
            return await self._login_state_unlocked()

    async def _login_state_unlocked(self) -> dict[str, Any]:
        """Read auth state while the caller owns _action_lock."""
        try:
            page = await self._ensure_page()
            body = " ".join((await page.locator("body").inner_text(timeout=1800)).lower().split())
            composer_count = await self._visible_count(
                'div.input-message-input[contenteditable="true"], '
                'div[contenteditable="true"][data-placeholder]'
            )
            chat_shell_count = await self._visible_count("#column-left, .chatlist, .chat-list, .ChatList")
            login_hints = ("log in by qr", "log in by phone", "log in with phone", "scan the qr", "scan qr code",
                           "qr code to log in", "enter your phone number", "phone number to continue",
                           "enter the code", "enter login code", "two-step verification", "two step verification",
                           "two-factor authentication", "enter your password", "password hint")
            auth_screen = any(hint in body for hint in login_hints)
            if not auth_screen and (composer_count > 0 or chat_shell_count > 0):
                return {"stage": "connected", "connected": True, "message": "حساب متصل است"}
            if any(term in body for term in (
                "invalid phone", "phone number invalid", "phone number is invalid",
                "invalid code", "code is invalid", "wrong code", "expired code",
                "flood_wait", "too many attempts", "too many tries",
                "too many tries", "wrong password", "invalid password", "incorrect password",
                "password is invalid", "check your password", "invalid two-step password",
            )):
                if any(term in body for term in ("invalid code", "code is invalid", "wrong code", "expired code")):
                    return {"stage": "error", "connected": False, "message": "کد ورود معتبر نیست یا منقضی شده؛ کد جدید را از تلگرام بررسی کن."}
                if any(term in body for term in ("flood_wait", "too many attempts", "too many tries")):
                    return {"stage": "error", "connected": False, "message": "تلگرام موقتاً درخواست ورود را محدود کرده است؛ تا پایان زمان محدودیت دوباره تلاش نکن."}
                if any(term in body for term in (
                    "wrong password", "invalid password", "incorrect password",
                    "this password is incorrect", "password is invalid", "invalid two-step password",
                )):
                    return {"stage": "error", "connected": False, "message": "رمز دومرحله‌ای صحیح نیست؛ دوباره بررسی کن."}
                return {"stage": "error", "connected": False, "message": "شماره تلفن پذیرفته نشد؛ آن را با کد کشور بررسی کن."}
            password_fields = await self._visible_inputs([
                'input[type="password"]', 'input[name*="password" i]',
                'input[autocomplete="current-password"]',
            ])
            if password_fields:
                hint = ""
                for marker in ("hint", "password hint"):
                    if marker in body:
                        hint = "راهنمای رمز دومرحله‌ای در تلگرام نمایش داده شده است."
                        break
                return {"stage": "password", "connected": False,
                        "message": hint or "رمز عبور تأیید دومرحله‌ای را وارد کن."}

            code_fields = await self._visible_inputs([
                'input[name*="code" i]', 'input[autocomplete="one-time-code"]',
                'input[placeholder*="code" i]', 'input[type="tel"][maxlength="1"]',
                'input[type="text"][maxlength="1"]',
            ])
            numeric_fields = await self._visible_inputs(['input[inputmode="numeric"]'])
            tel_fields = await self._visible_inputs(['input[type="tel"]'])
            phone_fields = await self._visible_inputs([
                'input[name*="phone" i]', 'input[placeholder*="phone" i]',
                'input[aria-label*="phone" i]', 'input[autocomplete="tel"]',
                'input[inputmode="tel"]', 'input[type="tel"]',
            ])
            code_prompt = any(term in body for term in (
                "enter the code", "enter code", "sent you a code", "sent a code",
                "code was sent", "code sent to", "check your telegram", "telegram app",
                "confirmation code", "login code", "sms code", "email code", "we've sent",
                "we have sent", "enter login code",
            ))
            phone_prompt = any(term in body for term in ("enter your phone", "your phone number", "phone number to continue",
                                                            "log in with phone", "log in by phone"))
            if code_fields or ((numeric_fields or tel_fields) and code_prompt):
                return {"stage": "code", "connected": False,
                        "message": "کد ورود ارسال شد؛ کدی را که تلگرام فرستاده وارد کن."}
            if phone_fields or phone_prompt:
                return {"stage": "phone", "connected": False,
                        "message": "شماره تلفن را با کد کشور وارد کن."}
            if any(x in body for x in ("log in by phone", "log in by qr", "scan the qr", "qr code")):
                return {"stage": "choose_method", "connected": False,
                        "message": "روش ورود را انتخاب کن: شماره تلفن یا QR."}
            if any(x in body for x in ("invalid phone", "invalid code", "too many", "flood")):
                return {"stage": "error", "connected": False,
                        "message": "تلگرام ورود را نپذیرفت یا محدودیت موقت اعمال کرده است؛ متن خطا را در صفحه بررسی کن."}
            return {"stage": "loading", "connected": False,
                    "message": "صفحه ورود تلگرام در حال آماده‌شدن است؛ چند لحظه دیگر تازه‌سازی کن."}
        except Exception as exc:
            self.last_error = str(exc)[:300]
            return {"stage": "unavailable", "connected": False,
                    "message": "صفحه ورود تلگرام فعلاً پاسخ نمی‌دهد؛ کمی بعد دوباره تلاش کن."}

    async def start_phone_login(self, phone: str) -> dict[str, Any]:
        """Start Telegram Web phone login. Phone is used only for this request."""
        number = self._normalise_phone(phone)
        phone_selector = (
            'input[name*="phone" i], input[placeholder*="phone" i], '
            'input[aria-label*="phone" i], input[autocomplete="tel"], '
            'input[inputmode="tel"], input[type="tel"]'
        )
        async with self._action_lock:
            page = await self._ensure_page()
            state = await self._login_state_unlocked()
            if state.get("connected") or state.get("stage") in {"code", "password"}:
                return state

            # Do not blindly re-submit during Telegram's flood wait. A retryable
            # invalid-phone screen, however, should allow the owner to correct the number.
            try:
                body_before = " ".join((await page.locator("body").inner_text(timeout=1000)).lower().split())
            except Exception:
                body_before = ""
            if any(term in body_before for term in ("flood_wait", "too many attempts", "too many tries")):
                return {"stage": "error", "connected": False,
                        "message": "تلگرام موقتاً درخواست ورود را محدود کرده است؛ بعداً دوباره تلاش کن."}

            phone_fields = await self._visible_inputs([phone_selector])
            if not phone_fields:
                await self._click_auth_action(("log in by phone", "phone number", "use phone"))
                # Wait for the visible input, not merely the first DOM match (which may be hidden).
                deadline = asyncio.get_running_loop().time() + 7.0
                while not phone_fields and asyncio.get_running_loop().time() < deadline:
                    await page.wait_for_timeout(300)
                    phone_fields = await self._visible_inputs([phone_selector])
            if not phone_fields:
                state = await self._login_state_unlocked()
                raise TelegramWebError(
                    state.get("message") or "فیلد شماره تلفن در صفحه رسمی تلگرام پیدا نشد؛ صفحه را تازه کن و دوباره تلاش کن."
                )
            phone_input = phone_fields[0]
            await phone_input.fill(number)
            # Do not match the "Log in by phone" method selector again here.
            # Telegram normally labels the submit action Next/Continue/Send code.
            if not await self._click_auth_action(("next", "continue", "send code", "confirm")):
                await phone_input.press("Enter")

            # Telegram transitions asynchronously. Poll sparingly so the single
            # Chromium process on Render Free is not busy scanning the DOM repeatedly.
            for _ in range(16):
                await page.wait_for_timeout(500)
                state = await self._login_state_unlocked()
                if state.get("stage") in {"code", "password", "connected", "error"}:
                    return state
            return await self._login_state_unlocked()

    async def submit_login_code(self, code: str) -> dict[str, Any]:
        digits = []
        for ch in str(code or ""):
            try:
                digits.append(str(unicodedata.digit(ch)))
            except (TypeError, ValueError):
                if ch.isspace() or ch in "-":
                    continue
                raise TelegramWebError("کد ورود فقط باید شامل رقم باشد")
        value = "".join(digits)
        if not 4 <= len(value) <= 10:
            raise TelegramWebError("کد ورود باید بین ۴ تا ۱۰ رقم باشد")
        async with self._action_lock:
            page = await self._ensure_page()
            state = await self._login_state_unlocked()
            if state.get("stage") == "password":
                return state
            fields = await self._visible_inputs([
                'input[name*="code" i]', 'input[autocomplete="one-time-code"]',
                'input[inputmode="numeric"]', 'input[placeholder*="code" i]',
                'input[type="tel"][maxlength="1"]', 'input[type="text"][maxlength="1"]',
                'input[type="tel"]',
            ])
            if not fields:
                raise TelegramWebError("کادر کد ورود پیدا نشد؛ احتمالاً کد منقضی شده یا صفحه تغییر کرده است")
            first_maxlength = await fields[0].get_attribute("maxlength")
            is_single_digit_box = first_maxlength == "1" or len(fields) > 1
            if is_single_digit_box:
                await fields[0].click()
                # Telegram Web can render one box per digit and auto-focus the next.
                # Keyboard typing handles both equal and partially discovered box counts.
                await page.keyboard.type(value, delay=55)
            else:
                await fields[0].fill(value)
            if not await self._click_auth_action(("next", "continue", "submit", "confirm")):
                try:
                    await fields[-1].press("Enter")
                except Exception:
                    pass
            deadline = asyncio.get_running_loop().time() + 12
            while asyncio.get_running_loop().time() < deadline:
                await page.wait_for_timeout(400)
                state = await self._login_state_unlocked()
                if state.get("stage") in {"password", "connected", "error"}:
                    return state
            return await self._login_state_unlocked()

    async def submit_2fa_password(self, password: str) -> dict[str, Any]:
        value = str(password or "")
        if not value or len(value) > 256:
            raise TelegramWebError("رمز دومرحله‌ای نامعتبر است")
        async with self._action_lock:
            page = await self._ensure_page()
            fields = await self._visible_inputs([
                'input[type="password"]', 'input[name*="password" i]',
                'input[autocomplete="current-password"]',
            ])
            if not fields:
                raise TelegramWebError("کادر رمز دومرحله‌ای پیدا نشد")
            await fields[0].fill(value)
            if not await self._click_auth_action(("next", "continue", "submit", "confirm")):
                await fields[0].press("Enter")
            deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < deadline:
                await page.wait_for_timeout(650)
                state = await self._login_state_unlocked()
                if state.get("stage") in {"connected", "error"}:
                    return state
                if state.get("stage") == "password":
                    # Remain on the password screen: report failure without exposing input.
                    body = " ".join((await page.locator("body").inner_text(timeout=1500)).lower().split())
                    if any(x in body for x in ("wrong password", "invalid password", "incorrect password")):
                        try:
                            await fields[0].fill("")
                        except Exception:
                            pass
                        return {"stage": "error", "connected": False,
                                "message": "رمز دومرحله‌ای صحیح نیست؛ دوباره بررسی کن."}
            return await self._login_state_unlocked()

    async def prepare_qr_login(self) -> dict[str, Any]:
        """Return Telegram Web to its QR screen when the user explicitly selects QR."""
        async with self._action_lock:
            page = await self._ensure_page()
            state = await self._login_state_unlocked()
            if state.get("connected"):
                return state
            if state.get("stage") != "choose_method":
                clicked = await self._click_auth_action(("log in by qr", "use qr code", "show qr code"))
                if clicked:
                    try:
                        await page.wait_for_timeout(350)
                    except Exception:
                        pass
            return await self._login_state_unlocked()

    async def screenshot(self) -> bytes:
        async with self._action_lock:
            page = await self._ensure_page()
            # Telegram's QR is usually a square SVG/canvas inside a qr-named wrapper.
            # Search those wrappers first so we do not accidentally capture an icon.
            selectors = (
                '[class*="qr" i] canvas:visible, [class*="qr" i] svg:visible, '
                '[data-testid*="qr" i] canvas:visible, [data-testid*="qr" i] svg:visible',
                'canvas:visible',
            )
            for selector in selectors:
                locator = page.locator(selector)
                try:
                    count = min(await locator.count(), 20)
                except Exception:
                    continue
                candidates = []
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if not await candidate.is_visible():
                            continue
                        box = await candidate.bounding_box()
                        if not box or box["width"] < 130 or box["height"] < 130:
                            continue
                        ratio = max(box["width"], box["height"]) / max(1, min(box["width"], box["height"]))
                        if ratio <= 1.35:
                            candidates.append((box["width"] * box["height"], candidate))
                    except Exception:
                        continue
                if candidates:
                    # The real QR is normally the largest square item inside its wrapper.
                    _, candidate = max(candidates, key=lambda item: item[0])
                    try:
                        return await candidate.screenshot(type="png", timeout=5000, animations="disabled")
                    except Exception:
                        continue

            raise TelegramWebError(
                "عنصر QR هنوز در صفحه تلگرام آماده نیست؛ چند ثانیه دیگر دوباره تلاش کن یا از ورود با شماره استفاده کن."
            )

    async def restart_page(self):
        page = await self._ensure_page()
        await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        return await self.status()

    async def _visible_locator(self, selectors: list[str]):
        page = await self._ensure_page()
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                for index in range(count - 1, -1, -1):
                    item = locator.nth(index)
                    if await item.is_visible():
                        return item
            except Exception:
                continue
        return None

    async def _assert_logged_in(self):
        state = await self.status()
        if not state["connected"]:
            raise TelegramWebError("تلگرام هنوز وارد حساب نشده است؛ ابتدا QR Code را با گوشی اسکن کن")

    async def open_chat(self, target: str):
        await self._assert_logged_in()
        page = await self._ensure_page()
        target = target.strip()
        if not target or len(target) > 160:
            raise TelegramWebError("نام کاربری یا مقصد گفتگو نامعتبر است")

        # Use exact visible search results only. A partial match can select a different person.
        search = await self._visible_locator([
            'input[placeholder*="Search"]',
            'input[aria-label*="Search"]',
            'input.input-search',
            '.input-search input',
        ])
        if search is None:
            raise TelegramWebError("کادر جستجوی Telegram Web پیدا نشد؛ رابط وب ممکن است تغییر کرده باشد")

        query = target[1:] if target.startswith("@") else target
        await search.click()
        await search.fill(query)
        await page.wait_for_timeout(1400)

        sidebar = page.locator("#column-left")
        exact = sidebar.get_by_text(query, exact=True)
        matches = []
        try:
            count = await exact.count()
            for index in range(count):
                candidate = exact.nth(index)
                if await candidate.is_visible():
                    matches.append(candidate)
        except Exception:
            matches = []

        if not matches:
            raise TelegramWebError("گفتگوی دقیق پیدا نشد؛ نام کاربری @ را کامل وارد کن")
        if len(matches) != 1:
            raise TelegramWebError("چند نتیجه مشابه پیدا شد؛ از نام کاربری دقیق با @ استفاده کن")

        await matches[0].click(timeout=5000)
        await page.wait_for_timeout(900)
        return page

    async def _message_composer(self):
        return await self._visible_locator([
            'div.input-message-input[contenteditable="true"]',
            'div[contenteditable="true"][data-placeholder*="Message"]',
            'div[contenteditable="true"][role="textbox"]',
        ])

    async def send_message(self, target: str, text: str) -> dict[str, Any]:
        text = str(text).strip()
        if not text:
            raise TelegramWebError("متن پیام خالی است")
        async with self._action_lock:
            page = await self.open_chat(target)
            composer = await self._message_composer()
            if composer is None:
                raise TelegramWebError("کادر نوشتن پیام پیدا نشد")
            await composer.click()
            await composer.fill(text)
            await composer.press("Enter")
            await page.wait_for_timeout(700)
            # Do not claim success merely because Enter was pressed.
            remaining = (await composer.inner_text()).strip()
            if remaining:
                raise TelegramWebError("پیام در کادر نوشتن باقی مانده؛ ارسال آن تأیید نشد")
            return {"ok": True, "action": "send", "target": target}

    async def click_button(self, target: str, button_text: str) -> dict[str, Any]:
        button_text = str(button_text).strip()
        if not button_text:
            raise TelegramWebError("متن دکمه خالی است")
        async with self._action_lock:
            page = await self.open_chat(target)
            await page.wait_for_timeout(500)
            # Restrict clicks to Telegram's reply-keyboard area; never click arbitrary page controls.
            selectors = [
                ".reply-markup-button",
                ".reply-markup-row button",
                ".ReplyMarkup button",
                '[class*="reply-markup"] [role="button"]',
            ]
            found = None
            for selector in selectors:
                loc = page.locator(selector)
                try:
                    count = await loc.count()
                    for index in range(count - 1, -1, -1):
                        candidate = loc.nth(index)
                        if not await candidate.is_visible():
                            continue
                        visible_text = (await candidate.inner_text()).strip()
                        if visible_text == button_text:
                            found = candidate
                            break
                except Exception:
                    continue
                if found is not None:
                    break
            if found is None:
                raise TelegramWebError(f"دکمه دقیق «{button_text}» در صفحه فعلی پیدا نشد")
            await found.click()
            await page.wait_for_timeout(500)
            return {"ok": True, "action": "click", "target": target, "button": button_text}

    async def read_recent_messages(self, target: str, limit: int = 12) -> list[str]:
        async with self._action_lock:
            page = await self.open_chat(target)
            results = await page.evaluate("""(limit) => {
              const selectors = ['.message', '.bubble', '[data-mid]', '.Message'];
              let nodes = [];
              for (const selector of selectors) {
                const found = Array.from(document.querySelectorAll(selector));
                if (found.length > nodes.length) nodes = found;
              }
              return nodes.slice(-limit).map(n => (n.innerText || n.textContent || '').trim())
                .filter(Boolean).slice(-limit);
            }""", max(1, min(int(limit), 30)))
            return results
