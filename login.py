import asyncio
import json
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from playwright.async_api import async_playwright

from browser_utils import get_browser_channel


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


COOKIE_FILE = get_base_dir() / "cookies.json"
LOGIN_URL = "https://passport.bilibili.com/h5-app/passport/login"
LOGIN_STATUS_URL = "https://api.bilibili.com/x/web-interface/nav"


async def is_login(context, timeout_ms: float = 5000) -> bool | None:
    """返回登录状态，None 表示网络或响应异常，无法确认。"""
    try:
        response = await context.request.get(LOGIN_STATUS_URL, timeout=timeout_ms)
        try:
            if not response.ok:
                return None
            payload = await response.json()
            status = (payload.get("data") or {}).get("isLogin")
            if isinstance(status, bool):
                return status
            if payload.get("code") == -101:
                return False
            return None
        finally:
            await response.dispose()
    except Exception:
        print("登录状态检查失败，请确认网络连接")
        return None


async def save_cookies(context, cookie_file=None):
    path = COOKIE_FILE if cookie_file is None else cookie_file
    cookies = await context.cookies()
    temp_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temp_path = Path(temporary.name)
            json.dump(cookies, temporary, ensure_ascii=False, indent=2)
        temp_path.replace(path)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
    print(f"Cookie 已保存到: {path}")


async def load_cookies(context, cookie_file=None):
    path = COOKIE_FILE if cookie_file is None else cookie_file
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cookies, list) or not cookies:
            raise ValueError("Cookie 文件必须包含非空列表")
        await context.add_cookies(cookies)
    except FileNotFoundError:
        return False
    except Exception:
        print("无法读取本地 Cookie，将重新检查登录状态")
        return False
    print(f"已加载 Cookie: {path}")
    return True


async def ensure_login(headless: bool = False):
    """确保已登录，未登录则打开扫码页让用户登录"""
    channel = get_browser_channel()
    print(f"使用浏览器: {channel or 'Playwright Chromium'}")

    async with async_playwright() as p:
        launch_kwargs = {"channel": channel} if channel else {}
        try:
            browser = await p.chromium.launch(headless=headless, **launch_kwargs)
        except Exception:
            print("浏览器启动失败，请检查 Chrome 或 Edge，或运行 python -m playwright install chromium")
            return False
        context = await browser.new_context(
            viewport={"width": 400, "height": 811},
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Pixel 5) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            locale="zh-CN",
        )

        if await load_cookies(context):
            status = await is_login(context)
            if status is None:
                status = await is_login(context)
            if status is None:
                print("暂时无法确认登录状态，原 Cookie 已保留，请恢复网络后重试")
                return False
            if status:
                print("当前已登录")
                await save_cookies(context)
                return True
        if headless:
            print("当前登录已失效，请用可见窗口重新扫码")
            return False

        print("未登录，正在打开 B站扫码登录页...")
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        print("请用手机 B站 App 扫描页面上的二维码登录（脚本会自动检测）")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 300
        while not page.is_closed():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            if await is_login(context, timeout_ms=min(5000, remaining * 1000)):
                print("登录成功！")
                await save_cookies(context)
                return True
            await asyncio.sleep(max(0, min(3, deadline - loop.time())))

        print("登录超时或扫码窗口已关闭，请重试")
        return False


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = asyncio.run(ensure_login(headless=False))
    except Exception as e:
        print(f"登录程序异常: {e}")
        result = False
    finally:
        print("\n按回车键退出...")
        try:
            input()
        except Exception:
            pass
    sys.exit(0 if result else 1)
