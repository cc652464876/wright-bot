"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 搜索引擎抓取策略
@Pattern : Strategy Pattern —— 继承 BaseCrawlStrategy，实现搜索引擎专用算法族
@Description:
    SearchCrawlStrategy 是面向搜索引擎场景（Google / Bing / DuckDuckGo）的具体策略实现。
    对应 MasterDispatcher 中 task_info.mode == 'search' 的分支。
    执行流程：
    1. 通过 Search API 或 Playwright 爬取搜索结果页获取目标 URL 列表；
    2. 将结果 URL 列表作为种子注入 SiteCrawlStrategy，复用完整下载管线；
    3. 对比 SiteCrawlStrategy，主要差异在 generate() 和 validate() 阶段。

    架构边界（FSM）：
    - 搜索任务与站点任务共用 **同一 CrawlerStateManager**（由 Dispatcher 注入），
      保证 UI 心跳、ChallengeSolver 与仪表盘状态连续。
    - SERP 抓取阶段由本策略驱动 INITIALIZING → RUNNING；移交站点管线时
      **将 state_manager 传入 SiteCrawlStrategy**，由后者在已进入 RUNNING 时
      跳过重复的 INITIALIZING/RUNNING 转移（见 SiteCrawlStrategy.run）。
    - 历史上 `state_manager=None` 的委托会导致站点阶段 FSM 与外层脱节；
      现已改为显式传递，**非独立状态机**。

    Pattern: Strategy Pattern —— 与 SiteCrawlStrategy 共享同一 BaseCrawlStrategy 接口，
    Dispatcher 可在运行时无缝切换，业务调用方无需修改任何代码。
