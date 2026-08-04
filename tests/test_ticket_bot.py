"""Offline regression tests for the failure-prone parts of the ticket flow.

These tests intentionally do not contact Bilibili or create an order.  They model
browser DOM changes and network responses using small fakes, so they are safe to
run before every real ticket attempt.
"""

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import ticket_bot as bot
from browser_utils import get_browser_channel, get_browser_name


class FakeElement:
    def __init__(self, text="", *, visible=True, class_name="", attrs=None):
        self.text = text
        self.visible = visible
        self.class_name = class_name
        self.attrs = attrs or {}
        self.clicked = 0

    async def is_visible(self):
        return self.visible

    async def inner_text(self):
        return self.text

    async def evaluate(self, _script):
        return self.class_name

    async def get_attribute(self, name):
        return self.attrs.get(name)

    async def click(self):
        self.clicked += 1


class FakePage:
    def __init__(self, selector_map=None, body_text=""):
        self.selector_map = selector_map or {}
        self.body_text = body_text
        self.url = "https://show.bilibili.com/platform/detail.html?id=1"
        self.events = {}

    async def query_selector_all(self, selector):
        return self.selector_map.get(selector, [])

    async def query_selector(self, selector):
        elements = self.selector_map.get(selector, [])
        return elements[0] if elements else None

    async def inner_text(self, selector):
        return self.body_text if selector == "body" else ""

    async def screenshot(self, **_kwargs):
        return None

    def on(self, event, callback):
        self.events[event] = callback


class FakeResponse:
    def __init__(self, url, payload=None, error=None):
        self.url = url
        self.payload = payload
        self.error = error

    async def json(self):
        if self.error:
            raise self.error
        return self.payload


class FakeContext:
    def __init__(self):
        self.added = None

    async def add_cookies(self, cookies):
        self.added = cookies


