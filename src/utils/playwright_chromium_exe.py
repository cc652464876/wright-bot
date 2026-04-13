"""
在 PLAYWRIGHT_BROWSERS_PATH / REBROWSER_BROWSERS_PATH 根目录下解析 Chromium 可执行文件。

Playwright 1.40+ 使用 chromium-<rev>/chrome-win64/chrome.exe（及 linux/mac 对应路径）；
旧版曾为 chrome-<rev>/chrome.exe。本模块按平台依次尝试新旧布局并取字典序最后一条（通常最高版本）。
"""

from __future__ import annotations

import glob
import os
import sys
from typing import Optional


def find_chromium_executable_in_browsers_path(pw_root: str) -> Optional[str]:
    if not pw_root.strip() or not os.path.isdir(pw_root):
        return None
    resolved = os.path.abspath(pw_root)
    patterns: list[str]
    if sys.platform == "win32":
        patterns = [
            os.path.join(resolved, "chromium-*", "chrome-win64", "chrome.exe"),
            os.path.join(resolved, "chrome-*", "chrome.exe"),
        ]
    elif sys.platform == "darwin":
        patterns = [
            os.path.join(
                resolved,
                "chromium-*",
                "chrome-mac",
                "Chromium.app",
                "Contents",
                "MacOS",
                "Chromium",
            ),
            os.path.join(
                resolved,
                "chrome-*",
                "chrome-mac",
                "Chromium.app",
                "Contents",
                "MacOS",
                "Chromium",
            ),
        ]
    else:
        patterns = [
            os.path.join(resolved, "chromium-*", "chrome-linux", "chrome"),
            os.path.join(resolved, "chrome-*", "chrome-linux", "chrome"),
        ]

    matches: list[str] = []
    for pat in patterns:
        matches.extend(glob.glob(pat))
    if not matches:
        return None
    return os.path.abspath(sorted(matches)[-1])
