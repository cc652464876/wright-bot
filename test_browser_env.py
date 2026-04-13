# -*- coding: utf-8 -*-
"""
最小化验证：Rebrowser 补丁 Chromium + Camoufox 无头访问网页、打印标题并截图。
运行前请在项目根目录执行: python test_browser_env.py
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_URL = "https://httpbin.org/headers"
SCREENSHOT_CHROMIUM = ROOT / "chromium_test.png"
SCREENSHOT_CAMOUFOX = ROOT / "camoufox_test.png"


def _init_project_browser_env() -> None:
    from src.config.settings import _init_browser_env

    cores = ROOT / "browser_cores"
    _init_browser_env(str(cores.resolve()), force=True)


async def test_chromium_rebrowser() -> None:
    print("\n=== Chromium (Rebrowser 补丁) ===", flush=True)
    try:
        import rebrowser_patches

        rebrowser_patches.patch()
        print("[chromium] rebrowser_patches.patch() 已执行", flush=True)
    except ImportError as e:
        print(f"[chromium] 警告: 未安装 rebrowser 补丁包 ({e})，将使用标准 Playwright Chromium", flush=True)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=90_000)
            title = await page.title()
            print(f"[chromium] URL: {TEST_URL}", flush=True)
            print(f"[chromium] title: {title!r}", flush=True)
            await page.screenshot(path=str(SCREENSHOT_CHROMIUM), full_page=False)
            print(f"[chromium] 截图已保存: {SCREENSHOT_CHROMIUM}", flush=True)
        finally:
            await browser.close()


async def test_camoufox() -> None:
    print("\n=== Camoufox (Firefox) ===", flush=True)
    # AsyncCamoufox 内部会先启动 Playwright，再调用 AsyncNewBrowser(playwright, ...)
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=True) as browser:
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = await browser.new_context()
        page = await context.new_page()
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=90_000)
        title = await page.title()
        print(f"[camoufox] URL: {TEST_URL}", flush=True)
        print(f"[camoufox] title: {title!r}", flush=True)
        await page.screenshot(path=str(SCREENSHOT_CAMOUFOX), full_page=False)
        print(f"[camoufox] 截图已保存: {SCREENSHOT_CAMOUFOX}", flush=True)


async def main() -> None:
    print(f"项目根目录: {ROOT}", flush=True)
    print(f"测试 URL: {TEST_URL}", flush=True)
    _init_project_browser_env()
    print("[env] _init_browser_env(browser_cores) 已完成", flush=True)

    chromium_ok = False
    camoufox_ok = False

    try:
        await test_chromium_rebrowser()
        chromium_ok = True
    except Exception:
        print("[chromium] 测试失败:", flush=True)
        traceback.print_exc()

    try:
        await test_camoufox()
        camoufox_ok = True
    except Exception:
        print("[camoufox] 测试失败:", flush=True)
        traceback.print_exc()

    print("\n=== 汇总 ===", flush=True)
    print(f"Chromium 测试: {'OK' if chromium_ok else 'FAIL'}", flush=True)
    print(f"Camoufox 测试: {'OK' if camoufox_ok else 'FAIL'}", flush=True)

    if not chromium_ok or not camoufox_ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
