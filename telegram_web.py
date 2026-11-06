from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

log = logging.getLogger("meow.telegram_web")


class TelegramWebError(RuntimeError):
    pass


class TelegramWeb:
    """Automates the official Telegram Web UI; no user API_ID/API_HASH is requested.

    Authentication uses Telegram Web's QR flow. The browser storage state (cookies,
    localStorage and IndexedDB) is exported to the encrypted Supabase runtime-state row.
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
                      "--no-first-run", "--no-default-browser-check"],
            )
        if not self.browser or not self.browser.is_connected():
            raise TelegramWebError("مرورگر مشترک آماده نیست")
        context_args = {"viewport": {"width": 1440, "height": 960}, "locale": "en-US"}
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
            self.page = await self.context.new_page()
            self.page.set_default_timeout(9000)
            self.page.on("pageerror", lambda exc: log.warning("Telegram Web page error: %s", exc))
            await self.page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(3500)
            self.started = True
            log.info("Telegram Web account context opened")
        except Exception:
            await self.stop()
            raise

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
        try:
            page = await self._ensure_page()
            body = (await page.locator("body").inner_text(timeout=3000)).lower()
            login_hints = (
                "log in by qr", "log in by phone", "scan the qr", "scan qr code",
                "qr code to log in", "phone number",
            )
            has_login_hint = any(hint in body for hint in login_hints)
            composer_count = await page.locator(
                'div.input-message-input[contenteditable="true"], '
                'div[contenteditable="true"][data-placeholder]'
            ).count()
            chat_shell_count = await page.locator("#column-left, .chatlist, .chat-list, .ChatList").count()
            connected = (composer_count > 0 or chat_shell_count > 0) and not has_login_hint
            return {
                "connected": bool(connected),
                "needs_qr": not bool(connected),
                "url": page.url,
                "last_error": self.last_error,
            }
        except Exception as exc:
            self.last_error = str(exc)[:500]
            return {"connected": False, "needs_qr": True, "url": self.url, "last_error": self.last_error}

    async def screenshot(self) -> bytes:
        page = await self._ensure_page()
        return await page.screenshot(type="png", full_page=False, animations="disabled")

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
