"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 底层网络响应拦截探针
@Pattern : Observer Pattern（监听 Playwright response 事件） +
           Producer-Consumer（asyncio.Queue 异步解耦落盘 I/O）
@Description:
    NetSniffer 在 Playwright Page 的 response 事件上注册监听器，
    绕过 DOM 直接截获通过 AJAX / Fetch / 302 重定向加载的目标文件流
    （如 content-type: application/pdf 的响应）。
    为避免在高频事件回调中执行耗时 I/O 操作（写 audit_center）导致事件循环卡顿，
    采用 asyncio.Queue 将"事件捕获"和"数据持久化"解耦：
    - handle_response（事件监听器）：捕获目标响应后非阻塞 put_nowait 到队列。
    - _queue_worker（后台 Task）    ：独立消费队列，执行 audit_center.record_result_batch。
    任务结束时通过"毒药丸"（None sentinel）通知 Worker 安全退出，
    并通过 queue.join() 等待所有落盘完成（Graceful Shutdown）。
    Pattern: Observer（response 事件） + Producer-Consumer（Queue 解耦）
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Optional

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.modules.site.parser import SiteDataParser
    from src.modules.site.audit.audit_center import SiteAuditCenter
    from src.modules.site.handlers.downloader import Downloader

_log = get_logger(__name__)

# 这些 content-type 属于页面框架资源，永远不是下载目标
_SKIP_CONTENT_TYPES = (
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/json",
    "application/xml",
    "text/xml",
)


