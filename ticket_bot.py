import asyncio
import argparse
import math
import os
import random
import re
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from loguru import logger
from playwright.async_api import ElementHandle, Page, async_playwright

from browser_utils import get_browser_channel, get_browser_name
from login import ensure_login, is_login
from login import load_cookies as load_login_cookies
from login import save_cookies as save_login_cookies
from order_guard import clear_order_attempt, has_order_attempt, lock_order_guard, record_order_attempt

def get_base_dir() -> Path:
    """获取程序所在目录（兼容源码和 PyInstaller 打包）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


BASE_DIR = get_base_dir()
CONFIG_FILE = BASE_DIR / "config.yaml"
COOKIE_FILE = BASE_DIR / "cookies.json"
USER_STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
ORDER_STATE_DIR = USER_STATE_DIR / "BilibiliTicketBot" / "order_state"
MANUAL_AVAILABLE = ContextVar("manual_available", default=True)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

DEFAULT_SALE_TIMEZONE = "Asia/Shanghai"
PURCHASE_BUTTON_TEXTS = (
    "立即预订", "立即购买", "立即购票", "立即下单", "立即抢购", "马上购票",
)
PURCHASE_BUTTON_SELECTORS = [
    ".pre-sale-button", ".footer-container .bili-button", ".detail-floating-btn .bili-button",
    ".bottom-bar .bili-button", ".footer-menu-wrapper .bili-button", "footer .bili-button",
    ".fixed-bottom .bili-button", ".submit-bar .bili-button",
    "[data-testid*='buy']", "[data-testid*='purchase']", "[class*='bottom'] [class*='button']",
    "[class*='bottom'][class*='button']", "[class*='footer'] [class*='button']",
    "[class*='footer'][class*='button']", "[class*='buy-button']", "[class*='purchase-button']",
]
TICKET_MODAL_SELECTOR = (
    ".ticket-modal-container, .selection-modal.ticket-modal, "
    ".bili-modal.selection-modal, [role='dialog']"
)


def setup_logger():
    logger.remove()
    # 控制台输出
    logger.add(sys.stdout, level="INFO", colorize=True)
    # 文件日志
    logger.add(BASE_DIR / "ticket_bot.log", level="DEBUG", encoding="utf-8", rotation="10 MB")


def browser_launch_error_message(error: Exception) -> str:
    """Return an actionable error for a missing/broken Playwright browser runtime."""
    return (
        "浏览器启动失败。请确认 Chrome 或 Edge 可以正常打开；若只安装了源码依赖，"
        "请在项目目录运行 `python -m playwright install chromium` 后重试。"
        f" 原始错误: {error}"
    )


async def launch_ticket_browser(playwright, launch_kwargs: dict):
    """Launch a browser without letting a missing runtime close the program silently."""
    try:
        return await playwright.chromium.launch(**launch_kwargs)
    except Exception as error:
        logger.error(browser_launch_error_message(error))
        return None


def resolve_sale_timezone(timezone_name: str):
    """Windows 未安装 tzdata 时仍可靠地支持会员购使用的中国标准时间。"""
    if timezone_name in ("Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "PRC"):
        return timezone(timedelta(hours=8), name="Asia/Shanghai")
    return ZoneInfo(timezone_name)


class ConfigLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key, _ in node.value:
            if not isinstance(key, yaml.ScalarNode):
                raise ValueError("配置项名称必须为文本")
            if key.value in keys:
                raise ValueError(f"配置项 {key.value} 出现多次")
            keys.add(key.value)
        return super().construct_mapping(node, deep=deep)


def load_config():
    try:
        config = yaml.load(CONFIG_FILE.read_text(encoding="utf-8-sig"), Loader=ConfigLoader)
    except FileNotFoundError:
        return {}
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("config.yaml 顶层必须是配置项映射")
    return config


def save_config(config: dict):
    temp_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=CONFIG_FILE.parent, delete=False
        ) as temporary:
            temp_path = Path(temporary.name)
            yaml.safe_dump(config, temporary, allow_unicode=True, sort_keys=False)
        temp_path.replace(CONFIG_FILE)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def prompt_input(header: str, default: str = "") -> str:
    """在控制台询问用户输入"""
    try:
        if default:
            val = input(f"{header} [{default}]: ").strip()
            if not val:
                val = default
        else:
            val = input(f"{header}: ").strip()
        return val
    except EOFError:
        if default:
            logger.warning(f"无法读取输入，使用默认值: {default}")
            return default
        raise


def ensure_config(config: dict) -> dict:
    """确保必填配置存在，缺失则询问用户并保存"""
    changed = False
    known_keys = {
        "ticket_url", "sale_time", "sale_timezone", "target_date", "target_ticket",
        "ticket_count", "viewer_name", "payment_method", "test_mode", "headless",
        "sale_time_offset", "countdown_seconds_before", "max_retry", "action_delay",
        "keep_browser_open", "keep_browser_open_on_failure",
    }
    unknown = config.keys() - known_keys
    if unknown:
        raise ValueError(f"未知配置项 {', '.join(sorted(map(str, unknown)))}，请检查拼写")
    for key, default in (
        ("keep_browser_open", True), ("keep_browser_open_on_failure", True),
        ("test_mode", True), ("headless", False),
    ):
        value = config.get(key, default)
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须为 true 或 false")
        if key not in config or config[key] is not value:
            config[key] = value
            changed = True

    formal = not config["test_mode"]
    for key, prompt in (
        ("ticket_url", "请输入票务页面链接"),
        ("sale_time", "请输入开售时间（YYYY-MM-DD HH:MM:SS）"),
        ("target_date", "请输入目标日期"),
    ):
        if not config.get(key):
            if formal:
                raise ValueError(f"正式模式需要在 config.yaml 中填写 {key}")
            config[key] = prompt_input(prompt)
            changed = True

    timezone_name = config.get("sale_timezone", DEFAULT_SALE_TIMEZONE)
    try:
        resolve_sale_timezone(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as error:
        if formal:
            raise ValueError("sale_timezone 无法识别，请核对开售时区") from error
        logger.warning(f"无效的开售时区 {timezone_name}，已改为 {DEFAULT_SALE_TIMEZONE}")
        timezone_name = DEFAULT_SALE_TIMEZONE
    if config.get("sale_timezone") != timezone_name:
        config["sale_timezone"] = timezone_name
        changed = True

    config["sale_time"] = str(config["sale_time"]).strip()
    try:
        parse_sale_time(config["sale_time"], timezone_name)
    except ValueError as error:
        raise ValueError("sale_time 必须是 YYYY-MM-DD HH:MM:SS 格式的有效时间") from error

    for key in ("ticket_url", "target_date", "target_ticket", "viewer_name"):
        value = config.get(key)
        config[key] = "" if value is None else str(value).strip()
    if not config["target_date"] or not config["ticket_url"]:
        raise ValueError("ticket_url 和 target_date 不能为空")
    target_date = DATE_PATTERN.fullmatch(config["target_date"])
    if target_date:
        try:
            datetime(int(target_date["year"] or 2000), int(target_date["month"]), int(target_date["day"]))
        except ValueError as error:
            raise ValueError("target_date 不是有效日期") from error
    payment_method = str(config.get("payment_method") or "wechat").strip().lower()
    if payment_method not in ("wechat", "alipay"):
        raise ValueError("payment_method 必须为 wechat 或 alipay")
    config["payment_method"] = payment_method

    for key, default in (
        ("action_delay", 0.5), ("sale_time_offset", 0.0), ("countdown_seconds_before", 60),
    ):
        try:
            value = float(config.get(key, default))
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError(f"{key} 必须是数字") from error
        if not math.isfinite(value) or (key != "sale_time_offset" and value < 0):
            raise ValueError(f"{key} 的数值超出有效范围")
        config[key] = value

    for key, default, upper in (("ticket_count", 1, 4), ("max_retry", 30, 30)):
        value = str(config.get(key, default)).strip()
        if not re.fullmatch(r"[0-9]{1,2}", value) or not 1 <= int(value) <= upper:
            if formal:
                raise ValueError(f"{key} 必须是 1–{upper} 之间的整数")
            logger.warning(f"{key} 必须是 1–{upper} 之间的整数，演练使用 {default}")
            value = str(default)
        if type(config.get(key)) is not int or config[key] != int(value):
            config[key] = int(value)
            changed = True

    if formal:
        if not config["target_ticket"]:
            raise ValueError("正式模式需要填写 target_ticket，以确认要购买的票档")
        if config["headless"]:
            raise ValueError("正式模式需要可见浏览器，请将 headless 设为 false")
    if changed and not formal:
        save_config(config)
        logger.info("配置已保存到 config.yaml")

    return config


def random_delay(base: float, jitter: float = 0.15) -> float:
    """返回基础值 + 随机抖动，模拟人类操作间隔"""
    return max(0.05, base + random.uniform(-jitter, jitter))


async def human_like_delay(page: Page, base: float = 0.5):
    """类人延迟：随机等待 + 随机小幅度滚动，降低风控概率"""
    await asyncio.sleep(random_delay(base, 0.15))
    try:
        # 50% 概率做一次小幅随机滚动
        if random.random() < 0.5:
            scroll_y = random.randint(-30, 30)
            await page.evaluate(f"window.scrollBy(0, {scroll_y})")
    except Exception:
        pass


async def detect_captcha(page: Page) -> bool:
    """检测页面是否出现人机验证/验证码/拥挤提示"""
    captcha_keywords = [
        "人机验证", "安全验证", "点击验证", "拖动滑块", "请输入验证码",
        "complete the captcha", "verify you are human",
    ]
    captcha_selectors = [
        ".geetest", "#captcha", ".captcha", "[class*='captcha']",
        "[class*='geetest']", "[id*='captcha']",
        "[class*='verify-box']", "[class*='slider-verify']", "[class*='slide-verify']",
    ]
    try:
        # 检查特征选择器
        for sel in captcha_selectors:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                logger.warning(f"检测到验证码元素: {sel}")
                return True

        # 检查页面文字
        text = await page.inner_text("body")
        lower_text = text.lower()
        for kw in captcha_keywords:
            if kw.lower() in lower_text:
                logger.warning(f"检测到验证码/风控关键词: {kw}")
                return True

        # 检查是否有大幅图片遮挡（常见验证码形式）
        imgs = await page.query_selector_all("img")
        for img in imgs:
            try:
                if await img.is_visible():
                    box = await img.bounding_box()
                    if box and box["width"] > 200 and box["height"] > 200:
                        # 大图可能是验证码图片
                        src = await img.get_attribute("src") or ""
                        if src and ("captcha" in src.lower() or "verify" in src.lower()):
                            logger.warning("检测到验证码图片")
                            return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def handle_captcha(page: Page) -> bool:
    """遇到验证码时暂停，等待用户手动处理"""
    logger.warning("=" * 40)
    logger.warning("检测到人机验证/验证码，已暂停自动操作")
    logger.warning("请在浏览器中手动完成验证，完成后按回车继续...")
    logger.warning("=" * 40)
    while not page.is_closed():
        if not await prompt_continue("请手动完成验证码", page):
            return False
        if not await detect_captcha(page):
            return not page.is_closed()
        logger.warning("验证码仍然可见，自动操作继续暂停")
    return False


SCREENSHOT_DIR = BASE_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)


async def take_screenshot(page: Page, name: str):
    """保存页面截图，用于复盘"""
    try:
        ts = datetime.now().strftime("%m%d_%H%M%S_%f")[:-3]
        path = SCREENSHOT_DIR / f"{name}_{ts}.png"
        await page.screenshot(path=str(path), full_page=False)
        logger.info(f"已保存截图: {path}")
    except Exception as e:
        logger.debug(f"截图失败: {e}")


async def prompt_continue(message: str, page: Optional[Page] = None) -> bool:
    """暂停并提示用户手动操作，完成后按回车继续"""
    if not MANUAL_AVAILABLE.get():
        logger.warning(f"{message}。当前浏览器不可见，停止自动操作")
        return False
    logger.warning("=" * 40)
    logger.warning(message)
    if page is not None and os.name == "nt":
        import msvcrt

        if not sys.stdin or not sys.stdin.isatty():
            logger.warning("没有可交互的控制台，停止自动操作")
            return False
        logger.warning("完成后按回车继续，按 Esc 或关闭浏览器取消")
        while not page.is_closed():
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key == "\r":
                    return True
                if key == "\x1b":
                    return False
                if key in ("\x00", "\xe0"):
                    msvcrt.getwch()
            await asyncio.sleep(0.1)
        return False
    logger.warning("完成后请按回车键继续...")
    logger.warning("=" * 40)
    try:
        input("[手动兜底] " + message + " 完成后按回车...")
        return True
    except EOFError:
        logger.warning("无法读取输入，停止自动操作")
        return False


async def load_cookies(context):
    return await load_login_cookies(context, COOKIE_FILE)


async def save_cookies(context):
    try:
        await save_login_cookies(context, COOKIE_FILE)
    except Exception as e:
        logger.warning(f"保存 Cookie 失败: {e}")


# 与 B站服务器时间同步的偏移量（秒）
_server_time_offset = 0.0
_server_time_reference: Optional[tuple[float, float]] = None


def _fetch_bilibili_server_time() -> Optional[tuple[float, float, float]]:
    """返回服务器时间及请求起止时间，用请求中点降低网络延迟造成的误差。"""
    import urllib.request
    urls = [
        "https://www.bilibili.com",
        "https://api.bilibili.com/x/web-interface/zone",
        "https://show.bilibili.com",
    ]
    for url in urls:
        try:
            request_started = time.time()
            req = urllib.request.Request(url, method="HEAD", headers={
                "User-Agent": MOBILE_USER_AGENT,
                "Accept": "*/*",
                "Cache-Control": "no-cache, no-store",
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                request_finished = time.time()
                date_header = resp.headers.get("Date")
                if date_header:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_header)
                    return dt.timestamp(), request_started, request_finished
        except Exception as e:
            logger.debug(f"从 {url} 获取服务器时间失败: {e}")
            continue
    return None


async def sync_server_time(page: Page) -> float:
    """多次同步 B站服务器时间，取中位数降低网络抖动影响。"""
    global _server_time_offset, _server_time_reference
    try:
        measurements = await asyncio.gather(*(
            asyncio.to_thread(_fetch_bilibili_server_time) for _ in range(3)
        ))
        samples = []
        for sample in measurements:
            if sample:
                server_timestamp, request_started, request_finished = sample
                midpoint = (request_started + request_finished) / 2
                samples.append(server_timestamp - midpoint)
        if not samples:
            logger.warning("同步服务器时间失败: 无法从 B站获取时间")
            return _server_time_offset

        samples.sort()
        _server_time_offset = samples[len(samples) // 2]
        _server_time_reference = (time.time() + _server_time_offset, time.monotonic())
        logger.info(
            f"服务器时间同步完成，本地时间偏移: {_server_time_offset:.3f} 秒"
            f"（{len(samples)} 次取样）"
        )
    except Exception as e:
        logger.warning(f"同步服务器时间异常: {e}")
    return _server_time_offset


def get_synced_now() -> datetime:
    """获取同步后的当前时间"""
    return datetime.fromtimestamp(get_synced_timestamp())


def get_synced_timestamp() -> float:
    if _server_time_reference:
        server_timestamp, monotonic_reference = _server_time_reference
        return server_timestamp + (time.monotonic() - monotonic_reference)
    return time.time() + _server_time_offset


def parse_sale_time(value: str, timezone_name: str) -> datetime:
    """把配置中的开售时间明确解释为指定时区，避免电脑系统时区造成误抢。"""
    local_dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return local_dt.replace(tzinfo=resolve_sale_timezone(timezone_name))


def sale_time_is_stale(sale_time: datetime, server_timestamp: float, grace_seconds: float = 60) -> bool:
    """Guard against a forgotten historical sale_time in non-test mode."""
    return sale_time.timestamp() < server_timestamp - grace_seconds


def ticket_project_id(url: str) -> Optional[str]:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() != "show.bilibili.com":
        return None
    if parts.path != "/platform/detail.html":
        return None
    ids = parse_qs(parts.query, keep_blank_values=True).get("id", [])
    if len(ids) == 1 and re.fullmatch(r"[1-9][0-9]{0,19}", ids[0]):
        return ids[0]
    return None


def confirm_formal_mode(project_id: str) -> bool:
    confirmation = f"BUY {project_id}"
    try:
        return input(f"请核对上方配置。确认后请输入 {confirmation}，直接回车可取消：").strip() == confirmation
    except EOFError:
        return False


async def wait_for_element_by_text(
    page: Page,
    text: str,
    selector: str = "*",
    timeout: float = 10,
    exact: bool = False,
    unique: bool = False,
) -> Optional[ElementHandle]:
    """根据文本查找可见元素"""
    deadline = time.time() + timeout
    text_norm = text.strip()
    while time.time() < deadline:
        elements = await page.query_selector_all(selector)
        best = None
        best_rank = None
        for el in elements:
            try:
                if not await el.is_visible():
                    continue
                t = (await el.inner_text()).strip()
                matched = t == text_norm if exact else text_norm in t
                if not matched:
                    continue
                if unique and best is not None:
                    return None
                # 页面经常让外层 div 也包含按钮文字。优先精确文本和更短的
                # 元素文本，避免把整个弹窗/页面当成按钮点击。
                rank = (0 if t == text_norm else 1, len(t))
                if best_rank is None or rank < best_rank:
                    best = el
                    best_rank = rank
            except Exception:
                continue
        if best:
            return best
        await asyncio.sleep(0.05)
    return None


async def element_is_disabled(el: ElementHandle) -> bool:
    """统一判断按钮、单选项等元素是否不可用。"""
    try:
        cls = str(await el.evaluate("e => e.className || ''")).lower()
        disabled = await el.get_attribute("disabled")
        aria_disabled = (await el.get_attribute("aria-disabled") or "").lower()
        return (
            disabled is not None
            or aria_disabled == "true"
            or any(token in cls for token in ("is-disabled", "disabled", "disable", "sold-out"))
        )
    except Exception:
        return True


async def element_is_selected(el: ElementHandle) -> bool:
    try:
        cls = str(await el.evaluate("e => e.className || ''")).lower()
        aria_checked = (await el.get_attribute("aria-checked") or "").lower()
        aria_selected = (await el.get_attribute("aria-selected") or "").lower()
        checked = await el.evaluate(
            "e => e.matches(':checked') || !!e.querySelector('input:checked')"
        )
        return checked is True or "true" in (aria_checked, aria_selected) or any(
            re.search(r"(?:^|[-_])(?:selected|active|checked)$", token)
            and not re.search(r"(?:^|[-_])(?:not|un|no)[-_]", token)
            for token in cls.split()
        )
    except Exception:
        return False


async def click_by_text(
    page: Page,
    text: str,
    selector: str = "*",
    timeout: float = 10,
    exact: bool = False,
) -> bool:
    """点击包含指定文本的元素"""
    el = await wait_for_element_by_text(page, text, selector, timeout, exact)
    if el:
        await el.click()
        return True
    return False


def is_purchase_button_text(text: str) -> bool:
    normalized = "".join(text.split())
    return any(candidate in normalized for candidate in PURCHASE_BUTTON_TEXTS)


async def log_visible_actions(page: Page, reason: str):
    """页面改版时输出无敏感信息的可点击控件摘要，方便快速更新定位器。"""
    selector = "button, [role='button'], .bili-button, [class*='button'], [class*='btn']"
    try:
        elements = await page.query_selector_all(selector)
        actions = []
        for el in elements:
            if not await el.is_visible():
                continue
            text = " ".join((await el.inner_text()).split())[:80]
            if not text:
                continue
            cls = str(await el.evaluate("e => e.className || ''"))[:100]
            actions.append(f"{text} [{cls}]")
            if len(actions) >= 15:
                break
        logger.warning(f"页面诊断（{reason}）: {actions or '未找到可见操作控件'}")
    except Exception as e:
        logger.debug(f"页面诊断失败: {e}")


async def wait_for_sale_start(
    target_dt: datetime,
    before_seconds: float = 60,
    sale_time_offset: float = 0.0,
    resync_callback=None,
    page: Optional[Page] = None,
) -> bool:
    """等待开售（使用同步后的服务器时间）"""
    # 应用用户配置的时间偏移
    effective_target_dt = target_dt.timestamp() + sale_time_offset

    now_timestamp = get_synced_timestamp()
    start_wait = effective_target_dt - before_seconds
    if now_timestamp < start_wait:
        logger.info(
            f"离开售还有 {start_wait - now_timestamp:.1f} 秒（已同步服务器时间），"
            f"提前 {before_seconds:g} 秒进入倒计时"
        )
        next_sync = time.monotonic() + 60
        while get_synced_timestamp() < start_wait:
            if page and page.is_closed():
                return False
            sleep_sec = min(1.0, max(0, start_wait - get_synced_timestamp()))
            await asyncio.sleep(sleep_sec)
            if (
                resync_callback and time.monotonic() >= next_sync
                and effective_target_dt - get_synced_timestamp() > 60
            ):
                await resync_callback()
                next_sync = time.monotonic() + 60

    logger.info("进入开售倒计时")
    last_log = -1
    while True:
        if page and page.is_closed():
            return False
        diff = effective_target_dt - get_synced_timestamp()
        if diff <= 0:
            logger.info("已到达开售时间！")
            return True
        sec_int = int(diff)
        if sec_int != last_log and sec_int % 5 == 0:
            logger.info(f"倒计时: {diff:.1f} 秒")
            last_log = sec_int
        await asyncio.sleep(0.05)


async def find_ticket_modal(page: Page) -> Optional[ElementHandle]:
    for modal in await page.query_selector_all(TICKET_MODAL_SELECTOR):
        if await modal.is_visible():
            text = await modal.inner_text()
            if any(keyword in text for keyword in ("场次", "票价", "票档")):
                return modal
    return None


async def wait_for_modal(page: Page, timeout: float = 3.0) -> bool:
    """等待购票弹窗出现"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await find_ticket_modal(page):
            return True
        await asyncio.sleep(0.1)
    return False


