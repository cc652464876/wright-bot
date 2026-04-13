"""
@Layer   : Engine 层（第三层 · 引擎基础设施）
@Role    : Playwright 浏览器上下文与页面生命周期管理
@Pattern : Context Manager（async with 资源保护） + Object Pool Pattern（页面池限速）
@Description:
    将 Playwright Browser / BrowserContext / Page 的创建与销毁封装为可复用组件，
    确保资源在任何异常情况下都能被正确释放（async with 协议）。
    BrowserContextManager 负责单个上下文的生命周期；
    PagePool 在此基础上实现异步信号量限速的页面对象池，
    防止并发场景下无限开 Tab 导致内存溢出。
    此模块不包含任何抓取业务逻辑，仅提供底层浏览器资源管理原语。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

if TYPE_CHECKING:
    from src.config.settings import AppConfig, StealthConfig


# ---------------------------------------------------------------------------
# 浏览器启动后端抽象基类（策略接口）
# ---------------------------------------------------------------------------

class AbstractBrowserBackend(ABC):
    """
    浏览器启动后端抽象基类（策略接口契约）。

    定义所有具体浏览器后端必须实现的标准生命周期接口，
    使 CrawleeEngineFactory 和 PagePool 完全面向接口编程，
    不感知底层是哪种浏览器或补丁方案。

    已有的具体实现（见 engine/anti_bot/stealth/）：
        PlaywrightBackend  : stealth_engine="chromium"，标准 Playwright Chromium（默认，无额外依赖）。
                             ⚠️ 旧键名 "playwright" 已于 V10.2 废弃，统一为 "chromium"。
        CamoufoxBackend    : stealth_engine="camoufox"，补丁版 Firefox（需安装 camoufox 包）。
        RebrowserBackend   : stealth_engine="rebrowser"，CDP 补丁版 Chromium（需安装 rebrowser-patches）。

    新增策略指引：
        1. 在 engine/anti_bot/stealth/ 下新建 xxx_backend.py。
        2. 继承本类并实现所有 @abstractmethod。
        3. 在 StealthConfig.stealth_engine 的 Literal 中追加新键名。
        4. 在 MasterDispatcher 后端工厂映射表中注册 新键名 → 新Backend类。
        ★ 全程不需要修改其他任何后端文件。

    Pattern: Strategy —— 每个具体子类代表一种物理隔离的浏览器伪装策略。
    """

    def __init__(self, stealth_config: "StealthConfig", app_config: "AppConfig") -> None:
        """
        统一注入反检测配置与应用级配置，供 BrowserFactory 以 ``type[AbstractBrowserBackend]``
        静态类型安全地实例化各后端；子类须 ``super().__init__(...)`` 后再初始化自身状态。
        """
        self._stealth_config = stealth_config
        self._app_config = app_config

    @abstractmethod
    async def __aenter__(self) -> "AbstractBrowserBackend":
        """
        启动浏览器进程并初始化 BrowserContext。
        返回 self，供 `async with backend as b: page = await b.new_page()` 链式调用。
        """
        ...

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """
        关闭 BrowserContext 和 Browser，释放所有底层资源。
        无论 with 块内是否抛出异常均须执行清理。
        """
        ...

    @abstractmethod
    async def new_page(self) -> Page:
        """
        在当前 BrowserContext 下创建并返回新 Page 实例。
        若后端尚未通过 __aenter__ 初始化则须抛出 RuntimeError。

        Returns:
            新建的 Playwright-compatible Page 实例。
        Raises:
            RuntimeError: 后端未初始化时抛出。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """
        显式关闭后端，与 __aexit__ 逻辑等价。
        供不使用 async with 语法的场景（如 try/finally 块）调用。
        """
        ...

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """
        True 表示 BrowserContext 已成功初始化且尚未关闭。
        供外部在调用 new_page() 前做前置检查。
        """
        ...

    def _apply_patches(self) -> None:
        """
        浏览器启动前的全局补丁注入钩子（可选实现）。

        AbstractBrowserBackend 提供空实现（非 abstractmethod），因为：
            - PlaywrightBackend / CamoufoxBackend 不需要启动前补丁。
            - RebrowserBackend 在 __aenter__ 中自行调用，不依赖此钩子。
            - CrawleeEngineFactory.create() 通过 duck typing 调用 getattr(_, "_apply_patches", None)
              兼容所有后端。

        若新增后端需要启动前全局补丁（如 monkey-patch 第三方库），在子类中 override 此方法。
        """
        pass


# ---------------------------------------------------------------------------
# 标准 Playwright 后端实现（AbstractBrowserBackend 的默认策略）
# ---------------------------------------------------------------------------

