"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 全局错误去重登记册与 Markdown 报告导出
@Pattern : Registry Pattern（错误去重登记） + Context Variable（contextvars 跨层传递）
@Description:
    ErrorRegistry 负责拦截并聚合爬虫运行期间产生的所有异常，通过"代码位置哈希"
    算法对同类错误去重（而非按错误消息去重），彻底解决 Playwright 动态报错导致
    无法聚合的问题。
    error_interceptor 是一个异步上下文管理器（@asynccontextmanager），
    作为 AOP 切面无侵入地包裹 Playwright 操作，捕获异常后：
    1. 调用 ErrorRegistry.register_error() 登记并去重。
    2. 若为首次出现的新错误，自动对 Page 截图并保存 HTML 快照。
    3. 将原始异常重新抛出，交由 Crawlee 自身的重试机制处理。
    Pattern: Registry（错误去重存储） + AOP Context Manager（无侵入拦截）
             + Context Variable（通过 ContextVar 跨调用栈传递 Registry 实例）
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import AsyncIterator, Callable, Dict, Optional, Protocol, Set

from src.utils.logger import get_logger


class _ErrorSnapshotPage(Protocol):
    """Playwright Page 在 error_interceptor 中仅用到的接口；避免依赖 playwright 类型解析。"""

    async def screenshot(
        self,
        *,
        path: Optional[str] = None,
        full_page: Optional[bool] = None,
    ) -> bytes: ...

    async def content(self) -> str: ...


_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# ContextVar 声明（通过 contextvars 跨调用栈无侵入传递 Registry 实例）
# ---------------------------------------------------------------------------

current_registry: ContextVar[Optional["ErrorRegistry"]] = ContextVar(
    "current_registry", default=None
)
current_error_dir: ContextVar[Optional[str]] = ContextVar(
    "current_error_dir", default=None
)
# 站点策略在 run() 中注入：接受 domain 返回该域工作区绝对路径（通常 audit._get_workspace）
current_error_workspace_resolver: ContextVar[Optional[Callable[[str], str]]] = ContextVar(
    "current_error_workspace_resolver", default=None
)


# ---------------------------------------------------------------------------
# 错误登记册
# ---------------------------------------------------------------------------

