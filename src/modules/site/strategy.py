"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 网站批量抓取具体策略（主线路总装配器）
@Pattern : Strategy Pattern（具体策略） + Facade（组装并隐藏子组件复杂度）
@Description:
    SiteCrawlStrategy 是当前项目的主要业务策略，对应 task_info.mode == 'site'。
    作为 Facade，负责将 site/ 包下的所有子组件（generator、parser、audit_center、
    error_registry 以及 handlers 管线中的六个 handler）组装为一条完整的抓取流水线，
    并对外只暴露 BaseCrawlStrategy 定义的三个生命周期接口（validate / run / cleanup）。
    具体的抓取逻辑分散在各子组件中，SiteCrawlStrategy 本身只负责"导演"角色，
    不包含任何网络请求或文件 I/O 的直接实现。
    Pattern: Strategy（继承基类） + Facade（组装子组件） + Composite（管线编排）
"""

from __future__ import annotations

import asyncio
import datetime
import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from src.modules.base_strategy import BaseCrawlStrategy
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import PrismSettings
    from src.engine.anti_bot.challenge_solver import ChallengeSolver
    from src.engine.anti_bot.proxy_rotator import ProxyRotator
    from src.modules.site.generator import SiteUrlGenerator
    from src.modules.site.parser import SiteDataParser
    from src.modules.site.audit.audit_center import SiteAuditCenter
    from src.modules.site.audit.error_registry import ErrorRegistry
    from src.modules.site.handlers.net_sniffer import NetSniffer
    from src.modules.site.handlers.downloader import Downloader
    from src.modules.site.handlers.interactor import Interactor
    from src.modules.site.handlers.strategist import Strategist
    from src.modules.site.handlers.action import ActionHandler
    from src.modules.site.handlers.action_downloader import ActionDownloader
    from src.engine.crawlee_engine import CrawleeEngineFactory
    from src.engine.state_manager import CrawlerStateManager

_log = get_logger(__name__)

# 合法的 site 类型爬取策略标识符
_VALID_SITE_STRATEGIES = frozenset({"direct", "full", "sitemap"})

# graceful shutdown 轮询间隔（秒）和最大等待时间（秒）
_DRAIN_POLL_INTERVAL = 1.0
_DRAIN_MAX_WAIT_SECS = 120


class SiteCrawlStrategy(BaseCrawlStrategy):
    """
    网站批量抓取具体策略（主线路）。

    生命周期：
    1. validate()  : 检查 target_urls 非空、crawl_strategy 为 site 类型。
    2. run()       : 按 direct / full / sitemap 模式生成种子 → 启动 Crawlee 引擎
                     → 挂载请求处理管线 → 等待完成 → 收尾。
    3. cleanup()   : 导出审计报告、等待下载队列清空、关闭数据库连接。

    Pattern: Strategy Pattern + Facade
    """

    def __init__(
        self,
        settings: "PrismSettings",
        state_manager: Optional["CrawlerStateManager"] = None,
        proxy_rotator: Optional["ProxyRotator"] = None,
        challenge_solver: Optional["ChallengeSolver"] = None,
    ) -> None:
        """
        Args:
            settings         : 全局运行时参数单例。
            state_manager    : 可选的状态机实例；提供时在关键节点驱动状态转移。
            proxy_rotator    : 可选；注入 CrawleeEngineFactory 以生成 ProxyConfiguration。
            challenge_solver: 可选；在 Playwright 页面处理器中执行挑战检测与 solve()。
        """
        super().__init__(settings)
        self._state_manager = state_manager
        self._proxy_rotator = proxy_rotator
        self._challenge_solver = challenge_solver

        # 子组件引用（由 _assemble_pipeline 延迟初始化）
        self._audit_center: Optional["SiteAuditCenter"] = None
        self._error_registry: Optional["ErrorRegistry"] = None
        self._generator: Optional["SiteUrlGenerator"] = None
        self._parser: Optional["SiteDataParser"] = None
        self._net_sniffer: Optional["NetSniffer"] = None
        self._downloader: Optional["Downloader"] = None
        self._interactor: Optional["Interactor"] = None
        self._strategist: Optional["Strategist"] = None
        self._action_handler: Optional["ActionHandler"] = None
        self._action_downloader: Optional["ActionDownloader"] = None

        # 目标扩展名缓存（_assemble_pipeline 中设置）
        self._target_ext: str = ".pdf"

    # ------------------------------------------------------------------
    # 实现抽象方法
    # ------------------------------------------------------------------

    def validate(self) -> bool:
        """
        校验 site 任务所需的必要参数。
        检查项：
        - settings.strategy_settings.target_urls 非空。
        - settings.strategy_settings.crawl_strategy 为 site 类型
          （'direct' / 'full' / 'sitemap'）。
        - settings.task_info.save_directory 可写（目录不存在时尝试创建）。

        Returns:
            True 表示参数合法；False 表示校验失败。
        """
        strat = self.settings.strategy_settings
        task = self.settings.task_info

        if not strat.target_urls:
            _log.error("[SiteCrawlStrategy.validate] target_urls 为空，任务无法启动")
            return False

        if strat.crawl_strategy not in _VALID_SITE_STRATEGIES:
            _log.error(
                f"[SiteCrawlStrategy.validate] crawl_strategy={strat.crawl_strategy!r} "
                f"不是合法的 site 类型（允许值: {_VALID_SITE_STRATEGIES}）"
            )
            return False

        try:
            os.makedirs(task.save_directory, exist_ok=True)
        except Exception as exc:
            _log.error(
                f"[SiteCrawlStrategy.validate] save_directory={task.save_directory!r} "
                f"不可写: {exc!r}"
            )
            return False

        return True

    async def _fsm_transition(
        self,
        new_state: "CrawlerState",
        reason: str,
        *,
        swallow: bool = False,
    ) -> None:
        """
        统一包装状态转移：非法转移时 state_manager 已打 ERROR 日志；
        swallow=True 用于收尾阶段（避免掩盖主异常）。
        """
        if not self._state_manager:
            return
        from src.engine.state_manager import StateTransitionError

        try:
            await self._state_manager.transition_to(new_state, reason)
        except StateTransitionError as exc:
            if swallow:
                _log.warning("[SiteCrawlStrategy][FSM] 收尾转移未执行: {}", exc)
            else:
                raise

    async def run(self) -> None:
        """
        Site 主线抓取流程。
        1. 通知 StateManager → INITIALIZING。
        2. 实例化并组装所有子组件（_assemble_pipeline）。
        3. 调用 generator.generate() 获取种子 URL 列表。
        4. 通过 CrawleeEngineFactory 创建 crawler，挂载 request handler。
        5. 通知 StateManager → RUNNING，启动 crawler.run()。
        6. 等待下载队列清空（graceful shutdown）。
        7. 通知 StateManager → STOPPING → STOPPED。
        """
        from src.engine.state_manager import CrawlerState

        # ── Step 1: INITIALIZING（Search 子阶段已置 RUNNING 时跳过，避免非法边） ─
        if self._state_manager:
            cur = self._state_manager.state
            if cur == CrawlerState.RUNNING:
                _log.info(
                    "[SiteCrawlStrategy] 接续 Search 阶段：FSM 已为 RUNNING，跳过 INITIALIZING"
                )
            else:
                await self._fsm_transition(
                    CrawlerState.INITIALIZING,
                    "SiteCrawlStrategy.run() 开始",
                )

        # ── Step 2: 装配管线 ─────────────────────────────────────────
        self._assemble_pipeline()

        # ── Step 3: 生成种子 URL ─────────────────────────────────────
        seeds = await self._generator.generate(
            self.settings.strategy_settings,
            max_targets=self.settings.task_info.max_pdf_count or 5000,
        )
        if not seeds:
            _log.error("[SiteCrawlStrategy] 种子 URL 列表为空，任务中止")
            await self._fsm_transition(
                CrawlerState.ERROR,
                "种子 URL 为空",
                swallow=True,
            )
            return

        _log.info(f"[SiteCrawlStrategy] 种子 URL 数量: {len(seeds)}")

        # ── 在 DB 中创建 task 记录并注入 audit_center ────────────────
        await self._create_task_record()

        # ── Step 4: 创建 Crawlee 爬虫并挂载处理管线 ─────────────────
        from src.engine.crawlee_engine import CrawleeEngineFactory

        # BrowserFactory.create_backend() 在 CrawleeEngineFactory 内部自动调用，
        # 此处无需手动 resolve 后端；backend 参数已被移除（V11 重构）。
        factory = CrawleeEngineFactory(
            settings=self.settings,
            proxy_rotator=self._proxy_rotator,
        )
        crawler = factory.create()
        self._register_crawlee_handlers(crawler)

        # ── 设置 ErrorRegistry 上下文变量（供 error_interceptor 使用） ─
        from src.modules.site.audit.error_registry import (
            current_registry,
            current_error_workspace_resolver,
        )

        token_r = current_registry.set(self._error_registry)
        token_res = current_error_workspace_resolver.set(
            self._audit_center._get_workspace  # noqa: SLF001 — 与快照目录公式一致
        )

        # ── Step 5: RUNNING → 启动爬虫 ────────────────────────────────
        self._is_running = True
        if self._state_manager:
            if self._state_manager.state != CrawlerState.RUNNING:
                await self._fsm_transition(
                    CrawlerState.RUNNING,
                    "Crawlee 引擎启动",
                )
            else:
                _log.debug(
                    "[SiteCrawlStrategy] FSM 已为 RUNNING（如由 Search 阶段设置），"
                    "跳过重复转移"
                )

        try:
            await crawler.run(seeds)
        except Exception as exc:
            _log.error(f"[SiteCrawlStrategy] crawler.run() 异常: {exc!r}")
            await self._fsm_transition(
                CrawlerState.ERROR,
                str(exc),
                swallow=True,
            )
        finally:
            current_registry.reset(token_r)
            current_error_workspace_resolver.reset(token_res)

        # ── Step 6: 等待活跃下载任务排空（graceful drain） ───────────
        waited = 0.0
        while self.files_active > 0 and waited < _DRAIN_MAX_WAIT_SECS:
            await asyncio.sleep(_DRAIN_POLL_INTERVAL)
            waited += _DRAIN_POLL_INTERVAL

        if waited >= _DRAIN_MAX_WAIT_SECS:
            _log.warning(
                f"[SiteCrawlStrategy] 下载排空超时 ({_DRAIN_MAX_WAIT_SECS}s)，"
                f"仍有 {self.files_active} 个活跃任务"
            )

        self._is_running = False

        # ── Step 7: STOPPING → STOPPED ────────────────────────────────
        await self._fsm_transition(
            CrawlerState.STOPPING,
            "爬取完成，正在清理",
            swallow=True,
        )
        await self._fsm_transition(
            CrawlerState.STOPPED,
            "任务结束",
            swallow=True,
        )

        # ── 更新 DB 任务状态为 finished ──────────────────────────────
        await self._update_task_status("finished")

    async def cleanup(self) -> None:
        """
        收尾与资源释放。
        1. 等待 net_sniffer 异步队列彻底消费完毕（join + poison pill）。
        2. 调用 audit_center.export_final_reports()。
        3. 调用 error_registry.export_to_markdown()（若有错误）。
        4. 释放 generator 持有的 Playwright 浏览器资源。
        """
        # ── 1. 停止 NetSniffer（drain 未处理队列） ───────────────────
        if self._net_sniffer is not None:
            try:
                await self._net_sniffer.stop()
            except Exception as exc:
                _log.debug(f"[SiteCrawlStrategy.cleanup] NetSniffer.stop() 异常: {exc!r}")

        # ── 2. 导出审计报告 ──────────────────────────────────────────
        if self._audit_center is not None:
            try:
                await self._audit_center.export_final_reports()
            except Exception as exc:
                _log.warning(f"[SiteCrawlStrategy.cleanup] export_final_reports 异常: {exc!r}")

        # ── 3. 导出错误注册表 Markdown 报告 ──────────────────────────
        if self._error_registry is not None and self._audit_center is not None:
            summary = self._error_registry.get_summary()
            if summary.get("unique_errors", 0) > 0:
                domains = self._error_registry.iter_domains_with_errors()
                if self._error_registry.has_entries_without_urls():
                    domains.add("unknown_domain")
                if not domains:
                    domains.add("unknown_domain")
                for dom in sorted(domains):
                    ws = self._audit_center._get_workspace(dom)  # noqa: SLF001
                    report_path = os.path.join(ws, "errors", "error_report.md")
                    try:
                        self._error_registry.export_to_markdown_for_domain(
                            report_path, dom
                        )
                    except Exception as exc:
                        _log.warning(
                            f"[SiteCrawlStrategy.cleanup] 分域错误报告写入失败 "
                            f"{report_path}: {exc!r}"
                        )

        # ── 4. 释放 Playwright 浏览器（generator） ───────────────────
        if self._generator is not None:
            try:
                await self._generator.close()
            except Exception as exc:
                _log.debug(f"[SiteCrawlStrategy.cleanup] generator.close() 异常: {exc!r}")

    # ------------------------------------------------------------------
    # 私有：组件装配
    # ------------------------------------------------------------------

    def _assemble_pipeline(self) -> None:
        """
        实例化并组装 site 抓取所需的全部子组件，储存为实例属性。
        装配顺序：
        1. SiteAuditCenter（审计中心，其他组件可能依赖它）
        2. ErrorRegistry
        3. SiteUrlGenerator
        4. SiteDataParser
        5. 六个 handlers（按职责链顺序）
        Pattern: Facade —— 将复杂的组件初始化集中隐藏在此私有方法中。
        """
        from src.config.settings import get_app_config
        from src.modules.site.audit.audit_center import SiteAuditCenter
        from src.modules.site.audit.error_registry import ErrorRegistry
        from src.modules.site.audit.realtime_file_exporter import RealtimeFileExporter
        from src.modules.site.generator import SiteUrlGenerator
        from src.modules.site.parser import SiteDataParser
        from src.modules.site.handlers.net_sniffer import NetSniffer
        from src.modules.site.handlers.downloader import Downloader
        from src.modules.site.handlers.interactor import Interactor
        from src.modules.site.handlers.strategist import Strategist
        from src.modules.site.handlers.action import ActionHandler
        from src.modules.site.handlers.action_downloader import ActionDownloader

        app_cfg = get_app_config()
        strat = self.settings.strategy_settings
        task = self.settings.task_info

        # ── 目标扩展名（统一计算，下游组件共享） ─────────────────────
        ft = strat.file_type
        self._target_ext = f".{ft}" if ft not in ("img", "all") else f".{ft}"

        # ── 1. SiteAuditCenter（可选挂载实时文件流导出器）──────────────
        realtime_exporter = None
        if task.enable_realtime_jsonl_export:
            realtime_exporter = RealtimeFileExporter(
                base_save_dir=task.save_directory,
                strategy_prefix=strat.crawl_strategy,
            )
            _log.info("[SiteCrawlStrategy] enable_realtime_jsonl_export=True，已启用 JSONL/TXT 实时落盘")
        self._audit_center = SiteAuditCenter(
            db_path=app_cfg.db_path,
            base_save_dir=task.save_directory,
            strategy_prefix=strat.crawl_strategy,
            realtime_exporter=realtime_exporter,
        )

        # ── 2. ErrorRegistry ─────────────────────────────────────────
        self._error_registry = ErrorRegistry()

        # ── 3. SiteUrlGenerator ──────────────────────────────────────
        self._generator = SiteUrlGenerator()

        # ── 4. SiteDataParser ────────────────────────────────────────
        self._parser = SiteDataParser()

        # ── 5. Downloader ────────────────────────────────────────────
        max_conc = self.settings.get_effective_max_concurrency()
        self._downloader = Downloader(
            settings=self.settings,
            audit_center=self._audit_center,
            is_running=self.is_running,
            max_concurrency=max_conc,
        )

        # ── 6. ActionDownloader（共享 Downloader 的 semaphore / lock） ─
        self._action_downloader = ActionDownloader(
            main_downloader=self._downloader,
        )

        # ── 7. Interactor ────────────────────────────────────────────
        self._interactor = Interactor(
            is_running=self.is_running,
            record_interaction=self._audit_center.record_interaction,
        )

        # ── 8. NetSniffer ────────────────────────────────────────────
        self._net_sniffer = NetSniffer(
            parser=self._parser,
            target_ext=self._target_ext,
            is_running=self.is_running,
        )

        # ── 9. Strategist ────────────────────────────────────────────
        self._strategist = Strategist(
            settings=self.settings,
            is_running=self.is_running,
        )

        # ── 10. ActionHandler ─────────────────────────────────────────
        self._action_handler = ActionHandler(
            interactor=self._interactor,
            action_downloader=self._action_downloader,
            is_running=self.is_running,
            record_interaction=self._audit_center.record_interaction,
        )

        _log.info("[SiteCrawlStrategy] 管线装配完成")

    def _register_crawlee_handlers(self, crawler: Any) -> None:
        """
        向 Crawlee crawler 注册 default_handler / 标签 handler / failed_request_handler。
        使用 crawlee-python 的 Router API（非 JS/TS 的 addDefaultHandler）。

        Args:
            crawler: 由 CrawleeEngineFactory 创建的 Crawlee 爬虫实例。
        """
        crawler.router.default_handler(self._default_page_handler)
        crawler.router.handler("NEED_CLICK")(self._action_handler.handle_action)

        # 失败请求处理（Crawlee Python 通过 failed_request_handler 属性注入）
        if hasattr(crawler, "failed_request_handler"):
            crawler.failed_request_handler = self._failed_request_handler
        else:
            _log.debug(
                "[SiteCrawlStrategy] 爬虫不支持 failed_request_handler 属性，跳过注册"
            )

    # ------------------------------------------------------------------
    # 私有：Crawlee 请求处理器
    # ------------------------------------------------------------------

    async def _default_page_handler(self, context: Any) -> None:
        """
        默认页面处理器：串联 interactor → net_sniffer → 链接提取 →
        下载调度 → trigger_download_buttons → strategist 的完整管线。
        """
        if not self._is_running:
            return

        page = getattr(context, "page", None)
        request = getattr(context, "request", None)
        current_url: str = getattr(request, "url", "Unknown") if request else "Unknown"

        response = getattr(context, "response", None)
        status_code: int = getattr(response, "status", 200) if response else 200

        # ── 反爬挑战：在持有 Playwright Page 的 handler 内检测并调用 ChallengeSolver ──
        if page is not None and self._challenge_solver is not None:
            await self._maybe_handle_challenge_page(page, status_code)

        # ── 记录页面访问成功 ─────────────────────────────────────────
        domain = self._parser.get_core_domain(current_url)
        await self._audit_center.record_page_success(domain, current_url, status_code)

        # ── 清理 Cookie 弹窗 ─────────────────────────────────────────
        if page is not None:
            await self._interactor.clear_cookie_banners(page)

        # ── 挂载网络嗅探探针 ─────────────────────────────────────────
        workspace = self._audit_center._get_workspace(domain)  # noqa: SLF001
        await self._net_sniffer.attach_probe(
            context=context,
            domain=domain,
            domain_workspace=workspace,
            audit_center=self._audit_center,
            downloader=self._downloader,
        )

        # ── 提取页面链接并调度下载 ────────────────────────────────────
        raw_hrefs = await self._interactor.extract_raw_links(context)
        for href in raw_hrefs:
            item = self._parser.parse_link(current_url, href, self._target_ext)
            if item is None:
                continue
            file_url = item["file_url"]
            file_domain = self._parser.get_core_domain(file_url)
            file_workspace = self._audit_center._get_workspace(file_domain)  # noqa: SLF001
            save_path = os.path.join(file_workspace, item["file_name"])
            asyncio.create_task(
                self._downloader.native_download_task(page, file_url, save_path, item),
                name=f"dl-{file_url[-40:]}",
            )

        # ── 触发页面下载按钮扫描（enqueue NEED_CLICK requests） ──────
        await self._interactor.trigger_download_buttons(context)

        # ── 扩展爬取范围（enqueueing 下一批链接） ────────────────────
        await self._strategist.enqueue_next_pages(context)

    async def _maybe_handle_challenge_page(self, page: Any, status_code: int) -> None:
        """
        在持有 Playwright Page 的 Crawlee handler 内做挑战闭环：
        HTTP 层拦截码（401/403/429/503 等）或 ChallengeDetector 识别到盾页时，
        调用 ChallengeSolver.solve()（内部驱动 CHALLENGE → RUNNING / BANNED）。
        """
        from src.engine.anti_bot.challenge_solver import ChallengeDetector, ChallengeType

        if self._challenge_solver is None:
            return

        try:
            ctype = await ChallengeDetector().detect(page)
        except Exception:
            ctype = ChallengeType.UNKNOWN

        http_challenge = status_code in (401, 403, 407, 429, 503)
        if ctype == ChallengeType.NONE and not http_challenge:
            return
        if ctype == ChallengeType.NONE:
            ctype = ChallengeType.UNKNOWN

        try:
            await self._challenge_solver.solve(page, ctype)
        except Exception as exc:
            _log.debug(f"[SiteCrawlStrategy] ChallengeSolver.solve 异常（可忽略）: {exc!r}")

    async def _failed_request_handler(self, context: Any) -> None:
        """
        失败请求处理器：将请求失败记录写入 audit_center。
        若上下文中仍有 Page（部分失败路径会保留），尝试挑战恢复。
        """
        page = getattr(context, "page", None)
        if page is not None and self._challenge_solver is not None:
            await self._maybe_handle_challenge_page(page, 403)

        request = getattr(context, "request", None)
        if request is None:
            return
        url: str = getattr(request, "url", "Unknown")
        domain = self._parser.get_core_domain(url) if self._parser else "unknown_domain"
        error_msg = getattr(request, "error_message", "请求失败") or "请求失败"
        if self._audit_center:
            await self._audit_center.record_page_failure(domain, url, 0, error_msg)

    # ------------------------------------------------------------------
    # 私有：DB 任务记录管理
    # ------------------------------------------------------------------

    async def _create_task_record(self) -> None:
        """
        在 tasks 表插入当前任务记录，并将生成的 task_id 注入 audit_center。
        使用时间戳作为唯一键，通过 INSERT + SELECT 两步获取 lastrowid。
        """
        from src.config.settings import get_app_config
        from src.db.database import get_db

        try:
            app_cfg = get_app_config()
            db = await get_db(app_cfg.db_path)
            strat = self.settings.strategy_settings
            task = self.settings.task_info

            task_name = getattr(task, "task_name", "unnamed_task") or "unnamed_task"
            mode = getattr(task, "mode", "site") or "site"
            started_at = datetime.datetime.utcnow().isoformat()

            await db.execute(
                "INSERT INTO tasks "
                "(task_name, mode, strategy, save_directory, file_type, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (
                    task_name,
                    mode,
                    strat.crawl_strategy,
                    task.save_directory,
                    strat.file_type,
                    started_at,
                ),
            )

            # 获取刚插入行的 id（使用精确时间戳匹配，单实例下唯一）
            row = await db.query_one(
                "SELECT id FROM tasks WHERE task_name = ? AND started_at = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_name, started_at),
            )
            if row is not None:
                task_id = row["id"]
                self._audit_center.set_task_id(task_id)
                _log.info(f"[SiteCrawlStrategy] DB task_id={task_id} 已注入 audit_center")
            else:
                _log.warning("[SiteCrawlStrategy] 无法获取 task_id，audit_center 将以降级模式运行")
        except Exception as exc:
            _log.warning(f"[SiteCrawlStrategy] 创建 task 记录失败（降级继续）: {exc!r}")

    async def _update_task_status(self, status: str) -> None:
        """
        更新 tasks 表中当前任务的 status 和 finished_at 字段。

        Args:
            status: 目标状态字符串（'finished' / 'error' / 'stopped'）。
        """
        from src.config.settings import get_app_config
        from src.db.database import get_db

        if self._audit_center is None:
            return
        task_id = getattr(self._audit_center, "_task_id", None)
        if task_id is None:
            return
        try:
            app_cfg = get_app_config()
            db = await get_db(app_cfg.db_path)
            finished_at = datetime.datetime.utcnow().isoformat()
            await db.execute(
                "UPDATE tasks SET status = ?, finished_at = ? WHERE id = ?",
                (status, finished_at, task_id),
            )
        except Exception as exc:
            _log.debug(f"[SiteCrawlStrategy] 更新 task status 失败（可忽略）: {exc!r}")

    # ------------------------------------------------------------------
    # 属性代理
    # ------------------------------------------------------------------

    @property
    def files_active(self) -> int:
        """
        透传 downloader.files_active，供 Runner 在 graceful shutdown 阶段轮询。

        Returns:
            当前正在进行中的下载任务数量。
        """
        if self._downloader is None:
            return 0
        return self._downloader.files_active

    def get_dashboard_data(self) -> Dict[str, Any]:
        """
        聚合 downloader 统计 + audit_center 总计 + StateManager 当前状态，
        返回 UI 仪表盘所需的完整数据快照。

        Returns:
            包含 files_found / files_downloaded / scraped_count / state 等字段的字典。
        """
        dl_stats: Dict[str, int] = (
            self._downloader.get_stats() if self._downloader else {}
        )
        scraped_count: int = (
            self._audit_center.get_total_scraped_count()
            if self._audit_center
            else 0
        )
        state_name: str = (
            self._state_manager.state.name
            if self._state_manager is not None
            else ("RUNNING" if self._is_running else "IDLE")
        )
        return {
            "files_found":      dl_stats.get("files_found", 0),
            "files_downloaded": dl_stats.get("files_downloaded", 0),
            "files_active":     dl_stats.get("files_active", 0),
            "scraped_count":    scraped_count,
            "state":            state_name,
            "is_running":       self._is_running,
        }

    def get_strategy_name(self) -> str:
        """
        Returns:
            '网站批量抓取 ({crawl_strategy})' 形式的可读名称。
        """
        strategy = self.settings.strategy_settings.crawl_strategy
        return f"网站批量抓取 ({strategy})"