class BrowserContextManager(AbstractBrowserBackend):
    """
    AbstractBrowserBackend 的标准 Playwright Chromium 实现。

    通过 async_playwright() 启动 Chromium（或 Firefox / WebKit），
    将 StealthConfig 中的 headless / ignore_ssl / 视口参数注入浏览器启动参数，
    提供标准 async with 协议确保资源不泄漏。

    此类仅处理标准 Playwright 场景。若需 Camoufox 或 rebrowser，
    请使用 engine/anti_bot/stealth/ 下对应的后端类——
    两者实现相同的 AbstractBrowserBackend 接口，CrawleeEngineFactory 无感切换。

    Pattern: Context Manager（async __aenter__ / __aexit__） + Strategy（AbstractBrowserBackend 具体策略）
    """

    def __init__(self, stealth_config: "StealthConfig", app_config: "AppConfig") -> None:
        """
        Args:
            stealth_config: 来自 PrismSettings 的反检测配置块，
                            控制 headless / ignore_ssl / 视口大小等浏览器启动参数。
            app_config    : 应用级配置（chromium 路径解析等）。
        """
        super().__init__(stealth_config, app_config)
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def __aenter__(self) -> "BrowserContextManager":
        """
        启动 Playwright 进程、Browser 实例和 BrowserContext。

        Returns:
            已就绪的 BrowserContextManager 自身（可链式调用 .new_page()）。
        """
        app_cfg = self._app_config

        self._playwright = await async_playwright().start()

        launch_opts = self._build_launch_options(app_cfg)
        self._browser = await self._playwright.chromium.launch(**launch_opts)
        self._context = await self._browser.new_context(**self._build_context_options())
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """
        关闭 BrowserContext 和 Browser，停止 Playwright 进程。
        无论 with 块内是否抛出异常均执行清理。
        """
        await self.close()

    async def new_page(self) -> Page:
        """
        在当前 BrowserContext 下创建并返回新 Page 实例。
        若 Context 尚未启动则抛出 RuntimeError。

        Returns:
            新建的 Playwright Page 实例。
        Raises:
            RuntimeError: BrowserContext 尚未通过 __aenter__ 初始化。
        """
        if not self.is_ready:
            raise RuntimeError(
                "BrowserContextManager 尚未初始化，请先通过 async with 或 __aenter__() 启动。"
            )
        return await self._context.new_page()  # type: ignore[union-attr]

    async def close(self) -> None:
        """
        显式关闭 BrowserContext 和 Browser，释放所有底层资源。
        与 __aexit__ 逻辑相同，供不使用 async with 的场景调用。
        """
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
        """
        检查 BrowserContext 是否已成功初始化且未关闭。

        Returns:
            True 表示上下文可用，False 表示未初始化或已关闭。
        """
        return self._context is not None

    # ------------------------------------------------------------------
    # 私有：启动参数构建
    # ------------------------------------------------------------------

    def _build_launch_options(self, app_cfg: "Any") -> dict:
        """
        构建 playwright.chromium.launch() 所需参数字典。

        executable_path 解析（三级查找）：
        1. AppConfig.chromium_path 非空且文件存在 → 显式路径（最高优先级）。
        2. browser_cores/playwright 目录下存在 chrome.exe → 自动发现
           （由 PLAYWRIGHT_BROWSERS_PATH 环境变量定位，由 _init_browser_env() 设置）。
        3. 省略 → 由 Playwright 根据 PLAYWRIGHT_BROWSERS_PATH 环境变量自动查找。

        若首次运行且未手动下载浏览器，不传入 executable_path，
        Playwright 会根据 PLAYWRIGHT_BROWSERS_PATH 自动下载并缓存到 ./browser_cores/playwright。
        """
        import glob
        import os
        cfg = self._stealth_config
        window_mode = cfg.window_mode

        if window_mode == "headless":
            headless = True
            args_extra: List[str] = []
        elif window_mode == "minimized":
            headless = False
            args_extra = ["--start-minimized", "--window-position=-32000,-32000"]
        else:  # "normal"
            headless = False
            args_extra = []

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
        ] + args_extra

        opts: dict = {
            "headless": headless,
            "ignore_https_errors": cfg.ignore_ssl_error,
            "args": chromium_args,
        }

        # 三级路径解析（优先级从高到低）
        exe = self._resolve_executable_path(app_cfg)
        if exe:
            opts["executable_path"] = exe

        user_data_dir = cfg.user_data_dir.strip()
        if user_data_dir:
            os.makedirs(user_data_dir, exist_ok=True)
            opts["user_data_dir"] = user_data_dir

        return opts

    def _resolve_executable_path(self, app_cfg: "Any") -> Optional[str]:
        """
        按优先级解析 Chromium 可执行文件路径（三级查找）：

        1. AppConfig.chromium_path 非空且文件存在 → 显式路径（最高优先级）。
        2. browser_cores/playwright 目录下存在 chrome.exe → 自动发现
           （由 PLAYWRIGHT_BROWSERS_PATH 环境变量定位，由 _init_browser_env() 设置）。
        3. None → 由 Playwright 根据 PLAYWRIGHT_BROWSERS_PATH 自动查找。

        Returns:
            Chromium 可执行文件绝对路径；无本地二进制时返回 None。
        """
        import os

        chromium_path: str = getattr(app_cfg, "chromium_path", "")
        if chromium_path and os.path.isfile(chromium_path):
            return os.path.abspath(chromium_path)

        pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if pw_root:
            from src.utils.playwright_chromium_exe import find_chromium_executable_in_browsers_path

            found = find_chromium_executable_in_browsers_path(pw_root)
            if found:
                return found

        return None

    def _build_context_options(self) -> dict:
        """构建 browser.new_context() 所需参数字典。"""
        return {
            "viewport": {"width": 1280, "height": 800},
            "ignore_https_errors": self._stealth_config.ignore_ssl_error,
            "permissions": [],
        }


