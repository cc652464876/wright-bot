"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 网站爬取种子 URL 生成器
@Pattern : Template Method Pattern（generate 定义骨架，子方法实现各策略变体）
@Description:
    SiteUrlGenerator 负责根据 crawl_strategy（direct / full / sitemap）
    生成 Crawlee 引擎所需的初始种子 URL 列表，将"如何获取起始 URL"的
    复杂性从主策略（SiteCrawlStrategy）中剥离出来。
    三种模式的核心差异：
    - direct / full : 直接将 target_urls 返回（无需额外网络请求）。
    - sitemap       : 通过 Playwright 爬取 robots.txt → 解析 Sitemap XML
                      → 递归展开子 Sitemap → 返回所有叶子 URL。
    使用资源复用池（单次 Playwright 浏览器冷启动）降低开销，
    任务完成后通过 close() 彻底释放浏览器资源。
    Pattern: Template Method —— generate() 是算法骨架，
    _get_direct_urls / _get_sitemap_urls 是可替换的具体步骤。
"""

from __future__ import annotations

import asyncio
import os
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Playwright

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import StrategyConfig

_log = get_logger(__name__)

# 最大 Sitemap 递归深度，防止无限嵌套
_SITEMAP_MAX_DEPTH = 5

# 网络请求资源类型：抓取文档时屏蔽以加速加载
_BLOCK_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})

# robots.txt Sitemap 行的前缀（大小写不敏感）
_ROBOTS_SITEMAP_PREFIX = "sitemap:"

# 默认回退 Sitemap 路径（robots.txt 无声明时使用）
_DEFAULT_SITEMAP_PATHS = ("/sitemap_index.xml", "/sitemap.xml")


class SiteUrlGenerator:
    """
    网站爬取种子 URL 生成器。

    职责：
    1. 根据 crawl_strategy 路由到对应的 URL 获取方法。
    2. 管理复用的 Playwright 浏览器资源（单次冷启动，多次复用）。
    3. 对所有输出 URL 执行去重并按 max_targets 截断。

    Pattern: Template Method Pattern
    """

    def __init__(self) -> None:
        """
        初始化生成器，预先定位本地 Chromium 路径（若存在）。
        Playwright 资源采用懒加载，在首次调用 _ensure_browser() 时启动。
        """
        # Playwright 资源三层引用（懒初始化）
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def generate(
        self,
        strategy_cfg: "StrategyConfig",
        max_targets: int = 100,
    ) -> List[str]:
        """
        根据 strategy_cfg.crawl_strategy 生成种子 URL 列表（主入口）。

        Args:
            strategy_cfg: 来自 PrismSettings.strategy_settings 的配置块。
            max_targets : 最大返回 URL 数量上限（防止 Sitemap 模式无限扩张）。
        Returns:
            去重、截断后的种子 URL 字符串列表；失败时返回空列表。
        """
        strategy = strategy_cfg.crawl_strategy
        target_urls: List[str] = strategy_cfg.target_urls or []

        if not target_urls:
            _log.warning("[Generator] target_urls 为空，无法生成种子 URL")
            return []

        all_urls: List[str] = []

        for target_url in target_urls:
            try:
                if strategy in ("direct", "full"):
                    urls = self._get_direct_urls(target_url)
                elif strategy == "sitemap":
                    urls = await self._get_sitemap_urls(target_url, max_targets)
                else:
                    _log.warning(f"[Generator] 未知 crawl_strategy={strategy!r}，回退到 direct 模式")
                    urls = self._get_direct_urls(target_url)
                all_urls.extend(urls)
            except Exception as exc:
                _log.error(f"[Generator] 处理 {target_url} 时异常: {exc!r}")

        # 去重并保持原始顺序
        seen: set = set()
        unique: List[str] = []
        for u in all_urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)

        truncated = unique[:max_targets]
        _log.info(f"[Generator] 策略={strategy}, 原始={len(all_urls)}, 去重={len(unique)}, 截断={len(truncated)}")
        return truncated

    async def preview_robots_txt(self, input_url: str) -> Optional[str]:
        """
        UI 监控专用接口：获取并返回目标域名的 robots.txt 原始文本内容。

        Args:
            input_url: 目标网站 URL（支持不带协议头的裸域名）。
        Returns:
            robots.txt 文本内容字符串；获取失败时返回 None。
        """
        normalized = self._normalize_scheme(input_url)
        parsed = urlparse(normalized)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        raw = await self._fetch_via_playwright(robots_url, timeout=15000, max_retries=1)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return None

    async def close(self) -> None:
        """
        彻底释放 Playwright 浏览器资源（Context → Browser → Playwright 进程）。
        应在 generate() 或 preview_robots_txt() 完成后调用。
        """
        try:
            if self._context is not None:
                await self._context.close()
                self._context = None
        except Exception as exc:
            _log.debug(f"[Generator] 关闭 context 时异常（可忽略）: {exc!r}")

        try:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
        except Exception as exc:
            _log.debug(f"[Generator] 关闭 browser 时异常（可忽略）: {exc!r}")

        try:
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
        except Exception as exc:
            _log.debug(f"[Generator] 停止 playwright 时异常（可忽略）: {exc!r}")

    # ------------------------------------------------------------------
    # 私有：浏览器资源管理
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> None:
        """
        懒加载单例浏览器：若尚未启动则初始化 Playwright + Browser + BrowserContext，
        后续所有请求复用此内核（单次冷启动，降低资源开销）。
        优先使用本地 chromium-1208/chrome.exe；不存在时使用 Playwright 系统浏览器。
        """
        if self._playwright is not None:
            return

        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()

        launch_opts: dict = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }

        # 优先使用本地捆绑 Chromium
        try:
            from src.config.settings import get_app_config
            chromium_path = get_app_config().chromium_path
            if chromium_path and os.path.isfile(chromium_path):
                launch_opts["executable_path"] = chromium_path
                _log.debug(f"[Generator] 使用本地 Chromium: {chromium_path}")
        except Exception:
            pass

        self._browser = await self._playwright.chromium.launch(**launch_opts)
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        _log.debug("[Generator] Playwright 浏览器已启动（懒加载）")

    async def _fetch_via_playwright(
        self,
        url: str,
        timeout: int = 60000,
        max_retries: int = 2,
    ) -> Optional[bytes]:
        """
        通过复用的 Playwright 上下文发起 HTTP 请求，返回响应体字节。
        内置智能资源拦截（原始文件模式仅放行 document 请求，
        普通页面模式拦截图片/视频以加速加载）、超时重试和指数退避。

        Args:
            url        : 目标 URL。
            timeout    : 单次请求超时（毫秒）。
            max_retries: 失败后最大重试次数。
        Returns:
            响应体字节内容；所有重试均失败时返回 None。
        """
        await self._ensure_browser()

        ctx = self._context
        assert ctx is not None

        for attempt in range(max_retries + 1):
            page = None
            try:
                page = await ctx.new_page()

                # 屏蔽非必要资源，降低带宽消耗和加载时间
                async def _route_filter(route):
                    if route.request.resource_type in _BLOCK_RESOURCE_TYPES:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", _route_filter)

                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout,
                )
                if response is None:
                    _log.debug(f"[Generator] goto({url}) 返回 None response")
                    return None
                if not response.ok:
                    _log.debug(f"[Generator] {url} → HTTP {response.status}")
                    return None

                body = await response.body()
                return body

            except Exception as exc:
                if attempt < max_retries:
                    backoff = (2 ** attempt) * 1.0
                    _log.debug(
                        f"[Generator] 请求失败 (attempt {attempt + 1}/{max_retries + 1}), "
                        f"退避 {backoff:.1f}s: {exc!r} — {url}"
                    )
                    await asyncio.sleep(backoff)
                else:
                    _log.warning(f"[Generator] 所有重试均失败 {url}: {exc!r}")
                    return None
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass

        return None  # pragma: no cover

    # ------------------------------------------------------------------
    # 私有：各策略的 URL 获取方法
    # ------------------------------------------------------------------

    def _get_direct_urls(self, target_url: str) -> List[str]:
        """
        direct / full 模式：直接将 target_url 标准化后包装为单元素列表。
        补全 https:// 协议头（如缺失）。

        Args:
            target_url: 来自 strategy_cfg.target_urls 的单条 URL。
        Returns:
            包含标准化 URL 的列表（通常长度为 1）。
        """
        normalized = self._normalize_scheme(target_url.strip())
        if not normalized:
            return []
        return [normalized]

    async def _detect_sitemap_from_robots(self, domain_url: str) -> List[str]:
        """
        通过解析目标域名的 robots.txt 提取 Sitemap 入口 URL 列表。
        若 robots.txt 中无 Sitemap 声明，则回退到 /sitemap_index.xml 默认路径。

        Args:
            domain_url: 目标网站根 URL 或域名。
        Returns:
            Sitemap 入口 URL 列表（通常 1～3 个）。
        """
        parsed = urlparse(self._normalize_scheme(domain_url))
        base = f"{parsed.scheme}://{parsed.netloc}"
        robots_url = f"{base}/robots.txt"

        raw = await self._fetch_via_playwright(robots_url, timeout=15000, max_retries=1)

        sitemap_urls: List[str] = []

        if raw is not None:
            try:
                text = raw.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.lower().startswith(_ROBOTS_SITEMAP_PREFIX):
                        sm_url = stripped[len(_ROBOTS_SITEMAP_PREFIX):].strip()
                        if sm_url:
                            sitemap_urls.append(sm_url)
            except Exception as exc:
                _log.debug(f"[Generator] robots.txt 解析异常: {exc!r}")

        if not sitemap_urls:
            # 回退：尝试常见默认路径
            _log.debug(f"[Generator] robots.txt 无 Sitemap 声明，尝试默认路径: {base}")
            for path in _DEFAULT_SITEMAP_PATHS:
                sitemap_urls.append(f"{base}{path}")

        return sitemap_urls

    async def _get_sitemap_urls(
        self,
        target_url: str,
        max_limit: int,
    ) -> List[str]:
        """
        递归解析 Sitemap XML（支持 Sitemap Index → 子 Sitemap 多层嵌套），
        最大深度 5 层，提取所有叶子页面 URL。

        Args:
            target_url: Sitemap 入口 URL 或目标域名。
            max_limit : 最大提取 URL 数量（0 表示不限制）。
        Returns:
            去重、截断后的页面 URL 列表。
        """
        entry_urls = await self._detect_sitemap_from_robots(target_url)
        if not entry_urls:
            _log.warning(f"[Generator] 无法获取 {target_url} 的 Sitemap 入口")
            return []

        results: List[str] = []
        visited_sitemaps: set = set()

        # BFS 栈：(sitemap_url, depth)
        stack: List[tuple] = [(u, 0) for u in entry_urls]

        while stack:
            if max_limit and len(results) >= max_limit:
                break

            sm_url, depth = stack.pop()

            if sm_url in visited_sitemaps:
                continue
            if depth > _SITEMAP_MAX_DEPTH:
                _log.debug(f"[Generator] 超出最大深度({_SITEMAP_MAX_DEPTH})，跳过: {sm_url}")
                continue

            visited_sitemaps.add(sm_url)
            _log.debug(f"[Generator] 解析 Sitemap (depth={depth}): {sm_url}")

            raw = await self._fetch_via_playwright(sm_url, timeout=20000, max_retries=1)
            if raw is None:
                _log.debug(f"[Generator] Sitemap 获取失败，跳过: {sm_url}")
                continue

            try:
                root = ET.fromstring(raw)
            except ET.ParseError as exc:
                _log.debug(f"[Generator] Sitemap XML 解析失败 {sm_url}: {exc!r}")
                continue

            # 提取命名空间前缀（统一处理有无命名空间两种情况）
            ns = ""
            if "}" in root.tag:
                ns = root.tag.split("}")[0][1:]
            ns_prefix = f"{{{ns}}}" if ns else ""

            root_local = root.tag.split("}")[-1] if "}" in root.tag else root.tag

            if root_local == "sitemapindex":
                # Sitemap Index：提取子 Sitemap 入口，压入栈继续递归
                for sm_elem in root.findall(f"{ns_prefix}sitemap"):
                    loc = sm_elem.find(f"{ns_prefix}loc")
                    if loc is not None and loc.text:
                        child_url = loc.text.strip()
                        if child_url not in visited_sitemaps:
                            stack.append((child_url, depth + 1))
            else:
                # URL Set：提取叶子页面 URL
                for url_elem in root.findall(f"{ns_prefix}url"):
                    if max_limit and len(results) >= max_limit:
                        break
                    loc = url_elem.find(f"{ns_prefix}loc")
                    if loc is not None and loc.text:
                        results.append(loc.text.strip())

        # 去重并保持顺序
        seen: set = set()
        unique: List[str] = []
        for u in results:
            if u not in seen:
                seen.add(u)
                unique.append(u)

        if max_limit:
            unique = unique[:max_limit]

        _log.info(f"[Generator] Sitemap 模式提取到 {len(unique)} 个 URL（来自 {target_url}）")
        return unique

    # ------------------------------------------------------------------
    # 私有：工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_scheme(url: str) -> str:
        """
        若 URL 缺少协议头则自动补全 https://。
        空字符串或无效输入返回原值。
        """
        url = url.strip()
        if not url:
            return url
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return url
