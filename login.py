import asyncio
import json
import shutil
import sys
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="ignore")


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


COOKIE_FILE = get_base_dir() / "cookies.json"


def find_system_browser():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        Path.home() / r"AppData\Local\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        p = Path(path)
        if p.exists():
            return str(p.resolve())
    found = shutil.which("chrome.exe") or shutil.which("msedge.exe")
    return found


def get_browser_channel():
    path = find_system_browser()
    if not path:
        return None
    lower = path.lower()
    if "chrome" in lower:
        return "chrome"
    if "edge" in lower or "msedge" in lower:
        return "msedge"
    return None
LOGIN_URL = "https://passport.bilibili.com/h5-app/passport/login"


async def is_login(page) -> bool:
    """检查是否已登录"""
    try:
        resp = await page.evaluate("""async () => {
            const res = await fetch('https://api.bilibili.com/x/web-interface/nav', {
                credentials: 'include'
            });
            return await res.json();
        }""")
        return resp.get("data", {}).get("isLogin", False)
    except Exception as e:
        print(f"登录检查出错: {e}")
        return False


async def save_cookies(context):
    cookies = await context.cookies()
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Cookie 已保存到: {COOKIE_FILE}")


async def load_cookies(context):
    if not COOKIE_FILE.exists():
        return False
    cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    await context.add_cookies(cookies)
    print(f"已加载 Cookie: {COOKIE_FILE}")
    return True


async def ensure_login(headless: bool = False):
    """确保已登录，未登录则打开扫码页让用户登录"""
    channel = get_browser_channel()
    if not channel:
        print("错误：未找到系统 Chrome 或 Edge 浏览器，请安装后再试")
        return False
    print(f"使用浏览器: {channel}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, channel=channel, slow_mo=50)
        context = await browser.new_context(
            viewport={"width": 400, "height": 811},
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Pixel 5) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            locale="zh-CN",
        )

        # 先尝试加载已有 Cookie
        await load_cookies(context)
        page = await context.new_page()

        # 检查登录状态
        print("正在检查登录状态...")
        await page.goto("https://m.bilibili.com", wait_until="networkidle", timeout=30000)
        if await is_login(page):
            print("当前已登录")
            await save_cookies(context)
            await browser.close()
            return True

        # 未登录，打开扫码页
        print("未登录，正在打开 B站扫码登录页...")
        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        print("请用手机 B站 App 扫描页面上的二维码登录（脚本会自动检测）")

        # 轮询检测登录状态，最多 5 分钟
        max_wait = 300
        for i in range(max_wait):
            await asyncio.sleep(3)
            # 刷新当前页保持登录页活跃
            if i % 10 == 0:
                await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
            logged_in = await is_login(page)
            print(f"[{i*3}s] 登录状态: {'已登录' if logged_in else '未登录'}")
            if logged_in:
                print("登录成功！")
                await save_cookies(context)
                await browser.close()
                return True

        print("登录超时，请重试")
        await browser.close()
        return False


if __name__ == "__main__":
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