class PagePool:
    """
    Playwright 页面对象池（异步信号量限速）。

    通过 asyncio.Semaphore 控制同时打开的 Page 上限，
    防止高并发场景下无限创建 Tab 导致内存爆炸。
    Page 使用完毕后通过 release() 归还；若页面已损坏（is_closed）则关闭并补充新页面。

    Pattern: Object Pool Pattern（资源复用 + 数量上限）
    """

    def __init__(
        self,
        max_size: int,
        context_manager: BrowserContextManager,
    ) -> None:
        """
        Args:
            max_size: 池内最大并发 Page 数量（对应 ConcurrencySettings.max_concurrency）。
            context_manager: 已就绪的 BrowserContextManager 实例，用于创建新 Page。
        """
        if max_size < 1:
            raise ValueError(f"max_size 必须 >= 1，得到 {max_size}")
        self._max_size = max_size
        self._ctx = context_manager
        # 信号量：控制同时借出的 Page 数量上限
        self._semaphore = asyncio.Semaphore(max_size)
        # 空闲 Page 列表（归还但尚未被借用的 Page）
        self._available: List[Page] = []
        # 保护 _available 和 _total 的并发读-写序列
        self._lock = asyncio.Lock()
        # 当前池内总 Page 数（空闲 + 占用中）
        self._total: int = 0

    async def acquire(self) -> Page:
        """
        从池中获取一个可用 Page。
        若池内有空闲 Page 则直接返回；
        若池未满则新建 Page 加入池后返回；
        若池已满则阻塞等待其他协程调用 release()。

        Returns:
            可用的 Playwright Page 实例。
        """
        # 阻塞直到有"槽位"可用（信号量 > 0 代表尚有并发配额）
        await self._semaphore.acquire()

        async with self._lock:
            # 优先从空闲列表复用，跳过已关闭的损坏页面
            while self._available:
                page = self._available.pop()
                if not page.is_closed():
                    return page
                # 损坏页面：丢弃并减少总数，继续尝试下一个
                self._total -= 1

            # 空闲列表为空，新建一个 Page
            page = await self._ctx.new_page()
            self._total += 1
            return page

    async def release(self, page: Page) -> None:
        """
        将 Page 归还池中。
        若页面已关闭或出现异常（is_closed=True）则丢弃并创建新 Page 补充，
        保证池内 Page 数量始终稳定。

        Args:
            page: 待归还的 Playwright Page 实例。
        """
        if page.is_closed():
            # 损坏页面：补充一个新页面以维持池容量稳定
            async with self._lock:
                self._total -= 1
            try:
                replacement = await self._ctx.new_page()
                async with self._lock:
                    self._available.append(replacement)
                    self._total += 1
            except Exception:
                # 新建补充页面失败时直接放行（不补充），不吃掉调用方的信号量槽位
                pass
        else:
            # 健康页面：归还到空闲列表
            async with self._lock:
                self._available.append(page)

        # 无论页面是否健康，都归还信号量槽位
        self._semaphore.release()

    async def close_all(self) -> None:
        """
        关闭池内所有 Page 并清空池。
        通常在爬虫任务结束时（Runner.stop() 阶段）调用。
        """
        async with self._lock:
            pages = list(self._available)
            self._available.clear()
            self._total = 0

        for page in pages:
            if not page.is_closed():
                try:
                    await page.close()
                except Exception:
                    pass

    @property
    def available_count(self) -> int:
        """
        当前池内空闲（已归还未被占用）的 Page 数量。

        Returns:
            空闲 Page 数量整数。
        """
        return len(self._available)

    @property
    def total_count(self) -> int:
        """
        当前池内 Page 总数（空闲 + 占用中）。

        Returns:
            总 Page 数量整数。
        """
        return self._total
