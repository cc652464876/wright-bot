"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 信号量限速下载器（原生流落盘 + 并发去重）
@Pattern : Semaphore Pattern（并发限速） + Deduplication（URL 去重集合）
@Description:
    Downloader 负责将目标文件 URL 通过 Playwright BrowserContext 的原生 API
    下载到本地磁盘，核心机制：
    - asyncio.Semaphore(5) 控制最大并发下载数，防止内存溢出和服务器封禁。
    - URL 清洗去重（剥离 query string 参数），防止带随机时间戳的 URL 重复下载。
    - asyncio.Lock（io_lock）保护物理文件名冲突处理（同名文件自动追加数字后缀）。
    - 落盘质检：文件大小 > 1KB 才计入 files_downloaded，否则删除并报告错误。
    - 失败时通过 audit_center.record_download_failure() 记录，不影响主流程。
    ActionDownloader 共享此类的 semaphore / io_lock / downloaded_urls，
    保证两条下载路径的并发总量受同一令牌桶约束。
    Pattern: Semaphore（并发控制） + Deduplication（内存集合去重）
"""

from __future__ import annotations

import asyncio
import os
from typing import Set, TYPE_CHECKING, Callable, Dict, Optional
from urllib.parse import urlparse, urlunparse

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page
    from src.config.settings import PrismSettings
    from src.modules.site.audit.audit_center import SiteAuditCenter

_log = get_logger(__name__)

# 常见图片扩展名集合（小写，含点号）
_IMAGE_EXTS: frozenset = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff", ".avif"}
)

# 落盘质检最小文件大小阈值（字节）
_MIN_FILE_SIZE_BYTES: int = 1024  # 1 KB


class Downloader:
    """
    信号量限速文件下载器（职责链节点）。

    共享状态（供 ActionDownloader 访问）：
    - semaphore       : asyncio.Semaphore，控制最大并发下载数。
    - io_lock         : asyncio.Lock，序列化物理文件名冲突处理。
    - downloaded_urls : Set[str]，内存级全局 URL 去重集合。

    统计属性（供 UI 仪表盘读取）：
    - files_found     : 发现的目标文件总数。
    - files_downloaded: 成功落盘的文件总数。
    - files_active    : 当前进行中的下载任务数。
    """

    def __init__(
        self,
        settings: "PrismSettings",
        audit_center: "SiteAuditCenter",
        is_running: Callable[[], bool],
        max_concurrency: int = 5,
    ) -> None:
        """
        Args:
            settings       : 全局参数单例（读取 save_directory / file_type 等）。
            audit_center   : 失败记录写入目标。
            is_running     : 状态检查函数（False 时立即返回不执行下载）。
            max_concurrency: 信号量上限（并发下载数），默认 5。
        """
        self._settings = settings
        self._audit_center = audit_center
        self._is_running = is_running

        # 共享并发原语（ActionDownloader 通过引用访问）
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.io_lock = asyncio.Lock()
        self.downloaded_urls: Set[str] = set()

        # UI 统计计数器（由 NetSniffer / native_download_task 更新）
        self.files_found: int = 0
        self.files_downloaded: int = 0
        self.files_active: int = 0

        # 目标扩展名（从 file_type 派生，如 '.pdf'）
        ft = settings.strategy_settings.file_type
        self._target_ext: str = f".{ft}" if ft not in ("img", "all") else ft

    # ------------------------------------------------------------------
    # 公开统计接口
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, int]:
        """
        返回当前下载统计快照（供 UI 仪表盘轮询）。

        Returns:
            {'files_found': int, 'files_downloaded': int, 'files_active': int}
        """
        return {
            "files_found":      self.files_found,
            "files_downloaded": self.files_downloaded,
            "files_active":     self.files_active,
        }

    # ------------------------------------------------------------------
    # 公开下载接口（职责链节点入口）
    # ------------------------------------------------------------------

    async def native_download_task(
        self,
        page: "Page",
        url: str,
        save_path: str,
        item: Dict[str, str],
    ) -> None:
        """
        后台异步原生下载任务（职责链入口）。
        通过 page.context.request.get() 使用 Playwright 共享上下文发起 HTTP 请求，
        完全继承当前防爬 Cookie / Session 状态。

        流程：
        1. 检查 is_running()，False 则立即返回。
        2. 获取 semaphore，更新 files_active。
        3. URL 清洗去重（剥离 query string），已下载则跳过。
        4. 发起 HTTP 请求，校验 content-type 过滤图片。
        5. 加 io_lock 处理文件名冲突（同名自动追加 _N 后缀）。
        6. 写入文件，质检（> 1KB 才计数），通知 audit_center 记录失败。
        7. finally 块递减 files_active。

        Args:
            page     : 当前 Playwright Page 实例（继承其 context）。
            url      : 目标文件绝对 URL。
            save_path: 预期保存路径（目录部分用于确定存放位置）。
            item     : parse_link() 返回的任务字典（含 file_name 等）。
        """
        if not self._is_running():
            return

        # ── Step 2: 获取并发令牌，进入下载临界区 ────────────────────────
        async with self.semaphore:
            self.files_active += 1
            domain = urlparse(url).netloc or "unknown_domain"
            try:
                # ── Step 3: URL 清洗去重 ──────────────────────────────────
                clean = self._clean_url(url)
                if clean in self.downloaded_urls:
                    _log.debug(f"[Downloader] 跳过重复 URL: {clean[:80]}")
                    return
                # 先占位，防止并发协程重复处理同一 URL
                self.downloaded_urls.add(clean)

                # ── Step 4: Playwright 原生 HTTP 请求（继承 Cookie/Session）
                response = await page.context.request.get(url)
                if not response.ok:
                    await self._audit_center.record_download_failure(
                        domain, url, f"HTTP {response.status}"
                    )
                    return

                content_type = response.headers.get("content-type", "")
                if self._is_image_content(url, content_type):
                    _log.debug(f"[Downloader] 过滤图片响应: {url[:80]}")
                    return

                # ── Step 5 / 6: io_lock 处理文件名冲突并落盘 ─────────────
                target_dir = os.path.dirname(save_path)
                filename = item.get("file_name", os.path.basename(save_path))
                final_path = await self._resolve_save_path(target_dir, filename)

                body: bytes = await response.body()
                with open(final_path, "wb") as fh:
                    fh.write(body)

                file_size = os.path.getsize(final_path)
                if file_size < _MIN_FILE_SIZE_BYTES:
                    os.remove(final_path)
                    await self._audit_center.record_download_failure(
                        domain, url, f"文件过小 ({file_size}B < 1KB)，已丢弃"
                    )
                    return

                self.files_downloaded += 1
                _log.info(
                    f"[Downloader] ✓ {os.path.basename(final_path)}  "
                    f"{file_size // 1024}KB  {domain}"
                )

            except Exception as exc:
                await self._audit_center.record_download_failure(
                    domain, url, str(exc)[:500]
                )
                _log.warning(f"[Downloader] 下载失败 {url[:80]}: {exc!r}")
            finally:
                # ── Step 7: 无论成功/失败都释放活跃计数 ────────────────
                self.files_active -= 1

    # ------------------------------------------------------------------
    # 私有工具
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_url(url: str) -> str:
        """
        剥离 URL 的 query string 参数，返回纯净路径 URL。
        防止 ?t=timestamp 等动态参数导致同一文件被重复下载。

        Args:
            url: 原始 URL 字符串。
        Returns:
            格式为 'scheme://netloc/path' 的纯净 URL。
        """
        parsed = urlparse(url)
        # 保留 scheme + netloc + path，清空 params / query / fragment
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _is_image_content(self, url: str, content_type: str) -> bool:
        """
        判断响应是否为图片类型（需过滤拦截）。
        检查 URL 后缀和 content-type 两个维度。

        Args:
            url         : 请求 URL。
            content_type: 响应的 Content-Type 头部值。
        Returns:
            True 表示是图片，应拦截；False 表示可以继续下载。
        """
        # 目标扩展名是图片类型时不过滤（img 模式专用）
        if self._target_ext in ("img", ".jpg", ".jpeg", ".png"):
            return False

        url_path = url.lower().split("?")[0]
        ext = os.path.splitext(url_path)[1]
        if ext in _IMAGE_EXTS:
            return True
        if content_type.lower().startswith("image/"):
            return True
        return False

    async def _resolve_save_path(self, target_dir: str, filename: str) -> str:
        """
        在 io_lock 保护下处理文件名冲突：若目标路径已存在，
        自动追加数字后缀（_1, _2, ...）直到找到空闲路径，
        并立即创建空占位文件防止并发竞争。

        Args:
            target_dir: 目标存储目录。
            filename  : 初始文件名。
        Returns:
            已占位的最终绝对文件路径。
        """
        async with self.io_lock:
            os.makedirs(target_dir, exist_ok=True)
            base, ext = os.path.splitext(filename)
            candidate = os.path.join(target_dir, filename)
            counter = 1
            while os.path.exists(candidate):
                candidate = os.path.join(target_dir, f"{base}_{counter}{ext}")
                counter += 1
            # 立即占坑：创建空文件，防止并发协程取到相同路径
            open(candidate, "wb").close()
            return candidate