class FakeChromium:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def launch(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return "browser"


class FakePlaywright:
    def __init__(self, error=None):
        self.chromium = FakeChromium(error)


class TicketBotTests(unittest.IsolatedAsyncioTestCase):
    def test_shanghai_time_is_not_affected_by_windows_tzdata(self):
        sale_time = bot.parse_sale_time("2026-08-04 12:00:00", "Asia/Shanghai")
        self.assertEqual(sale_time.utcoffset(), timedelta(hours=8))
        self.assertEqual(sale_time.astimezone(timezone.utc).hour, 4)

    def test_invalid_timezone_and_invalid_limits_are_repaired(self):
        config = {
            "ticket_url": "https://example.test/ticket",
            "sale_time": "2026-08-04 12:00:00",
            "target_date": "2026-08-10",
            "sale_timezone": "Not/AZone",
            "ticket_count": "0",
            "max_retry": "not-a-number",
        }
        with patch.object(bot, "save_config"):
            result = bot.ensure_config(config)
        self.assertEqual(result["sale_timezone"], bot.DEFAULT_SALE_TIMEZONE)
        self.assertEqual(result["ticket_count"], 1)
        self.assertEqual(result["max_retry"], 30)
        self.assertTrue(result["keep_browser_open"])
        self.assertTrue(result["keep_browser_open_on_failure"])

    def test_stale_sale_time_is_blocked_but_a_recent_time_is_allowed(self):
        server_time = 1_800_000_000.0
        stale = datetime.fromtimestamp(server_time - 61, tz=timezone.utc)
        recent = datetime.fromtimestamp(server_time - 59, tz=timezone.utc)
        self.assertTrue(bot.sale_time_is_stale(stale, server_time))
        self.assertFalse(bot.sale_time_is_stale(recent, server_time))

    def test_browser_detection_has_a_safe_no_browser_fallback(self):
        with patch("browser_utils.find_system_browser", return_value=None):
            self.assertIsNone(get_browser_channel())
            self.assertEqual(get_browser_name(), "Playwright Chromium")

    async def test_malformed_cookie_file_does_not_crash(self):
        context = FakeContext()
        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "cookies.json"
            cookie_file.write_text("not json", encoding="utf-8")
            with patch.object(bot, "COOKIE_FILE", cookie_file):
                self.assertFalse(await bot.load_cookies(context))
        self.assertIsNone(context.added)

    async def test_missing_browser_is_reported_without_an_exception(self):
        playwright = FakePlaywright(RuntimeError("executable does not exist"))
        result = await bot.launch_ticket_browser(playwright, {"headless": False})
        self.assertIsNone(result)
        message = bot.browser_launch_error_message(RuntimeError("missing"))
        self.assertIn("playwright install chromium", message)

    async def test_text_lookup_prefers_the_real_short_button_over_wrapper(self):
        wrapper = FakeElement("页面内容 立即下单 更多内容")
        button = FakeElement("立即下单")
        page = FakePage({"*": [wrapper, button]})
        found = await bot.wait_for_element_by_text(page, "立即下单", timeout=0.1)
        self.assertIs(found, button)

    async def test_disabled_button_is_never_treated_as_purchasable(self):
        disabled = FakeElement("立即下单", class_name="bili-button is-disabled")
        ready = FakeElement("立即下单", attrs={"aria-disabled": "false"})
        page = FakePage({".pre-sale-button": [disabled, ready]})
        with patch.object(bot, "detect_captcha", new=AsyncMock(return_value=False)):
            self.assertTrue(await bot.wait_for_bottom_button_ready(page, timeout=0.1))
            with patch.object(bot, "human_like_delay", new=AsyncMock()), \
                 patch.object(bot, "take_screenshot", new=AsyncMock()):
                self.assertTrue(await bot.click_bottom_button(page))
        self.assertEqual(disabled.clicked, 0)
        self.assertEqual(ready.clicked, 1)

    async def test_new_bottom_button_label_is_supported(self):
        button = FakeElement(" 立即下单 ")
        page = FakePage({".footer-menu-wrapper .bili-button": [button]})
        with patch.object(bot, "detect_captcha", new=AsyncMock(return_value=False)):
            self.assertTrue(await bot.wait_for_bottom_button_ready(page, timeout=0.1))

    async def test_ticket_dialog_is_accepted_but_unrelated_dialog_is_rejected(self):
        ticket_dialog = FakeElement("请选择场次和票档")
        page = FakePage({
            ".ticket-modal-container, .selection-modal.ticket-modal, .bili-modal.selection-modal, [role='dialog']": [ticket_dialog]
        })
        self.assertTrue(await bot.wait_for_modal(page, timeout=0.1))

        unrelated = FakeElement("新版本功能介绍")
        page = FakePage({
            ".ticket-modal-container, .selection-modal.ticket-modal, .bili-modal.selection-modal, [role='dialog']": [unrelated]
        })
        self.assertFalse(await bot.wait_for_modal(page, timeout=0))

    async def test_order_observer_requires_a_real_success_response(self):
        observer = bot.OrderFlowObserver(FakePage())
        marker = observer.mark()
        await observer._record_response(FakeResponse(
            "https://show.bilibili.com/api/ticket/order/createV2", {"code": -412, "message": "busy"}
        ))
        self.assertFalse(observer.create_succeeded_since(marker))
        self.assertEqual(observer.create_failed_since(marker)["code"], -412)

        marker = observer.mark()
        await observer._record_response(FakeResponse(
            "https://show.bilibili.com/api/ticket/order/createV2", {"code": 0, "data": {"orderId": "1"}}
        ))
        self.assertTrue(observer.create_succeeded_since(marker))

    async def test_invalid_order_response_is_ignored(self):
        observer = bot.OrderFlowObserver(FakePage())
        await observer._record_response(FakeResponse(
            "https://show.bilibili.com/api/ticket/order/createV2", error=ValueError("not json")
        ))
        await observer._record_response(FakeResponse(
            "https://show.bilibili.com/api/ticket/order/createV2", ["not", "an", "object"]
        ))
        self.assertEqual(observer.events, [])

    async def test_time_sync_uses_the_median_and_keeps_previous_offset_when_offline(self):
        old_offset = bot._server_time_offset
        now = time.time()
        samples = iter([
            (now + 0.1, now, now),
            (now + 9.0, now, now),
            (now + 0.2, now, now),
        ])
        try:
            with patch.object(bot, "_fetch_bilibili_server_time", side_effect=lambda: next(samples)), \
                 patch.object(bot.asyncio, "sleep", new=AsyncMock()):
                self.assertAlmostEqual(await bot.sync_server_time(FakePage()), 0.2, places=3)

            bot._server_time_offset = 1.25
            with patch.object(bot, "_fetch_bilibili_server_time", return_value=None), \
                 patch.object(bot.asyncio, "sleep", new=AsyncMock()):
                self.assertEqual(await bot.sync_server_time(FakePage()), 1.25)
        finally:
            bot._server_time_offset = old_offset

    async def test_screenshot_failure_is_nonfatal(self):
        page = FakePage()
        page.screenshot = AsyncMock(side_effect=RuntimeError("page already closed"))
        await bot.take_screenshot(page, "closed_page")


if __name__ == "__main__":
    unittest.main(verbosity=2)