"""

from __future__ import annotations

import asyncio
import random
import urllib.parse
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.modules.base_strategy import BaseCrawlStrategy
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import PrismSettings
    from src.engine.anti_bot.challenge_solver import ChallengeSolver
    from src.engine.anti_bot.proxy_rotator import ProxyRotator
    from src.engine.state_manager import CrawlerStateManager
    from src.modules.site.strategy import SiteCrawlStrategy
    from playwright.async_api import Browser, BrowserContext, Playwright

_log = get_logger(__name__)

# ── 支持的搜索引擎标识符 ──────────────────────────────────────────────────
_VALID_SEARCH_STRATEGIES = frozenset({"google_search", "bing_search", "duckduckgo"})

# ── SERP 抓取：每次页面操作之间的最短反爬间隔（秒）──────────────────────
_SERP_MIN_DELAY = 1.5
_SERP_MAX_DELAY = 3.5

# ── SERP 翻页最大页数（防止无限翻页） ──────────────────────────────────────
_SERP_MAX_PAGES = 10

# ── 各搜索引擎的 CSS 结果选择器 ────────────────────────────────────────────
_SERP_RESULT_SELECTORS: Dict[str, str] = {
    "duckduckgo": "a.result__a",
    "bing":       "#b_results .b_algo h2 a",
    "google":     "#rso .g a[jsname], #rso .g h3 > a",
}

# ── 各搜索引擎的初始 SERP URL 模板 ─────────────────────────────────────────
_SERP_BASE_URLS: Dict[str, str] = {
    "duckduckgo": "https://html.duckduckgo.com/html/?q={query}",
    "bing":       "https://www.bing.com/search?q={query}&count=50",
    "google":     "https://www.google.com/search?q={query}&num=50",
}

# ── 各搜索引擎的翻页 URL 参数 ────────────────────────────────────────────────
# format: (base_url_template, page_offset_param, items_per_page)
_SERP_PAGINATION: Dict[str, tuple] = {
    "duckduckgo": ("https://html.duckduckgo.com/html/?q={query}&s={offset}", "s", 30),
    "bing":       ("https://www.bing.com/search?q={query}&count=50&first={offset}", "first", 50),
    "google":     ("https://www.google.com/search?q={query}&num=50&start={offset}", "start", 50),
}


class SearchCrawlStrategy(BaseCrawlStrategy):
    """
    搜索引擎抓取具体策略。

    支持的搜索引擎由 settings.strategy_settings.crawl_strategy 决定：
    - 'google_search' : 使用 Google Custom Search API 或 Playwright 爬 SERP
    - 'bing_search'   : 使用 Bing Web Search API
    - 'duckduckgo'    : 使用 DuckDuckGo HTML 接口（无 API Key 需求）

    Pattern: Strategy Pattern —— 继承 BaseCrawlStrategy，实现搜索引擎专用算法族。
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
            settings     : 全局运行时参数单例；
                           strategy_settings.search_keyword 为搜索关键词，
                           strategy_settings.api_key 为 API 鉴权密钥（可选）。
            state_manager: Dispatcher 注入的共享 CrawlerStateManager（应与站点子阶段共用）。
            proxy_rotator: 透传给内部 SiteCrawlStrategy / Crawlee 工厂。
            challenge_solver: 透传给内部 SiteCrawlStrategy。
        """
        super().__init__(settings)
        self._state_manager = state_manager
        self._proxy_rotator = proxy_rotator
        self._challenge_solver = challenge_solver

        # 统计计数器
        self._results_found: int = 0
        self._files_downloaded: int = 0

        # 内部委托的 SiteCrawlStrategy（在 run() 中初始化）
        self._site_strategy: Optional["SiteCrawlStrategy"] = None

        # Playwright 资源（SERP 抓取用，懒加载）
        self._playwright: Optional["Playwright"] = None
        self._browser: Optional["Browser"] = None
        self._context: Optional["BrowserContext"] = None

    # ------------------------------------------------------------------
    # 实现抽象方法
    # ------------------------------------------------------------------

    def validate(self) -> bool:
        """
        校验搜索任务所需的必要参数。
        检查项：
        - search_keyword 非空（无关键词无法发起搜索）。
        - crawl_strategy 为已知的搜索引擎类型。
        - 若 crawl_strategy 为 'google_search'，api_key 应非空（否则降级为 Playwright 抓取）。

        Returns:
            True 表示参数合法；False 表示参数不足，任务无法启动。
        """
        strat = self.settings.strategy_settings

        if not strat.search_keyword or not strat.search_keyword.strip():
            _log.error("[SearchCrawlStrategy.validate] search_keyword 为空，任务无法启动")
            return False

        if strat.crawl_strategy not in _VALID_SEARCH_STRATEGIES:
            _log.error(
                f"[SearchCrawlStrategy.validate] crawl_strategy={strat.crawl_strategy!r} "
                f"不是合法的搜索引擎类型（允许值: {_VALID_SEARCH_STRATEGIES}）"
            )
            return False

        if strat.crawl_strategy == "google_search" and not strat.api_key:
            _log.warning(
                "[SearchCrawlStrategy.validate] google_search 无 api_key，"
                "将降级为 Playwright SERP 爬取（准确率较低）"
            )
            # 降级是允许的，不返回 False

        return True

    async def _drive_fsm_after_empty_search_results(self) -> None:
        """搜索结果为空时从 RUNNING 收束到 STOPPED，避免 FSM 悬停在运行态。"""
        from src.engine.state_manager import CrawlerState, StateTransitionError

        sm = self._state_manager
        if sm is None:
            return
        try:
            if sm.state == CrawlerState.RUNNING:
                await sm.transition_to(
                    CrawlerState.STOPPING,
                    "搜索结果为空，提前结束",
                )
                await sm.transition_to(CrawlerState.STOPPED, "任务结束")
        except StateTransitionError as exc:
            _log.warning(
                "[SearchCrawlStrategy] 空结果时 FSM 收尾失败（可能已由其他路径推进）: {}",
                exc,
            )

    async def run(self) -> None:
        """
        搜索引擎抓取主流程。
        1. 调用 _fetch_search_results() 获取搜索结果 URL 列表。
        2. 将 URL 列表作为种子注入 SiteCrawlStrategy，复用完整下载管线。
        3. 监控进度并更新 StateManager 状态。
        """
        from src.engine.state_manager import CrawlerState, StateTransitionError

        # ── 状态: INITIALIZING ──────────────────────────────────────
        if self._state_manager:
            try:
                await self._state_manager.transition_to(
                    CrawlerState.INITIALIZING, "SearchCrawlStrategy.run() 开始"
                )
            except StateTransitionError as exc:
                _log.error("[SearchCrawlStrategy] INITIALIZING 转移失败: {}", exc)
                raise

        self._is_running = True

        try:
            # ── Step 1: 获取搜索结果 URL 列表 ────────────────────────
            if self._state_manager:
                try:
                    await self._state_manager.transition_to(
                        CrawlerState.RUNNING, "正在抓取搜索结果"
                    )
                except StateTransitionError as exc:
                    _log.error("[SearchCrawlStrategy] RUNNING 转移失败: {}", exc)
                    raise

            urls = await self._fetch_search_results()
            self._results_found = len(urls)
            _log.info(f"[SearchCrawlStrategy] 搜索结果: {self._results_found} 个 URL")

            if not urls:
                _log.warning("[SearchCrawlStrategy] 搜索结果为空，任务中止")
                await self._drive_fsm_after_empty_search_results()
                return

            # ── Step 2: 构造修改后的 settings 并委托 SiteCrawlStrategy ─
            max_count = self.settings.task_info.max_pdf_count or 5000
            site_urls = urls[:max_count]

            # 深拷贝 settings 并将爬取策略切换为 direct，target_urls 替换为搜索结果
            site_settings = self.settings.model_copy(deep=True)
            site_settings.strategy_settings.crawl_strategy = "direct"
            site_settings.strategy_settings.target_urls = site_urls

            # 与外层共用 FSM：站点阶段接续 RUNNING，由 SiteCrawlStrategy 跳过重复引导转移
            from src.modules.site.strategy import SiteCrawlStrategy
            self._site_strategy = SiteCrawlStrategy(
                site_settings,
                state_manager=self._state_manager,
                proxy_rotator=self._proxy_rotator,
                challenge_solver=self._challenge_solver,
            )

            _log.info(f"[SearchCrawlStrategy] 移交 {len(site_urls)} 个 URL 至 SiteCrawlStrategy")

            # ── Step 3: 执行下载管线 ──────────────────────────────────
            await self._site_strategy.run()

            # 同步文件下载数量
            if self._site_strategy._downloader:
                self._files_downloaded = self._site_strategy._downloader.files_downloaded

        except Exception as exc:
            _log.error(f"[SearchCrawlStrategy] run() 异常: {exc!r}")
            if self._state_manager:
                try:
                    await self._state_manager.transition_to(
                        CrawlerState.ERROR, str(exc)
                    )
                except StateTransitionError as fsm_exc:
                    _log.warning(
                        "[SearchCrawlStrategy] ERROR 状态转移失败（可能已处于终态）: {}",
                        fsm_exc,
                    )
        finally:
            self._is_running = False

            if self._state_manager:
                try:
                    current = self._state_manager.state
                    if current not in (
                        CrawlerState.STOPPED,
                        CrawlerState.ERROR,
                        CrawlerState.STOPPING,
                    ):
                        await self._state_manager.transition_to(
                            CrawlerState.STOPPING, "搜索任务完成"
                        )
                        await self._state_manager.transition_to(
                            CrawlerState.STOPPED, "任务结束"
                        )
                except StateTransitionError as fsm_exc:
                    _log.warning(
                        "[SearchCrawlStrategy] finally 收尾 FSM 转移跳过: {}",
                        fsm_exc,
                    )

    async def cleanup(self) -> None:
        """
        资源释放。
        关闭 Playwright 浏览器实例、导出审计报告、更新 DB 任务状态。
        """
        # 清理委托的 SiteCrawlStrategy
        if self._site_strategy is not None:
            try:
                await self._site_strategy.cleanup()
            except Exception as exc:
                _log.debug(f"[SearchCrawlStrategy.cleanup] site_strategy.cleanup 异常: {exc!r}")

        # 释放 SERP 抓取用 Playwright 资源
        await self._close_playwright()

    # ------------------------------------------------------------------
    # 私有：搜索结果获取
    # ------------------------------------------------------------------

    async def _fetch_search_results(self) -> List[str]:
        """
        根据 crawl_strategy 分发到对应的搜索引擎适配器，
        返回目标文件的 URL 列表。

        Returns:
            去重后的目标 URL 字符串列表。
        """
        strategy = self.settings.strategy_settings.crawl_strategy

        raw_urls: List[str] = []
        try:
            if strategy == "google_search":
                raw_urls = await self._fetch_via_google_api()
            elif strategy == "bing_search":
                raw_urls = await self._fetch_via_bing_api()
            elif strategy == "duckduckgo":
                raw_urls = await self._fetch_via_playwright_serp("duckduckgo")
            else:
                _log.warning(f"[SearchCrawlStrategy] 未知策略 {strategy!r}，跳过")
        except Exception as exc:
            _log.error(f"[SearchCrawlStrategy] _fetch_search_results 异常: {exc!r}")

        # 去重保序
        seen: set = set()
        unique: List[str] = []
        for u in raw_urls:
            if u and u not in seen:
                seen.add(u)
                unique.append(u)

        return unique

    async def _fetch_via_google_api(self) -> List[str]:
        """
        通过 Google Custom Search JSON API 获取搜索结果。
        使用 settings.strategy_settings.api_key 鉴权（格式：'API_KEY:CX_ID'）。
        若 api_key 缺失或格式不正确，自动降级为 Playwright SERP 抓取。

        Returns:
            从 API 响应中提取的目标页面 URL 列表。
        """
        import aiohttp

        strat = self.settings.strategy_settings
        api_key_raw = strat.api_key.strip()
        keyword = strat.search_keyword.strip()
        file_type = strat.file_type

        # api_key 格式为 "API_KEY:CX_ID"（CX = Custom Search Engine ID）
        if not api_key_raw or ":" not in api_key_raw:
            _log.warning(
                "[SearchCrawlStrategy] Google API key 格式不正确（应为 'API_KEY:CX_ID'），"
                "降级为 Playwright SERP 抓取"
            )
            return await self._fetch_via_playwright_serp("google")

        api_key, cx = api_key_raw.split(":", 1)
        query = f"{keyword} filetype:{file_type}" if file_type != "all" else keyword
        max_count = self.settings.task_info.max_pdf_count or 100

        urls: List[str] = []
        try:
            async with aiohttp.ClientSession() as session:
                # Google CSE API 每次最多返回 10 条，最多支持 start=91（10 页）
                for start in range(1, min(100, max_count + 1), 10):
                    if len(urls) >= max_count:
                        break
                    params = {
                        "key":   api_key,
                        "cx":    cx,
                        "q":     query,
                        "start": start,
                        "num":   10,
                    }
                    async with session.get(
                        "https://www.googleapis.com/customsearch/v1",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 429:
                            _log.warning("[SearchCrawlStrategy] Google API 速率限制，停止翻页")
                            break
                        if resp.status != 200:
                            _log.warning(f"[SearchCrawlStrategy] Google API HTTP {resp.status}")
                            break
                        data = await resp.json()
                        items = data.get("items", [])
                        if not items:
                            break
                        for item in items:
                            link = item.get("link", "")
                            if link:
                                urls.append(link)
                    # 翻页间隔：遵守 API 速率限制
                    await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception as exc:
            _log.warning(f"[SearchCrawlStrategy] Google API 请求失败，降级: {exc!r}")
            return await self._fetch_via_playwright_serp("google")

        _log.info(f"[SearchCrawlStrategy] Google API 获取到 {len(urls)} 个 URL")
        return urls

    async def _fetch_via_bing_api(self) -> List[str]:
        """
        通过 Bing Web Search API 获取搜索结果。
        api_key 为 Ocp-Apim-Subscription-Key；缺失时降级为 Playwright 抓取。

        Returns:
            从 API 响应中提取的目标页面 URL 列表。
        """
        import aiohttp

        strat = self.settings.strategy_settings
        api_key = strat.api_key.strip()
        keyword = strat.search_keyword.strip()
        file_type = strat.file_type
        max_count = self.settings.task_info.max_pdf_count or 100

        if not api_key:
            _log.warning("[SearchCrawlStrategy] Bing API key 为空，降级为 Playwright SERP 抓取")
            return await self._fetch_via_playwright_serp("bing")

        query = f"{keyword} filetype:{file_type}" if file_type != "all" else keyword
        urls: List[str] = []

        try:
            headers = {"Ocp-Apim-Subscription-Key": api_key}
            async with aiohttp.ClientSession() as session:
                for offset in range(0, min(max_count, 500), 50):
                    if len(urls) >= max_count:
                        break
                    params = {
                        "q":      query,
                        "count":  50,
                        "offset": offset,
                        "mkt":    "en-US",
                    }
                    async with session.get(
                        "https://api.bing.microsoft.com/v7.0/search",
                        headers=headers,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 429:
                            _log.warning("[SearchCrawlStrategy] Bing API 速率限制，停止翻页")
                            break
                        if resp.status != 200:
                            _log.warning(f"[SearchCrawlStrategy] Bing API HTTP {resp.status}")
                            break
                        data = await resp.json()
                        web_pages = data.get("webPages", {}).get("value", [])
                        if not web_pages:
                            break
                        for page in web_pages:
                            url = page.get("url", "")
                            if url:
                                urls.append(url)
                    await asyncio.sleep(random.uniform(0.2, 0.6))
        except Exception as exc:
            _log.warning(f"[SearchCrawlStrategy] Bing API 请求失败，降级: {exc!r}")
            return await self._fetch_via_playwright_serp("bing")

        _log.info(f"[SearchCrawlStrategy] Bing API 获取到 {len(urls)} 个 URL")
        return urls

    async def _fetch_via_playwright_serp(self, engine: str) -> List[str]:
        """
        无 API Key 降级方案：使用 Playwright 直接爬取搜索引擎结果页（SERP）。
        适用于 DuckDuckGo 或 API 鉴权失败的降级场景。
        内置翻页逻辑（最多 _SERP_MAX_PAGES 页）和反爬随机延迟。

        Args:
            engine: 搜索引擎标识，'duckduckgo' / 'google' / 'bing'。
                    对于 'google_search' / 'bing_search' 键名，内部会自动去掉 '_search' 后缀。
        Returns:
            从 SERP 页面提取的有机搜索结果 URL 列表。
        """
        # 规范化引擎名（google_search → google）
        engine_key = engine.replace("_search", "")
        if engine_key not in _SERP_RESULT_SELECTORS:
            engine_key = "duckduckgo"

        strat = self.settings.strategy_settings
        keyword = strat.search_keyword.strip()
        file_type = strat.file_type
        max_count = self.settings.task_info.max_pdf_count or 100

        # 构造搜索查询词
        query = f"{keyword} filetype:{file_type}" if file_type != "all" else keyword
        query_enc = urllib.parse.quote_plus(query)

        selector = _SERP_RESULT_SELECTORS[engine_key]
        base_url_tmpl, _, items_per_page = _SERP_PAGINATION[engine_key]

        urls: List[str] = []

        try:
            await self._ensure_playwright()
            ctx = self._context
            assert ctx is not None
            page = await ctx.new_page()

            try:
                for page_idx in range(_SERP_MAX_PAGES):
                    if len(urls) >= max_count:
                        break

                    # 构造翻页 URL
                    offset = page_idx * items_per_page
                    serp_url = base_url_tmpl.format(
                        query=query_enc, offset=offset
                    )

                    _log.debug(f"[SearchCrawlStrategy] SERP [{engine_key}] p{page_idx + 1}: {serp_url}")

                    try:
                        await page.goto(
                            serp_url,
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                    except Exception as exc:
                        _log.warning(f"[SearchCrawlStrategy] SERP 页面加载失败: {exc!r}")
                        break

                    # 人类化随机延迟（让页面 JS 充分执行，同时降低频次检测风险）
                    await asyncio.sleep(random.uniform(_SERP_MIN_DELAY, _SERP_MAX_DELAY))

                    # 提取有机搜索结果链接
                    try:
                        page_urls: List[str] = await page.eval_on_selector_all(
                            selector,
                            "elements => elements.map(el => el.href).filter(u => u && u.startsWith('http'))",
                        )
                    except Exception:
                        page_urls = []

                    if not page_urls:
                        _log.debug(f"[SearchCrawlStrategy] 第 {page_idx + 1} 页无结果，停止翻页")
                        break

                    # 过滤掉搜索引擎自身域名（重定向跳转链接等）
                    for href in page_urls:
                        if _is_external_result(href, engine_key):
                            urls.append(href)

                    _log.debug(
                        f"[SearchCrawlStrategy] SERP [{engine_key}] p{page_idx + 1}: "
                        f"提取 {len(page_urls)} 条，有效 {len(urls)} 条（累计）"
                    )

                    # 翻页前追加随机延迟（模拟用户阅读行为）
                    await asyncio.sleep(random.uniform(1.0, 2.5))

            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        except Exception as exc:
            _log.error(f"[SearchCrawlStrategy] Playwright SERP 抓取失败: {exc!r}")

        _log.info(f"[SearchCrawlStrategy] SERP [{engine_key}] 共获取 {len(urls)} 个 URL")
        return urls

    def get_dashboard_data(self) -> Dict[str, Any]:
        """
        返回搜索任务实时仪表盘数据。

        Returns:
            包含 search_keyword / results_found / files_downloaded 等字段的字典。
        """
        strat = self.settings.strategy_settings

        # 透传 site_strategy 的下载统计（若已初始化）
        site_data: Dict[str, Any] = {}
        if self._site_strategy is not None:
            try:
                site_data = self._site_strategy.get_dashboard_data()
            except Exception:
                pass

        state_name: str = (
            self._state_manager.state.name
            if self._state_manager is not None
            else ("RUNNING" if self._is_running else "IDLE")
        )

        return {
            "search_keyword":   strat.search_keyword,
            "search_engine":    strat.crawl_strategy,
            "results_found":    self._results_found,
            "files_found":      site_data.get("files_found", 0),
            "files_downloaded": site_data.get("files_downloaded", self._files_downloaded),
            "files_active":     site_data.get("files_active", 0),
            "scraped_count":    site_data.get("scraped_count", 0),
            "state":            state_name,
            "is_running":       self._is_running,
        }

    def get_strategy_name(self) -> str:
        """
        Returns:
            人类可读的策略名称，如 'Google 搜索抓取'。
        """
        strategy = self.settings.strategy_settings.crawl_strategy
        names: Dict[str, str] = {
            "google_search": "Google 搜索抓取",
            "bing_search":   "Bing 搜索抓取",
            "duckduckgo":    "DuckDuckGo 搜索抓取",
        }
        return names.get(strategy, f"搜索抓取 ({strategy})")

    # ------------------------------------------------------------------
    # 私有：Playwright 资源管理
    # ------------------------------------------------------------------

    async def _ensure_playwright(self) -> None:
        """
        懒加载单例 Playwright 浏览器（SERP 抓取专用）。
        优先使用本地 Chromium，其次使用系统 Playwright 安装。

        Step 3：统一接入 FingerprintGenerator，废弃硬编码 Chrome/120 UA。
        user_agent / viewport / locale / timezone_id / extra_http_headers
        均由 FingerprintGenerator().generate() 动态生成，与主引擎路径保持一致。
        """
        if self._playwright is not None:
            return

        import os
        from playwright.async_api import async_playwright
        from src.engine.anti_bot.fingerprint import FingerprintGenerator

        self._playwright = await async_playwright().start()

        launch_opts: dict = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }

        try:
            from src.config.settings import get_app_config
            chromium_path = get_app_config().chromium_path
            if chromium_path and os.path.isfile(chromium_path):
                launch_opts["executable_path"] = chromium_path
        except Exception:
            pass

        self._browser = await self._playwright.chromium.launch(**launch_opts)

        # Step 3：用 FingerprintGenerator 替换硬编码 UA（原 Chrome/120.0.0.0 已过期）
        # LOCAL_PC 档案读取本机真实 GPU / 屏幕 / 时区，与主引擎指纹体系完全一致。
        _fp = FingerprintGenerator().generate()
        self._context = await self._browser.new_context(
            user_agent=_fp.user_agent,
            viewport={"width": _fp.screen_width, "height": _fp.screen_height},
            locale=_fp.languages[0] if _fp.languages else "zh-CN",
            timezone_id=_fp.timezone,
            extra_http_headers=_fp.extra_headers,   # Accept / Sec-CH-UA / Accept-Language
            permissions=[],                          # 拒绝通知权限请求（搜索引擎常见弹窗）
        )
        _log.debug("[SearchCrawlStrategy] Playwright 浏览器已启动（SERP 专用）")

    async def _close_playwright(self) -> None:
        """
        关闭 Playwright SERP 抓取用浏览器资源。
        """
        for attr, name in [
            ("_context",   "context"),
            ("_browser",   "browser"),
            ("_playwright", "playwright"),
        ]:
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                await obj.close() if attr != "_playwright" else await obj.stop()
            except Exception as exc:
                _log.debug(f"[SearchCrawlStrategy] 关闭 {name} 异常（可忽略）: {exc!r}")
            setattr(self, attr, None)


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------

def _is_external_result(url: str, engine: str) -> bool:
    """
    判断 SERP 提取到的链接是否为真实外部搜索结果（而非搜索引擎内部链接）。

    Args:
        url   : 候选 URL。
        engine: 搜索引擎标识（'duckduckgo' / 'bing' / 'google'）。
    Returns:
        True 表示是外部结果链接；False 表示是搜索引擎内部链接，应过滤。
    """
    if not url or not url.startswith("http"):
        return False

    # 各搜索引擎的内部域名前缀黑名单
    _internal_prefixes: Dict[str, tuple] = {
        "duckduckgo": (
            "https://duckduckgo.com",
            "https://www.duckduckgo.com",
        ),
        "bing": (
            "https://www.bing.com",
            "https://bing.com",
            "https://go.microsoft.com",
        ),
        "google": (
            "https://www.google.com",
            "https://google.com",
            "https://support.google.com",
            "https://accounts.google.com",
        ),
    }

    prefixes = _internal_prefixes.get(engine, ())
    return not any(url.startswith(p) for p in prefixes)