class NetSniffer:
    """
    底层网络响应拦截探针（职责链节点）。

    attach_probe() 是职责链的入口方法，在每个 Crawlee request handler
    调用时注册 response 监听器；Queue Worker 在首次 attach_probe() 时惰性启动。

    Pattern: Observer + Producer-Consumer
    """

    def __init__(
        self,
        parser: "SiteDataParser",
        target_ext: str,
        is_running: Callable[[], bool],
    ) -> None:
        """
        Args:
            parser    : SiteDataParser 实例，用于解析拦截到的响应 URL。
            target_ext: 目标文件扩展名（如 '.pdf'），用于 content-type 过滤。
            is_running: 状态检查函数（返回 True 表示任务运行中）。
        """
        self._parser = parser
        self._target_ext = target_ext.lower().lstrip(".")  # 统一存储无点号形式，如 'pdf'
        self._is_running = is_running

        # Producer-Consumer 核心：无界队列避免回调阻塞
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

        # audit_center 在首次 attach_probe 时绑定（任务期间全局唯一实例）
        self._audit_center: Optional["SiteAuditCenter"] = None

    # ------------------------------------------------------------------
    # 公开接口（职责链节点入口）
    # ------------------------------------------------------------------

    async def attach_probe(
        self,
        context: object,
        domain: str,
        domain_workspace: str,
        audit_center: "SiteAuditCenter",
        downloader: "Downloader",
    ) -> None:
        """
        在当前 Crawlee 请求上下文（context.page）上注册 response 监听器。
        首次调用时惰性启动后台 Queue Worker Task。

        Args:
            context         : Crawlee PlaywrightCrawlingContext（含 .page 属性）。
            domain          : 当前页面所属核心域名。
            domain_workspace: 该域名的物理存储目录路径（供统计日志使用）。
            audit_center    : SiteAuditCenter 实例（Worker 消费时写入）。
            downloader      : Downloader 实例（更新 files_found 统计计数器）。
        """
        # 绑定 audit_center（同一任务内只有一个实例，首次赋值后不再变更）
        if self._audit_center is None:
            self._audit_center = audit_center

        # 惰性启动后台 Worker（任务生命周期内只启动一次）
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._queue_worker(), name="net-sniffer-worker"
            )

        page = getattr(context, "page", None)
        if page is None:
            return

        # 构建绑定了当前请求上下文变量的响应事件处理器
        handler = self._build_response_handler(context, domain, audit_center, downloader)
        page.on("response", handler)

        # 对称性：页面关闭时自动注销监听器，防止悬空引用与内存泄漏
        page.once("close", lambda: page.remove_listener("response", handler))

    async def stop(self) -> None:
        """
        安全停止 Queue Worker（Graceful Shutdown）。
        向队列投入 None（毒药丸）→ 等待队列消费完毕（queue.join()）→ 取消 Worker Task。
        由 SiteCrawlStrategy.cleanup() 在 crawler.run() 返回后调用。
        """
        if self._worker_task is None:
            return

        # 毒药丸：通知 Worker 在处理完剩余所有 item 后退出
        await self._queue.put(None)

        # 等待队列中所有 item（含毒药丸）都被 task_done() 确认
        await self._queue.join()

        # Worker 此时已退出循环，取消是 no-op，但显式取消防止极端竞态
        if not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # 私有：Queue Worker
    # ------------------------------------------------------------------

    async def _queue_worker(self) -> None:
        """
        后台异步 Worker：持续消费队列中的数据并调用 audit_center.record_result_batch 落盘。
        接收到 None（毒药丸）时安全退出循环。
        即使 is_running() 返回 False，也继续消费队列中剩余数据（确保数据不丢失）。
        """
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    break  # 毒药丸：干净退出

                domain, source_page, urls = item
                if self._audit_center is not None:
                    try:
                        await self._audit_center.record_result_batch(domain, source_page, urls)
                    except Exception as exc:
                        _log.warning(f"[NetSniffer] record_result_batch 失败: {exc!r}")
            except Exception as exc:
                _log.warning(f"[NetSniffer] Worker 意外错误: {exc!r}")
            finally:
                # task_done() 必须与每个 get() 严格配对，queue.join() 依赖此保证
                self._queue.task_done()

    # ------------------------------------------------------------------
    # 私有：响应事件处理器（闭包，由 attach_probe 动态创建）
    # ------------------------------------------------------------------

    def _build_response_handler(
        self,
        context: object,
        domain: str,
        audit_center: "SiteAuditCenter",
        downloader: "Downloader",
    ) -> Callable:
        """
        工厂方法：创建绑定了当前请求上下文变量的 handle_response 闭包函数。
        将其返回供 page.on('response', handler) 注册。

        Args:
            context     : Crawlee 上下文（用于获取 context.page.url 作为 source_page）。
            domain      : 当前域名。
            audit_center: 落盘目标审计中心。
            downloader  : 统计更新目标下载器。
        Returns:
            async 闭包函数，签名为 (response) -> None。
        """
        # 在侦察请求被处理时固化 source_page URL（闭包捕获）
        source_page_url: str = getattr(
            getattr(context, "request", None), "url", "Unknown"
        )

        async def handle_response(response) -> None:  # type: ignore[no-untyped-def]
            # 快速门卫：任务已停止则立即返回，防止空转
            if not self._is_running():
                return
            try:
                # 只处理 2xx 成功响应
                if not (200 <= response.status < 300):
                    return

                url: str = response.url
                headers: dict = response.headers
                content_type: str = headers.get("content-type", "").lower()

                if not self._is_target_response(url, content_type):
                    return

                # 统计计数：仅递增，asyncio 单线程保证原子性
                downloader.files_found += 1
                # 非阻塞入队：响应事件回调必须快速返回
                self._queue.put_nowait((domain, source_page_url, [url]))
                _log.debug(
                    f"[NetSniffer] 拦截 ✓  {url[:80]}  "
                    f"ct={content_type[:40]}  domain={domain}"
                )
            except Exception as exc:
                # 绝不让事件回调异常传播到 Playwright 内部
                _log.debug(f"[NetSniffer] handle_response 内部错误（忽略）: {exc!r}")

        return handle_response

    # ------------------------------------------------------------------
    # 私有：目标文件类型判断
    # ------------------------------------------------------------------

    def _is_target_response(self, url: str, content_type: str) -> bool:
        """
        综合 content-type 与 URL 路径后缀判断响应是否为爬取目标文件。

        Args:
            url         : 响应 URL。
            content_type: 响应 Content-Type 头（已转小写）。
        Returns:
            True 表示是目标文件，应拦截并记录。
        """
        # 页面框架资源快速排除
        for skip in _SKIP_CONTENT_TYPES:
            if skip in content_type:
                return False

        url_lower = url.lower().split("?")[0]
        ct = content_type

        target = self._target_ext  # 已是无点号小写，如 'pdf' / 'img' / 'all'

        if target == "pdf":
            return "application/pdf" in ct or url_lower.endswith(".pdf")

        if target in ("jpg", "jpeg", "png", "gif", "webp", "img"):
            img_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp")
            return ct.startswith("image/") or any(url_lower.endswith(e) for e in img_exts)

        if target == "all":
            # 排除纯文本和脚本类型，其余均视为目标
            return bool(ct) and not any(skip in ct for skip in _SKIP_CONTENT_TYPES)

        # 通用扩展名匹配
        return url_lower.endswith(f".{target}") or target in ct
