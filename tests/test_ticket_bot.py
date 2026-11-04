"""Offline regression tests for the failure-prone parts of the ticket flow.

These tests intentionally do not contact Bilibili or create an order.  They model
browser DOM changes and network responses using small fakes, so they are safe to
run before every real ticket attempt.
"""

import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import login
import ticket_bot as bot
from order_guard import has_order_attempt, lock_order_guard, record_order_attempt
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

    async def click(self, **_kwargs):
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

    async def inner_text(self, selector, **_kwargs):
        return self.body_text if selector == "body" else ""

    async def screenshot(self, **_kwargs):
        return None

    def on(self, event, callback):
        self.events[event] = callback

    def is_closed(self):
        return False


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
    def formal_config(self, **changes):
        config = {
            "ticket_url": "https://show.bilibili.com/platform/detail.html?id=123",
            "sale_time": "2099-07-01 12:00:00",
            "target_date": "2099-07-01",
            "target_ticket": "128",
            "test_mode": False,
        }
        config.update(changes)
        return config

    def test_config_rejects_duplicate_keys_and_accepts_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            with patch.object(bot, "CONFIG_FILE", config_path):
                config_path.write_text("test_mode: true\ntest_mode: false\n", encoding="utf-8-sig")
                with self.assertRaisesRegex(ValueError, "test_mode"):
                    bot.load_config()
                config_path.write_text("test_mode: true\n", encoding="utf-8-sig")
                self.assertEqual(bot.load_config(), {"test_mode": True})

    def test_formal_mode_rejects_bad_config_without_rewriting_it(self):
        cases = [
            ({"ticket_count": 1.5}, "ticket_count"), ({"ticket_count": True}, "ticket_count"),
            ({"max_retry": 300}, "max_retry"), ({"ticket_cout": 4}, "ticket_cout"),
            ({"sale_timezone": "Typo/Zone"}, "sale_timezone"),
            ({"target_ticket": ""}, "target_ticket"), ({"headless": True}, "headless"),
            ({"target_date": "2099-02-30"}, "target_date"),
        ]
        with patch.object(bot, "save_config") as save:
            for fields, key in cases:
                with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, key):
                    bot.ensure_config(self.formal_config(**fields))
            save.assert_not_called()

    def test_only_one_unambiguous_official_project_id_is_accepted(self):
        url = self.formal_config()["ticket_url"]
        self.assertEqual(bot.ticket_project_id(url), "123")
        self.assertIsNone(bot.ticket_project_id(url + "&id=456"))
        self.assertIsNone(bot.ticket_project_id(url.replace("show.bilibili.com", "show.bilibili.com.evil.test")))
        self.assertIsNone(bot.ticket_project_id(url.replace("https:", "http:")))

    async def test_stale_or_unconfirmed_formal_config_never_launches_browser(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot, "ORDER_STATE_DIR", Path(directory)), \
             patch.object(bot, "setup_logger"), \
             patch.object(bot, "run_browser", new_callable=AsyncMock) as run_browser, \
             patch("builtins.input", return_value="") as read_input:
            with patch.object(bot, "load_config", return_value=self.formal_config(
                sale_time="2020-07-01 12:00:00"
            )):
                self.assertFalse(await bot.main())
                read_input.assert_not_called()
            with patch.object(bot, "load_config", return_value=self.formal_config()):
                self.assertFalse(await bot.main())
                read_input.assert_called_once()
            run_browser.assert_not_awaited()

    async def test_order_guard_survives_interruption_and_requires_explicit_reset(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot, "ORDER_STATE_DIR", Path(directory)), \
             patch.object(bot, "setup_logger"), \
             patch.object(bot, "load_config", side_effect=self.formal_config), \
             patch("builtins.input", side_effect=["BUY 123", "", "CHECKED 123"]), \
             patch.object(bot, "run_browser", new_callable=AsyncMock) as run_browser:
            async def interrupted_browser(_config, before_submit):
                with self.assertRaises(BlockingIOError):
                    with lock_order_guard(Path(directory), "123"):
                        self.fail("concurrent run acquired the same project")
                before_submit()
                raise RuntimeError("browser disconnected")

            run_browser.side_effect = interrupted_browser
            self.assertFalse(await bot.main())
            with lock_order_guard(Path(directory), "123") as state:
                self.assertTrue(has_order_attempt(state))
                record_order_attempt(state)
            self.assertFalse(await bot.main())
            self.assertFalse(await bot.main("123"))
            self.assertTrue(await bot.main("123"))
            with lock_order_guard(Path(directory), "123") as state:
                self.assertFalse(has_order_attempt(state))
            run_browser.assert_awaited_once()

    async def test_countdown_cancels_if_browser_was_closed(self):
        page = FakePage()
        page.is_closed = lambda: True
        sale = datetime.fromtimestamp(time.time() + 600, tz=timezone.utc)
        self.assertFalse(await bot.wait_for_sale_start(sale, page=page))

    def test_order_guard_survives_process_exit_without_cleanup(self):
        script = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "from order_guard import lock_order_guard, record_order_attempt\n"
            "with lock_order_guard(Path(sys.argv[1]), '123') as state:\n"
            "    record_order_attempt(state)\n"
            "    os._exit(7)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script, directory],
                cwd=Path(bot.__file__).parent, timeout=15, check=False,
            )
            self.assertEqual(result.returncode, 7)
            with lock_order_guard(Path(directory), "123") as state:
                self.assertTrue(has_order_attempt(state))

    async def test_formal_mode_rejects_ambiguous_options(self):
        selector = ".date-item"
        modal = FakePage({selector: [FakeElement("7月1日 上午"), FakeElement("7月1日 下午")]})
        with self.assertRaises(ValueError):
            await bot.find_purchase_option(modal, [selector], lambda text: "7月1日" in text, strict=True)

    async def test_order_is_not_clicked_when_saving_the_attempt_fails(self):
        button = FakeElement("提交订单")
        page = FakePage({"button, [role='button'], .bili-button, .submit-button": [button]})
        with patch.object(bot, "select_payment_method", new=AsyncMock(return_value=True)), \
             patch.object(bot, "human_like_delay", new=AsyncMock()):
            def write_failed():
                raise OSError("disk full")

            with self.assertRaises(OSError):
                await bot.submit_order_confirmation(page, before_submit=write_failed)
        self.assertEqual(button.clicked, 0)

    async def test_formal_order_does_not_retry_on_ui_text_alone(self):
        page = FakePage()
        submit = FakeElement("提交订单")
        retry = FakeElement("再试一次")
        observer = bot.OrderFlowObserver(page)
        with patch.object(bot, "select_payment_method", new=AsyncMock(return_value=True)), \
             patch.object(bot, "human_like_delay", new=AsyncMock()), \
             patch.object(bot, "take_screenshot", new=AsyncMock()), \
             patch.object(bot, "wait_for_element_by_text", new=AsyncMock(side_effect=[submit, retry])), \
             patch.object(bot, "time", SimpleNamespace(monotonic=iter((0, 1, 9)).__next__)):
            self.assertFalse(await bot.submit_order_confirmation(
                page, observer=observer, before_submit=lambda: None,
            ))
        self.assertEqual(submit.clicked, 1)
        self.assertEqual(retry.clicked, 0)

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

    def test_unquoted_yaml_date_time_and_ticket_number_work(self):
        import yaml

        config = yaml.safe_load(
            'ticket_url: https://example.test/ticket\nsale_time: 2027-07-01 12:00:00\n'
            'target_date: 2027-07-01\ntarget_ticket: 128\ntest_mode: "true"\n'
        )
        with patch.object(bot, "save_config"):
            bot.ensure_config(config)
        self.assertEqual(config["sale_time"], "2027-07-01 12:00:00")
        self.assertEqual(config["target_date"], "2027-07-01")
        self.assertEqual(config["target_ticket"], "128")
        self.assertIs(config["test_mode"], True)

    def test_date_matching_works_across_years_and_rejects_other_dates(self):
        self.assertTrue(bot.matches_target_date("2027-07-01", "7月1日 周四 10:00"))
        self.assertTrue(bot.matches_target_date("2027-07-01", "07-01 周四"))
        self.assertTrue(bot.matches_target_date("周四", "7月1日 周四 10:00"))
        self.assertFalse(bot.matches_target_date("2027-07-01", "7月10日"))
        self.assertFalse(bot.matches_target_date("2027-07-01", "2026/07/01"))
        self.assertFalse(bot.matches_target_date("2027-07-01", "07-01 07-10"))

    async def test_manual_step_stops_without_input(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(await bot.prompt_continue("请确认场次"))

    async def test_captcha_cannot_be_skipped_by_pressing_enter_early(self):
        page = FakePage()
        with patch.object(bot, "prompt_continue", new=AsyncMock(side_effect=[True, False])) as prompt, \
             patch.object(bot, "detect_captcha", new=AsyncMock(return_value=True)):
            self.assertFalse(await bot.handle_captcha(page))
            self.assertEqual(prompt.await_count, 2)

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
            with patch.object(login, "COOKIE_FILE", cookie_file):
                self.assertFalse(await login.load_cookies(context))
        self.assertIsNone(context.added)

    async def test_login_status_uses_the_browser_context_and_disposes_the_response(self):
        response = type("Response", (), {
            "ok": True, "json": AsyncMock(return_value={"data": {"isLogin": True}}),
            "dispose": AsyncMock(),
        })()
        context = FakeContext()
        context.request = type("Request", (), {"get": AsyncMock(return_value=response)})()
        self.assertTrue(await login.is_login(context))
        context.request.get.assert_awaited_once_with(login.LOGIN_STATUS_URL, timeout=5000)
        response.dispose.assert_awaited_once()

    async def test_login_check_does_not_treat_a_network_failure_as_logout(self):
        context = FakeContext()
        context.request = SimpleNamespace(get=AsyncMock(side_effect=OSError("offline")))
        self.assertIsNone(await login.is_login(context))

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
        hidden = FakeElement("立即下单", visible=False)
        ready = FakeElement("立即下单", attrs={"aria-disabled": "false"})
        page = FakePage({".pre-sale-button": [disabled, hidden, ready]})
        with patch.object(bot, "detect_captcha", new=AsyncMock(return_value=False)):
            self.assertTrue(await bot.wait_for_bottom_button_ready(page, timeout=0.1))
            with patch.object(bot, "human_like_delay", new=AsyncMock()), \
                 patch.object(bot, "take_screenshot", new=AsyncMock()):
                self.assertTrue(await bot.click_bottom_button(page))
        self.assertEqual(disabled.clicked, 0)
        self.assertEqual(hidden.clicked, 0)
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
        self.assertFalse(await bot.wait_for_modal(page, timeout=0.05))

    async def test_modal_submit_does_not_click_a_button_outside_the_dialog(self):
        outside = FakeElement("立即预订")
        page = FakePage({".submit-button, .bottom-bar .bili-button, button, [role='button']": [outside]})
        with patch.object(bot, "take_screenshot", new=AsyncMock()):
            self.assertFalse(await bot.click_modal_submit(page, timeout=0.05))
        self.assertEqual(outside.clicked, 0)

    async def test_captcha_detection_ignores_normal_confirmation_text(self):
        self.assertFalse(await bot.detect_captcha(FakePage(body_text="请完成观演人信息验证 Confirm order")))
        self.assertTrue(await bot.detect_captcha(FakePage(body_text="请拖动滑块完成安全验证")))

    async def test_order_success_requires_more_than_a_pay_button(self):
        page = FakePage(body_text="确认订单 立即支付")
        self.assertFalse(await bot.order_submission_succeeded(page, page.url))
        page.body_text = "支付剩余时间 14:59"
        self.assertTrue(await bot.order_submission_succeeded(page, page.url))
        observer = bot.OrderFlowObserver(page)
        await observer._record_response(FakeResponse(
            "https://show.bilibili.com/api/ticket/order/createV2", {"code": -412}
        ))
        self.assertFalse(await bot.order_submission_succeeded(page, page.url, observer))

    async def test_unknown_order_result_does_not_submit_twice(self):
        button = FakeElement("提交订单")
        selector = "button, [role='button'], .bili-button, .submit-button"
        page = FakePage({selector: [button]})
        with patch.object(bot, "select_payment_method", new=AsyncMock(return_value=True)), \
             patch.object(bot, "human_like_delay", new=AsyncMock()), \
             patch.object(bot, "take_screenshot", new=AsyncMock()), \
             patch.object(bot, "time", SimpleNamespace(
                 time=time.time, monotonic=iter((0, 9)).__next__
             )):
            self.assertFalse(await bot.submit_order_confirmation(page, max_retry=3))
        self.assertEqual(button.clicked, 1)

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
        old_reference = bot._server_time_reference
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
            bot._server_time_reference = old_reference

    async def test_screenshot_failure_is_nonfatal(self):
        page = FakePage()
        page.screenshot = AsyncMock(side_effect=RuntimeError("page already closed"))
        await bot.take_screenshot(page, "closed_page")


if __name__ == "__main__":
    unittest.main(verbosity=2)
