"""
@Layer   : App 层（第五层 · 应用编排）
@Role    : 爬虫任务运行器（生命周期管理 + 优雅关闭）
@Pattern : Template Method Pattern（CrawlerRunner 定义骨架，子类实现差异化步骤）
@Description:
    CrawlerRunner 是抽象运行器基类，定义了"一次完整爬虫任务"的标准骨架：
    setup() → execute() → teardown()，子类只需覆盖差异化步骤。
    SiteRunner 和 SearchRunner 分别对应两条业务线路，
    持有对应的具体策略实例（SiteCrawlStrategy / SearchCrawlStrategy），
    负责在 asyncio 事件循环中调度策略的 run() 并处理优雅关闭（Graceful Shutdown）：
    - 等待下载队列清空（files_active == 0）
    - 等待 NetSniffer 异步队列 join()
    - 最后将 StateManager 推进到 STOPPED
    Runner 不包含任何抓取业务逻辑，只管"什么时候开始、什么时候停、怎么收尾"。
    Pattern: Template Method（run_task 骨架） + Facade（屏蔽 asyncio.Task 管理细节）
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from src.engine.state_manager import StateTransitionError
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import PrismSettings
    from src.engine.state_manager import CrawlerStateManager
    from src.modules.base_strategy import BaseCrawlStrategy
    from src.modules.site.strategy import SiteCrawlStrategy
    from src.modules.search.strategy import SearchCrawlStrategy
    from src.app.monitor import SiteMonitor


_logger = get_logger(__name__)

# 活跃下载排空的轮询间隔与最长等待时间（SiteRunner.teardown 使用）
_DRAIN_POLL_INTERVAL: float = 0.5     # 每次轮询间隔（秒）
_DRAIN_MAX_WAIT_SECS: float = 120.0   # 超过此时长时警告并放弃等待


# ---------------------------------------------------------------------------
# 模块级辅助：状态机终态驱动
# ---------------------------------------------------------------------------

async def _drive_to_terminal(
    state_manager: Optional["CrawlerStateManager"],
    reason: str = "",
) -> None:
    """
    将状态机从任意中间态安全驱动至终态（STOPPED 或 ERROR）。

    状态路径规则（与 VALID_TRANSITIONS 严格对齐）：
    - STOPPING              → STOPPED
    - RUNNING / BANNED / PAUSED → STOPPING → STOPPED
    - INITIALIZING / CHALLENGE  → ERROR → STOPPING → STOPPED
    - 已在 STOPPED / ERROR / IDLE 则立即返回（IDLE 表示任务从未启动）

    遇到非法转移时：`transition_to` 已在 state_manager 内记录 ERROR；
    此处再记 WARNING 说明竞态或 strategy 已自行收尾，不再向外抛。

    Args:
        state_manager: 共享的 CrawlerStateManager 实例；为 None 时直接返回。
        reason       : 转移原因描述（写入状态历史记录）。
    """
    if state_manager is None:
        return

    from src.engine.state_manager import CrawlerState

    current = state_manager.state

    # 已在终态或初始态（任务从未真正启动）——无需任何操作
    if current in (CrawlerState.STOPPED, CrawlerState.ERROR, CrawlerState.IDLE):
        return

    try:
        if current == CrawlerState.STOPPING:
            # 仅差最后一步
            await state_manager.transition_to(CrawlerState.STOPPED, reason)

        elif current in (
            CrawlerState.RUNNING,
            CrawlerState.BANNED,
            CrawlerState.PAUSED,
        ):
            # 这些状态可直接进入 STOPPING → STOPPED
            await state_manager.transition_to(CrawlerState.STOPPING, reason)
            await state_manager.transition_to(CrawlerState.STOPPED, reason)

        elif current in (CrawlerState.INITIALIZING, CrawlerState.CHALLENGE):
            # 只能先落到 ERROR，再经 STOPPING → STOPPED
            await state_manager.transition_to(CrawlerState.ERROR, reason)
            await state_manager.transition_to(CrawlerState.STOPPING, reason)
            await state_manager.transition_to(CrawlerState.STOPPED, reason)

    except StateTransitionError as exc:
        # transition_to 已在 state_manager 内打 ERROR 日志；此处说明竞态或 strategy 已收尾
        _logger.warning(
            "[Runner] _drive_to_terminal 遇非法转移（通常已由 strategy 推进终态）: {}",
            exc,
        )


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class CrawlerRunner(ABC):
    """
    爬虫任务运行器抽象基类。

    标准骨架（Template Method）：
    run_task() → setup() → execute() → teardown()

    子类必须实现：execute()（核心任务逻辑）
    子类可覆盖：setup() / teardown()（差异化的初始化与收尾）

    Pattern: Template Method Pattern
    """

    def __init__(
        self,
        strategy: "BaseCrawlStrategy",
        state_manager: "CrawlerStateManager",
        log_callback: Optional[Callable] = None,
    ) -> None:
        """
        Args:
            strategy     : 具体策略实例（SiteCrawlStrategy 或 SearchCrawlStrategy）。
            state_manager: 共享的 CrawlerStateManager 实例（由 Dispatcher 传入）。
            log_callback : 日志转发回调，签名 (message: str, level: str) -> None。
        """
        self._strategy    = strategy
        self._state_manager = state_manager
        self._log_callback  = log_callback

    # ------------------------------------------------------------------
    # 模板骨架（Template Method）
    # ------------------------------------------------------------------

    async def run_task(self) -> None:
        """
        任务运行骨架（模板方法，子类不应覆盖此方法）。
        顺序调用 setup() → execute() → teardown()，
        确保无论 execute() 成功还是抛出异常，teardown() 都会执行。
        """
        try:
            await self.setup()
            await self.execute()
        finally:
            # teardown() 必须在任意结果下执行：负责资源释放与状态机收尾
            await self.teardown()

    # ------------------------------------------------------------------
    # 抽象方法：子类必须实现
    # ------------------------------------------------------------------

    @abstractmethod
    async def execute(self) -> None:
        """
        核心任务执行（子类必须实现）。
        调用 strategy.run()，处理运行期间的异常，
        并在适当时机通知 state_manager 进行状态转移。
        """
        pass

    # ------------------------------------------------------------------
    # 可覆盖方法：子类按需实现
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """
        任务前置初始化（子类可覆盖）。
        默认实现：校验 strategy.validate()，失败时抛出 ValueError。
        子类可在此阶段初始化数据库表、备份配置文件等。
        """
        if not self._strategy.validate():
            raise ValueError(
                f"[{self._strategy.get_strategy_name()}] 任务参数校验失败，"
                "请检查配置后重试"
            )

    async def teardown(self) -> None:
        """
        任务收尾与资源释放（子类可覆盖）。
        默认实现：调用 strategy.cleanup()。
        子类可在此阶段导出报告、通知 UI 任务结束等。
        """
        try:
            await self._strategy.cleanup()
        except Exception as exc:
            self._log(f"strategy.cleanup() 异常（已忽略）: {exc!r}", "error")

    # ------------------------------------------------------------------
    # 公开：生命周期控制
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """
        外部发出停止信号（非阻塞）。
        调用 strategy.stop() 设置内部标志；
        execute() 中的循环应在下次检查时优雅退出。
        """
        self._strategy.stop()

    def get_dashboard_data(self) -> Dict[str, Any]:
        """
        委托给 strategy.get_dashboard_data() 返回实时仪表盘数据。

        Returns:
            包含统计字段的字典（由具体策略实现）。
        """
        try:
            return self._strategy.get_dashboard_data()
        except Exception:
            # 防御：strategy 异常不应影响 UI 轮询，返回空模板
            from src.app.monitor import SiteMonitor
            return dict(SiteMonitor.DEFAULT_SNAPSHOT)

    def _log(self, message: str, level: str = "info") -> None:
        """
        内部日志工具：同时写入 loguru 日志文件和可选的 UI 回调。

        Args:
            message: 日志消息。
            level  : 日志级别（'info' / 'warning' / 'error' / 'success'）。
        """
        # 写入模块级 loguru logger（持久化到日志文件）
        log_fn = getattr(_logger, level, _logger.info)
        try:
            log_fn(message)
        except Exception:
            _logger.info(message)

        # 转发给 UI 回调（若已注入）
        if self._log_callback is not None:
            try:
                self._log_callback(message, level)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 具体运行器：Site 线路
# ---------------------------------------------------------------------------

class SiteRunner(CrawlerRunner):
    """
    网站批量抓取任务运行器（Site 线路）。

    在 execute() 中调用 SiteCrawlStrategy.run()，
    并在 teardown() 中执行 Site 专属的 Graceful Shutdown：
    1. 轮询 strategy.files_active == 0（等待下载队列清空）。
    2. 通知 NetSniffer 队列 join()（等待审计数据落盘）。

    Pattern: Template Method（具体化 execute / teardown）
    """

    def __init__(
        self,
        strategy: "SiteCrawlStrategy",
        state_manager: "CrawlerStateManager",
        monitor: Optional["SiteMonitor"] = None,
        log_callback: Optional[Callable] = None,
    ) -> None:
        """
        Args:
            strategy     : SiteCrawlStrategy 实例。
            state_manager: 共享状态机实例。
            monitor      : 可选的 SiteMonitor 实例（提供冻结快照能力）。
            log_callback : 日志回调。
        """
        super().__init__(strategy, state_manager, log_callback)
        self._monitor = monitor

    async def setup(self) -> None:
        """
        Site 线路前置初始化。
        1. 清空 SiteMonitor 的旧任务冻结快照，防止脏数据污染新任务仪表盘。
        2. 调用父类 setup() 触发 strategy.validate() 校验。
        """
        if self._monitor is not None:
            self._monitor.reset()
        await super().setup()

    async def execute(self) -> None:
        """
        Site 线路核心执行。
        1. state_manager → RUNNING。
        2. 调用 strategy.run()，捕获并上报顶层异常。
        3. 爬虫引擎返回后执行 Graceful Shutdown 等待序列。
        """
        self._log(f"[SiteRunner] 任务启动: {self._strategy.get_strategy_name()}")
        try:
            # strategy.run() 内部负责 IDLE → INITIALIZING → RUNNING 的状态流转；
            # 正常结束时策略自行推进至 STOPPING → STOPPED。
            await self._strategy.run()
            self._log(
                f"[SiteRunner] strategy.run() 正常返回",
                "success",
            )
        except Exception as exc:
            self._log(
                f"[SiteRunner] strategy.run() 抛出顶层异常: {exc!r}",
                "error",
            )
            # 异常路径：strategy 内部可能已转入 ERROR，也可能停留在中间态，
            # 统一尝试驱动到终态（_drive_to_terminal 内部静默忽略竞态冲突）
            await _drive_to_terminal(
                self._state_manager,
                f"SiteRunner.execute() 捕获顶层异常: {exc!r}",
            )
            # 重新抛出，让 run_task() 的 finally 触发 teardown()，
            # 同时将异常向上传递给 Dispatcher 做日志记录与清理
            raise

    async def teardown(self) -> None:
        """
        Site 线路收尾：
        1. 轮询 strategy.files_active，每 500ms 检查一次，直到活跃下载归零。
        2. 调用父类 teardown()（strategy.cleanup()）。
        3. state_manager → STOPPED。
        """
        # ── Step 1: 等待活跃下载归零（最长 _DRAIN_MAX_WAIT_SECS 秒）──────
        await self._drain_active_downloads()

        # ── Step 2: 调用 strategy.cleanup()（via 父类 teardown）─────────
        await super().teardown()

        # ── Step 3: 确保状态机落到 STOPPED 终态 ──────────────────────────
        # strategy.run() 正常结束时已自行完成转移，此调用为异常路径的兜底保障
        await _drive_to_terminal(
            self._state_manager,
            "SiteRunner.teardown() 完成",
        )

        # ── Step 4: 向 monitor 写入最终快照 ──────────────────────────────
        # 以 is_running=False 调用 monitor，触发 strategy 最终统计字段的一次性刷新；
        # Crawlee 冻结快照保留自执行期间最后一次 is_running=True 时的数据。
        if self._monitor is not None:
            try:
                self._monitor.get_dashboard_data(
                    is_running=False,
                    crawler=None,
                    strategy=self._strategy,
                )
            except Exception:
                pass

        # ── 记录最终统计摘要（便于日志追溯） ─────────────────────────────
        try:
            final = self._strategy.get_dashboard_data()
            self._log(
                f"[SiteRunner] 任务结束 — "
                f"已发现: {final.get('files_found', 0)}, "
                f"已下载: {final.get('files_downloaded', 0)}, "
                f"已爬取页面: {final.get('scraped_count', 0)}, "
                f"终态: {final.get('state', 'unknown')}",
                "info",
            )
        except Exception:
            pass

    async def _drain_active_downloads(self) -> None:
        """
        轮询 strategy.files_active，每 _DRAIN_POLL_INTERVAL 秒检查一次，
        直到活跃下载任务归零或超过 _DRAIN_MAX_WAIT_SECS 为止。
        超时时记录警告并继续收尾（不阻塞 teardown 后续步骤）。
        """
        waited = 0.0
        while getattr(self._strategy, "files_active", 0) > 0:
            if waited >= _DRAIN_MAX_WAIT_SECS:
                remaining = getattr(self._strategy, "files_active", 0)
                self._log(
                    f"[SiteRunner] 活跃下载排空超时（{_DRAIN_MAX_WAIT_SECS:.0f}s），"
                    f"剩余 {remaining} 个任务，继续收尾流程",
                    "warning",
                )
                break
            await asyncio.sleep(_DRAIN_POLL_INTERVAL)
            waited += _DRAIN_POLL_INTERVAL


# ---------------------------------------------------------------------------
# 具体运行器：Search 线路
# ---------------------------------------------------------------------------

class SearchRunner(CrawlerRunner):
    """
    搜索引擎抓取任务运行器（Search 线路，占位骨架）。

    当前仅提供最小实现，与 SiteRunner 共享 CrawlerRunner 骨架。
    未来实现 SearchCrawlStrategy 后，在此处添加搜索专属的 Graceful Shutdown 逻辑。

    Pattern: Template Method（具体化 execute）
    """

    def __init__(
        self,
        strategy: "SearchCrawlStrategy",
        state_manager: "CrawlerStateManager",
        log_callback: Optional[Callable] = None,
    ) -> None:
        """
        Args:
            strategy     : SearchCrawlStrategy 实例。
            state_manager: 共享状态机实例。
            log_callback : 日志回调。
        """
        super().__init__(strategy, state_manager, log_callback)

    async def execute(self) -> None:
        """
        Search 线路核心执行（占位）。
        调用 strategy.run()，捕获顶层异常。
        """
        self._log(f"[SearchRunner] 任务启动: {self._strategy.get_strategy_name()}")
        try:
            # SearchCrawlStrategy.run() 内部负责完整的状态机流转；
            # 此处仅提供顶层异常捕获与终态保障兜底。
            await self._strategy.run()
            self._log(
                f"[SearchRunner] strategy.run() 正常返回",
                "success",
            )
        except Exception as exc:
            self._log(
                f"[SearchRunner] strategy.run() 顶层异常: {exc!r}",
                "error",
            )
            # 确保状态机从任意中间态落到终态，再向上传播异常
            await _drive_to_terminal(
                self._state_manager,
                f"SearchRunner.execute() 捕获顶层异常: {exc!r}",
            )
            raise
