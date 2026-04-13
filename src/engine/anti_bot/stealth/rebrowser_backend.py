"""
@Layer   : Engine 层（第三层 · 反爬工具箱 · 浏览器后端）
@Role    : rebrowser-patches 补丁版 Chromium 后端
@Pattern : Strategy Pattern（AbstractBrowserBackend 具体策略）
@Description:
    rebrowser-patches 是一套针对 Playwright / Puppeteer 驱动的 Chromium 所打的补丁，
    解决以下 CDP（Chrome DevTools Protocol）自动化信号泄露问题：
    - Runtime.enable 调用导致的 window.cdc_* 变量暴露。
    - Page.addScriptToEvaluateOnNewDocument 调用的执行时序特征。
    - navigator.webdriver = true 标志。
    - 其他 CDP 握手特征被 WAF 识别的问题。

    此文件仅包含 rebrowser 特有的启动与补丁注入逻辑，与 playwright_backend.py、
    camoufox_backend.py 物理隔离——修改本文件不会影响其他后端。

    依赖要求：
        pip install rebrowser-patches
        （补丁以 Python 包形式在启动时注入，无需独立二进制；
          若需使用独立打补丁的 Chromium 二进制，配置 AppConfig.rebrowser_path）

    与其他后端的差异：
        - 在标准 playwright.chromium.launch() 基础上，通过 rebrowser_patches.patch()
          在浏览器启动前注入 CDP 补丁（Python 层 monkey-patch）。
        - 若 AppConfig.rebrowser_path 非空，使用该路径的 Chromium 二进制；
          否则回退到 AppConfig.chromium_path 或系统 Playwright 安装版本。
        - 其余生命周期逻辑（BrowserContext / Page 管理）与 PlaywrightBackend 完全相同，
          差异仅在 _apply_patches() 和 _resolve_executable_path() 两个私有方法。

    扩展指引（本文件内部）：
        rebrowser-patches 版本更新时，只需修改 _apply_patches() 内的调用方式，
        不影响其他后端或 CrawleeEngineFactory。

    Pattern: Strategy —— 实现 AbstractBrowserBackend，代表"rebrowser Chromium 伪装策略"。
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from src.engine.browser_engine import AbstractBrowserBackend

if TYPE_CHECKING:
    from src.config.settings import StealthConfig, AppConfig

_logger = logging.getLogger(__name__)

# rebrowser 未安装时 _apply_patches 可能被工厂 pre_launch 与 __aenter__ 各调一次，只告警一次
_rebrowser_import_warned: bool = False


class RebrowserBackend(AbstractBrowserBackend):
    """
    rebrowser-patches 补丁版 Chromium 浏览器后端。

    生命周期：
        async with RebrowserBackend(stealth_cfg, app_cfg) as backend:
            page = await backend.new_page()
            ...

    外部调用方式与 PlaywrightBackend / CamoufoxBackend 完全一致，
    因为三者实现了相同的 AbstractBrowserBackend 接口。

    Pattern: Strategy（AbstractBrowserBackend 具体实现）
    """

    def __init__(
        self,
        stealth_config: "StealthConfig",
        app_config: "AppConfig",
    ) -> None:
        """
        Args:
            stealth_config: 反检测配置块，提供 headless / ignore_ssl_error 等参数。
            app_config    : 应用级配置，提供 rebrowser_path / chromium_path 等可执行路径。
        """
        self._stealth_config = stealth_config
        self._app_config     = app_config
        self._playwright: Optional["Playwright"] = None
        self._browser:    Optional["Browser"]    = None
        self._context:    Optional["BrowserContext"] = None
        from src.engine.anti_bot.fingerprint import FingerprintGenerator
        self._profile = FingerprintGenerator().generate()

    # ------------------------------------------------------------------
    # AbstractBrowserBackend 接口实现
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "RebrowserBackend":
        """
        注入 rebrowser 补丁 → 启动 Playwright 进程 → launch Chromium → new_context。

        实现要点：
        1. 调用 _apply_patches() 在 Playwright 启动前注入 CDP 补丁。
        2. 调用 async_playwright().__aenter__() 启动 Playwright 进程。
        3. 使用 _resolve_executable_path() 决定 executable_path 参数。
        4. playwright.chromium.launch(**_build_launch_options()) 启动浏览器。
        5. browser.new_context(**_build_context_options()) 创建上下文。
        6. 返回 self。
        """
        # 必须在 Playwright 进程启动前注入补丁（monkey-patch playwright 内部 CDP 方法）
        self._apply_patches()

        self._playwright = await async_playwright().start()
        self._browser    = await self._playwright.chromium.launch(**self._build_launch_options())
        self._context    = await self._browser.new_context(**self._build_context_options())
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """关闭 BrowserContext 和 Chromium 进程，停止 Playwright 进程。"""
        await self.close()

    async def new_page(self) -> Page:
        """
        在当前 BrowserContext 下创建并返回新 Page 实例。

        Raises:
            RuntimeError: 后端未通过 __aenter__ 初始化时抛出。
        """
        if not self.is_ready:
            raise RuntimeError(
                "RebrowserBackend 尚未初始化，请先通过 async with 或 __aenter__() 启动。"
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
    # 私有：rebrowser 特有逻辑
    # ------------------------------------------------------------------

    def _apply_patches(self) -> None:
        """
        在浏览器启动前通过 rebrowser_patches.patch() 注入 CDP 补丁。

        实现要点：
            import rebrowser_patches
            rebrowser_patches.patch()
            # 该调用 monkey-patch 了 playwright 内部的 CDP 握手方法，
            # 消除 Runtime.enable 调用暴露的自动化信号。
            # 必须在 async_playwright().__aenter__() 之前调用。
        """
        try:
            import rebrowser_patches
            rebrowser_patches.patch()
        except ImportError:
            # Step 5：未安装时降级为标准 Playwright，必须显式告警（否则运维误以为 CDP 补丁已生效）
            global _rebrowser_import_warned
            if not _rebrowser_import_warned:
                _rebrowser_import_warned = True
                _logger.warning(
                    "rebrowser-patches 未安装，CDP 自动化检测防护未启用，已降级为标准 Playwright。"
                    "请执行: pip install rebrowser-patches"
                )

    def _resolve_executable_path(self) -> Optional[str]:
        """
        按优先级解析 Chromium 可执行文件路径（三级查找）：

        1. AppConfig.rebrowser_path 非空且文件存在 → rebrowser 专属补丁二进制（最高优先级）。
        2. AppConfig.chromium_path 非空且文件存在 → 项目捆绑的标准 Chromium。
        3. browser_cores/rebrowser 目录下存在 chrome.exe → 自动发现
           （由 REBROWSER_BROWSERS_PATH 环境变量定位，由 _init_browser_env() 设置）。
        4. None → 由 Playwright 使用 PLAYWRIGHT_BROWSERS_PATH 自动查找。

        路径隔离说明：
            - rebrowser-patches 的 CDP 补丁在 Python 层注入，不依赖独立二进制，
              但为了与系统 Playwright 物理隔离，建议将打补丁的 Chromium 二进制
              存放在 REBROWSER_BROWSERS_PATH 目录下。
            - 若 AppConfig.rebrowser_path 非空，则优先使用该路径（独立二进制优先）。
            - 若 AppConfig.rebrowser_path 为空但 REBROWSER_BROWSERS_PATH 已设置，
              则 rebrowser 使用该目录下的 Chromium，与标准 Playwright 浏览器隔离。

        Returns:
            可执行文件绝对路径字符串；无可用路径时返回 None。
        """
        import os

        from src.utils.playwright_chromium_exe import find_chromium_executable_in_browsers_path

        # 优先级 1：AppConfig.rebrowser_path（rebrowser 专属二进制）
        rebrowser_path: str = getattr(self._app_config, "rebrowser_path", "")
        if rebrowser_path and os.path.isfile(rebrowser_path):
            return os.path.abspath(rebrowser_path)

        # 优先级 2：AppConfig.chromium_path（项目捆绑标准 Chromium）
        chromium_path: str = getattr(self._app_config, "chromium_path", "")
        if chromium_path and os.path.isfile(chromium_path):
            return os.path.abspath(chromium_path)

        # 优先级 3：browser_cores/rebrowser 下 Chromium（新版 chromium-* / 旧版 chrome-*）
        rb_root = os.environ.get("REBROWSER_BROWSERS_PATH", "")
        if rb_root:
            found = find_chromium_executable_in_browsers_path(rb_root)
            if found:
                return found

        # 优先级 4：回退到 PLAYWRIGHT_BROWSERS_PATH（rebrowser 的 Python 补丁在底层走 Playwright）
        pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if pw_root:
            found = find_chromium_executable_in_browsers_path(pw_root)
            if found:
                return found

        return None

    def _build_launch_options(self) -> dict:
        """
        构建 playwright.chromium.launch() 所需参数字典。
        逻辑与 PlaywrightBackend._build_launch_options() 基本相同，
        额外加入 rebrowser 建议的启动标志（如禁用 --enable-automation 标志）。

        包含（与 PlaywrightBackend 共同项）：
        - headless           : 由 stealth_config.window_mode 决定，与 PlaywrightBackend 相同：
                                   "headless"  → headless=True
                                   "minimized" → headless=False + --start-minimized 兜底标志
                                   "normal"    → headless=False
        - ignore_https_errors: 是否忽略 SSL 错误。
        - executable_path    : 由 _resolve_executable_path() 确定。
        - user_data_dir      : 持久化浏览器档案目录（来自 _resolve_user_data_dir()）。
                               ⚠️ 替代旧方案 launch_persistent_context，理由同
                                  PlaywrightBackend._build_launch_options() 说明。
        - args               : Chromium 启动标志列表，与 PlaywrightBackend 共同项一致，
                               额外加入 rebrowser-patches 推荐的反自动化标志：
            "--disable-blink-features=AutomationControlled"
            "--no-first-run" / "--no-default-browser-check" / "--disable-infobars"
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
                防止 WebRTC 通过非代理 UDP 泄露本机真实 IP（D1 网络层防护）。
            "--disable-webrtc-hw-decoding" / "--disable-webrtc-hw-encoding"
                辅助阻断 WebRTC 硬件编解码路径。
            rebrowser 特有说明：rebrowser-patches 已在 Python 层抹除 CDP 握手信号，
            args 层的 AutomationControlled 禁用与其协同，覆盖 JS 层和协议层两个
            检测向量，形成 D2 协议层防护闭环。

        Returns:
            可直接 **解包 传入 browser.launch() 的 kwargs 字典。
        """
        import os
        cfg         = self._stealth_config
        window_mode = cfg.window_mode

        if window_mode == "headless":
            headless    = True
            extra_args: list = []
        elif window_mode == "minimized":
            headless    = False
            extra_args  = ["--start-minimized", "--window-position=-32000,-32000"]
        else:
            headless    = False
            extra_args  = []

        # rebrowser 特有标志（与 AutomationControlled 禁用协同，覆盖 JS 层和协议层两个检测向量）
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

        exe_path = self._resolve_executable_path()
        if exe_path:
            opts["executable_path"] = exe_path

        user_data_dir = self._resolve_user_data_dir()
        if user_data_dir:
            opts["user_data_dir"] = user_data_dir

        return opts

    def _resolve_user_data_dir(self) -> Optional[str]:
        """
        解析并返回持久化浏览器数据目录的绝对路径（与 PlaywrightBackend 逻辑相同）。

        路径解析规则：
        1. stealth_config.user_data_dir 非空 → 直接使用该绝对路径。
        2. stealth_config.user_data_dir 为空 → 返回 None（不启用持久化）。

        当路径有效时自动创建目录（os.makedirs(..., exist_ok=True)）。
        建议路径规范：{save_directory}/_browser_profile/rebrowser/

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
        构建 browser.new_context() 所需参数字典（与 PlaywrightBackend 完全相同逻辑）。
        通过 new_context() 参数层注入指纹，而非 JS 覆盖。

        包含：
        - user_agent / viewport / locale / timezone_id（来自 FingerprintProfile）
        - permissions : []（空列表，预初始化所有权限为 denied 状态）
          理由同 PlaywrightBackend._build_context_options() 的 permissions 说明。

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
            "extra_http_headers":  p.extra_headers,  # Step1c: Accept / Sec-CH-UA / Accept-Language
        }