async def wait_for_bottom_button_ready(page: Page, timeout: float = 30.0) -> bool:
    """等待底部按钮从'即将开售'变为'立即预订'"""
    logger.info("等待底部按钮变为可点击状态...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await detect_captcha(page) and not await handle_captcha(page):
                return False

            for sel in PURCHASE_BUTTON_SELECTORS:
                btns = await page.query_selector_all(sel)
                for b in btns:
                    if not await b.is_visible():
                        continue
                    text = (await b.inner_text()).strip()
                    if is_purchase_button_text(text) and not await element_is_disabled(b):
                        logger.info(f"底部按钮已就绪: {text}")
                        return True
                    if "即将开售" in text:
                        logger.debug(f"仍需等待: {text}")
        except Exception as e:
            logger.debug(f"等待按钮过程出错: {e}")
        await asyncio.sleep(0.1)
    logger.warning("等待底部按钮就绪超时")
    await log_visible_actions(page, "未识别到开售按钮")
    return False


async def click_bottom_button(page: Page) -> bool:
    """点击底部购票按钮（立即预订/立即购买）"""
    logger.info("点击底部购票按钮...")
    for _ in range(20):
        try:
            if await detect_captcha(page) and not await handle_captcha(page):
                return False

            for sel in PURCHASE_BUTTON_SELECTORS:
                btns = await page.query_selector_all(sel)
                for b in btns:
                    if not await b.is_visible():
                        continue
                    text = (await b.inner_text()).strip()
                    if is_purchase_button_text(text) and not await element_is_disabled(b):
                        await human_like_delay(page, 0.1)
                        await b.click(timeout=1000)
                        logger.info(f"已点击底部按钮: {text}")
                        await take_screenshot(page, "clicked_bottom_btn")
                        return True
        except Exception as e:
            logger.debug(f"点击底部按钮出错: {e}")
        await asyncio.sleep(random_delay(0.1, 0.03))
    await log_visible_actions(page, "点击开售按钮失败")
    return False


async def open_ticket_modal(page: Page) -> bool:
    """打开购票弹窗：等待按钮就绪后点击"""
    return await wait_for_bottom_button_ready(page, timeout=30) and await click_bottom_button(page)


DATE_PATTERN = re.compile(
    r"(?<![0-9])(?:(?P<year>[0-9]{4})[-./年])?"
    r"(?P<month>[0-9]{1,2})[-./月](?P<day>[0-9]{1,2})日?(?![0-9])"
)


def matches_target_date(target_date: str, text: str) -> bool:
    target = DATE_PATTERN.fullmatch(target_date)
    if not target:
        return target_date in text
    dates = list(DATE_PATTERN.finditer(text))
    if len(dates) != 1:
        return False
    candidate = dates[0]
    return (
        int(target["month"]) == int(candidate["month"])
        and int(target["day"]) == int(candidate["day"])
        and (not target["year"] or not candidate["year"] or target["year"] == candidate["year"])
    )


DATE_SELECTORS = (
    ".bili-radio-button", ".date-item", ".session-item", ".day-item",
    "[class*='date']", "[class*='session']", "[class*='day-']",
)
TICKET_SELECTORS = (
    ".ticket-selection-wrapper .bili-radio-button",
    ".price-container .bili-radio-button", ".sku-container .bili-radio-button",
    ".sku-item", ".ticket-item", ".ticket-sku", ".price-item",
    ".ticket-class-item", ".ticket-type-item", ".sku-card",
    "[class*='sku-item']", "[class*='price-item']",
    "[class*='ticket-class-item']", "[class*='ticket-type-item']",
)


async def find_purchase_option(modal, selectors, matches, strict):
    for selector in selectors:
        options = []
        for option in await modal.query_selector_all(selector):
            if await option.is_visible() and not await element_is_disabled(option):
                text = (await option.inner_text()).strip()
                if text and matches(text):
                    options.append(option)
        if options:
            if strict and len(options) > 1:
                raise ValueError("页面有多个可用选项符合配置")
            return options[0]
    return None


async def select_purchase_option(page, selectors, matches, name, timeout, strict, verify_only):
    deadline = time.monotonic() + timeout
    clicked = False
    while time.monotonic() < deadline and not page.is_closed():
        if await detect_captcha(page) and not await handle_captcha(page):
            return False
        modal = await find_ticket_modal(page)
        if modal:
            try:
                option = await find_purchase_option(modal, selectors, matches, strict)
            except ValueError as error:
                logger.warning(f"{name}有歧义：{error}")
                return False
            if option:
                if await element_is_selected(option):
                    logger.info(f"已确认{name}: {(await option.inner_text()).strip()}")
                    return True
                if not clicked and not verify_only:
                    await human_like_delay(page, 0.15)
                    try:
                        await option.click(timeout=1000)
                    except Exception as error:
                        logger.warning(f"点击{name}失败，等待页面确认: {error}")
                    else:
                        if not strict:
                            return True
                    clicked = True
        await asyncio.sleep(0.1)
    logger.warning(f"无法确认{name}已正确选中")
    return False


async def select_date(page: Page, target_date: str, timeout: float = 5.0, *, strict=False, verify_only=False) -> bool:
    return await select_purchase_option(
        page, DATE_SELECTORS, lambda text: matches_target_date(target_date, text),
        "场次", timeout, strict, verify_only,
    )


async def select_ticket(page: Page, target_ticket: str, timeout: float = 5.0, *, strict=False, verify_only=False) -> bool:
    if target_ticket.isdecimal():
        price = re.compile(rf"(?<![\d.,]){re.escape(target_ticket)}(?:\.0{{1,2}})?(?!\d|[.,]\d)")
    else:
        price = None

    def matches(text):
        return (
            (price.search(text) if price else target_ticket in text)
            and not any(word in text.lower() for word in ("售罄", "已售罄", "sold out", "sold-out"))
        )

    return await select_purchase_option(
        page, TICKET_SELECTORS, matches, "票档", timeout, strict, verify_only,
    )


async def set_ticket_count(page: Page, ticket_count: int, timeout: float = 3.0, *, verify_only=False) -> bool:
    """先读取数量，每次只点击一次，随后重新读取以确认调整生效。"""
    deadline = time.monotonic() + timeout
    expected = None
    while time.monotonic() < deadline:
        modal = await find_ticket_modal(page)
        if not modal:
            break
        counter = await modal.query_selector(
            "input[type='number']:visible, [role='spinbutton']:visible, "
            ".bili-input-number input:visible, .quantity-control input:visible, .ticket-count input:visible"
        )
        if not counter:
            if ticket_count == 1 and expected is None:
                plus = await wait_for_element_by_text(
                    modal, "+", selector="button, [role='button'], [class*='plus']",
                    timeout=0.15, exact=True,
                )
                return plus is None
            break
        value = await counter.evaluate("e => e.value || e.getAttribute('aria-valuenow')")
        if not str(value).isdigit():
            break
        current = int(value)
        if expected is not None and current != expected:
            await asyncio.sleep(0.05)
            continue
        if current == ticket_count:
            logger.info(f"已确认购票数量: {current}")
            return True
        if verify_only:
            break
        step = 1 if current < ticket_count else -1
        container = await counter.evaluate_handle(
            "e => e.closest('.bili-input-number, .quantity-control, .ticket-count') || e.parentElement"
        )
        controls = container.as_element()
        if not controls:
            break
        selector = "button, [role='button'], [class*='plus'], [class*='minus']"
        button = await wait_for_element_by_text(
            controls, "+" if step > 0 else "-", selector=selector, timeout=0.2, exact=True
        )
        if not button or await element_is_disabled(button):
            break
        try:
            await button.click(timeout=700)
        except Exception as error:
            logger.debug(f"调整购票数量失败: {error}")
        expected = current + step
    logger.warning(f"未能把购票数量调整为 {ticket_count}")
    return False


async def click_modal_submit(page: Page, timeout: float = 5.0, *, project_id=None, before_submit=None) -> bool:
    """点击弹窗底部的'立即预订'按钮"""
    logger.info("尝试点击弹窗底部立即预订...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if project_id and ticket_project_id(page.url) != project_id:
            return False
        if await detect_captcha(page) and not await handle_captcha(page):
            return False
        modal = await find_ticket_modal(page)
        if not modal:
            await asyncio.sleep(0.1)
            continue

        # 优先按文字匹配弹窗内的提交按钮
        for text in PURCHASE_BUTTON_TEXTS:
            el = await wait_for_element_by_text(
                modal, text,
                selector=".submit-button, .bottom-bar .bili-button, button, [role='button']",
                timeout=0.3, exact=True, unique=project_id is not None,
            )
            if el and not await element_is_disabled(el):
                await human_like_delay(page, 0.1)
                if project_id and ticket_project_id(page.url) != project_id:
                    return False
                if before_submit:
                    before_submit()
                try:
                    await el.click(timeout=1000)
                    logger.info(f"已点击弹窗按钮: {text}")
                    await take_screenshot(page, "clicked_modal_submit")
                    return True
                except Exception as error:
                    logger.warning(f"预订按钮操作结果未确认: {error}")
                    return False

        await asyncio.sleep(random_delay(0.1, 0.03))

    logger.warning("未找到弹窗提交按钮")
    await take_screenshot(page, "fail_click_modal_submit")
    return False


async def select_payment_method(page: Page, payment_method: str, *, verify_only=False) -> bool:
    """选择支付方式；找不到目标选项时不冒充成功。"""
    names = ["微信支付", "微信"] if payment_method == "wechat" else ["支付宝支付", "支付宝"]
    for name in names:
        option = await wait_for_element_by_text(
            page, name,
            selector="label, [role='radio'], [class*='payment'], [class*='pay-type'], [class*='payType']",
            timeout=0.5, exact=True, unique=True,
        )
        if not option or await element_is_disabled(option):
            continue
        if await element_is_selected(option):
            return True
        if verify_only:
            continue
        try:
            await option.click(timeout=1000)
            await asyncio.sleep(0.15)
            if await element_is_selected(option):
                logger.info(f"已确认支付方式: {name}")
                return True
            logger.warning(f"无法确认支付方式是否已选中: {name}")
            return False
        except Exception as e:
            logger.debug(f"选择支付方式失败: {e}")
    logger.warning(f"未找到配置的支付方式: {payment_method}")
    return False


class OrderFlowObserver:
    """仅观察页面本来就发出的订单接口响应，不主动请求或修改订单。"""

    def __init__(self, page: Page):
        self.page = page
        self.events: list[dict] = []

    def install(self):
        self.page.on("response", self._on_response)

    def mark(self) -> int:
        return len(self.events)

    def _on_response(self, response):
        url = urlsplit(response.url)
        if url.hostname != "show.bilibili.com" or not url.path.startswith("/api/ticket/order/"):
            return
        asyncio.create_task(self._record_response(response))

    async def _record_response(self, response):
        try:
            payload = await response.json()
        except Exception:
            return
        if not isinstance(payload, dict) or "code" not in payload:
            return

        action = urlsplit(response.url).path.rstrip("/").rsplit("/", 1)[-1].lower()
        phase = "create" if re.fullmatch(r"create(?:v[0-9]+)?", action) else action
        event = {
            "phase": phase,
            "code": payload.get("code"),
            "message": str(payload.get("message") or payload.get("msg") or ""),
        }
        self.events.append(event)
        if phase == "create":
            if event["code"] == 0:
                logger.info("服务端已确认创建订单")
            else:
                logger.warning(
                    f"服务端创建订单未成功: code={event['code']} {event['message']}"
                )

    def create_succeeded_since(self, marker: int) -> bool:
        return any(
            event["phase"] == "create" and event["code"] == 0
            for event in self.events[marker:]
        )

    def create_failed_since(self, marker: int) -> Optional[dict]:
        for event in reversed(self.events[marker:]):
            if event["phase"] == "create" and event["code"] != 0:
                return event
        return None


async def order_submission_succeeded(
    page: Page, previous_url: str, observer: Optional[OrderFlowObserver] = None,
    marker: int = 0,
) -> bool:
    """只根据明确的支付页/成功信号判断提交成功。"""
    if observer and observer.create_succeeded_since(marker):
        return True
    if observer and observer.create_failed_since(marker):
        return False
    url = urlsplit(page.url.lower())
    success_path_tokens = ("cashier", "payment", "pay.html", "/pay/", "order/success")
    if page.url != previous_url and any(token in url.path for token in success_path_tokens):
        return True
    try:
        text = await page.inner_text("body", timeout=500)
    except Exception:
        return False
    return any(signal in text for signal in ("支付剩余时间", "订单创建成功", "支付二维码"))


async def submit_order_confirmation(
    page: Page,
    payment_method: str = "wechat",
    max_retry: int = 10,
    observer: Optional[OrderFlowObserver] = None,
    *,
    before_submit=None,
    viewer_name="",
) -> bool:
    """在确认订单页面提交订单；仅在出现明确成功信号后返回 True。"""
    action_selector = "button, [role='button'], .bili-button, .submit-button"
    logger.info("进入确认订单页面，尝试提交订单...")
    order_url = page.url
    parts = urlsplit(order_url)
    if before_submit and (parts.scheme != "https" or parts.netloc.lower() != "show.bilibili.com"):
        logger.warning("订单确认页已离开会员购，停止自动提交")
        return False
    if not await select_payment_method(page, payment_method):
        if before_submit or not await prompt_continue(f"请手动确认支付方式 {payment_method}", page):
            return False

    label = "提交订单"
    for attempt in range(1, max_retry + 1):
        if await detect_captcha(page) and not await handle_captcha(page):
            return False
        button = await wait_for_element_by_text(
            page, label, selector=action_selector, timeout=3, exact=True,
            unique=before_submit is not None,
        )
        if not button or await element_is_disabled(button):
            logger.warning(f"未找到可用的“{label}”按钮，请检查浏览器页面")
            break

        await human_like_delay(page, 0.1)
        if attempt > 1 and await order_submission_succeeded(page, previous_url, observer, marker):
            return True
        if before_submit and (page.is_closed() or page.url != order_url):
            logger.warning("订单页已关闭或切换，停止自动提交")
            return False
        if before_submit and (
            not await select_payment_method(page, payment_method, verify_only=True)
            or (viewer_name and not await select_viewer(page, viewer_name, timeout=0.5, verify_only=True))
            or page.url != order_url
        ):
            logger.warning("订单页、观演人或支付方式已变化，停止自动提交")
            return False
        previous_url = page.url
        marker = observer.mark() if observer else 0
        if before_submit:
            before_submit()
        try:
            await button.click(timeout=1500)
            logger.info(f"第 {attempt} 次提交，已点击“{label}”")
        except Exception as error:
            logger.debug(f"点击“{label}”时出现异常: {error}")

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if await order_submission_succeeded(page, previous_url, observer, marker):
                logger.info("已确认订单创建成功，请检查浏览器页面")
                return True
            if await detect_captcha(page):
                if not await handle_captcha(page):
                    return False
                deadline = time.monotonic() + 8
                continue
            retry_btn = await wait_for_element_by_text(
                page, "再试一次", selector=action_selector, timeout=0.15, exact=True
            )
            if retry_btn and not await element_is_disabled(retry_btn):
                confirmed_failure = observer and observer.create_failed_since(marker)
                if confirmed_failure or (before_submit is None and label == "提交订单"):
                    label = "再试一次"
                    break
            await asyncio.sleep(0.1)
        else:
            logger.warning("订单状态未确认，已停止自动提交，请在浏览器中核对订单")
            break

    logger.warning("自动提交结束，未确认订单创建成功")
    await take_screenshot(page, "submit_order_failed")
    return False


async def select_viewer(page: Page, viewer_name: str, timeout: float = 10.0, *, verify_only=False) -> bool:
    """选择观演人"""
    if not viewer_name:
        logger.info("未配置观演人，跳过")
        return True

    logger.info(f"选择观演人: {viewer_name}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await detect_captcha(page) and not await handle_captcha(page):
            return False

        matches = [
            locator for locator in await page.get_by_text(viewer_name, exact=True).all()
            if await locator.is_visible()
        ]
        if len(matches) > 1:
            logger.warning("页面有多位同名观演人，请手动核对")
            return False
        if matches:
            try:
                name_el = await matches[0].element_handle(timeout=500)
                handle = await name_el.evaluate_handle(
                    "e => e.closest(\"label, [role='checkbox'], [class*='viewer'], "
                    "[class*='audience'], [class*='buyer'], [class*='attendee']\") || e.parentElement"
                )
                clickable = handle.as_element() if handle else name_el
                if not clickable or await element_is_disabled(clickable):
                    return False
                if await element_is_selected(clickable):
                    logger.info(f"观演人已选中: {viewer_name}")
                    return True
                if verify_only:
                    return False
                await human_like_delay(page, 0.2)
                await clickable.click(timeout=1000)
                await asyncio.sleep(0.15)
                if await element_is_selected(clickable):
                    logger.info(f"已确认观演人: {viewer_name}")
                    await take_screenshot(page, "selected_viewer")
                    return True
                logger.warning(f"无法确认观演人是否已选中: {viewer_name}")
                return False
            except Exception as e:
                logger.debug(f"选择观演人失败: {e}")
        await asyncio.sleep(random_delay(0.2, 0.05))

    logger.warning("未找到观演人")
    await take_screenshot(page, "fail_select_viewer")
    return False


async def click_pay(page: Page, payment_method: str, timeout: float = 10.0) -> bool:
    """点击去支付，并选择支付方式"""
    logger.info("尝试去支付...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await detect_captcha(page) and not await handle_captcha(page):
            return False

        # 去支付 / 确认支付 / 提交订单 / 立即支付
        texts = ["去支付", "确认支付", "提交订单", "立即支付", "确认付款"]
        for t in texts:
            if await click_by_text(
                page, t,
                selector="button, [role='button'], .bili-button, .submit-button",
                timeout=0.5, exact=True,
            ):
                logger.info(f"已点击: {t}")
                await take_screenshot(page, "clicked_pay")
                await human_like_delay(page, 0.5)
                # 选择支付方式
                if payment_method == "wechat":
                    await click_by_text(page, "微信支付", selector="label, [role='radio']", timeout=2)
                    await click_by_text(page, "微信", selector="label, [role='radio']", timeout=2)
                elif payment_method == "alipay":
                    await click_by_text(page, "支付宝", selector="label, [role='radio']", timeout=2)
                    await click_by_text(page, "支付宝支付", selector="label, [role='radio']", timeout=2)
                await take_screenshot(page, "payment_page")
                return True
        await asyncio.sleep(random_delay(0.15, 0.05))

    logger.warning("未找到支付按钮")
    await take_screenshot(page, "fail_click_pay")
    return await prompt_continue("未找到支付按钮，请手动点击去支付", page)


async def run_purchase_flow(page: Page, config: dict, before_submit=None):
    """抢票主流程（按视频观察到的流程）"""
    target_date = config["target_date"]
    target_ticket = config.get("target_ticket", "")
    payment_method = config.get("payment_method", "wechat")
    test_mode = config.get("test_mode", True)
    project_id = None if test_mode else ticket_project_id(config["ticket_url"])
    if not test_mode and (not project_id or before_submit is None):
        logger.error("正式购票需要已确认的活动及本地订单记录")
        return False
    ticket_count = config.get("ticket_count", 1)
    action_delay = config.get("action_delay", 0.5)
    order_observer = OrderFlowObserver(page)
    order_observer.install()

    # 1. 打开页面
    logger.info("打开购票页面...")
    bottom_selector = ", ".join(f"{selector}:visible" for selector in PURCHASE_BUTTON_SELECTORS)
    for retry in range(3):
        try:
            await page.goto(config["ticket_url"], wait_until="domcontentloaded", timeout=30000)
            if project_id and ticket_project_id(page.url) != project_id:
                logger.error("票务页面已重定向到其他活动或网址，停止购票")
                return False
            if await detect_captcha(page) and not await handle_captcha(page):
                return False
            await page.locator(bottom_selector).first.wait_for(timeout=5000)
            break
        except Exception as error:
            logger.debug(f"加载票务页面失败: {error}")
            if page.is_closed():
                return False
        logger.warning(f"页面加载未完成，第 {retry + 1} 次重试")
        await asyncio.sleep(2)
    else:
        logger.error("页面加载失败")
        await take_screenshot(page, "page_load_failed")
        return False

    if await detect_captcha(page) and not await handle_captcha(page):
        return False

    # 同步 B站服务器时间
    await sync_server_time(page)
    if project_id and _server_time_reference is None:
        logger.error("无法校准服务器时间，停止正式购票")
        return False

    # 预探测
    try:
        btn = await page.query_selector(bottom_selector)
        if btn:
            btn_text = (await btn.inner_text()).strip()
            logger.info(f"当前底部按钮文字: {btn_text}")
        date_labels = await page.query_selector_all(".bili-radio-button")
        logger.info(f"当前页面日期选项数量: {len(date_labels)}")
    except Exception:
        pass
    await take_screenshot(page, "presale_probe")

    # 2. 等待开售
    sale_time = parse_sale_time(
        config["sale_time"], config.get("sale_timezone", DEFAULT_SALE_TIMEZONE)
    )
    sale_time_offset = config.get("sale_time_offset", 0.0)
    if not test_mode and sale_time_is_stale(sale_time, get_synced_timestamp() - sale_time_offset):
        logger.error(
            "开售时间已超过 60 秒；为避免使用旧配置误操作，已停止自动下单。"
            "请检查 sale_time 或暂时启用 test_mode。"
        )
        return False
    logger.info(
        f"开售时间按 {config.get('sale_timezone', DEFAULT_SALE_TIMEZONE)} 解释: "
        f"{sale_time.isoformat()}"
    )
    if not await wait_for_sale_start(
        sale_time,
        config.get("countdown_seconds_before", 60),
        sale_time_offset,
        resync_callback=lambda: sync_server_time(page),
        page=page,
    ):
        logger.warning("浏览器已关闭，取消倒计时")
        return False

    # 开售瞬间：强制刷新页面
    logger.info("开售时间到，强制刷新页面...")
    for retry in range(2):
        try:
            await page.reload(wait_until="domcontentloaded", timeout=10000)
            break
        except Exception as error:
            logger.warning(f"第 {retry + 1} 次刷新页面失败: {error}")
            if page.is_closed():
                return False
    else:
        if project_id:
            return False
    if project_id and ticket_project_id(page.url) != project_id:
        logger.error("刷新后活动链接发生变化，停止购票")
        return False

    # 3. 等待底部按钮变为'立即预订'并点击
    logger.info("等待底部购票按钮就绪...")
    if not await wait_for_bottom_button_ready(page, timeout=30):
        logger.error("开售后 30 秒内购票按钮仍未就绪")
        return False
    if not await click_bottom_button(page):
        logger.error("无法点击底部购票按钮")
        return False

    # 4. 等待购票弹窗出现
    logger.info("等待购票弹窗...")
    if not await wait_for_modal(page, timeout=5):
        logger.error("购票弹窗未出现")
        await take_screenshot(page, "modal_not_appear")
        await log_visible_actions(page, "购票弹窗未出现")
        if not await prompt_continue("购票弹窗未出现，请手动点击底部按钮", page):
            return False
        if not await wait_for_modal(page, timeout=5):
            return False

    # 5. 选择日期
    if not await select_date(page, target_date, timeout=5, strict=bool(project_id)):
        logger.warning("选择日期失败")
        await log_visible_actions(page, "未匹配到目标日期")
        if project_id or not await prompt_continue("未找到目标日期，请手动选择", page):
            return False

    await human_like_delay(page, action_delay)

    # 6. 选择票档
    if not await select_ticket(page, target_ticket, timeout=5, strict=bool(project_id)):
        logger.warning("选择票档失败")
        await log_visible_actions(page, "未匹配到目标票档")
        if project_id or not await prompt_continue("未找到目标票档，请手动选择", page):
            return False

    await human_like_delay(page, action_delay)

    if not await set_ticket_count(page, ticket_count):
        if project_id or not await prompt_continue(f"请手动把购票数量调整为 {ticket_count}", page):
            return False

    # 7. 点击弹窗底部'立即预订'
    if test_mode:
        logger.info("测试模式：已选完日期票档，停止后续操作")
        return True

    if not (
        await select_date(page, target_date, timeout=0.5, strict=True, verify_only=True)
        and await select_ticket(page, target_ticket, timeout=0.5, strict=True, verify_only=True)
        and await set_ticket_count(page, ticket_count, timeout=0.5, verify_only=True)
    ):
        logger.warning("场次、票档或票数已变化，停止自动提交，请核对页面")
        return False

    if not await click_modal_submit(
        page, timeout=5, project_id=project_id, before_submit=before_submit
    ):
        logger.error("无法点击弹窗立即预订")
        return False

    # 8. 处理可能出现的验证码
    await asyncio.sleep(1)
    if await detect_captcha(page) and not await handle_captcha(page):
        return False

    # 9. 进入确认订单页面，选择观演人并提交订单（带重试）
    await asyncio.sleep(1.5)
    await take_screenshot(page, "order_confirmation")
    viewer_name = config.get("viewer_name", "").strip()
    if not viewer_name or ticket_count > 1 or not await select_viewer(page, viewer_name, timeout=5):
        logger.warning("无法自动核对所有观演人，请在浏览器中确认并手动提交")
        return False
    success = await submit_order_confirmation(
        page, payment_method, max_retry=config.get("max_retry", 30), observer=order_observer,
        before_submit=before_submit, viewer_name=viewer_name,
    )

    if success:
        logger.info("订单提交流程执行完毕，请在页面上确认最终支付")
        return True
    else:
        logger.error("订单提交失败")
        return False


async def main(clear_project_id=None) -> bool:
    setup_logger()
    try:
        if clear_project_id is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,19}", clear_project_id):
                raise ValueError("活动编号必须是正整数")
            with lock_order_guard(ORDER_STATE_DIR, clear_project_id) as state:
                if not has_order_attempt(state):
                    logger.info("该活动没有待核对的本地订单记录")
                    return True
                phrase = f"CHECKED {clear_project_id}"
                answer = input(f"请先在 B 站会员购订单页核对订单状态。确认后输入 {phrase}：")
                if answer.strip() != phrase:
                    logger.warning("订单记录继续保留")
                    return False
                clear_order_attempt(state)
                logger.info("已解除本地保护。请重新运行购票程序并核对配置")
                return True

        config = ensure_config(load_config())
        project_id = None
        if not config["test_mode"]:
            project_id = ticket_project_id(config["ticket_url"])
            if not project_id:
                raise ValueError("正式模式需要 show.bilibili.com/platform/detail.html?id=活动编号")
            sale_time = parse_sale_time(config["sale_time"], config["sale_timezone"])
            if sale_time_is_stale(sale_time, time.time() - config["sale_time_offset"]):
                raise ValueError("按当前电脑时钟，开售时间已过，请核对日期和时区")
    except (OSError, yaml.YAMLError, ValueError, EOFError) as error:
        logger.error(f"启动检查未通过（{error}）")
        return False
    logger.info("=" * 40)
    logger.info("B站会员购抢票脚本启动")
    logger.info(f"目标: {config['ticket_url']}")
    logger.info(f"开售时间: {config['sale_time']} {config['sale_timezone']}（偏移 {config['sale_time_offset']:+g} 秒）")
    logger.info(f"目标日期: {config['target_date']}")
    logger.info(f"目标票档: {config['target_ticket'] or '首个可用票档'}")
    logger.info(f"购票数量: {config.get('ticket_count', 1)}")
    logger.info(f"观演人: {config['viewer_name'] or '需要手动选择'}")
    logger.info(f"支付方式: {config['payment_method']}")
    logger.info(f"运行模式: {'演练' if config['test_mode'] else '正式购票'}")

    manual_token = MANUAL_AVAILABLE.set(not config["headless"])
    try:
        if project_id is None:
            return await run_browser(config)
        with lock_order_guard(ORDER_STATE_DIR, project_id) as state:
            if has_order_attempt(state):
                logger.error(
                    f"活动 {project_id} 有待核对的订单记录。请先检查会员购订单页，"
                    f"确认后使用 --clear-order-guard {project_id} 解除本地保护"
                )
                return False
            if not confirm_formal_mode(project_id):
                logger.info("未确认，已取消正式购票")
                return False
            return await run_browser(config, before_submit=lambda: record_order_attempt(state))
    except Exception as error:
        logger.error(f"运行已停止（{error}）")
        return False
    finally:
        MANUAL_AVAILABLE.reset(manual_token)


async def run_browser(config, before_submit=None) -> bool:
    async with async_playwright() as p:
        channel = get_browser_channel()
        logger.info(f"使用浏览器: {get_browser_name()}")

        launch_kwargs = {"headless": config.get("headless", False)}
        if channel:
            launch_kwargs["channel"] = channel

        browser = await launch_ticket_browser(p, launch_kwargs)
        if browser is None:
            await prompt_continue("浏览器无法启动，已停止运行；请安装或修复 Chrome/Edge 后重试")
            return False
        try:
            context = await browser.new_context(
                viewport={"width": 400, "height": 811},
                user_agent=MOBILE_USER_AGENT,
                device_scale_factor=2,
                is_mobile=True,
                has_touch=True,
                locale="zh-CN",
            )
        except Exception as error:
            logger.error(f"浏览器上下文创建失败: {error}")
            await browser.close()
            await prompt_continue("浏览器初始化失败，已停止运行；请关闭占用的浏览器后重试")
            return False

        # 加载 Cookie，并在抢票前验证是否仍然有效。
        status = False
        if await load_cookies(context):
            status = await is_login(context)
            if status is None:
                status = await is_login(context)
        if status is None:
            logger.error("网络或服务异常，无法核对登录状态。恢复网络后可重新运行")
            return False
        if not status:
            logger.info("Cookie 不存在或已失效，请重新登录")
            await browser.close()
            if not await ensure_login(headless=False):
                logger.error("登录失败")
                return False
            browser = await launch_ticket_browser(p, launch_kwargs)
            if browser is None:
                await prompt_continue("重新登录后浏览器无法启动，已停止运行；请修复浏览器后重试")
                return False
            try:
                context = await browser.new_context(
                    viewport={"width": 400, "height": 811},
                    user_agent=MOBILE_USER_AGENT,
                    device_scale_factor=2,
                    is_mobile=True,
                    has_touch=True,
                    locale="zh-CN",
                )
            except Exception as error:
                logger.error(f"重新登录后浏览器初始化失败: {error}")
                await browser.close()
                await prompt_continue("浏览器初始化失败，已停止运行；请关闭占用的浏览器后重试")
                return False
            if not await load_cookies(context):
                logger.error("重新登录后仍无法加载 Cookie")
                await browser.close()
                return False

        page = await context.new_page()

        try:
            success = await run_purchase_flow(page, config, before_submit)
            if success:
                logger.info("抢票流程执行完毕")
                await take_screenshot(page, "success_final")
                if config.get("keep_browser_open", True) and not page.is_closed():
                    if config.get("test_mode", True):
                        await prompt_continue("测试流程已暂停，检查页面选项无误后", page)
                    else:
                        await prompt_continue("订单已进入支付/确认阶段，请手动完成支付或确认状态后", page)
            else:
                logger.error("抢票流程未能完成")
                await take_screenshot(page, "failed_final")
                if config.get("keep_browser_open_on_failure", True) and not page.is_closed():
                    await prompt_continue("自动流程失败，浏览器会保持打开；请检查页面后", page)
            return success
        except Exception as e:
            logger.exception(f"运行异常: {e}")
            await take_screenshot(page, "exception_final")
            if config.get("keep_browser_open_on_failure", True) and not page.is_closed():
                await prompt_continue("程序发生异常，浏览器会保持打开；请检查页面后", page)
            return False
        finally:
            if browser.is_connected():
                await save_cookies(context)
                await browser.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="B 站会员购购票程序")
    parser.add_argument("--clear-order-guard", metavar="活动编号", help="核对订单后解除本地防重复提交保护")
    args = parser.parse_args()
    try:
        sys.exit(0 if asyncio.run(main(args.clear_order_guard)) else 1)
    except KeyboardInterrupt:
        logger.info("用户中断")
        sys.exit(130)
