"""WebDriver — Playwright-backed driver implementing DeviceDriver duck-typing."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from mobilerun.agent.utils.dom_extractor import DOM_EXTRACTOR_JS

logger = logging.getLogger("mobilerun")


class WebDriver:
    """Playwright-backed driver — duck-types the DeviceDriver contract."""

    platform = "Web"

    def __init__(
        self,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        device_profile: str | None = None,
        user_agent: str | None = None,
        locale: str = "zh-CN",
        start_url: str = "about:blank",
        browser_type: str = "chromium",
        stealth: bool = False,
        timeout_ms: int = 30000,
        geolocation: dict | None = None,
        cookies: list[dict] | None = None,
        local_storage: dict | None = None,
        wechat_mock: bool = False,
    ):
        self._headless = headless
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._device_profile = device_profile
        self._custom_ua = user_agent
        self.locale = locale
        self._start_url = start_url
        self.browser_type = browser_type
        self.stealth = stealth
        self._timeout_ms = timeout_ms
        self._geolocation = geolocation
        self._cookies = cookies or []
        self._local_storage = local_storage or {}
        self._wechat_mock = wechat_mock
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    # ── DeviceDriver contract ──────────────────────────────────────────

    async def connect(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        browser_launcher = getattr(self._playwright, self.browser_type)
        launch_args = ["--disable-gpu", "--no-first-run", "--no-default-browser-check"]
        logger.info("🔧 Launching browser: headless=%s, type=%s", self._headless, self.browser_type)
        self._browser = await browser_launcher.launch(headless=self._headless, args=launch_args)

        if self._device_profile:
            devices = self._playwright.devices
            if self._device_profile in devices:
                device = devices[self._device_profile]
                self._viewport = device["viewport"]
                self._custom_ua = self._custom_ua or device.get("user_agent")
                logger.info("Using device profile: %s", self._device_profile)
            else:
                logger.warning(
                    "Unknown device profile '%s'. Available: %s",
                    self._device_profile,
                    ", ".join(sorted(devices.keys()))[:200],
                )

        context_kwargs = {"viewport": self._viewport, "locale": self.locale}
        if self._custom_ua:
            context_kwargs["user_agent"] = self._custom_ua
        if self._geolocation:
            context_kwargs["geolocation"] = self._geolocation
            context_kwargs["permissions"] = ["geolocation"]

        self._context = await self._browser.new_context(**context_kwargs)
        if self._cookies:
            await self._context.add_cookies(self._cookies)
        self._page = await self._context.new_page()
        if self._local_storage:
            await self._page.evaluate(
                "Object.entries(arguments[0]).forEach(([k,v])=>localStorage.setItem(k,v))",
                self._local_storage,
            )
        if self._wechat_mock:
            await self._inject_wechat_mock()
        if self.stealth:
            await self._inject_stealth()
        if self._start_url != "about:blank":
            await self._page.goto(self._start_url, wait_until="load", timeout=self._timeout_ms)

    async def screenshot(self, hide_overlay: bool = True) -> bytes:
        return await self._page.screenshot(type="png", full_page=False)

    async def get_ui_tree(self) -> dict:
        try:
            await self._page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(0.3)
        raw_elements = await self._page.evaluate(DOM_EXTRACTOR_JS)
        return {
            "raw_elements": raw_elements,
            "phone_state": {},
            "device_context": {
                "screen_bounds": {"width": self._viewport["width"], "height": self._viewport["height"]},
                "url": self._page.url,
                "title": await self._page.title(),
            },
        }

    async def tap(self, x: int, y: int) -> None:
        if self._is_mobile_viewport():
            await self._page.tap(x, y)
        else:
            await self._page.mouse.click(x, y)

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> None:
        steps = max(2, int(duration * 60))
        await self._page.mouse.move(x1, y1)
        await self._page.mouse.down()
        dx, dy = (x2 - x1) / steps, (y2 - y1) / steps
        step_delay = duration / steps
        for _ in range(steps):
            await asyncio.sleep(step_delay)
            x1 += dx
            y1 += dy
            await self._page.mouse.move(x1, y1)
        await self._page.mouse.up()

    async def input_text(self, text: str, clear: bool = False) -> bool:
        if clear:
            await self._page.keyboard.press("Control+a")
            await self._page.keyboard.press("Backspace")
        await self._page.keyboard.type(text, delay=20)
        return True

    async def press_button(self, button: str) -> None:
        BUTTON_MAP = {"back": "Back", "home": "Escape", "enter": "Enter", "tab": "Tab", "escape": "Escape"}
        key = BUTTON_MAP.get(button.lower(), button)
        try:
            await self._page.keyboard.press(key)
        except Exception:
            if button.lower() == "back":
                await self._page.go_back()

    async def start_app(self, target: str, activity: str | None = None) -> str:
        url = _resolve_url(target)
        await self._page.goto(url, wait_until="load", timeout=self._timeout_ms)
        return f"Navigated to {url}"

    async def get_date(self) -> str:
        return datetime.now().isoformat()

    async def get_apps(self) -> list:
        pages = self._context.pages
        return [{"name": (await p.title()) or p.url, "package": p.url} for p in pages]

    async def list_packages(self, include_system: bool = False) -> list:
        return [p.url for p in self._context.pages]

    @property
    def supported(self) -> set:
        return {"screenshot", "tap", "swipe", "drag", "input_text", "direct_text_input",
                "press_button", "start_app", "get_date", "element_index", "convert_point", "scroll"}

    @property
    def supported_buttons(self) -> set:
        return {"back", "enter", "tab", "escape"}

    # ── Web-specific ────────────────────────────────────────────────────

    async def navigate(self, url: str) -> None:
        await self._page.goto(url, wait_until="load", timeout=self._timeout_ms)

    async def scroll(self, direction: str, amount: int = 300) -> None:
        delta = amount if direction == "down" else -amount
        await self._page.mouse.wheel(0, delta)

    async def evaluate(self, js: str):
        return await self._page.evaluate(js)

    async def switch_to_tab(self, index: int) -> None:
        pages = self._context.pages
        if 0 <= index < len(pages):
            self._page = pages[index]
            await self._page.bring_to_front()

    async def close(self) -> None:
        for obj in (self._context, self._browser, self._playwright):
            try:
                if obj is not None:
                    await (obj.close() if hasattr(obj, "close") else obj.stop())
            except Exception:
                pass

    # ── Internal ────────────────────────────────────────────────────────

    def _is_mobile_viewport(self) -> bool:
        return self._viewport["width"] < 1024

    async def _inject_stealth(self) -> None:
        await self._page.evaluate("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh'] });
            const oq = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) => (
                p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : oq(p));
        """)

    async def _inject_wechat_mock(self) -> None:
        await self._page.evaluate("""
            window.wx = { ready: function(cb) { cb(); }, config: function() {}, error: function() {},
                checkJsApi: function(o) { o.success && o.success({checkResult:{}}); },
                getNetworkType: function(o) { o.success && o.success({networkType:'wifi'}); } };
            window.WeixinJSBridge = { invoke: function() {}, on: function() {} };
        """)


def _resolve_url(text: str) -> str:
    import re
    from urllib.parse import quote
    text = text.strip()
    if text.startswith(("http://", "https://")):
        return text
    if re.match(r"^[\w.-]+\.[a-z]{2,}", text):
        return f"https://{text}"
    return f"https://www.google.com/search?q={quote(text)}"
