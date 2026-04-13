"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 链接路由与爬取扩散策略器
@Pattern : Chain of Responsibility（职责链最末节点） + Strategy Pattern（策略模式内嵌）
@Description:
    Strategist 是请求处理管线的最后一个节点，负责在当前页面处理完毕后
    决定如何扩散爬取范围（翻页/跳转到子页面）。
    核心职责：
    1. 维护严格的正则排除规则集（图片/静态资源/媒体/目标文件自身），
       防止 Crawlee 将 PDF 文件当作网页请求，或爬进 CDN 图片域名。
    2. 根据 crawl_strategy（full / direct / sitemap）决定是否调用 enqueue_links：
       - full / direct : 调用 context.enqueue_links(strategy='same-domain', exclude=...)
       - sitemap       : 不扩散（种子 URL 已由 SiteUrlGenerator 预先生成）
    3. 在页面关闭/上下文销毁时优雅降级（捕获 'Execution context was destroyed' 错误）。
    Pattern: Chain of Responsibility（管线末节点） + Strategy（策略映射）
"""

from __future__ import annotations

import re
from typing import Callable, List, TYPE_CHECKING

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import PrismSettings

_log = get_logger(__name__)

# 页面上下文已销毁时 Playwright/Crawlee 会抛出这些关键词的异常
_CONTEXT_DESTROYED_KEYWORDS = (
    "Execution context was destroyed",
    "Target closed",
    "Page closed",
    "Browser closed",
    "context was destroyed",
)


class Strategist:
    """
    链接路由与爬取扩散策略器（职责链末节点）。

    职责链入口：enqueue_next_pages() —— 在 default_handler 的最后一步调用。
    """

    def __init__(
        self,
        settings: "PrismSettings",
        is_running: Callable[[], bool],
    ) -> None:
        """
        Args:
            settings  : 全局参数单例（读取 crawl_strategy / file_type）。
            is_running: 状态检查函数（False 时立即返回）。
        """
        self._settings = settings
        self._is_running = is_running

        # 目标文件扩展名（用于排除目标文件自身被当作页面爬取）
        ft = settings.strategy_settings.file_type
        self._target_ext: str = ft if ft in ("img", "all") else ft  # 如 'pdf'

        # 预编译排除正则，避免每次调用重复编译
        self._exclude_patterns: List[re.Pattern] = self._get_exclude_patterns()

    # ------------------------------------------------------------------
    # 公开接口（职责链节点入口）
    # ------------------------------------------------------------------

    async def enqueue_next_pages(self, context: object) -> None:
        """
        翻页与路由扩散（职责链末节点入口）。

        执行流程：
        1. 检查 is_running() 和 context 是否支持 enqueue_links。
        2. 若 context.page 存在，先等待 DOM 稳定（wait_for_load_state，超时 2s 静默跳过）。
        3. 根据 crawl_strategy 决定是否调用 enqueue_links。
        4. 捕获 'Execution context was destroyed' 错误，降级为 INFO 日志，不污染错误报告。

        Args:
            context: Crawlee PlaywrightCrawlingContext（须含 .enqueue_links() 方法）。
        """
        if not self._is_running():
            return

        if not hasattr(context, "enqueue_links"):
            return

        if not self._should_enqueue():
            return

        # 等待页面 DOM 稳定，防止在页面还在加载时就开始提取链接
        page = getattr(context, "page", None)
        if page is not None:
            try:
                import asyncio
                await asyncio.wait_for(
                    page.wait_for_load_state("domcontentloaded"),
                    timeout=2.0,
                )
            except Exception:
                # 超时或页面已关闭：静默继续，enqueue_links 本身能处理此情形
                pass

        try:
            await context.enqueue_links(  # type: ignore[union-attr]
                strategy=self._settings.engine_settings.link_strategy,
                exclude=self._exclude_patterns,
            )
        except Exception as exc:
            exc_str = str(exc)
            if any(kw in exc_str for kw in _CONTEXT_DESTROYED_KEYWORDS):
                # 页面已销毁是正常情况（如 SPA 导航、弹窗关闭等），降级为 INFO
                _log.info(
                    f"[Strategist] enqueue_links 优雅降级（页面上下文已关闭）: {exc_str[:120]}"
                )
            else:
                _log.warning(f"[Strategist] enqueue_links 失败: {exc!r}")

    # ------------------------------------------------------------------
    # 私有工具
    # ------------------------------------------------------------------

    def _get_exclude_patterns(self) -> List[re.Pattern]:
        """
        构建链接排除正则表达式列表（大小写不敏感）。
        排除类型：
        - 视觉图片：.jpg / .jpeg / .png / .gif / .webp / .svg / .ico / .bmp
        - 前端静态资源：.css / .js / .woff / .woff2 / .ttf / .eot
        - 媒体文件：.mp4 / .webm / .mp3 / .avi / .mov
        - 压缩包与程序：.zip / .rar / .7z / .exe / .dmg
        - 目标文件自身（如 .pdf）：防止 Crawlee 将其当作网页再次请求

        Returns:
            编译好的 re.Pattern 列表，直接传入 enqueue_links(exclude=...) 参数。
        """
        flags = re.IGNORECASE

        patterns: List[re.Pattern] = [
            # 图片格式
            re.compile(
                r"\.(jpg|jpeg|png|gif|webp|svg|ico|bmp|tiff|avif)(\?.*)?$", flags
            ),
            # 前端静态资源
            re.compile(
                r"\.(css|js|jsx|ts|tsx|woff|woff2|ttf|eot|otf|map)(\?.*)?$", flags
            ),
            # 音视频媒体
            re.compile(
                r"\.(mp4|webm|mp3|ogg|avi|mov|mkv|flv|m4v|wav|aac)(\?.*)?$", flags
            ),
            # 压缩包与可执行程序
            re.compile(
                r"\.(zip|rar|7z|tar|gz|bz2|exe|dmg|pkg|deb|rpm|msi|apk|iso)(\?.*)?$",
                flags,
            ),
        ]

        # 将目标文件自身加入排除列表，防止 PDF 被当作页面重复请求
        ft = self._settings.strategy_settings.file_type
        if ft == "pdf":
            patterns.append(re.compile(r"\.pdf(\?.*)?$", flags))
        elif ft not in ("img", "all"):
            patterns.append(re.compile(rf"\.{re.escape(ft)}(\?.*)?$", flags))

        return patterns

    def _should_enqueue(self) -> bool:
        """
        根据 crawl_strategy 决定是否需要调用 enqueue_links 扩散。
        - 'full' / 'direct' → True（需要扩散）
        - 'sitemap'         → False（种子已预生成，无需扩散）

        Returns:
            True 表示应调用 enqueue_links；False 表示跳过扩散。
        """
        strategy = self._settings.strategy_settings.crawl_strategy
        return strategy in ("full", "direct")
