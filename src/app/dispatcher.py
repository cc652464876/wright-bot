"""
@Layer   : App 层（第五层 · 应用编排）
@Role    : 策略模式 Context —— 任务路由与动态策略切换
@Pattern : Strategy Pattern Context（持有 BaseCrawlStrategy 引用，运行时动态替换）
           + Chain of Responsibility（异常驱动的策略降级链）
@Description:
    MasterDispatcher 是整个应用的总指挥，在策略模式中扮演 Context 角色：
    1. 接收来自 bridge.py（UI 层）的原始 JSON 配置字典。
    2. 调用 update_settings() 刷新全局 PrismSettings 单例。
    3. 根据 task_info.mode（'site' / 'search'）实例化对应的具体策略
       （SiteCrawlStrategy / SearchCrawlStrategy），并注入 CrawlerStateManager。
    4. 持有 CrawlerStateManager 监听器，在 BANNED / CHALLENGE 等异常状态下
       动态切换策略或触发 anti_bot 工具箱（ProxyRotator / ChallengeSolver）——
       这是"明确调度器职责"的核心体现。
    5. 向上（bridge.py）透传仪表盘数据和生命周期控制指令（stop / get_dashboard_data）。

    Pattern: Strategy Context —— 面向 BaseCrawlStrategy 接口编程，
             不直接依赖任何具体策略实现类，可在运行时无缝切换。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from src.engine.anti_bot.challenge_solver import ChallengeSolver
from src.engine.anti_bot.proxy_rotator import ProxyRotator
from src.engine.state_manager import CrawlerState, CrawlerStateManager
from src.modules.base_strategy import BaseCrawlStrategy
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import PrismSettings
    from src.app.runner import CrawlerRunner
    from src.app.monitor import SiteMonitor


_logger = get_logger(__name__)


class MasterDispatcher:
    """
    任务路由总指挥（策略模式 Context）。

    生命周期：
    1. run_with_config(config_dict) —— 解析配置 → 选择策略 → 执行。
    2. stop()                       —— 向当前策略发送停止信号。
    3. get_dashboard_data()         —— 从当前策略获取 UI 仪表盘数据。

    状态机联动：
    CrawlerStateManager 注册监听器 _on_state_change()，
    在 CHALLENGE / BANNED 状态时自动触发对应的 anti_bot 响应策略。

    Pattern: Strategy Pattern Context
    """

    def __init__(self, log_callback: Optional[Callable] = None) -> None:
        """
        Args:
            log_callback: 可选的日志回调函数，签名 (message: str, level: str) -> None。
                          由 bridge.py 注入，用于将日志转发到 UI 控制台。
        """
        self._log_callback = log_callback

        # 单一共享状态机实例：每次新任务前通过 reset() 清空历史状态和监听器
        self._state_manager: CrawlerStateManager = CrawlerStateManager()

        # 当前活跃的策略与运行器（同一时刻最多一个任务）
        self._strategy: Optional[BaseCrawlStrategy] = None
        self._runner:   Optional["CrawlerRunner"]   = None

        # SiteMonitor 懒加载单例：跨任务保持冻结快照（任务结束后 UI 仍能显示末态数据）
        self._monitor: Optional["SiteMonitor"] = None

        # 反爬工具箱：默认装配空代理池 + 与挑战页联动的 ChallengeSolver；
        # configure_anti_bot() 可在运行前整体替换实例。
        self._proxy_rotator: ProxyRotator = ProxyRotator()
        self._challenge_solver: ChallengeSolver = ChallengeSolver(
            state_manager=self._state_manager,
        )

        # 执行互斥标志：防止同一时刻多个任务并发运行
        self._running: bool = False

    # ------------------------------------------------------------------
    # 公开：生命周期控制
    # ------------------------------------------------------------------

    async def run_with_config(self, config_dict: dict) -> None:
        """
        统一任务入口：解析配置 → 刷新全局设置 → 选择策略 → 校验 → 执行。

        流程：
        1. 调用 update_settings(config_dict) 刷新 PrismSettings 单例。
        2. 调用 _select_strategy() 根据 task_info.mode 实例化具体策略。
        3. 调用 _strategy.validate()，校验失败时向 UI 报错并返回。
        4. 向 state_manager 注册 _on_state_change 监听器。
        5. 调用 _strategy.run()，捕获顶层异常写日志后清理。
        6. finally 调用 _strategy.cleanup()。

        Args:
            config_dict: 来自 UI 的原始 JSON 配置字典（ui-config.js 格式）。
        """
        # 互斥保护：同一时刻只允许一个任务运行
        if self._running:
            self._log(
                "[Dispatcher] 已有任务正在运行，请先停止当前任务再启动新任务",
                "warning",
            )
            return

        self._running = True
        try:
            await self._execute_task(config_dict)
        finally:
            # 无论任务如何结束，确保引用被清空，为下一次任务做准备
            self._strategy = None
            self._runner   = None
            self._running  = False

    async def stop(self) -> None:
        """
        向当前正在运行的策略发送停止信号（非阻塞）。
        若无活跃任务则静默返回。
        """
        runner = self._runner
        if runner is None:
            self._log("[Dispatcher] stop() 调用时无活跃任务，已忽略", "warning")
            return

        self._log("[Dispatcher] 已收到停止请求，正在通知 Runner 优雅退出…")
        # runner.stop() 设置 strategy._is_running = False（非阻塞信号）；
        # 策略内部的 graceful drain 将在下次 poll 时自然结束。
        await runner.stop()

    def get_dashboard_data(self) -> Dict[str, Any]:
        """
        从当前活跃策略获取实时仪表盘数据快照，供 UI 轮询。
        若无活跃策略则返回全零默认模板。

        Returns:
            包含 requests_total / files_found / files_downloaded / state 等字段的字典。
        """
        runner = self._runner
        if runner is not None:
            return runner.get_dashboard_data()

        # 无活跃任务：返回带 'idle' 状态的全零模板，防止 UI 轮询时崩溃
        from src.app.monitor import SiteMonitor
        return dict(SiteMonitor.DEFAULT_SNAPSHOT)

    def is_task_running(self) -> bool:
        """供 UI 心跳（bridge.get_status）查询是否有任务占用调度器。"""
        return self._running

    def is_canary_run_active(self) -> bool:
        """当前调度器上的策略是否为金丝雀合成任务（用于看板 system_state=running）。"""
        strat = self._strategy
        return bool(getattr(strat, "is_canary_strategy", False))

    # ------------------------------------------------------------------
    # 公开：反爬工具箱注入（可选，运行前配置）
    # ------------------------------------------------------------------

    def configure_anti_bot(
        self,
        proxy_rotator: Optional[ProxyRotator] = None,
        challenge_solver: Optional[ChallengeSolver] = None,
    ) -> None:
        """
        注入反爬工具箱实例，供策略与状态机使用。
        传入 None 表示保留该槽位已有实例；若需清空代理池可传入空的 ProxyRotator()。

        Args:
            proxy_rotator   : 代理池；非 None 时替换默认实例。
            challenge_solver: 挑战解决器；非 None 时替换默认实例（建议仍绑定本调度器 state_manager）。
        """
        if proxy_rotator is not None:
            self._proxy_rotator = proxy_rotator
        if challenge_solver is not None:
            self._challenge_solver = challenge_solver
        self._log(
            f"[Dispatcher] 反爬工具箱已更新 — "
            f"proxy_rotator 节点数={self._proxy_rotator.count}, "
            f"challenge_solver={'✓' if self._challenge_solver else '✗'}"
        )

    # ------------------------------------------------------------------
    # 私有：策略选择与实例化
    # ------------------------------------------------------------------

    def _select_strategy(self, settings: "PrismSettings") -> BaseCrawlStrategy:
        """
        根据 settings.task_info.mode 工厂化创建具体策略实例。
        - 'site'   → SiteCrawlStrategy(settings, state_manager)
        - 'search' → SearchCrawlStrategy(settings, state_manager)
        - 未知模式 → 抛出 ValueError

        Args:
            settings: 已刷新的全局 PrismSettings 单例。
        Returns:
            BaseCrawlStrategy 具体实现实例。
        Raises:
            ValueError: 遇到未知 task_info.mode 时抛出。
        """
        mode = settings.task_info.mode

        if mode == "site":
            if settings.task_info.is_canary:
                from src.modules.canary.strategy import CanaryMockStrategy

                return CanaryMockStrategy(
                    settings,
                    self._state_manager,
                    proxy_rotator=self._proxy_rotator,
                    challenge_solver=self._challenge_solver,
                )
            from src.modules.site.strategy import SiteCrawlStrategy

            return SiteCrawlStrategy(
                settings,
                self._state_manager,
                proxy_rotator=self._proxy_rotator,
                challenge_solver=self._challenge_solver,
            )

        if mode == "search":
            from src.modules.search.strategy import SearchCrawlStrategy
            return SearchCrawlStrategy(
                settings,
                self._state_manager,
                proxy_rotator=self._proxy_rotator,
                challenge_solver=self._challenge_solver,
            )

        raise ValueError(
            f"未知任务模式: {mode!r}（支持: 'site' / 'search'）"
        )

    def _create_runner(self, mode: str) -> "CrawlerRunner":
        """
        Runner 工厂：根据任务模式创建对应的 Runner 实例。

        - 'site'   → SiteRunner（含 SiteMonitor 注入）
        - 'search' → SearchRunner

        self._strategy 与 self._monitor 必须在调用此方法前已完成初始化。

        Args:
            mode: 任务模式字符串（'site' / 'search'）。
        Returns:
            CrawlerRunner 具体子类实例。
        Raises:
            ValueError: 遇到未知模式时抛出。
        """
        from src.app.runner import SiteRunner, SearchRunner
        from src.modules.site.strategy import SiteCrawlStrategy
        from src.modules.search.strategy import SearchCrawlStrategy

        if self._strategy is None:
            raise RuntimeError("策略未初始化：_create_runner 仅在 _select_strategy 成功之后调用")

        if mode == "site":
            assert isinstance(self._strategy, SiteCrawlStrategy)
            return SiteRunner(
                strategy=self._strategy,
                state_manager=self._state_manager,
                monitor=self._monitor,
                log_callback=self._log,
            )

        if mode == "search":
            assert isinstance(self._strategy, SearchCrawlStrategy)
            return SearchRunner(
                strategy=self._strategy,
                state_manager=self._state_manager,
                log_callback=self._log,
            )

        raise ValueError(f"未知任务模式: {mode!r}")

    # ------------------------------------------------------------------
    # 私有：任务执行主体
    # ------------------------------------------------------------------

    async def _execute_task(self, config_dict: dict) -> None:
        """
        完整任务生命周期执行体（由 run_with_config() 调用）。

        步骤：
        1. 解析并刷新 PrismSettings 全局单例。
        2. 重置状态机，清空上次任务的状态历史与监听器。
        3. 工厂化创建具体策略（_select_strategy）。
        4. 注册状态变更监听器，激活 BANNED / CHALLENGE 自动响应。
        5. 懒加载 SiteMonitor 单例（仅 site 模式需要）。
        6. Runner 工厂创建运行器（_create_runner），注入策略 / 状态机 / Monitor。
        7. 调用 runner.run_task()，捕获顶层异常记录日志（teardown 由 runner 保证）。
        """
        from src.config.settings import update_settings
        from src.app.monitor import SiteMonitor

        # ── Step 1: 刷新 PrismSettings 单例 ───────────────────────────
        try:
            settings = update_settings(config_dict)
        except Exception as exc:
            self._log(f"[Dispatcher] 配置解析失败（Pydantic 校验错误）: {exc!r}", "error")
            return

        mode     = settings.task_info.mode
        strategy = settings.strategy_settings.crawl_strategy
        conc     = settings.get_effective_max_concurrency()
        self._log(
            f"[Dispatcher] 任务启动 — mode={mode!r}, "
            f"strategy={strategy!r}, max_concurrency={conc}"
        )

        # ── Step 2: 重置状态机（清空历史状态 + 监听器列表） ────────────
        await self._state_manager.reset()

        # ── Step 3: 策略工厂 —— 根据 mode 创建具体策略，注入状态机 ────
        try:
            self._strategy = self._select_strategy(settings)
        except ValueError as exc:
            self._log(f"[Dispatcher] 策略选择失败: {exc}", "error")
            return

        # ── Step 4: 注册状态变更监听器（BANNED / CHALLENGE 自动响应） ─
        self._state_manager.register_listener(self._on_state_change)

        # ── Step 5: 懒加载 SiteMonitor（跨任务保留冻结快照能力） ───────
        if self._monitor is None:
            self._monitor = SiteMonitor()

        # ── Step 6: Runner 工厂 —— 注入策略、状态机、Monitor、日志回调 ─
        try:
            self._runner = self._create_runner(mode)
        except ValueError as exc:
            self._log(f"[Dispatcher] Runner 创建失败: {exc}", "error")
            return

        # ── Step 7: 执行任务，顶层异常仅记录（teardown 由 runner 保证） ─
        try:
            await self._runner.run_task()
        except Exception as exc:
            # run_task() 内部的 finally 已确保 teardown() 被调用；
            # 此处只负责将顶层异常写入日志，不做额外资源清理。
            self._log(f"[Dispatcher] run_task() 顶层异常（已收尾）: {exc!r}", "error")

    # ------------------------------------------------------------------
    # 私有：状态机监听器（策略动态切换核心）
    # ------------------------------------------------------------------

    def _on_state_change(
        self,
        old_state: CrawlerState,
        new_state: CrawlerState,
        reason: str,
    ) -> None:
        """
        CrawlerStateManager 状态变更监听器（Observer 回调）。
        根据新状态触发对应的 anti_bot 响应：
        - CHALLENGE → 调用 challenge_solver.solve()（若已配置）。
        - BANNED    → 调用 proxy_rotator.get_next() 切换代理（若已配置）。
        - ERROR     → 记录致命错误日志，触发 cleanup。
        - STOPPED   → 清除当前策略引用，准备接受下一个任务。

        Args:
            old_state: 转移前的状态。
            new_state: 转移后的状态。
            reason   : 转移原因描述。
        """
        label = f"（{reason}）" if reason else ""
        self._log(f"[状态机] {old_state.name} → {new_state.name}{label}")

        # ── CHALLENGE：由 Handler 内 ChallengeSolver.solve() 驱动状态机至此 ─
        if new_state == CrawlerState.CHALLENGE:
            self._log(
                "[Dispatcher] 状态机已进入 CHALLENGE（由页面处理器调用 ChallengeSolver）",
                "warning",
            )

        # ── BANNED：IP 被封禁，触发代理轮换 ───────────────────────────
        elif new_state == CrawlerState.BANNED:
            if self._proxy_rotator is not None:
                proxy_count = len(getattr(self._proxy_rotator, "_proxies", []))
                if proxy_count > 0:
                    self._log(
                        f"[Dispatcher] IP 封禁，代理池中有 {proxy_count} 个节点；"
                        "Crawlee SessionPool 将在下次请求时自动切换代理",
                        "warning",
                    )
                else:
                    self._log(
                        "[Dispatcher] IP 封禁，代理池为空，建议手动添加代理后重试",
                        "error",
                    )
            else:
                self._log(
                    "[Dispatcher] IP 封禁，未配置 ProxyRotator；"
                    "考虑通过 configure_anti_bot() 注入代理池",
                    "error",
                )

        # ── ERROR：不可恢复错误，记录并等待 teardown 完成 ──────────────
        elif new_state == CrawlerState.ERROR:
            self._log(
                f"[Dispatcher] 任务遭遇不可恢复错误: {reason}",
                "error",
            )

        # ── STOPPED：任务已彻底完成，清理策略引用 ─────────────────────
        elif new_state == CrawlerState.STOPPED:
            self._log("[Dispatcher] 任务已完成，状态机归位 STOPPED", "success")
            # 注意：_strategy / _runner 的清空在 run_with_config() 的 finally 中完成，
            # 不在此处操作，避免与 teardown() 中正在进行的 cleanup() 竞态。

    def _log(self, message: str, level: str = "info") -> None:
        """
        内部日志工具：优先调用 log_callback，无回调时回退到 print。
        同时持久化到 loguru 日志文件（不受回调是否配置的影响）。

        Args:
            message: 日志消息字符串。
            level  : 日志级别字符串（'info' / 'warning' / 'error' / 'success'）。
        """
        # 持久化到 loguru 日志文件（无论是否有 UI 回调）
        log_fn = getattr(_logger, level, _logger.info)
        try:
            log_fn(message)
        except Exception:
            _logger.info(message)

        # 转发到 UI 回调（若已注入）；否则 print 到标准输出（开发期可见性）
        if self._log_callback is not None:
            try:
                self._log_callback(message, level)
            except Exception:
                pass
        else:
            print(f"[{level.upper()}] {message}")
