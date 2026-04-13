"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : NEED_CLICK 分支的沙箱交互调度器（敢死队）
@Pattern : Chain of Responsibility（独立 action_handler 路由分支） +
           Command Pattern（handle_action 执行来自 Interactor 封装的 Request 命令）
@Description:
    ActionHandler 是请求处理管线中 NEED_CLICK 路由分支的专属处理节点，
    由 SiteCrawlStrategy 挂载到 Crawlee 的 router.handler('NEED_CLICK') 上。
    每个 NEED_CLICK Request 由 Interactor.trigger_download_buttons() 生成，
    携带 user_data.target_index（目标按钮索引）和 user_data.label='NEED_CLICK'。
    ActionHandler 从 user_data 中读取 target_index，重新锁定页面上的目标按钮，
    将点击 + 下载逻辑移交给 ActionDownloader 执行（单一职责原则）。
    整个执行过程包裹在 error_interceptor 中，异常不静默，而是上报并由 Crawlee 重试。
    Pattern: Chain of Responsibility（独立路由分支）+ Command（执行预封装的按钮命令）
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional, TYPE_CHECKING
from urllib.parse import urlparse

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.modules.site.handlers.interactor import Interactor
    from src.modules.site.handlers.action_downloader import ActionDownloader
    from src.modules.site.audit.error_registry import ErrorRegistry

_log = get_logger(__name__)


class ActionHandler:
    """
    NEED_CLICK 分支沙箱交互调度器（职责链独立节点）。

    职责链入口：handle_action() —— 由 SiteCrawlStrategy 挂载到 Crawlee router。
    """

    def __init__(
        self,
        interactor: "Interactor",
        action_downloader: "ActionDownloader",
        is_running: Callable[[], bool],
        record_interaction: Optional[Callable] = None,
    ) -> None:
        """
        Args:
            interactor        : Interactor 实例（用于战前清理 clear_cookie_banners）。
            action_downloader : ActionDownloader 实例（执行实际的点击+下载逻辑）。
            is_running        : 状态检查函数。
            record_interaction: 可选的审计埋点回调，记录按钮点击交互行为。
        """
        self._interactor = interactor
        self._action_downloader = action_downloader
        self._is_running = is_running
        self._record_interaction = record_interaction

    # ------------------------------------------------------------------
    # 公开接口（职责链节点入口）
    # ------------------------------------------------------------------

    async def handle_action(self, context: object) -> None:
        """
        NEED_CLICK 分支主处理函数（职责链入口）。

        执行流程：
        1. 检查 is_running()，False 则立即返回。
        2. 从 context.request.user_data 读取 target_index。
        3. 调用 interactor.clear_cookie_banners() 进行战前清理。
        4. 使用与 Interactor 完全一致的选择器重新锁定目标按钮（保证索引对齐）。
        5. 校验按钮索引合法性（target_index >= count 则记录警告并返回）。
        6. 触发 record_interaction 埋点（异步，await 同步生命周期，不用 create_task）。
        7. 调用 ActionDownloader.execute() 移交点击+下载逻辑。
        整个流程包裹在 error_interceptor 中，异常截图后重新抛出。

        Args:
            context: Crawlee PlaywrightCrawlingContext（NEED_CLICK 路由分支传入）。
        """
        if not self._is_running():
            return

        page = getattr(context, "page", None)
        if page is None:
            return

        request = getattr(context, "request", None)
        current_url: str = getattr(request, "url", "Unknown")
        user_data: dict = getattr(request, "user_data", {}) or {}
        target_index: int = int(user_data.get("target_index", 0))

        from src.modules.site.audit.error_registry import error_interceptor

        async with error_interceptor(page, current_url):
            # Step 3: 战前清理 Cookie 弹窗，防止遮罩层阻挡后续点击
            await self._interactor.clear_cookie_banners(page)

            # Step 4: 使用与 Interactor.trigger_download_buttons 完全一致的选择器
            # 重新锁定目标按钮，保证 target_index 与侦察阶段严格对应
            button_locators = self._build_button_locator(page)
            button_count = await button_locators.count()

            # Step 5: 索引越界保护
            if target_index >= button_count:
                _log.warning(
                    f"[ActionHandler] target_index={target_index} >= "
                    f"button_count={button_count}，页面 DOM 结构已变化，跳过"
                )
                return

            btn = button_locators.nth(target_index)

            # Step 6: 审计埋点（同步 await，保持与任务生命周期一致）
            if self._record_interaction:
                domain = urlparse(current_url).netloc or "unknown"
                await self._record_interaction(
                    domain,
                    {
                        "url": current_url,
                        "action": "download_button_click",
                        "description": f"btn[{target_index}] via NEED_CLICK",
                    },
                )

            # Step 7: 移交点击+下载逻辑给 ActionDownloader
            await self._action_downloader.execute(page, btn, current_url)

    # ------------------------------------------------------------------
    # 私有工具
    # ------------------------------------------------------------------

    def _build_button_locator(self, page: object) -> object:
        """
        使用与 Interactor.DOWNLOAD_BUTTON_SELECTORS 完全一致的选择器字符串
        创建 Playwright Locator，确保 target_index 与侦察兵扫描时的索引严格对应。

        Args:
            page: Playwright Page 实例。
        Returns:
            合并所有选择器的 Playwright Locator 对象。
        """
        # 运行时导入以获取 DOWNLOAD_BUTTON_SELECTORS 常量，避免模块级循环依赖
        from src.modules.site.handlers.interactor import Interactor
        combined_selector = ", ".join(Interactor.DOWNLOAD_BUTTON_SELECTORS)
        return page.locator(combined_selector)  # type: ignore[union-attr]
