"""
@Layer   : Engine 层（第三层 · 引擎基础设施）
@Role    : Crawlee 爬虫工厂封装
@Pattern : Factory Pattern（根据配置创建不同引擎） +
           Strategy Pattern（引擎类型可替换） +
           Dependency Injection（浏览器后端由 BrowserFactory 统一注入）
@Description:
    将原 core/site_engines.py 中的 SiteCrawlerFactory 提升为与业务无关的通用引擎工厂。
    接收强类型 PrismSettings 替代原始 config_dict，通过 create() 工厂方法
    统一分发创建 PlaywrightCrawler 或 BeautifulSoupCrawler。
    并发策略（ConcurrencySettings）、超时、重试、会话池、指纹生成器等底层参数
    全部由此类集中计算并注入，业务模块（Runner / Strategy）无需关心引擎细节。

    浏览器后端（AbstractBrowserBackend 子类）通过 BrowserFactory.create_backend()
    在构造函数内自动解析并注入，工厂本身不做 if/elif 分支判断：
        - stealth_engine="chromium"  → PlaywrightBackend（标准，默认）
        - stealth_engine="rebrowser"   → RebrowserBackend（CDP 补丁版 Chromium）
        - stealth_engine="camoufox"   → CamoufoxBackend（补丁版 Firefox，
                                         当前 Crawlee 路径存在兼容性，返回 None 并告警）
    所有后端切换对 CrawleeEngineFactory 完全透明，全程零修改。

    Pattern: Factory —— 对上层屏蔽 Crawlee API 差异，统一返回 crawler 对象。
             DI     —— 后端策略由 BrowserFactory 从外部注入，工厂不感知具体后端类型。
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

from crawlee import ConcurrencySettings
from crawlee.crawlers import BeautifulSoupCrawler, PlaywrightCrawler
from crawlee.fingerprint_suite import DefaultFingerprintGenerator
from crawlee.sessions import SessionPool

if TYPE_CHECKING:
    from crawlee.crawlers import PlaywrightPreNavCrawlingContext
    from src.config.settings import PrismSettings
    from src.engine.anti_bot.fingerprint import FingerprintProfile
    from src.engine.browser_engine import AbstractBrowserBackend
    from src.engine.browser_factory import BrowserFactory
    from src.engine.anti_bot.proxy_rotator import ProxyRotator

# 类型别名：工厂可能返回的两种爬虫类型
Anycrawler = Union[PlaywrightCrawler, BeautifulSoupCrawler]

# Playwright 模式并发硬上限（每个 BrowserContext 约 150–300MB，超过 16 易 OOM）
_PLAYWRIGHT_MAX_CONCURRENCY = 16

# 反检测 Chromium 启动标志（D1 网络层 + D2 协议层防护闭环）
_ANTIBOT_CHROMIUM_ARGS = [
    # JS 层：隐藏 navigator.webdriver=true（最基础反检测项）
    "--disable-blink-features=AutomationControlled",
    # 消除首次启动弹窗和信息栏，避免页面布局异常
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    # 稳定性 / 沙箱
    "--disable-dev-shm-usage",
    "--no-sandbox",
    # D1 网络层：阻断 WebRTC 非代理 UDP 通道，防止本机真实 IP 泄露
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-webrtc-hw-decoding",
    "--disable-webrtc-hw-encoding",
]


def _get_browser_factory() -> "type[BrowserFactory]":
    """延迟导入 BrowserFactory 类对象，避免顶层循环导入。"""
    from src.engine.browser_factory import BrowserFactory
    return BrowserFactory


class CrawleeEngineFactory:
    """
    Crawlee 爬虫实例工厂（支持多浏览器后端自动注入）。

    根据 PrismSettings.engine_settings.crawler_type 决定创建
    PlaywrightCrawler（动态渲染）或 BeautifulSoupCrawler（静态极速）。
    将并发、超时、重试、指纹、会话池等底层参数与业务代码完全隔离。

    浏览器后端（AbstractBrowserBackend 子类）通过 BrowserFactory.create_backend()
    在构造函数内部自动解析并注入，工厂本身不做任何 if/elif 后端分支判断：
        - stealth_engine="chromium"  → PlaywrightBackend（标准，默认）
        - stealth_engine="rebrowser"   → RebrowserBackend（CDP 补丁版 Chromium）
        - stealth_engine="camoufox"   → CamoufoxBackend（补丁版 Firefox；
                                         Crawlee 路径返回 None 并告警）
        ★ 切换伪装策略只需改 StealthConfig.stealth_engine 配置值，本类代码零修改。

    Pattern: Factory Pattern + Dependency Injection（BrowserFactory 作为工厂服务注入者）
    """

    def __init__(
        self,
        settings: "PrismSettings",
        proxy_rotator: Optional["ProxyRotator"] = None,
    ) -> None:
        """
        Args:
            settings     : 全局运行时参数单例（PrismSettings）。
            proxy_rotator: 可选的代理轮换器；若提供则注入 ProxyConfiguration。
        """
        self._settings      = settings
        self._proxy_rotator = proxy_rotator
        # BrowserFactory 在构造时自动解析 stealth_engine 并注入对应后端，
        # 对上层完全屏蔽后端类型差异；若 stealth_engine 为 camoufox 此处返回 None，
        # CrawleeEngineFactory.create() 走标准 Chromium 路径并记告警。
        self._backend = _get_browser_factory().create_backend(settings)

    # ------------------------------------------------------------------
    # 公开工厂方法
    # ------------------------------------------------------------------

    def create(self) -> Anycrawler:
        """
        根据 settings.engine_settings.crawler_type 创建并返回爬虫实例。
        'playwright' → PlaywrightCrawler
        'beautifulsoup' → BeautifulSoupCrawler

        Returns:
            配置完毕、可直接调用 .run() 的 Crawlee 爬虫实例。
        """
        # 若后端提供了 pre-launch 钩子（如 rebrowser CDP 补丁注入），在启动前调用。
        # 使用 duck typing 检查，避免对具体后端类型做 isinstance 分支（DI 原则）。
        if self._backend is not None:
            _pre_launch = getattr(self._backend, "_apply_patches", None)
            if callable(_pre_launch):
                _pre_launch()

        common = self._build_common_kwargs()
        crawler_type = self._settings.engine_settings.crawler_type

        if crawler_type == "playwright":
            return self._create_playwright_crawler(common)
        else:  # "beautifulsoup"
            return self._create_beautifulsoup_crawler(common)

    def _canary_storage_client_kwargs(self) -> dict:
        """
        金丝雀合成任务：Crawlee RequestQueue / KeyValueStore / Dataset 走纯内存实现，
        避免与主任务共享默认落盘存储产生脏数据（阅后即焚）。
        """
        if not getattr(self._settings.task_info, "is_canary", False):
            return {}
        from crawlee.storage_clients import MemoryStorageClient

        return {"storage_client": MemoryStorageClient()}

    # ------------------------------------------------------------------
    # 私有：参数构建
    # ------------------------------------------------------------------

    def _build_concurrency_settings(self) -> ConcurrencySettings:
        """
        将 PrismSettings.performance 转换为 Crawlee ConcurrencySettings。
        处理 max_concurrency='auto' 的情况（映射为平台默认值 16），
        并应用物理上限截断（Playwright 模式最大 16 并发）。

        Returns:
            配置好 min / desired / max 并发数的 ConcurrencySettings 对象。
        """
        perf     = self._settings.performance
        max_conc = self._settings.get_effective_max_concurrency()

        # Playwright 模式硬上限：超过 16 并发时内存压力过大
        if self._settings.engine_settings.crawler_type == "playwright":
            max_conc = min(max_conc, _PLAYWRIGHT_MAX_CONCURRENCY)

        # 人工限速：limit_rate=True 时启用 max_tasks_per_minute
        max_tpm = float(perf.max_tasks_per_minute) if perf.limit_rate else float("inf")

        return ConcurrencySettings(
            min_concurrency=perf.min_concurrency,
            max_concurrency=max_conc,
            desired_concurrency=max_conc,
            max_tasks_per_minute=max_tpm,
        )

    def _build_common_kwargs(self) -> dict:
        """
        构建 BS4 和 Playwright 共用的爬虫构造参数字典：
        max_requests_per_crawl、max_request_retries、
        request_handler_timeout（转换为 timedelta）、concurrency_settings。

        Returns:
            可直接 **解包 传入 Crawler 构造函数的 kwargs 字典。
        """
        tr   = self._settings.timeouts_and_retries
        perf = self._settings.performance
        return {
            "max_requests_per_crawl":  perf.max_requests_per_crawl,
            "max_request_retries":     tr.max_request_retries,
            "request_handler_timeout": timedelta(seconds=tr.request_handler_timeout_secs),
            "concurrency_settings":    self._build_concurrency_settings(),
        }

    def _create_playwright_crawler(self, common_kwargs: dict) -> PlaywrightCrawler:
        """
        构建 PlaywrightCrawler，额外注入：
        - browser_type / browser_launch_options（headless、SSL、下载目录、本地 Chromium）
        - 指纹系统（见下方互斥规则）
        - SessionPool（多身份轮换）
        - ProxyConfiguration（若 proxy_rotator 已提供）

        ── 双指纹生成器互斥规则（P0-04）──────────────────────────────────
        系统内同时存在两套指纹方案，二者绝对互斥，不可同时注入同一 BrowserContext：

          use_fingerprint=True（默认，精确模式）：
            - 使用自研 FingerprintGenerator，从 LOCAL_PC 档案读取真实硬件
              （RTX 3070 GPU、真实屏幕分辨率、真实 CPU 核数），
              精心构建的硬件指纹注入 new_context() 参数。
            - DefaultFingerprintGenerator 传入 None（不实例化）。
              ★ 若两者同时注入，Crawlee 在 crawler.run() 内部会用
                DefaultFingerprintGenerator 的随机预设覆盖 new_context() 参数，
                LOCAL_PC 真实硬件优势将完全失效。

          use_fingerprint=False（快速模式）：
            - 跳过自研 FingerprintGenerator，不构建 context_options 指纹包。
            - 使用 DefaultFingerprintGenerator(...)，由 Crawlee 自动生成标准预设指纹。

        ── SessionPool 策略切换规则（P1-03）────────────────────────────────
        根据 settings.stealth.session_mode 决定 SessionPool 的 max_pool_size：

          session_mode="pool"（默认，多身份轮换）：
            SessionPool(max_pool_size=app_cfg.max_session_pool_size)
            多个 session 轮换使用，每个 session 维护独立 Cookie/身份，
            适合高并发快速抓取，但每个 session 的信誉积累深度有限。

          session_mode="persistent"（单一固定身份）：
            SessionPool(max_pool_size=1)
            整个任务过程固定使用同一 session，持续积累 Cookie 与访问历史，
            适合需要建立深度访问信誉的高防护目标。
            ★ 建议与 user_data_dir 持久化配合使用，实现跨次任务的信誉延续。

        ── 浏览器后端说明 ────────────────────────────────────────────────
        若构造函数传入了 backend（AbstractBrowserBackend 子类），则此方法
        应将后端的 BrowserContext 生命周期与 PlaywrightCrawler 的 browser_options
        协调对接；否则使用 _resolve_chromium_path() 提供的默认 Chromium 路径。
        Camoufox / rebrowser 后端在其各自的实现文件中处理差异，本方法不做 if/elif 判断。

        Args:
            common_kwargs: 由 _build_common_kwargs() 生成的共用参数字典。
        Returns:
            配置完毕的 PlaywrightCrawler 实例。
        """
        from src.config.settings import get_app_config
        app_cfg = get_app_config()

        stealth = self._settings.stealth
        engine  = self._settings.engine_settings
        tr      = self._settings.timeouts_and_retries

        # ── 浏览器启动参数 ──────────────────────────────────────────────
        window_mode = stealth.window_mode
        if window_mode == "headless":
            headless    = True
            extra_args: list = []
        elif window_mode == "minimized":
            headless    = False
            extra_args  = ["--start-minimized", "--window-position=-32000,-32000"]
        else:  # "normal"
            headless    = False
            extra_args  = []

        launch_opts: dict = {
            "headless":            headless,
            "ignore_https_errors": stealth.ignore_ssl_error,
            "args":                _ANTIBOT_CHROMIUM_ARGS + extra_args,
            "downloads_path":      self._build_download_dir(),
        }

        chromium_path = self._resolve_chromium_path()
        if chromium_path:
            launch_opts["executable_path"] = chromium_path

        user_data_dir = stealth.user_data_dir.strip()
        if user_data_dir:
            os.makedirs(user_data_dir, exist_ok=True)
            launch_opts["user_data_dir"] = user_data_dir

        # ── P0-04：双指纹互斥规则 ─────────────────────────────────────
        # use_fingerprint=True → 自研指纹注入 context，禁用 Crawlee 内置指纹生成器
        # use_fingerprint=False → 委托 Crawlee DefaultFingerprintGenerator 自动处理
        profile: Optional["FingerprintProfile"] = None
        if stealth.use_fingerprint:
            from src.engine.anti_bot.fingerprint import FingerprintGenerator
            profile = FingerprintGenerator().generate()
            browser_context_options: dict = {
                "user_agent":          profile.user_agent,
                "viewport":            {
                    "width":  profile.screen_width,
                    "height": profile.screen_height,
                },
                "locale":              profile.languages[0] if profile.languages else "zh-CN",
                "timezone_id":         profile.timezone,
                "permissions":         [],          # 消除"全新浏览器"特征（P2-05）
                "ignore_https_errors": stealth.ignore_ssl_error,
                "extra_http_headers":  profile.extra_headers,  # Step1a: Accept / Sec-CH-UA / Accept-Language
            }
            fingerprint_generator: Any = None       # 阻止 Crawlee 覆盖上述参数
        else:
            browser_context_options = {}
            fingerprint_generator   = DefaultFingerprintGenerator()

        # ── P1-03：Session Pool 策略切换 ──────────────────────────────
        pool_size    = 1 if stealth.session_mode == "persistent" else app_cfg.max_session_pool_size
        session_pool = SessionPool(max_pool_size=pool_size)

        # ── 代理配置 ──────────────────────────────────────────────────
        proxy_config = self._build_proxy_configuration()

        # ── 组装 PlaywrightCrawler ────────────────────────────────────
        # P0-FIX: Crawlee 1.4.0 正确键名为 browser_new_context_options（原 browser_context_options 会
        # 导致 TypeError，所有指纹参数均被静默丢弃）；navigation_timeout 接受 timedelta（原
        # navigation_timeout_secs=float 同样引发 TypeError，导致工厂每次调用均崩溃）。
        pw_kwargs: dict = {
            **common_kwargs,
            "browser_type":                engine.browser_type,
            "browser_launch_options":      launch_opts,
            "browser_new_context_options": browser_context_options,
            "fingerprint_generator":       fingerprint_generator,
            "session_pool":                session_pool,
            "navigation_timeout":          timedelta(seconds=tr.navigation_timeout_secs),
            **self._canary_storage_client_kwargs(),
        }
        if proxy_config is not None:
            pw_kwargs["proxy_configuration"] = proxy_config

        crawler = PlaywrightCrawler(**pw_kwargs)

        # ── Step 2：FingerprintInjector → pre_navigation_hook ─────────────
        # pre_navigation_hook 是 Crawlee 1.4.0 的方法注册器（非构造参数）：
        #   执行时序：browser_pool.new_page() 完成 → 本钩子 → page.goto()
        # 每个请求使用全新 Page 实例，add_init_script 不跨请求累积。
        # 仅在 use_fingerprint=True 时注册，False 路径由 DefaultFingerprintGenerator 自动处理。
        if stealth.use_fingerprint:
            from src.engine.anti_bot.fingerprint import FingerprintInjector
            _injector = FingerprintInjector()
            assert profile is not None
            _fp = profile  # 闭包捕获：与 browser_new_context_options 使用同一份 profile

            async def _fingerprint_hook(ctx: "PlaywrightPreNavCrawlingContext") -> None:
                await _injector.inject(ctx.page, _fp)

            crawler.pre_navigation_hook(_fingerprint_hook)

        return crawler

    def _create_beautifulsoup_crawler(self, common_kwargs: dict) -> BeautifulSoupCrawler:
        """
        构建 BeautifulSoupCrawler（静态极速模式，无浏览器开销）。

        Args:
            common_kwargs: 由 _build_common_kwargs() 生成的共用参数字典。
        Returns:
            配置完毕的 BeautifulSoupCrawler 实例。
        """
        proxy_config = self._build_proxy_configuration()
        bs_kwargs: dict = {**common_kwargs, **self._canary_storage_client_kwargs()}
        if proxy_config is not None:
            bs_kwargs["proxy_configuration"] = proxy_config
        return BeautifulSoupCrawler(**bs_kwargs)

    def _resolve_chromium_path(self) -> Optional[str]:
        """
        按优先级解析 Chromium 可执行文件路径（三级查找）：

        1. AppConfig.chromium_path 非空且文件存在 → 使用该显式路径（最高优先级）。
        2. browser_cores/playwright 目录下存在 chrome.exe → 自动发现
           （由 PLAYWRIGHT_BROWSERS_PATH 环境变量定位，由 _init_browser_env() 设置）。
        3. None → 由 Playwright 使用 PLAYWRIGHT_BROWSERS_PATH 自动查找。

        若项目首次运行且未手动下载浏览器，则不传入 executable_path，
        Playwright 会根据 PLAYWRIGHT_BROWSERS_PATH 自动下载并缓存到 ./browser_cores/playwright。

        Returns:
            Chromium 可执行文件的绝对路径；无本地二进制时返回 None。
        """
        import os
        from pathlib import Path
        from src.config.settings import get_app_config

        # 优先级 1：显式配置的 chromium_path
        chromium_path = get_app_config().chromium_path
        if chromium_path and os.path.isfile(chromium_path):
            return str(Path(chromium_path).resolve())

        # 优先级 2：browser_cores/playwright 下 Chromium（新版 chromium-* / 旧版 chrome-*）
        pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if pw_root:
            from src.utils.playwright_chromium_exe import find_chromium_executable_in_browsers_path

            found = find_chromium_executable_in_browsers_path(pw_root)
            if found:
                return found

        # 优先级 3：返回 None，由 Playwright 根据 PLAYWRIGHT_BROWSERS_PATH 自动查找
        return None

    def _build_download_dir(self) -> str:
        """
        在 settings.task_info.save_directory 下创建 _temp_playwright 子目录，
        作为 Playwright 底层接水盘（browser_launch_options['downloads_path']）。
        目录不存在时自动创建。

        Returns:
            下载临时目录的绝对路径字符串。
        """
        from src.config.settings import get_app_config
        subdir  = get_app_config().download_temp_subdir
        dl_dir  = Path(self._settings.task_info.save_directory) / subdir
        dl_dir.mkdir(parents=True, exist_ok=True)
        return str(dl_dir.resolve())

    def _build_proxy_configuration(self) -> Optional[Any]:
        """
        若 proxy_rotator 已提供，将其转换为 Crawlee ProxyConfiguration。
        否则返回 None（不注入代理）。

        实现细节：
        直接使用 ProxyConfiguration(proxy_urls=[...]) 同步构造函数，
        规避在同步 create() 方法中调用 proxy_rotator.build()（该方法为 async）。

        Returns:
            Crawlee ProxyConfiguration 实例；无代理时返回 None。
        """
        if self._proxy_rotator is None or self._proxy_rotator.count == 0:
            return None

        from crawlee.proxy_configuration import ProxyConfiguration

        # 同步提取 URL 列表（绕过 async build()）；list[str|None] 满足 Crawlee 参数不变性
        proxy_urls: list[str | None] = list[str | None](
            [p.url for p in self._proxy_rotator._proxies]  # noqa: SLF001
        )
        return ProxyConfiguration(proxy_urls=proxy_urls)
