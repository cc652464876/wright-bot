"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 点击触发下载的执行引擎（表单突破 + 原生流落盘）
@Pattern : Chain of Responsibility（ActionHandler 调用的下游节点） +
           Competitive Race（asyncio.wait FIRST_COMPLETED 竞速检测）
@Description:
    ActionDownloader 专门处理 Playwright "点击按钮 → 触发下载" 的复杂交互场景，
    与 Downloader（直链下载）互补，共享其 semaphore / io_lock / downloaded_urls。
    核心机制：
    - 点击按钮后同时等待两个竞争事件（asyncio.wait FIRST_COMPLETED）：
      a. download 事件触发  → 直接调用 _process_and_save_payload() 落盘。
      b. 注册墙表单出现    → 调用 _fill_fake_form() 填充 Faker 假数据 → 提交表单 → 等待 download。
    - _is_target_file() 终点防线：落盘前检查文件名扩展名和 Content-Type，
      坚决过滤图片等非目标文件。
    - URL 级去重（继承 Downloader.downloaded_urls），防止同一文件被多次点击重复下载。
    - io_lock 保护文件名冲突处理（与 Downloader 共享同一把锁）。
    Pattern: Competitive Race（asyncio.wait）+ Deduplication（共享去重集合）
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import TYPE_CHECKING, Callable, Optional
from urllib.parse import urlparse

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page, Download
    from src.modules.site.handlers.downloader import Downloader

_log = get_logger(__name__)

# 落盘质检最小文件大小（字节）
_MIN_FILE_SIZE_BYTES: int = 1024

# 常见图片扩展名（小写，含点号）
_IMAGE_EXTS: frozenset = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff", ".avif"}
)

# 注册墙表单识别选择器
_FORM_DETECTION_SELECTOR = (
    'form input[type="email"], form input[name*="email" i], '
    'form input[name*="firstname" i], form input[name*="first_name" i]'
)


