import asyncio
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger
from playwright.async_api import ElementHandle, Page, async_playwright

from browser_utils import get_browser_channel, get_browser_name
from login import ensure_login, is_login

# Windows 控制台 GBK 编码兼容
sys.stdout.reconfigure(encoding="utf-8", errors="ignore")

def get_base_dir() -> Path:
    """获取程序所在目录（兼容源码和 PyInstaller 打包）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


BASE_DIR = get_base_dir()
CONFIG_FILE = BASE_DIR / "config.yaml"
COOKIE_FILE = BASE_DIR / "cookies.json"

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


def setup_logger():
    logger.remove()
    # 控制台输出
    logger.add(sys.stdout, level="INFO", colorize=True)
    # 文件日志
    logger.add(BASE_DIR / "ticket_bot.log", level="DEBUG", encoding="utf-8", rotation="10 MB")


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)


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

    if not config.get("ticket_url"):
        config["ticket_url"] = prompt_input("请输入票务页面链接")
        changed = True

    if not config.get("sale_time"):
        config["sale_time"] = prompt_input("请输入开售时间", "2026-06-20 12:00:00")
        changed = True

    if not config.get("target_date"):
        config["target_date"] = prompt_input("请输入目标日期", "2026-07-10")
        changed = True

    try:
        config["ticket_count"] = int(config.get("ticket_count", 1))
    except (TypeError, ValueError):
        config["ticket_count"] = 1
        changed = True
    if not 1 <= config["ticket_count"] <= 4:
        logger.warning("购票数量必须在 1-4 之间，已改为 1")
        config["ticket_count"] = 1
        changed = True

    try:
        config["max_retry"] = max(1, int(config.get("max_retry", 30)))
    except (TypeError, ValueError):
        config["max_retry"] = 30
        changed = True

    if changed:
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
        "验证", "人机验证", "安全验证", "请完成", "点击验证", "拖动滑块",
        "captcha", "geetest", "verify", "slider", "slide", "confirm",
    ]
    captcha_selectors = [
        ".geetest", "#captcha", ".captcha", "[class*='captcha']",
        "[class*='geetest']", "[id*='captcha']", "[id*='verify']",
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


async def handle_captcha(page: Page):
    """遇到验证码时暂停，等待用户手动处理"""
    logger.warning("=" * 40)
    logger.warning("检测到人机验证/验证码，已暂停自动操作")
    logger.warning("请在浏览器中手动完成验证，完成后按回车继续...")
    logger.warning("=" * 40)
    try:
        input("[验证码等待] 请手动完成验证后按回车继续...")
    except EOFError:
        logger.warning("无法读取回车，等待 10 秒后自动继续")
        await asyncio.sleep(10)
    logger.info("继续自动抢票流程...")


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


def prompt_continue(message: str) -> bool:
    """暂停并提示用户手动操作，完成后按回车继续"""
    logger.warning("=" * 40)
    logger.warning(message)
    logger.warning("完成后请按回车键继续...")
    logger.warning("=" * 40)
    try:
        input("[手动兜底] " + message + " 完成后按回车...")
        return True
    except EOFError:
        logger.warning("无法读取输入，5 秒后自动继续")
        time.sleep(5)
        return True


async def load_cookies(context):
    if not COOKIE_FILE.exists():
        return False
    try:
        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        await context.add_cookies(cookies)
        logger.info("已加载本地 Cookie")
        return True
    except Exception as e:
        logger.warning(f"加载 Cookie 失败: {e}")
        return False


async def save_cookies(context):
    try:
        cookies = await context.cookies()
        COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存 Cookie 失败: {e}")


# 与 B站服务器时间同步的偏移量（秒）
_server_time_offset = 0.0


def _fetch_bilibili_server_time() -> Optional[float]:
    """通过 HTTP 请求获取 B站服务器时间戳（秒）"""
    import urllib.request
    urls = [
        "https://www.bilibili.com",
        "https://api.bilibili.com/x/web-interface/zone",
        "https://show.bilibili.com",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={
                "User-Agent": MOBILE_USER_AGENT,
                "Accept": "*/*",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                date_header = resp.headers.get("Date")
                if date_header:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_header)
                    return dt.timestamp()
        except Exception as e:
            logger.debug(f"从 {url} 获取服务器时间失败: {e}")
            continue
    return None


async def sync_server_time(page: Page) -> float:
    """同步 B站服务器时间，返回本地时间与服务器时间的偏移量（秒）"""
    global _server_time_offset
    try:
        server_timestamp = await asyncio.get_event_loop().run_in_executor(None, _fetch_bilibili_server_time)
        if server_timestamp is None:
            logger.warning("同步服务器时间失败: 无法从 B站获取时间")
            return _server_time_offset

        local_timestamp = time.time()
        _server_time_offset = server_timestamp - local_timestamp
        logger.info(f"服务器时间同步完成，本地时间偏移: {_server_time_offset:.3f} 秒")
    except Exception as e:
        logger.warning(f"同步服务器时间异常: {e}")
    return _server_time_offset


def get_synced_now() -> datetime:
    """获取同步后的当前时间"""
    return datetime.fromtimestamp(time.time() + _server_time_offset)


async def wait_for_element_by_text(
    page: Page,
    text: str,
    selector: str = "*",
    timeout: float = 10,
    exact: bool = False,
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
        checked = await el.get_attribute("checked")
        return checked is not None or aria_checked == "true" or any(
            token in cls for token in ("selected", "active", "checked")
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


async def wait_for_sale_start(target_dt: datetime, before_seconds: int = 60, sale_time_offset: float = 0.0):
    """等待开售（使用同步后的服务器时间）"""
    # 应用用户配置的时间偏移
    effective_target_dt = target_dt.timestamp() + sale_time_offset

    now = get_synced_now()
    start_wait = effective_target_dt - before_seconds
    if now.timestamp() < start_wait:
        sleep_sec = start_wait - now.timestamp()
        logger.info(f"离开售还有 {sleep_sec:.1f} 秒（已同步服务器时间），提前 {before_seconds} 秒再启动高频刷新")
        await asyncio.sleep(sleep_sec)

    logger.info("进入高频刷新阶段")
    last_log = -1
    while True:
        now = get_synced_now()
        diff = effective_target_dt - now.timestamp()
        if diff <= 0:
            logger.info("已到达开售时间！")
            return
        sec_int = int(diff)
        if sec_int != last_log and sec_int % 5 == 0:
            logger.info(f"倒计时: {diff:.1f} 秒")
            last_log = sec_int
        await asyncio.sleep(0.05)


async def wait_for_modal(page: Page, timeout: float = 3.0) -> bool:
    """等待购票弹窗出现"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        modal = await page.query_selector(
            ".ticket-modal-container, .selection-modal.ticket-modal"
        )
        if modal and await modal.is_visible():
            return True
        await asyncio.sleep(0.1)
    return False


