"""
@Layer   : Engine 层（第三层 · 反爬工具箱 · 浏览器后端）
@Role    : Camoufox 补丁版 Firefox 后端
@Pattern : Strategy Pattern（AbstractBrowserBackend 具体策略）
@Description:
    Camoufox 是一个在 Firefox 二进制层打了反指纹补丁的浏览器：
    - WebGL UNMASKED_VENDOR / RENDERER 在底层随机化，无需 JS 覆盖（不留 defineProperty 痕迹）。
    - Canvas 噪点、AudioContext 指纹、字体枚举均在 C++ 层处理。
    - 基于 Firefox（Gecko），天然规避 Chromium 特有的 CDP 自动化检测信号。

    此文件仅包含 Camoufox 特有的启动逻辑，与 playwright_backend.py、
    rebrowser_backend.py 物理隔离——修改本文件不会影响其他后端。

    依赖要求：
        pip install camoufox[geoip]
        python -m camoufox fetch   # 首次使用时下载补丁版 Firefox 二进制

    与其他后端的差异：
        - 使用 camoufox.AsyncNewBrowser() 替代 playwright.chromium.launch()。
        - BrowserContext 由 Camoufox 内部管理，new_page() 从其返回的 context 创建。
        - AppConfig.camoufox_path 非空时使用自定义二进制路径；
          为空时由 camoufox 包自动查找已下载的版本（推荐）。
        - headless 模式通过 Camoufox 特有的 headless=True 参数传入（非 --headless 标志）。

    扩展指引（本文件内部）：
        Camoufox 支持通过 config= 参数传入 JSON 配置，可控制更细粒度的指纹参数
        （如 screen.width / navigator.languages）。如需接入 FingerprintProfile，
        在 _build_camoufox_config() 中将 FingerprintProfile 字段映射为 Camoufox config 格式。
        ★ 不需要修改 PlaywrightBackend、RebrowserBackend 或 CrawleeEngineFactory。

    Pattern: Strategy —— 实现 AbstractBrowserBackend，代表"Camoufox Firefox 伪装策略"。
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from src.engine.browser_engine import AbstractBrowserBackend

if TYPE_CHECKING:
    from playwright.async_api import Page
    from src.config.settings import StealthConfig, AppConfig


class CamoufoxBackend(AbstractBrowserBackend):
    """
    Camoufox 补丁版 Firefox 浏览器后端。

    生命周期：
        async with CamoufoxBackend(stealth_cfg, app_cfg) as backend:
            page = await backend.new_page()
            ...

    外部调用方式与 PlaywrightBackend / RebrowserBackend 完全一致，
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
            app_config    : 应用级配置，提供 camoufox_path（为空时由 camoufox 包自动管理）。
        """
        super().__init__(stealth_config, app_config)
        # _browser_cm: camoufox.AsyncNewBrowser 上下文管理器实例（持有 __aexit__ 引用）
        self._browser_cm = None
        self._browser    = None  # camoufox Browser（Playwright Browser 兼容）
        self._context    = None  # BrowserContext
        # 预生成指纹包用于 Camoufox config 映射
        from src.engine.anti_bot.fingerprint import FingerprintGenerator
        self._profile = FingerprintGenerator().generate()

    # ------------------------------------------------------------------
    # AbstractBrowserBackend 接口实现
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "CamoufoxBackend":
        """
        通过 camoufox.AsyncNewBrowser() 启动 Camoufox Firefox 进程并获取 BrowserContext。

        实现要点：
        1. 调用 camoufox.AsyncNewBrowser(headless=..., config=_build_camoufox_config())。
        2. 将返回的 browser 对象存入 self._browser；context 从 browser.contexts[0] 或
           browser.new_context() 获取（Camoufox 默认已创建一个 context）。
        3. 返回 self。
        """
        from camoufox.async_api import AsyncNewBrowser

        headless = self._stealth_config.window_mode == "headless"
        config   = self._build_camoufox_config()

        self._browser_cm = AsyncNewBrowser(headless=headless, **config)
        self._browser    = await self._browser_cm.__aenter__()

        # Camoufox 启动后通常已有一个默认 context
        if self._browser.contexts:
            self._context = self._browser.contexts[0]
        else:
            self._context = await self._browser.new_context()

        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """
        关闭 Camoufox BrowserContext 和 Firefox 进程，释放所有资源。
        与 PlaywrightBackend.__aexit__ 语义相同，但调用 camoufox 特有的关闭 API。
        """
        await self.close()

    async def new_page(self) -> "Page":
        """
        在当前 Camoufox BrowserContext 下创建并返回新 Page 实例。
        返回的 Page 对象与标准 Playwright Page API 完全兼容。

        Raises:
            RuntimeError: 后端未通过 __aenter__ 初始化时抛出。
        """
        if not self.is_ready:
            raise RuntimeError(
                "CamoufoxBackend 尚未初始化，请先通过 async with 或 __aenter__() 启动。"
            )
        return await self._context.new_page()  # type: ignore[union-attr]

    async def close(self) -> None:
        """显式关闭后端，与 __aexit__ 逻辑等价。"""
        if self._context is not None:
            try:
                await self._context.close()
            finally:
                self._context = None
        if self._browser_cm is not None:
            try:
                await self._browser_cm.__aexit__(None, None, None)
            finally:
                self._browser_cm = None
                self._browser    = None

    @property
    def is_ready(self) -> bool:
        """True 表示 Camoufox BrowserContext 已成功初始化且尚未关闭。"""
        return self._context is not None

    # ------------------------------------------------------------------
    # 私有：Camoufox 特有配置构建
    # ------------------------------------------------------------------

    def _build_camoufox_config(self) -> dict:
        """
        构建传入 camoufox.AsyncNewBrowser(config=...) 的 JSON 配置字典。

        Camoufox config 格式参考：
            https://camoufox.com/python/usage/#config-options

        基础映射（来自 StealthConfig / FingerprintProfile）：
            screen.width / screen.height    → FingerprintProfile.screen_width/height
            navigator.languages             → FingerprintProfile.languages
            navigator.hardwareConcurrency   → FingerprintProfile.hardware_concurrency
            intl.accept_languages           → FingerprintProfile.languages[0]

        window_mode 映射（P1-05）：
            Camoufox 不支持 --start-minimized 启动标志，窗口控制通过
            camoufox.AsyncNewBrowser() 的 headless 参数实现：
                window_mode="headless"  → headless=True（传入 AsyncNewBrowser）
                window_mode="minimized" → headless=False（有头，窗口最小化需额外处理）
                window_mode="normal"    → headless=False
            ⚠️ minimized 模式下 Camoufox 无内置最小化支持，需在 __aenter__ 中
               通过 OS 级 API（如 pygetwindow）在浏览器启动后手动最小化窗口。

        持久化档案注入（P0-03，替代 launch_persistent_context）：
            Camoufox 不支持 Chromium 的 user_data_dir 参数，持久化通过
            Camoufox 特有的 profile_path 参数实现：
                profile_path → stealth_config.user_data_dir（非空时注入）
            非空时 Camoufox 在该目录下维护 Firefox 用户档案（cookies/prefs.js 等），
            实现等效的"数字包浆"信誉积累效果。
            建议路径规范：{save_directory}/_browser_profile/camoufox/

        Permissions API 预初始化（P2-05，Firefox 等效配置）：
            Firefox 通过 prefs 控制通知权限初始状态：
                "permissions.default.desktop-notification": 2
                    值 2 = Block（等效于 Chromium 的 permissions=[]），
                    使 Notification.permission 从启动即为 "denied" 而非 "default"，
                    消除"全新浏览器"特征（真实用户通常已拒绝过通知权限请求）。

        WebRTC IP 泄漏防护（P1-04，Firefox 等效配置）：
            Firefox 不使用 Chromium 启动标志，WebRTC 禁用通过 Firefox prefs 实现：
                "media.peerconnection.enabled": False
                    完全禁用 RTCPeerConnection API，从根本上阻断所有 WebRTC 通道。
                "media.peerconnection.ice.default_address_only": True
                    限制 ICE 候选地址为默认接口，防止多宿主机器泄露额外 IP。
            这两项 prefs 通过 Camoufox config 字典的顶层键直接传入，
            与 Chromium 系的 --force-webrtc-ip-handling-policy 标志等效。

        Returns:
            Camoufox 格式配置字典；无需覆盖的字段留空由 Camoufox 自动随机化。
        """
        import os
        p   = self._profile
        cfg = self._stealth_config

        config: dict = {}

        # 屏幕尺寸：映射到 Camoufox screen 参数
        config["screen"] = {"width": p.screen_width, "height": p.screen_height}

        # Step 4：补全 docstring 承诺的 navigator 字段（与 FingerprintProfile 一致）
        config["navigator.languages"] = p.languages
        config["navigator.hardwareConcurrency"] = p.hardware_concurrency

        # Firefox 用户偏好：WebRTC 防护 + 权限预初始化（通过 user_prefs 注入）
        user_prefs: dict = {
            # WebRTC IP 泄漏防护（D1 网络层）
            "media.peerconnection.enabled":                  False,
            "media.peerconnection.ice.default_address_only": True,
            # 通知权限预置为 denied（消除"全新浏览器"特征，P2-05）
            "permissions.default.desktop-notification":      2,
        }
        config["user_prefs"] = user_prefs

        # 持久化档案目录（替代 Chromium 系的 user_data_dir）
        profile_path = cfg.user_data_dir.strip()
        if profile_path:
            os.makedirs(profile_path, exist_ok=True)
            config["profile"] = profile_path

        # camoufox_path：若 AppConfig 有自定义二进制路径则注入
        camoufox_path: str = getattr(self._app_config, "camoufox_path", "")
        if camoufox_path and os.path.isfile(camoufox_path):
            config["executable_path"] = camoufox_path

        return config