class ActionDownloader:
    """
    点击触发下载执行引擎（ActionHandler 调用的下游节点）。

    共享 Downloader 的核心资源：
    - semaphore       : 控制全局最大并发下载数（含 Downloader 的直链下载）。
    - io_lock         : 序列化文件名冲突处理。
    - downloaded_urls : 全局 URL 去重集合。
    - files_downloaded / files_active : 统计计数器（通过引用共享，UI 看到一致数值）。
    """

    def __init__(
        self,
        main_downloader: "Downloader",
    ) -> None:
        """
        Args:
            main_downloader: Downloader 实例，ActionDownloader 从中共享
                             semaphore / io_lock / downloaded_urls 以及统计属性。
        """
        self._dl = main_downloader  # 共享资源入口

    # ------------------------------------------------------------------
    # 公开接口（职责链节点入口）
    # ------------------------------------------------------------------

    async def execute(
        self,
        page: "Page",
        btn: object,
        source_url: str,
    ) -> None:
        """
        核心枢纽：点击按钮 → 竞速检测（下载流 vs 注册墙）→ 落盘（职责链入口）。

        执行流程：
        1. 获取 semaphore，递增 files_active。
        2. 随机延迟 0.5~2.2s（防反爬限速检测）。
        3. 挂起 page.wait_for_event('download', timeout=20000)。
        4. 执行 btn.click()。
        5. asyncio.wait([download_task, form_task], FIRST_COMPLETED) 竞速：
           - download 先触发 → _process_and_save_payload()。
           - 注册墙表单先出现 → _fill_fake_form() → 提交 → _process_and_save_payload()。
           - 均超时 → 日志警告，静默放弃。
        6. finally 递减 files_active。

        Args:
            page      : 当前 Playwright Page 实例。
            btn       : Playwright Locator 对象（目标按钮）。
            source_url: 触发此次点击的父页面 URL（用于域名计算和错误记录）。
        """
        async with self._dl.semaphore:
            self._dl.files_active += 1
            try:
                # Step 2: 拟人化随机延迟，规避时序特征检测
                await asyncio.sleep(random.uniform(0.5, 2.2))

                # Step 3 & 4: 先挂起下载事件侦听，再点击，防止错过瞬发 download 事件
                download_task = asyncio.create_task(
                    page.wait_for_event("download", timeout=20_000),
                    name="ad-download-event",
                )
                form_task = asyncio.create_task(
                    page.wait_for_selector(_FORM_DETECTION_SELECTOR, timeout=5_000),
                    name="ad-form-detection",
                )

                try:
                    await btn.click()  # type: ignore[union-attr]
                except Exception as click_exc:
                    _log.warning(
                        f"[ActionDownloader] 按钮点击失败 ({source_url[:80]}): {click_exc!r}"
                    )
                    download_task.cancel()
                    form_task.cancel()
                    # 等待任务清理，避免悬空 task 警告
                    await _cancel_tasks(download_task, form_task)
                    return

                # Step 5: 竞速等待（22s 总超时，稍大于 download 的 20s timeout）
                done, pending = await asyncio.wait(
                    {download_task, form_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=22.0,
                )

                # 取消未完成的竞争任务
                await _cancel_tasks(*pending)

                if not done:
                    _log.warning(
                        f"[ActionDownloader] 超时：下载和表单事件均未触发 ({source_url[:80]})"
                    )
                    return

                # ── 分析竞速结果 ──────────────────────────────────────────
                if download_task in done and not download_task.cancelled():
                    try:
                        dl_obj = download_task.result()
                        await self._process_and_save_payload(dl_obj, source_url)
                    except Exception as exc:
                        _log.warning(
                            f"[ActionDownloader] download 事件处理失败: {exc!r}"
                        )
                    return

                if form_task in done and not form_task.cancelled():
                    try:
                        form_task.result()  # 若任务本身抛出异常则跳过表单填充
                    except Exception:
                        return  # form_task 超时或异常：无表单，直接放弃

                    # 填写注册墙假数据
                    _log.info(
                        f"[ActionDownloader] 检测到注册墙，尝试填充假数据 ({source_url[:80]})"
                    )
                    await self._fill_fake_form(page)

                    # 表单提交后等待 download 事件
                    try:
                        dl_obj = await page.wait_for_event("download", timeout=15_000)
                        await self._process_and_save_payload(dl_obj, source_url)
                    except Exception as exc:
                        _log.warning(
                            f"[ActionDownloader] 表单填充后仍无下载事件: {exc!r}"
                        )

            except Exception as exc:
                _log.warning(f"[ActionDownloader] execute 意外错误: {exc!r}")
            finally:
                self._dl.files_active -= 1

    # ------------------------------------------------------------------
    # 私有：文件类型过滤
    # ------------------------------------------------------------------

    def _is_target_file(self, url: str, headers: dict) -> bool:
        """
        终点防线：判断即将落盘的文件是否为目标类型（如 PDF）。
        规则：
        1. 明确拒绝所有常见图片 URL 后缀（.jpg / .png / .gif 等）。
        2. 明确拒绝 content-type 为 image/* 的响应。
        3. 通过 content-type=application/pdf 或 URL 含目标扩展名确认为目标文件。
        4. application/octet-stream 类型需 URL 含目标扩展名痕迹才放行。

        Args:
            url    : 下载对象的实际 URL（download_obj.url）。
            headers: 响应头字典（download_obj 不直接暴露，通过 suggested_filename 推断）。
        Returns:
            True 表示是目标文件，可以落盘；False 表示应丢弃。
        """
        url_lower = url.lower().split("?")[0]
        ext = os.path.splitext(url_lower)[1]
        content_type = headers.get("content-type", headers.get("Content-Type", "")).lower()

        # 规则 1 & 2: 明确拒绝图片
        if ext in _IMAGE_EXTS:
            return False
        if content_type.startswith("image/"):
            return False

        target_ext: str = getattr(self._dl, "_target_ext", ".pdf")
        target_bare = target_ext.lower().lstrip(".")

        # 规则 3a: content-type 明确匹配
        if "application/pdf" in content_type and target_bare in ("pdf", "all"):
            return True
        if target_bare not in ("pdf", "img", "all") and target_bare in content_type:
            return True

        # 规则 3b: URL 含目标扩展名
        if target_ext.startswith(".") and url_lower.endswith(target_ext):
            return True
        if not target_ext.startswith(".") and f".{target_bare}" in url_lower:
            return True

        # 规则 4: octet-stream 需 URL 含扩展名痕迹才放行
        if "application/octet-stream" in content_type:
            return f".{target_bare}" in url_lower

        # target == 'all': 只要不是图片就放行
        if target_bare == "all":
            return not content_type.startswith("image/")

        return False

    # ------------------------------------------------------------------
    # 私有：表单填充
    # ------------------------------------------------------------------

    async def _fill_fake_form(self, page: "Page") -> None:
        """
        智能表单填充：使用 Faker 生成假身份信息填充注册墙表单，
        尝试绕过"留资获取报告"类表单。
        若 Faker 未安装，降级使用内置 DummyFake 占位数据。
        填充目标字段：First Name / Last Name / City / Email / Checkbox。

        Args:
            page: 当前 Playwright Page 实例（包含注册表单）。
        """
        # 生成假数据（优先 Faker，降级内置占位）
        try:
            from faker import Faker
            _fake = Faker()
            first_name = _fake.first_name()
            last_name = _fake.last_name()
            email = _fake.email()
            city = _fake.city()
            company = _fake.company()
        except ImportError:
            first_name = "John"
            last_name = "Smith"
            email = "john.smith@example.com"
            city = "New York"
            company = "Acme Corp"

        # (selector, value) 填充映射：从宽泛到精确，第一个可见 input 优先
        fill_targets = [
            ('input[name*="first" i], input[placeholder*="first" i]', first_name),
            ('input[name*="last" i], input[placeholder*="last" i]', last_name),
            ('input[type="email"], input[name*="email" i]', email),
            ('input[name*="city" i], input[placeholder*="city" i]', city),
            ('input[name*="company" i], input[name*="organization" i]', company),
        ]

        for selector, value in fill_targets:
            try:
                loc = page.locator(selector).first  # type: ignore[union-attr]
                if await loc.is_visible():
                    await loc.fill(value)
            except Exception:
                continue  # 字段不存在时静默跳过

        # 尝试勾选隐私政策复选框（常见注册墙必填项）
        try:
            checkbox = page.locator('input[type="checkbox"]').first  # type: ignore[union-attr]
            if await checkbox.is_visible() and not await checkbox.is_checked():
                await checkbox.click()
        except Exception:
            pass

        # 提交表单
        try:
            submit = page.locator(  # type: ignore[union-attr]
                'input[type="submit"], button[type="submit"], '
                'button:has-text("Submit"), button:has-text("提交"), '
                'button:has-text("Download"), button:has-text("Get")'
            ).first
            if await submit.is_visible():
                await submit.click()
                # 等待页面响应表单提交
                await page.wait_for_load_state("networkidle", timeout=5_000)  # type: ignore[union-attr]
        except Exception as exc:
            _log.debug(f"[ActionDownloader] 表单提交失败（忽略）: {exc!r}")

    # ------------------------------------------------------------------
    # 私有：落盘
    # ------------------------------------------------------------------

    async def _process_and_save_payload(
        self,
        download_obj: "Download",
        source_url: str,
    ) -> None:
        """
        全面接管 Playwright Download 对象的严格落盘逻辑。
        1. 检查 suggested_filename 扩展名（非目标则 cancel()）。
        2. URL 去重（继承 Downloader.downloaded_urls）。
        3. 计算目标域名目录（get_core_domain(source_url)）。
        4. 加 io_lock 处理文件名冲突，物理占坑（open + close）。
        5. 调用 download_obj.save_as() 执行真实落盘。
        6. 质检（> 1KB 计入 files_downloaded），失败通知 audit_center。

        Args:
            download_obj: Playwright Download 事件对象。
            source_url  : 触发下载的父页面 URL。
        """
        dl_url: str = download_obj.url
        suggested: str = download_obj.suggested_filename or "download"
        suggested_ext: str = os.path.splitext(suggested.lower())[1]
        domain: str = urlparse(source_url).netloc or "unknown_domain"

        # Step 1: 文件类型终点防线（基于文件名后缀推断）
        if suggested_ext in _IMAGE_EXTS:
            _log.debug(f"[ActionDownloader] 过滤图片文件: {suggested}")
            await download_obj.cancel()
            return

        target_ext: str = getattr(self._dl, "_target_ext", ".pdf")
        # 若扩展名明确且与目标不符，则取消（all 模式跳过此检查）
        if (
            suggested_ext
            and target_ext not in ("img", "all")
            and suggested_ext != target_ext.lower()
            and not dl_url.lower().split("?")[0].endswith(target_ext.lower())
        ):
            _log.debug(
                f"[ActionDownloader] 扩展名不匹配 ({suggested_ext} vs {target_ext}): {suggested}"
            )
            await download_obj.cancel()
            return

        # Step 2: URL 级去重（与 Downloader 共享同一集合）
        clean_url = dl_url.split("?")[0]
        if clean_url in self._dl.downloaded_urls:
            _log.debug(f"[ActionDownloader] 跳过重复 URL: {clean_url[:80]}")
            await download_obj.cancel()
            return
        self._dl.downloaded_urls.add(clean_url)

        # Step 3: 计算目标目录
        workspace: str = self._dl._audit_center._get_workspace(domain)

        # Step 4: io_lock 处理文件名冲突 + 物理占坑
        async with self._dl.io_lock:
            os.makedirs(workspace, exist_ok=True)
            base, ext = os.path.splitext(suggested)
            if not ext:
                ext = target_ext if target_ext.startswith(".") else f".{target_ext}"
            candidate = os.path.join(workspace, suggested)
            counter = 1
            while os.path.exists(candidate):
                candidate = os.path.join(workspace, f"{base}_{counter}{ext}")
                counter += 1
            # 物理占坑：防止并发竞争取到相同路径
            open(candidate, "wb").close()

        # Step 5: 真实落盘（在 io_lock 外执行，不阻塞其他文件命名操作）
        try:
            await download_obj.save_as(candidate)
        except Exception as exc:
            # 落盘失败：删除占坑文件，上报错误
            if os.path.exists(candidate) and os.path.getsize(candidate) == 0:
                os.remove(candidate)
            await self._dl._audit_center.record_download_failure(
                domain, dl_url, f"save_as 失败: {exc!r}"
            )
            _log.warning(
                f"[ActionDownloader] 落盘失败 ({candidate}): {exc!r}"
            )
            return

        # Step 6: 质检
        file_size = os.path.getsize(candidate)
        if file_size < _MIN_FILE_SIZE_BYTES:
            os.remove(candidate)
            await self._dl._audit_center.record_download_failure(
                domain, dl_url, f"文件过小 ({file_size}B < 1KB)"
            )
            return

        self._dl.files_downloaded += 1
        _log.info(
            f"[ActionDownloader] ✓ {os.path.basename(candidate)}  "
            f"{file_size // 1024}KB  {domain}"
        )


# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------

async def _cancel_tasks(*tasks: asyncio.Task) -> None:
    """取消并等待一组 asyncio.Task 完成清理，抑制所有异常。"""
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
