import shutil
import subprocess
from pathlib import Path
from typing import Optional


def find_system_browser() -> Optional[str]:
    """查找系统中已安装的 Chrome 或 Edge 可执行文件路径"""
    candidates = [
        # Chrome
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
        # Edge
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        Path.home() / r"AppData\Local\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        p = Path(path)
        if p.exists():
            return str(p.resolve())

    # 尝试 where 命令
    for name in ["chrome.exe", "msedge.exe"]:
        found = shutil.which(name)
        if found:
            return found

    return None


def get_browser_channel() -> Optional[str]:
    """返回 Playwright 可用的 channel 名称：chrome 或 msedge"""
    path = find_system_browser()
    if not path:
        return None
    lower = path.lower()
    if "chrome" in lower:
        return "chrome"
    if "edge" in lower or "msedge" in lower:
        return "msedge"
    return None


def get_browser_name() -> str:
    path = find_system_browser()
    if not path:
        return "Playwright Chromium"
    lower = path.lower()
    if "chrome" in lower:
        return "Google Chrome"
    if "edge" in lower or "msedge" in lower:
        return "Microsoft Edge"
    return "未知浏览器"
