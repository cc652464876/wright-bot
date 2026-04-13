"""
@Layer   : Engine 层（第三层 · 反爬工具箱 · 浏览器后端）
@Role    : 标准 Playwright Chromium 后端（AbstractBrowserBackend 默认实现）
@Pattern : Strategy Pattern（AbstractBrowserBackend 具体策略）
@Description:
    将 engine/browser_engine.py 中的 BrowserContextManager 提升为
    AbstractBrowserBackend 策略体系下的"默认策略"封装。

    此文件负责：
    - 通过 async_playwright() 启动标准 Chromium（或 Firefox / WebKit）。
    - 将 StealthConfig.headless / ignore_ssl_error 注入浏览器启动参数。
    - 若 AppConfig.chromium_path 文件存在，使用本地捆绑版 Chromium；
      否则回退到系统 Playwright 安装版本。
    - 注入 FingerprintProfile 中的 user_agent / viewport / locale / timezone_id
      到 new_context() 参数层（而非 JS 覆盖，避免 WAF Object.defineProperty 检测）。

    此文件只处理"标准 Playwright 场景"，不含任何 Camoufox 或 rebrowser 逻辑。
    若需要其他伪装策略，请使用同目录下的对应文件——
    三者实现相同的 AbstractBrowserBackend 接口，CrawleeEngineFactory 无感切换。

    配置键映射：
        StealthConfig.stealth_engine = "chromium" → MasterDispatcher 实例化本类
        （V10.2 重命名：旧键名 "playwright" 已废弃，统一为 "chromium"）

    扩展指引（本文件内部）：
        未来如需支持更多 browser_type（如 webkit），只需在 launch() 内增加对应
        playwright.webkit.launch() 分支，外部调用方代码零修改。

    Pattern: Strategy —— 实现 AbstractBrowserBackend，代表 stealth_engine="chromium" 伪装策略。
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from src.engine.browser_engine import AbstractBrowserBackend

if TYPE_CHECKING:
    from src.config.settings import StealthConfig, AppConfig


class PlaywrightBackend(AbstractBrowserBackend):
    """
    标准 Playwright Chromium 浏览器后端（stealth_engine="chromium"）。

    生命周期：
        async with PlaywrightBackend(stealth_cfg, app_cfg) as backend:
            page = await backend.new_page()
            ...

    由 MasterDispatcher 在 stealth_engine="chromium" 时实例化。
    （V10.2 重命名：旧配置键 "playwright" 已废弃，新代码统一写 "chromium"）

    Pattern: Strategy（AbstractBrowserBackend 具体实现）
    """

    def __init__(
        self,
        stealth_config: "StealthConfig",
        app_config: "AppConfig",
    ) -> None:
        """
        Args:
            stealth_config: 反检测配置块，提供 headless / ignore_ssl_error /
                            browser_type（通过 EngineConfig 间接传入）等参数。
            app_config    : 应用级配置，提供 chromium_path 等可执行路径。
        """
        self._stealth_config = stealth_config
        self._app_config = app_config
        self._playwright: Optional[Playwright] = None
        self._browser:   Optional[Browser]     = None
        self._context:   Optional[BrowserContext] = None
        # 在 __init__ 时预生成指纹包，供 _build_context_options() 消费
        from src.engine.anti_bot.fingerprint import FingerprintGenerator
        self._profile = FingerprintGenerator().generate()

    # ------------------------------------------------------------------
    # AbstractBrowserBackend 接口实现
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PlaywrightBackend":
        """
        启动 Playwright 进程 → launch Browser → new_context。
        返回 self，供链式调用 .new_page()。
        """
        self._playwright = await async_playwright().start()
        self._browser   = await self._playwright.chromium.launch(**self._build_launch_options())
        self._context   = await self._browser.new_context(**self._build_context_options())
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """关闭 BrowserContext 和 Browser，停止 Playwright 进程。"""
        await self.close()

    async def new_page(self) -> Page:
        """
        在当前 BrowserContext 下创建并返回新 Page 实例。

        Raises:
            RuntimeError: 后端未通过 __aenter__ 初始化时抛出。
        """
        if not self.is_ready:
            raise RuntimeError(
                "PlaywrightBackend 尚未初始化，请先通过 async with 或 __aenter__() 启动。"
            )
        return await self._context.new_page()  # type: ignore[union-attr]

    async def close(self) -> None:
        """显式关闭后端，与 __aexit__ 逻辑等价。"""
        if self._context is not None:
            try:
                await self._context.close()
            finally:
                self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            finally:
                self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None

    @property
    def is_ready(self) -> bool:
        """True 表示 BrowserContext 已成功初始化且尚未关闭。"""
        return self._context is not None

    # ------------------------------------------------------------------
    # 私有：启动参数构建
    # ------------------------------------------------------------------

    def _build_launch_options(self) -> dict:
        """
        根据 stealth_config 和 app_config 构建 browser.launch() 所需参数字典。

        包含：
        - headless           : 由 stealth_config.window_mode 决定（优先于 headless 字段）：
                                   "headless"  → headless=True（新一代无头，速度最优）
                                   "minimized" → headless=False（有头最小化，GPU 直通）
                                   "normal"    → headless=False（有头正常窗口，调试用）
        - ignore_https_errors: 是否忽略 SSL 错误（来自 stealth_config.ignore_ssl_error）。
        - executable_path    : 若 chromium_path 文件存在则注入本地二进制；否则省略。
        - downloads_path     : 临时下载目录（由 app_config.download_temp_subdir 衍生）。
        - user_data_dir      : 持久化浏览器档案目录（来自 _resolve_user_data_dir()）。
                               非空时 Chromium 复用该目录下的 Cookie/localStorage/
                               IndexedDB，实现"数字包浆"信誉积累。
                               ⚠️ 替代旧方案 launch_persistent_context：
                                  launch_persistent_context 返回 BrowserContext 对象，
                                  跳过 Browser 层，与 Crawlee PlaywrightCrawler 的
                                  Browser→BrowserContext→Page 生命周期模型不兼容，
                                  会导致会话池失效；user_data_dir 在 browser.launch()
                                  层注入，完全兼容 Crawlee 管理的 BrowserContext。
        - args               : Chromium 启动标志列表，包含以下反检测与 WebRTC 防护项：
            "--disable-blink-features=AutomationControlled"
                隐藏 navigator.webdriver=true 标志（最基础的反检测项）。
            "--no-first-run" / "--no-default-browser-check" / "--disable-infobars"
                消除首次启动弹窗和信息栏，避免页面布局异常。
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
                禁止 WebRTC 通过非代理 UDP 通道发送流量，防止本机真实 IP
                通过 RTCPeerConnection STUN 泄露（即使已配置代理也会发生）。
            "--disable-webrtc-hw-decoding" / "--disable-webrtc-hw-encoding"
                禁用 WebRTC 硬件编解码，辅助阻断 WebRTC IP 泄漏路径。
            注：WAF 据 WebRTC 泄露的本机 IP 与代理 IP 不一致来判定"使用代理的
                非真实用户"，上述三条 WebRTC 标志是 D1 网络层防护的关键闭环。
            window_mode="minimized" 时额外附加：
            "--start-minimized"
                Chromium 启动后立即最小化窗口（任务栏可见，不遮挡桌面）。
            "--window-position=-32000,-32000"
                兜底方案：将窗口移至极端负坐标区域，应对 --start-minimized
                在部分 Windows 环境（多显示器、DPI 缩放）下不生效的情况。

        Returns:
            可直接 **解包 传入 browser.launch() 的 kwargs 字典。
        """
        import os
        cfg         = self._stealth_config
        window_mode = cfg.window_mode

        if window_mode == "headless":
            headless   = True
            extra_args: list = []
        elif window_mode == "minimized":
            headless   = False
            extra_args = ["--start-minimized", "--window-position=-32000,-32000"]
        else:  # "normal"
            headless   = False
            extra_args = []

        chromium_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-webrtc-hw-decoding",
            "--disable-webrtc-hw-encoding",
        ] + extra_args

        opts: dict = {
            "headless":            headless,
            "ignore_https_errors": cfg.ignore_ssl_error,
            "args":                chromium_args,
        }

        chromium_path: str = getattr(self._app_config, "chromium_path", "")
        if chromium_path and os.path.isfile(chromium_path):
            opts["executable_path"] = chromium_path

        # 三级路径解析（优先级从高到低）：
        #   1. 显式配置的 chromium_path（上方已处理）
        #   2. browser_cores/playwright 目录下的 chrome.exe（由 PLAYWRIGHT_BROWSERS_PATH 定位）
        #   3. 省略 → 由 Playwright 使用 PLAYWRIGHT_BROWSERS_PATH 环境变量自动查找
        if "executable_path" not in opts:
            auto_exe = self._resolve_executable_path()
            if auto_exe:
                opts["executable_path"] = auto_exe

        # 临时下载目录
        dl_subdir: str = getattr(self._app_config, "download_temp_subdir", "_temp_playwright")
        if dl_subdir:
            opts["downloads_path"] = dl_subdir

        user_data_dir = self._resolve_user_data_dir()
        if user_data_dir:
            opts["user_data_dir"] = user_data_dir

        return opts

    def _resolve_executable_path(self) -> Optional[str]:
        """
        按优先级解析 Chromium 可执行文件路径（三级查找）：

        1. AppConfig.chromium_path 非空且文件存在 → 使用该显式路径（最高优先级）。
        2. browser_cores/playwright 目录下存在 chrome.exe → 自动发现
           （由 PLAYWRIGHT_BROWSERS_PATH 环境变量定位，由 _init_browser_env() 设置）。
        3. None → 由 Playwright 使用 PLAYWRIGHT_BROWSERS_PATH 自动查找已安装的浏览器。

        Returns:
            Chromium 可执行文件绝对路径字符串；无本地二进制时返回 None。
        """
        import os

        # 优先级 1：显式配置的 chromium_path
        chromium_path: str = getattr(self._app_config, "chromium_path", "")
        if chromium_path and os.path.isfile(chromium_path):
            return os.path.abspath(chromium_path)

        # 优先级 2：browser_cores/playwright 下 Chromium（新版 chromium-* / 旧版 chrome-*）
        pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if pw_root:
            from src.utils.playwright_chromium_exe import find_chromium_executable_in_browsers_path

            found = find_chromium_executable_in_browsers_path(pw_root)
            if found:
                return found

        return None

    def _resolve_user_data_dir(self) -> Optional[str]:
        """
        解析并返回持久化浏览器数据目录的绝对路径。

        路径解析规则（优先级从高到低）：
        1. stealth_config.user_data_dir 非空 → 直接使用该绝对路径。
        2. stealth_config.user_data_dir 为空 → 返回 None（不启用持久化，每次临时 context）。

        当路径有效时：
        - 若目录不存在则自动创建（os.makedirs(..., exist_ok=True)）。
        - 建议路径规范：{save_directory}/_browser_profile/chromium/

        Returns:
            持久化目录绝对路径字符串；不启用持久化时返回 None。
        """
        import os
        path = self._stealth_config.user_data_dir.strip()
        if not path:
            return None
        os.makedirs(path, exist_ok=True)
        return path

    def _build_context_options(self) -> dict:
        """
        构建 browser.new_context() 所需参数字典。

        包含：
        - user_agent  : FingerprintProfile.user_agent
        - viewport    : {"width": profile.screen_width, "height": profile.screen_height}
        - locale      : FingerprintProfile.languages[0]（如 "zh-CN"）
        - timezone_id : FingerprintProfile.timezone（IANA 格式，如 "Asia/Shanghai"）
        - permissions : []（空列表，预初始化所有权限为 denied 状态）

        注入策略说明：通过 new_context() 参数层注入而非 JS page.evaluate() 覆盖，
        是因为 JS 覆盖会留下 Object.defineProperty 调用链，被现代 WAF 检测。

        permissions 说明（P2-05）：
            全新 BrowserContext 的 Notification.permission 默认为 "default"
            （从未被询问状态），而真实用户的浏览器通常为 "denied"（曾被拒绝）。
            WAF 通过 JS 探测 Notification.permission 值来识别"全新自动化浏览器"。
            传入 permissions=[] 等效于将所有权限置为 denied 状态，
            模拟已经使用过浏览器的真实用户环境。

        Returns:
            可直接 **解包 传入 browser.new_context() 的 kwargs 字典。
        """
        p = self._profile
        return {
            "user_agent":          p.user_agent,
            "viewport":            {"width": p.screen_width, "height": p.screen_height},
            "locale":              p.languages[0] if p.languages else "zh-CN",
            "timezone_id":         p.timezone,
            "permissions":         [],
            "ignore_https_errors": self._stealth_config.ignore_ssl_error,
            "extra_http_headers":  p.extra_headers,  # Step1b: Accept / Sec-CH-UA / Accept-Language
        }