async def wait_for_bottom_button_ready(page: Page, timeout: float = 30.0) -> bool:
    """等待底部按钮从'即将开售'变为'立即预订'"""
    logger.info("等待底部按钮变为可点击状态...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await detect_captcha(page):
                await handle_captcha(page)

            # 先找明确的'立即预订'按钮
            selectors = [
                ".pre-sale-button",
                ".footer-container .bili-button",
                ".detail-floating-btn .bili-button",
                ".bottom-bar .bili-button",
                ".footer-menu-wrapper .bili-button",
                "footer .bili-button",
                ".fixed-bottom .bili-button",
                ".submit-bar .bili-button",
            ]
            for sel in selectors:
                btns = await page.query_selector_all(sel)
                for b in btns:
                    text = (await b.inner_text()).strip()
                    if "立即预订" in text or "立即购买" in text or "立即购票" in text:
                        logger.info(f"底部按钮已就绪: {text}")
                        return True
                    if "即将开售" in text:
                        logger.debug(f"仍需等待: {text}")
        except Exception as e:
            logger.debug(f"等待按钮过程出错: {e}")
        await asyncio.sleep(0.1)
    logger.warning("等待底部按钮就绪超时")
    return False


async def click_bottom_button(page: Page) -> bool:
    """点击底部购票按钮（立即预订/立即购买）"""
    logger.info("点击底部购票按钮...")
    for _ in range(20):
        try:
            if await detect_captcha(page):
                await handle_captcha(page)

            selectors = [
                ".pre-sale-button",
                ".footer-container .bili-button",
                ".detail-floating-btn .bili-button",
                ".bottom-bar .bili-button",
                ".footer-menu-wrapper .bili-button",
                "footer .bili-button",
                ".fixed-bottom .bili-button",
                ".submit-bar .bili-button",
            ]
            for sel in selectors:
                btns = await page.query_selector_all(sel)
                for b in btns:
                    text = (await b.inner_text()).strip()
                    if "立即预订" in text or "立即购买" in text or "立即购票" in text:
                        await human_like_delay(page, 0.1)
                        await b.click()
                        logger.info(f"已点击底部按钮: {text}")
                        await take_screenshot(page, "clicked_bottom_btn")
                        return True
        except Exception as e:
            logger.debug(f"点击底部按钮出错: {e}")
        await asyncio.sleep(random_delay(0.1, 0.03))
    return False


async def open_ticket_modal(page: Page) -> bool:
    """打开购票弹窗：等待按钮就绪后点击"""
    await wait_for_bottom_button_ready(page, timeout=30)
    return await click_bottom_button(page)


async def select_date(page: Page, target_date: str, timeout: float = 5.0) -> bool:
    """选择场次日期"""
    logger.info(f"选择日期: {target_date}")
    modal = await page.query_selector(".ticket-modal-container")
    scope = modal if modal else page

    deadline = time.time() + timeout
    while time.time() < deadline:
        if await detect_captcha(page):
            await handle_captcha(page)

        # 多种日期选择器
        selectors = [
            ".bili-radio-button",
            ".date-item",
            ".session-item",
            ".day-item",
            "[class*='date']",
            "[class*='session']",
            "[class*='day-']",
        ]
        for sel in selectors:
            labels = await scope.query_selector_all(sel)
            for label in labels:
                try:
                    if not await label.is_visible():
                        continue
                    text = (await label.inner_text()).strip()
                    if not text:
                        continue
                    matched = (
                        target_date in text
                        or text in target_date
                        or target_date.replace("-", ".") in text
                        or target_date.replace("2026-", "") in text
                        or target_date.replace("-", "/") in text
                    )
                    if not matched:
                        continue
                    if await element_is_disabled(label):
                        continue
                    await human_like_delay(page, 0.15)
                    await label.click()
                    logger.info(f"已选择日期: {text}")
                    await take_screenshot(page, "selected_date")
                    return True
                except Exception:
                    continue
        await asyncio.sleep(random_delay(0.1, 0.03))

    logger.warning("未找到可用日期")
    await take_screenshot(page, "fail_select_date")
    return False


async def select_ticket(page: Page, target_ticket: str, timeout: float = 5.0) -> bool:
    """选择票档"""
    logger.info(f"选择票档: {target_ticket or '第一个可用票档'}")
    modal = await page.query_selector(".ticket-modal-container") or page

    deadline = time.time() + timeout
    while time.time() < deadline:
        if await detect_captcha(page):
            await handle_captcha(page)

        # 常见票档元素选择器
        selectors = [
            ".ticket-selection-wrapper .bili-radio-button",
            ".price-container .bili-radio-button", ".sku-container .bili-radio-button",
            ".sku-item", ".ticket-item", ".ticket-sku", ".price-item",
            ".ticket-class-item", ".ticket-type-item", ".sku-card",
            "[class*='sku-item']", "[class*='price-item']",
            "[class*='ticket-class-item']", "[class*='ticket-type-item']",
        ]
        matches = []
        for sel in selectors:
            candidates = await modal.query_selector_all(sel)
            for item in candidates:
                try:
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text()).strip()
                    if not text or (target_ticket and target_ticket not in text):
                        continue
                    lower_text = text.lower()
                    if (
                        "售罄" in lower_text or "已售" in lower_text or "sold" in lower_text
                        or await element_is_disabled(item)
                    ):
                        continue
                    matches.append((len(text), item, text))
                except Exception:
                    continue
        if matches:
            _, item, text = min(matches, key=lambda value: value[0])
            if await element_is_selected(item):
                logger.info(f"票档已选中: {text}")
                return True
            try:
                await human_like_delay(page, 0.15)
                await item.click()
                await asyncio.sleep(0.15)
                logger.info(f"已选择票档: {text}")
                await take_screenshot(page, "selected_ticket")
                return True
            except Exception as e:
                logger.debug(f"点击票档失败: {e}")
        await asyncio.sleep(random_delay(0.1, 0.03))

    logger.warning("未找到匹配票档")
    await take_screenshot(page, "fail_select_ticket")
    return False


