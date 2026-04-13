# -*- coding: utf-8 -*-
"""
端到端冒烟：反检测管线（Crawlee 工厂、指纹、遗留 site_engines、Rebrowser 告警、可选真实浏览器）。

用法（项目根目录）:
    python scripts/smoke_antidetect.py
    set SMOKE_SKIP_BROWSER=1 && python scripts/smoke_antidetect.py   # 跳过 Playwright 真机启动
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"[OK]   {msg}")


def smoke_crawlee_factory_fingerprint_on() -> None:
    from src.config.settings import PrismSettings, StealthConfig, TaskInfoConfig
    from src.engine.crawlee_engine import CrawleeEngineFactory

    with tempfile.TemporaryDirectory() as td:
        settings = PrismSettings(
            task_info=TaskInfoConfig(save_directory=td),
            stealth=StealthConfig(use_fingerprint=True, window_mode="headless"),
        )
        factory = CrawleeEngineFactory(settings=settings)
        crawler = factory.create()

    if type(crawler).__name__ != "PlaywrightCrawler":
        _fail(f"期望 PlaywrightCrawler，得到 {type(crawler)!r}")

    hooks = getattr(crawler, "_pre_navigation_hooks", [])
    if len(hooks) != 1:
        _fail(f"use_fingerprint=True 时应注册 1 个 pre_navigation_hook，实际 {len(hooks)}")

    plugin = crawler._browser_pool._plugins[0]
    opts = getattr(plugin, "browser_new_context_options", None) or {}
    if "extra_http_headers" not in opts:
        _fail("browser_new_context_options 缺少 extra_http_headers")
    hdrs = opts["extra_http_headers"]
    if not isinstance(hdrs, dict) or not hdrs:
        _fail("extra_http_headers 应为非空 dict")
    for key in ("Accept", "Sec-CH-UA", "Accept-Language"):
        if key not in hdrs:
            _fail(f"extra_http_headers 缺少关键头: {key}")

    if not opts.get("user_agent"):
        _fail("browser_new_context_options 缺少 user_agent")
    if not opts.get("viewport"):
        _fail("browser_new_context_options 缺少 viewport")

    if getattr(plugin, "_fingerprint_generator", object()) is not None:
        _fail("use_fingerprint=True 时 _fingerprint_generator 应为 None（避免覆盖自研 context 参数）")

    _ok("CrawleeEngineFactory + use_fingerprint=True（hooks、extra_http_headers、UA、viewport）")


def smoke_crawlee_factory_fingerprint_off() -> None:
    from src.config.settings import PrismSettings, StealthConfig, TaskInfoConfig
    from src.engine.crawlee_engine import CrawleeEngineFactory

    with tempfile.TemporaryDirectory() as td:
        settings = PrismSettings(
            task_info=TaskInfoConfig(save_directory=td),
            stealth=StealthConfig(use_fingerprint=False, window_mode="headless"),
        )
        crawler = CrawleeEngineFactory(settings=settings).create()

    hooks = getattr(crawler, "_pre_navigation_hooks", [])
    if len(hooks) != 0:
        _fail(f"use_fingerprint=False 时不应注册指纹 hook，实际 {len(hooks)}")

    # Crawlee 将 DefaultFingerprintGenerator 包装为 BrowserforgeFingerprintGenerator 等实现
    plugin = crawler._browser_pool._plugins[0]
    fg = getattr(plugin, "_fingerprint_generator", None)
    if fg is None:
        _fail("use_fingerprint=False 时 _fingerprint_generator 应存在（内置指纹管线）")

    _ok("CrawleeEngineFactory + use_fingerprint=False（无 hook，内置指纹生成器）")


def smoke_legacy_site_engines() -> None:
    from core.site_engines import SiteCrawlerFactory

    with tempfile.TemporaryDirectory() as td:
        config = {
            "engine_settings": {"crawler_type": "playwright", "browser_type": "chromium"},
            "stealth": {"headless": True, "ignore_ssl_error": True},
            "performance": {"max_concurrency": 1, "max_requests_per_crawl": 1},
            "timeouts_and_retries": {"max_request_retries": 0, "request_handler_timeout_secs": 30},
            "task_info": {"save_directory": td},
        }
        crawler = SiteCrawlerFactory().create_crawler(config)

    launch = crawler._browser_pool._plugins[0].browser_launch_options
    if "ignore_https_errors" not in launch:
        _fail("遗留 site_engines: browser_launch_options 缺少 ignore_https_errors")
    args = launch.get("args") or []
    if "--disable-blink-features=AutomationControlled" not in args:
        _fail("遗留 site_engines: args 缺少 AutomationControlled 禁用标志")

    _ok("core/site_engines.py Playwright 启动参数（ignore_https_errors + 反自动化 args）")


def smoke_rebrowser_warning_once() -> None:
    import src.engine.anti_bot.stealth.rebrowser_backend as rb_mod
    from src.config.settings import AppConfig, StealthConfig
    from src.engine.anti_bot.stealth.rebrowser_backend import RebrowserBackend

    rb_mod._rebrowser_import_warned = False

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    h = _Capture()
    h.setLevel(logging.WARNING)
    old_propagate = rb_mod._logger.propagate
    rb_mod._logger.propagate = False
    rb_mod._logger.addHandler(h)
    try:
        stealth = StealthConfig()
        app = AppConfig()
        backend = RebrowserBackend(stealth, app)
        backend._apply_patches()
        backend._apply_patches()
    finally:
        rb_mod._logger.removeHandler(h)
        rb_mod._logger.propagate = old_propagate

    warns = [r for r in records if r.levelno >= logging.WARNING]
    try:
        import rebrowser_patches  # noqa: F401
    except ImportError:
        if len(warns) != 1:
            _fail(f"未安装 rebrowser-patches 时应仅 1 条 WARNING，实际 {len(warns)}")
        _ok("RebrowserBackend ImportError → 单次 logging.warning")
    else:
        if warns:
            _fail(f"已安装 rebrowser-patches 时不应有 WARNING，实际 {len(warns)}")
        _ok("rebrowser-patches 已安装 → 无降级告警")


def smoke_fingerprint_generator() -> None:
    from src.engine.anti_bot.fingerprint import FingerprintGenerator

    fp = FingerprintGenerator().generate()
    if not fp.user_agent or "Chrome/" not in fp.user_agent:
        _fail("FingerprintGenerator.user_agent 异常")
    if not fp.extra_headers or "Sec-CH-UA" not in fp.extra_headers:
        _fail("FingerprintGenerator.extra_headers 缺少 Sec-CH-UA")
    _ok("FingerprintGenerator（UA + Client Hints 头）")


async def smoke_playwright_real_browser() -> None:
    from playwright.async_api import async_playwright
    from src.engine.anti_bot.fingerprint import FingerprintGenerator
    from src.config.settings import get_app_config

    profile = FingerprintGenerator().generate()
    launch_opts: dict = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    }
    chromium_path = get_app_config().chromium_path
    if chromium_path and os.path.isfile(chromium_path):
        launch_opts["executable_path"] = chromium_path

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(**launch_opts)
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
            await page.goto("about:blank", timeout=15_000)
            await ctx.close()
        finally:
            await browser.close()
    finally:
        await pw.stop()

    _ok("Playwright 真机：new_context（指纹全套）+ about:blank")


def main() -> None:
    print("PrismPDF smoke_antidetect — 开始\n")
    smoke_fingerprint_generator()
    smoke_crawlee_factory_fingerprint_on()
    smoke_crawlee_factory_fingerprint_off()
    smoke_legacy_site_engines()
    smoke_rebrowser_warning_once()

    if os.environ.get("SMOKE_SKIP_BROWSER", "").strip().lower() in ("1", "true", "yes"):
        _ok("SMOKE_SKIP_BROWSER 已设置 — 跳过 Playwright 真机")
    else:
        asyncio.run(smoke_playwright_real_browser())

    print("\n全部冒烟通过。")


if __name__ == "__main__":
    main()