class ErrorRegistry:
    """
    全局错误去重登记册。

    使用"代码位置指纹"（异常类型 + 源文件名 + 行号的 MD5[:8]）对错误去重，
    而非使用错误消息字符串（Playwright 的动态消息包含变量数据，会导致无法聚合）。
    每种唯一错误只保存一份完整信息，重复出现时仅递增计数和追加受影响的 URL。

    Pattern: Registry Pattern —— 集中存储、按指纹键检索。
    """

    def __init__(self) -> None:
        """
        初始化空登记册。
        _errors 结构：
        {
            err_id: {
                'type'      : str,          # 异常类名
                'location'  : str,          # 'filename:lineno' 定位字符串
                'first_time': str,          # 首次发生时间
                'message'   : str,          # 截断的错误消息（最多 300 字符）
                'urls'      : Set[str],     # 受影响 URL 集合（最多 50 个）
                'count'     : int,          # 累计出现次数
            }
        }
        """
        self._errors: Dict[str, Dict] = {}
        # threading.Lock 而非 asyncio.Lock：register_error 是同步方法，
        # 可能被 loguru 后台线程或异步任务并发调用。
        self._lock = threading.Lock()

    def register_error(
        self,
        exc_val: BaseException,
        exc_tb: object,
        url: str = "",
    ) -> Dict[str, object]:
        """
        登记一条异常记录（去重逻辑核心）。

        Args:
            exc_val: 异常实例（sys.exc_info()[1]）。
            exc_tb : 异常 traceback 对象（sys.exc_info()[2]）。
            url    : 发生错误时正在处理的页面 URL（可为空）。
        Returns:
            {'is_new': bool, 'err_id': str}
            is_new=True 表示这是首次出现的新错误，调用方应触发截图快照。
        """
        err_id, location_str = self._generate_fingerprint(exc_val, exc_tb)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        exc_type_name = type(exc_val).__name__
        # 截断消息，避免动态内容导致的存储膨胀
        message = str(exc_val)[:300]

        with self._lock:
            if err_id not in self._errors:
                self._errors[err_id] = {
                    "type":       exc_type_name,
                    "location":   location_str,
                    "first_time": now,
                    "message":    message,
                    "urls":       set(),
                    "count":      0,
                }
                is_new = True
            else:
                is_new = False

            entry = self._errors[err_id]
            entry["count"] += 1
            if url and len(entry["urls"]) < 50:
                entry["urls"].add(url)

        if is_new:
            _log.warning(
                f"[ErrorRegistry] 新错误 #{err_id} {exc_type_name} @ {location_str}"
            )
        else:
            _log.debug(
                f"[ErrorRegistry] 已知错误 #{err_id} 第 {entry['count']} 次重现"
            )

        return {"is_new": is_new, "err_id": err_id}

    def export_to_markdown(self, filepath: str) -> None:
        """
        将所有已登记的错误导出为 Markdown 格式的聚合报告文件。
        报告结构：标题 → 统计摘要 → 每条错误的详细信息（类型/位置/计数/消息/URL）。

        Args:
            filepath: 输出 Markdown 文件的绝对路径。
        """
        # 在锁外做文件 I/O；先在锁内复制一份快照，避免持锁时 I/O 阻塞
        with self._lock:
            errors_snapshot = {
                k: {**v, "urls": set(v["urls"])}
                for k, v in self._errors.items()
            }

        summary = self.get_summary()
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines: list[str] = [
            "# 错误聚合报告\n\n",
            f"**生成时间：** {generated_at}  \n",
            f"**唯一错误数：** {summary['unique_errors']}  \n",
            f"**总发生次数：** {summary['total_occurrences']}  \n\n",
            "---\n\n",
        ]

        for i, (err_id, entry) in enumerate(errors_snapshot.items(), start=1):
            urls_block = (
                "\n".join(f"  - `{u}`" for u in sorted(entry["urls"]))
                if entry["urls"]
                else "  - *(无记录)*"
            )
            lines.append(
                f"## #{i} `{err_id}` — {entry['type']}\n\n"
                f"| 属性 | 值 |\n"
                f"|------|----|\n"
                f"| **位置** | `{entry['location']}` |\n"
                f"| **首次发生** | {entry['first_time']} |\n"
                f"| **累计次数** | {entry['count']} |\n\n"
                f"**错误消息：**\n```\n{entry['message']}\n```\n\n"
                f"**受影响 URL（最多 50 条）：**\n{urls_block}\n\n"
                "---\n\n"
            )

        parent_dir = os.path.dirname(filepath)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.writelines(lines)

        _log.info(
            f"[ErrorRegistry] 已导出错误报告 → {filepath} "
            f"（{summary['unique_errors']} 种 / {summary['total_occurrences']} 次）"
        )

    def iter_domains_with_errors(self) -> Set[str]:
        """从已登记条目的 urls 中收集核心域名（用于分域导出 error_report）。"""
        from src.modules.site.parser import SiteDataParser

        domains: Set[str] = set()
        with self._lock:
            for entry in self._errors.values():
                for u in entry["urls"]:
                    domains.add(SiteDataParser.get_core_domain(u))
        return domains

    def has_entries_without_urls(self) -> bool:
        """是否存在完全无 URL 上下文的错误条目（归入 unknown_domain 报告）。"""
        with self._lock:
            return any(len(e["urls"]) == 0 for e in self._errors.values())

    def export_to_markdown_for_domain(self, filepath: str, domain: str) -> None:
        """
        按域名导出 Markdown：仅包含「至少一条 URL 属于 domain」的条目；
        无 URL 的条目仅写入 domain == 'unknown_domain' 的报告。
        """
        from src.modules.site.parser import SiteDataParser

        with self._lock:
            snapshot = {
                k: {**v, "urls": set(v["urls"])}
                for k, v in self._errors.items()
            }

        blocks: list[tuple[str, Dict, list[str]]] = []
        for err_id, entry in snapshot.items():
            matching = sorted(
                u for u in entry["urls"] if SiteDataParser.get_core_domain(u) == domain
            )
            if matching:
                blocks.append((err_id, entry, matching))
            elif not entry["urls"] and domain == "unknown_domain":
                blocks.append((err_id, entry, []))

        if not blocks:
            return

        total_occ = sum(entry["count"] for _, entry, _ in blocks)
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines: list[str] = [
            f"# 错误报告（域名：{domain}）\n\n",
            f"**生成时间：** {generated_at}  \n",
            f"**本文件条目数：** {len(blocks)}  \n",
            f"**本文件涉及发生次数合计：** {total_occ}  \n\n",
            "---\n\n",
        ]

        for i, (err_id, entry, url_list) in enumerate(blocks, start=1):
            urls_block = (
                "\n".join(f"  - `{u}`" for u in url_list)
                if url_list
                else "  - *(无 URL 上下文)*"
            )
            lines.append(
                f"## #{i} `{err_id}` — {entry['type']}\n\n"
                f"| 属性 | 值 |\n"
                f"|------|----|\n"
                f"| **位置** | `{entry['location']}` |\n"
                f"| **首次发生** | {entry['first_time']} |\n"
                f"| **累计次数** | {entry['count']} |\n\n"
                f"**错误消息：**\n```\n{entry['message']}\n```\n\n"
                f"**本域相关 URL：**\n{urls_block}\n\n"
                "---\n\n"
            )

        parent_dir = os.path.dirname(filepath)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.writelines(lines)

        _log.info(
            f"[ErrorRegistry] 已导出分域错误报告 → {filepath}（{len(blocks)} 条）"
        )

    def get_summary(self) -> Dict[str, int]:
        """
        返回错误汇总统计（供 UI 展示或日志输出）。

        Returns:
            {'unique_errors': int, 'total_occurrences': int}
        """
        with self._lock:
            total = sum(entry["count"] for entry in self._errors.values())
            return {
                "unique_errors":    len(self._errors),
                "total_occurrences": total,
            }

    def clear(self) -> None:
        """
        清空所有登记记录（任务重置时调用）。
        """
        with self._lock:
            self._errors.clear()
        _log.debug("[ErrorRegistry] 已清空所有错误记录")

    # ------------------------------------------------------------------
    # 私有：指纹生成
    # ------------------------------------------------------------------

    def _generate_fingerprint(
        self,
        exc_val: BaseException,
        exc_tb: object,
    ) -> tuple:
        """
        生成错误指纹：逆向遍历 traceback，找到最后一个属于项目代码（非 site-packages）的帧，
        拼接 '{ExceptionType}|{filename}:{lineno}' 后取 MD5[:8] 作为唯一 ID。

        Args:
            exc_val: 异常实例。
            exc_tb : traceback 对象。
        Returns:
            (err_id: str, location_str: str) 元组。
        """
        exc_type_name = type(exc_val).__name__
        filename = "<unknown>"
        lineno = 0

        if exc_tb is not None:
            tb = exc_tb  # type: ignore[assignment]
            last_project_frame: Optional[tuple] = None
            while tb is not None:
                frame_filename: str = tb.tb_frame.f_code.co_filename  # type: ignore[union-attr]
                # 跳过所有第三方库帧（site-packages、标准库 lib/python）
                if (
                    "site-packages" not in frame_filename
                    and "lib" + os.sep + "python" not in frame_filename.lower()
                    and "lib/python" not in frame_filename
                ):
                    last_project_frame = (frame_filename, tb.tb_lineno)  # type: ignore[union-attr]
                tb = tb.tb_next  # type: ignore[union-attr]

            if last_project_frame is not None:
                filename = os.path.basename(last_project_frame[0])
                lineno = last_project_frame[1]

        location_str = f"{filename}:{lineno}"
        raw = f"{exc_type_name}|{location_str}"
        err_id = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
        return err_id, location_str