async def set_ticket_count(page: Page, ticket_count: int, timeout: float = 3.0) -> bool:
    """把弹窗中的购票数量调整到配置值；1 张通常是页面默认值。"""
    if ticket_count == 1:
        return True
    modal = await page.query_selector(".ticket-modal-container") or page
    deadline = time.time() + timeout
    while time.time() < deadline:
        plus = await wait_for_element_by_text(
            modal, "+", selector="button, [role='button'], [class*='plus'], [class*='add']",
            timeout=0.2, exact=True,
        )
        if plus and not await element_is_disabled(plus):
            try:
                for _ in range(ticket_count - 1):
                    await plus.click()
                    await asyncio.sleep(0.1)
                logger.info(f"已设置购票数量: {ticket_count}")
                return True
            except Exception as e:
                logger.debug(f"调整购票数量失败: {e}")
        await asyncio.sleep(0.1)
    logger.warning(f"未能把购票数量调整为 {ticket_count}")
    return False


async def click_modal_submit(page: Page, timeout: float = 5.0) -> bool:
    """点击弹窗底部的'立即预订'按钮"""
    logger.info("尝试点击弹窗底部立即预订...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await detect_captcha(page):
            await handle_captcha(page)

        # 优先按文字匹配弹窗内的提交按钮
        for text in ["立即预订", "立即购买", "立即购票"]:
            modal = await page.query_selector(".ticket-modal-container") or page
            el = await wait_for_element_by_text(
                modal, text,
                selector=".submit-button, .bottom-bar .bili-button, button, [role='button']",
                timeout=0.3, exact=True,
            )
            if el:
                try:
                    if await element_is_disabled(el):
                        continue
                    await human_like_delay(page, 0.1)
                    await el.click()
                    logger.info(f"已点击弹窗按钮: {text}")
                    await take_screenshot(page, "clicked_modal_submit")
                    return True
                except Exception:
                    continue

        await asyncio.sleep(random_delay(0.1, 0.03))

    logger.warning("未找到弹窗提交按钮")
    await take_screenshot(page, "fail_click_modal_submit")
    return False


async def select_payment_method(page: Page, payment_method: str) -> bool:
    """选择支付方式；找不到目标选项时不冒充成功。"""
    names = ["微信支付", "微信"] if payment_method == "wechat" else ["支付宝支付", "支付宝"]
    for name in names:
        option = await wait_for_element_by_text(
            page, name,
            selector="label, [role='radio'], [class*='payment'], [class*='pay-type'], [class*='payType']",
            timeout=0.5,
        )
        if not option:
            continue
        if await element_is_selected(option):
            return True
        try:
            await option.click()
            await asyncio.sleep(0.15)
            logger.info(f"已选择支付方式: {name}")
            return True
        except Exception as e:
            logger.debug(f"选择支付方式失败: {e}")
    logger.warning(f"未找到配置的支付方式: {payment_method}")
    return False


async def order_submission_succeeded(page: Page, previous_url: str) -> bool:
    """只根据明确的支付页/成功信号判断提交成功。"""
    url = page.url.lower()
    success_url_tokens = ("cashier", "payment", "pay.html", "/pay/", "order/success")
    if url != previous_url.lower() and any(token in url for token in success_url_tokens):
        return True
    for text in ("支付剩余时间", "订单创建成功", "收银台", "支付二维码", "立即支付", "去支付"):
        if await wait_for_element_by_text(page, text, selector="body, body *", timeout=0.15):
            return True
    return False


async def submit_order_confirmation(page: Page, payment_method: str = "wechat", max_retry: int = 10) -> bool:
    """在确认订单页面提交订单；仅在出现明确成功信号后返回 True。"""
    logger.info("进入确认订单页面，尝试提交订单...")
    await select_payment_method(page, payment_method)

    action_selector = "button, [role='button'], .bili-button, .submit-button"
    for attempt in range(1, max_retry + 1):
        logger.info(f"第 {attempt} 次提交订单...")

        retry_btn = await wait_for_element_by_text(
            page, "再试一次", selector=action_selector, timeout=0.4, exact=True
        )
        if retry_btn and not await element_is_disabled(retry_btn):
            logger.warning("检测到拥挤提示，点击'再试一次'...")
            try:
                await retry_btn.click()
                await take_screenshot(page, "clicked_retry")
                await asyncio.sleep(random_delay(0.25, 0.08))
            except Exception as e:
                logger.debug(f"点击重试失败: {e}")

        if await order_submission_succeeded(page, page.url):
            return True

        submit_btn = await wait_for_element_by_text(
            page, "提交订单", selector=action_selector, timeout=0.8, exact=True
        )
        if not submit_btn:
            logger.warning("未找到可点击的'提交订单'按钮")
            await asyncio.sleep(random_delay(0.25, 0.05))
            continue
        if await element_is_disabled(submit_btn):
            logger.warning("'提交订单'按钮仍不可用，请检查观演人、协议和支付方式")
            await asyncio.sleep(random_delay(0.25, 0.05))
            continue

        previous_url = page.url
        try:
            await human_like_delay(page, 0.1)
            await submit_btn.click()
            logger.info("已点击提交订单")
            await take_screenshot(page, "clicked_submit_order")
        except Exception as e:
            logger.debug(f"点击提交订单失败: {e}")
            continue

        deadline = time.time() + 2.5
        while time.time() < deadline:
            if await order_submission_succeeded(page, previous_url):
                logger.info("已确认进入支付流程")
                return True
            retry_btn = await wait_for_element_by_text(
                page, "再试一次", selector=action_selector, timeout=0.15, exact=True
            )
            if retry_btn:
                break
            await asyncio.sleep(0.1)

    logger.warning("提交订单重试次数用尽，未检测到成功信号")
    await take_screenshot(page, "submit_order_failed")
    return False


async def select_viewer(page: Page, viewer_name: str, timeout: float = 10.0) -> bool:
    """选择观演人"""
    if not viewer_name:
        logger.info("未配置观演人，跳过")
        return True

    logger.info(f"选择观演人: {viewer_name}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await detect_captcha(page):
            await handle_captcha(page)

        # 先匹配姓名，再选择最短文本元素，避免点击整张订单卡片。
        elements = await page.query_selector_all(
            "label, [role='checkbox'], [class*='viewer'], [class*='audience'], "
            "[class*='buyer'], [class*='attendee'], span, p"
        )
        matches = []
        for el in elements:
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip()
                if viewer_name in text:
                    matches.append((len(text), el))
            except Exception:
                continue
        if matches:
            _, name_el = min(matches, key=lambda value: value[0])
            try:
                handle = await name_el.evaluate_handle(
                    "e => e.closest(\"label, [role='checkbox'], [class*='viewer'], "
                    "[class*='audience'], [class*='buyer'], [class*='attendee']\") || e.parentElement"
                )
                clickable = handle.as_element() if handle else name_el
                if await element_is_selected(clickable):
                    logger.info(f"观演人已选中: {viewer_name}")
                    return True
                await human_like_delay(page, 0.2)
                await clickable.click()
                logger.info(f"已选择观演人: {viewer_name}")
                await take_screenshot(page, "selected_viewer")
                return True
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
        if await detect_captcha(page):
            await handle_captcha(page)

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
    prompt_continue("未找到支付按钮，请手动点击去支付后")
    return True


async def run_purchase_flow(page: Page, config: dict):
    """抢票主流程（按视频观察到的流程）"""
    target_date = config["target_date"]
    target_ticket = config.get("target_ticket", "")
    payment_method = config.get("payment_method", "wechat")
    test_mode = config.get("test_mode", True)
    action_delay = config.get("action_delay", 0.5)

    # 1. 打开页面
    logger.info("打开购票页面...")
    for retry in range(3):
        try:
            await page.goto(config["ticket_url"], wait_until="load", timeout=120000)
        except Exception:
            pass
        await asyncio.sleep(5)
        await take_screenshot(page, "page_loaded")
        try:
            btn = await page.query_selector(".pre-sale-button, .footer-container .bili-button, .footer-menu-wrapper .bili-button")
            if btn:
                break
        except Exception:
            pass
        logger.warning(f"页面加载未完成，第 {retry + 1} 次重试")
        await asyncio.sleep(2)
    else:
        logger.error("页面加载失败")
        await take_screenshot(page, "page_load_failed")
        return False

    if await detect_captcha(page):
        await handle_captcha(page)

    # 同步 B站服务器时间
    await sync_server_time(page)

    # 预探测
    try:
        btn = await page.query_selector(".pre-sale-button, .footer-container .bili-button, .footer-menu-wrapper .bili-button")
        if btn:
            btn_text = (await btn.inner_text()).strip()
            logger.info(f"当前底部按钮文字: {btn_text}")
        date_labels = await page.query_selector_all(".bili-radio-button")
        logger.info(f"当前页面日期选项数量: {len(date_labels)}")
    except Exception:
        pass
    await take_screenshot(page, "presale_probe")

    # 2. 等待开售
    sale_time = datetime.strptime(config["sale_time"], "%Y-%m-%d %H:%M:%S")
    sale_time_offset = config.get("sale_time_offset", 0.0)
    await wait_for_sale_start(sale_time, config.get("countdown_seconds_before", 60), sale_time_offset)

    # 开售瞬间：强制刷新页面
    logger.info("开售时间到，强制刷新页面...")
    try:
        await page.reload(wait_until="load", timeout=60000)
        await asyncio.sleep(1)
        await sync_server_time(page)
        await take_screenshot(page, "after_reload")
    except Exception as e:
        logger.warning(f"刷新页面失败: {e}")

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
        prompt_continue("购票弹窗未出现，请手动点击底部按钮")
        if not await wait_for_modal(page, timeout=5):
            return False

    # 5. 选择日期
    if not await select_date(page, target_date, timeout=5):
        logger.warning("选择日期失败")
        prompt_continue("未找到目标日期，请手动选择后")

    await human_like_delay(page, action_delay)

    # 6. 选择票档
    if not await select_ticket(page, target_ticket, timeout=5):
        logger.warning("选择票档失败")
        prompt_continue("未找到目标票档，请手动选择后")

    await human_like_delay(page, action_delay)

    if not await set_ticket_count(page, config.get("ticket_count", 1)):
        prompt_continue(f"请手动把购票数量调整为 {config.get('ticket_count', 1)}")

    # 7. 点击弹窗底部'立即预订'
    if test_mode:
        logger.info("测试模式：已选完日期票档，停止后续操作")
        return True

    if not await click_modal_submit(page, timeout=5):
        logger.error("无法点击弹窗立即预订")
        return False

    # 8. 处理可能出现的验证码
    await asyncio.sleep(1)
    if await detect_captcha(page):
        await handle_captcha(page)

    # 9. 进入确认订单页面，选择观演人并提交订单（带重试）
    await asyncio.sleep(1.5)
    await take_screenshot(page, "order_confirmation")
    viewer_name = config.get("viewer_name", "").strip()
    if viewer_name and not await select_viewer(page, viewer_name, timeout=5):
        prompt_continue(f"未能自动选择观演人 {viewer_name}，请手动选择")
    success = await submit_order_confirmation(
        page, payment_method, max_retry=config.get("max_retry", 30)
    )

    if success:
        logger.info("订单提交流程执行完毕，请在页面上确认最终支付")
        await take_screenshot(page, "success_final")
        return True
    else:
        logger.error("订单提交失败")
        await take_screenshot(page, "failed_final")
        return False


async def main():
    setup_logger()
    config = load_config()
    config = ensure_config(config)
    logger.info("=" * 40)
    logger.info("B站会员购抢票脚本启动")
    logger.info(f"目标: {config['ticket_url']}")
    logger.info(f"开售时间: {config['sale_time']}")
    logger.info(f"目标日期: {config['target_date']}")
    logger.info(f"目标票档: {config.get('target_ticket', '未配置')}")
    logger.info(f"购票数量: {config.get('ticket_count', 1)}")
    logger.info(f"观演人: {config.get('viewer_name', '未配置')}")
    logger.info(f"测试模式: {config.get('test_mode', True)}")

    async with async_playwright() as p:
        channel = get_browser_channel()
        logger.info(f"使用浏览器: {get_browser_name()}")

        launch_kwargs = {"headless": config.get("headless", False), "slow_mo": 50}
        if channel:
            launch_kwargs["channel"] = channel

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 400, "height": 811},
            user_agent=MOBILE_USER_AGENT,
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            locale="zh-CN",
        )

        # 加载 Cookie，并在抢票前验证是否仍然有效。
        cookies_valid = False
        if await load_cookies(context):
            check_page = await context.new_page()
            try:
                await check_page.goto("https://m.bilibili.com", wait_until="domcontentloaded", timeout=30000)
                cookies_valid = await is_login(check_page)
            except Exception as e:
                logger.warning(f"登录状态验证失败: {e}")
            finally:
                await check_page.close()

        if not cookies_valid:
            logger.info("Cookie 不存在或已失效，请重新登录")
            await browser.close()
            if not await ensure_login(headless=False):
                logger.error("登录失败")
                return
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                viewport={"width": 400, "height": 811},
                user_agent=MOBILE_USER_AGENT,
                device_scale_factor=2,
                is_mobile=True,
                has_touch=True,
                locale="zh-CN",
            )
            if not await load_cookies(context):
                logger.error("重新登录后仍无法加载 Cookie")
                await browser.close()
                return

        page = await context.new_page()

        try:
            success = await run_purchase_flow(page, config)
            if success:
                logger.info("抢票流程执行完毕")
                await take_screenshot(page, "success_final")
            else:
                logger.error("抢票流程未能完成")
                await take_screenshot(page, "failed_final")
        except Exception as e:
            logger.exception(f"运行异常: {e}")
            await take_screenshot(page, "exception_final")
        finally:
            await save_cookies(context)
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户中断")
