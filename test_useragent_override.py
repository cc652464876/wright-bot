# -*- coding: utf-8 -*-
"""
验证 navigator.userAgent 覆盖是否生效
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

async def test_useragent_override():
    from playwright.async_api import async_playwright
    from src.engine.anti_bot.fingerprint import FingerprintGenerator, FingerprintInjector

    # 生成指纹
    profile = FingerprintGenerator().generate()
    print(f"预期 User-Agent: {profile.user_agent}")
    print(f"预期 Platform: {profile.platform}")

    # 启动浏览器
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        )
        try:
            ctx = await browser.new_context(
                user_agent=profile.user_agent,
                viewport={"width": profile.screen_width, "height": profile.screen_height},
                locale=profile.languages[0] if profile.languages else "zh-CN",
                timezone_id=profile.timezone,
                extra_http_headers=profile.extra_headers,
                permissions=[],
            )
            page = await ctx.new_page()

            # 注入指纹覆盖脚本
            injector = FingerprintInjector()
            await injector.inject(page, profile)

            # 访问测试页面并检查 navigator.userAgent
            await page.goto("about:blank", timeout=15_000)

            actual_ua = await page.evaluate("navigator.userAgent")
            actual_platform = await page.evaluate("navigator.platform")

            print(f"\n实际 User-Agent: {actual_ua}")
            print(f"实际 Platform: {actual_platform}")

            # 验证
            if "HeadlessChrome" in actual_ua:
                print("\n[FAIL] 伪装失败！navigator.userAgent 仍包含 HeadlessChrome")
                return False
            elif actual_ua == profile.user_agent:
                print("\n[OK] 伪装成功！navigator.userAgent 已被正确覆盖")
                return True
            else:
                print(f"\n[WARN] User-Agent 不完全匹配，但不包含 HeadlessChrome")
                return True

        finally:
            await browser.close()
    finally:
        await pw.stop()

if __name__ == "__main__":
    success = asyncio.run(test_useragent_override())
    sys.exit(0 if success else 1)