# ---------------------------------------------------------------------------
# AOP 异步上下文管理器（贴身保镖拦截器）
# ---------------------------------------------------------------------------

@asynccontextmanager
async def error_interceptor(
    page: _ErrorSnapshotPage,
    current_url: str = "Unknown_URL",
) -> AsyncIterator[None]:
    """
    无侵入异步错误拦截器（AOP 切面）。

    通过 contextvars 获取当前任务的 ErrorRegistry 实例与错误目录。
    错误目录解析顺序：
    1. 若 `current_error_dir` 已设为绝对路径，直接使用（兼容旧调用方）。
    2. 否则若存在 `current_error_workspace_resolver`，用 `get_core_domain(current_url)`
       得到 domain，再 `os.path.join(resolver(domain), "errors")`（按域隔离快照）。
    若 registry 为 None 则直接放行不做拦截。

    拦截流程：
    1. try/except 捕获 with 块内的所有异常。
    2. 调用 registry.register_error() 去重登记。
    3. 若 is_new=True（首次出现的新错误），执行防御性快照：
       - page.screenshot() → {err_id}_screenshot.png
       - page.content()    → {err_id}_page.html
       （整个快照操作包裹在 asyncio.wait_for(timeout=5.0) 中防止二次崩溃）
    4. 将原始异常 re-raise，交由 Crawlee 的重试机制处理。

    Pattern: AOP Context Manager —— 无需修改业务代码，只需 `async with error_interceptor(page, url):`

    Args:
        page       : 当前 Playwright Page 实例（用于截图）。
        current_url: 正在处理的页面 URL（用于错误记录）。

    Yields:
        None（标准异步上下文管理器协议）。
    """
    registry: Optional[ErrorRegistry] = current_registry.get()
    err_dir: Optional[str] = current_error_dir.get()
    if err_dir is None:
        resolver = current_error_workspace_resolver.get()
        if resolver is not None and current_url:
            from src.modules.site.parser import SiteDataParser

            dom = SiteDataParser.get_core_domain(current_url)
            err_dir = os.path.join(resolver(dom), "errors")

    # 不在受保护上下文内——直接透传，不做任何拦截
    if registry is None:
        yield
        return

    try:
        yield
    except BaseException:
        _, exc_val, exc_tb = sys.exc_info()
        if exc_val is None:
            raise  # 极少情况：exc_info 为空则直接重抛

        result = registry.register_error(exc_val, exc_tb, url=current_url)
        err_id: str = str(result["err_id"])

        # 仅对首次出现的新错误触发快照，避免大量重复文件
        if result.get("is_new") and err_dir is not None and page is not None:
            async def _take_snapshot() -> None:
                os.makedirs(err_dir, exist_ok=True)
                screenshot_path = os.path.join(err_dir, f"{err_id}_screenshot.png")
                html_path = os.path.join(err_dir, f"{err_id}_page.html")
                await page.screenshot(path=screenshot_path, full_page=False)
                html_content = await page.content()
                with open(html_path, "w", encoding="utf-8") as fh:
                    fh.write(html_content)

            try:
                await asyncio.wait_for(_take_snapshot(), timeout=5.0)
                _log.debug(
                    f"[error_interceptor] 快照已保存：{err_id}_screenshot.png / {err_id}_page.html"
                )
            except Exception as snap_exc:
                # 快照本身出错时仅记录日志，不影响主流程
                _log.debug(
                    f"[error_interceptor] 快照保存失败（忽略）: {snap_exc!r}"
                )

        raise
