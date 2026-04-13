"""
金丝雀合成任务策略：继承 SiteCrawlStrategy，复用 Crawlee + 挑战检测链路，
拦截默认页面处理（不落库、不拉 PDF）。Sannysoft 与 QUADRANT_HANDLERS 注册探针
写看板；hardware 等未注册象限仍使用 title + HTTP 占位断言。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.config.settings import PrismSettings
from src.modules.canary.dashboard import (
    mark_all_failed,
    reset_for_new_run,
    set_progress,
    set_quadrant_group,
)
from src.modules.canary.probes_combat import run_combat_probe
from src.modules.canary.probes_network import run_network_probe
from src.modules.canary.probes_sannysoft import (
    DOM_PROBE_FAIL_DESC,
    apply_sannysoft_probe_updates,
    run_sannysoft_identity_probe,
)
from src.engine.state_manager import CrawlerState
from src.modules.site.audit.error_registry import ErrorRegistry
from src.modules.site.generator import SiteUrlGenerator
from src.modules.site.strategy import SiteCrawlStrategy
from src.utils.logger import get_logger

_log = get_logger(__name__)

QuadrantProbe = Callable[
    [Any, Any, str, PrismSettings],
    Awaitable[List[Tuple[str, str, str]]],
]

QUADRANT_HANDLERS: Dict[str, QuadrantProbe] = {
    "network": run_network_probe,
    "combat": run_combat_probe,
}

# 与 dashboard 中 quadrants.*.id 一一对应（每项 1 个种子 URL）
DEFAULT_CANARY_SEED_URLS: List[str] = [
    "https://httpbin.org/get",
    "https://bot.sannysoft.com/",
    "https://browserleaks.com/webgl",
    "https://www.cloudflare.com/",
]

# (象限键, [item_id, ...]) 与 DEFAULT_CANARY_SEED_URLS 按下标对齐
_CANARY_QUADRANT_GROUPS: List[Tuple[str, List[str]]] = [
    ("network", ["tls_ja3", "http_headers", "webrtc_leak"]),
    ("identity", ["identity_locale", "viewport_fit"]),
    ("hardware", ["webgl_vendor", "canvas_audio"]),
    ("combat", ["cf_shield", "cdp_automation", "behavior_score"]),
]

# 无法解析域名时的显式归因（带告警日志），非「静默回退第一象限」
_CANARY_UNMAPPED_HOST_QUADRANT: Tuple[str, List[str]] = _CANARY_QUADRANT_GROUPS[0]

# Crawlee Request.user_data：保留入队时的原始种子 URL，供 CDN/301 后仍归因象限
_CANARY_USER_SEED_KEY = "canary_seed_url"

# 父类 run 整段（含 crawler + 排空）的全局硬超时，避免底层挂死导致 Context 泄漏
_CANARY_RUN_HARD_TIMEOUT_SECS = 180.0

_DESC_MAX = 500


def _normalize_canary_hostname(hostname: Optional[str]) -> str:
    h = (hostname or "").lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def _build_canary_host_to_quadrant() -> Dict[str, Tuple[str, List[str]]]:
    m: Dict[str, Tuple[str, List[str]]] = {}
    for i, seed in enumerate(DEFAULT_CANARY_SEED_URLS):
        if i >= len(_CANARY_QUADRANT_GROUPS):
            break
        host = _normalize_canary_hostname(urlparse(seed).hostname)
        if host:
            m[host] = _CANARY_QUADRANT_GROUPS[i]
    # 常见跳转/子域
    m.setdefault("sannysoft.com", _CANARY_QUADRANT_GROUPS[1])
    return m


_CANARY_HOST_TO_QUADRANT: Dict[str, Tuple[str, List[str]]] = (
    _build_canary_host_to_quadrant()
)


class _CanaryWorkspaceStub:
    """仅满足父类 run()/cleanup 对 audit 工作区路径的依赖，不打开 DB、不写生产审计。"""

    __slots__ = ("_root",)

    def __init__(self, save_directory: str) -> None:
        self._root = os.path.join(save_directory, "_canary_workspace")

    def _get_workspace(self, domain: str) -> str:
        safe = domain.replace(os.sep, "_").replace(":", "_")[:120] or "unknown_domain"
        p = os.path.join(self._root, safe)
        os.makedirs(p, exist_ok=True)
        return p

    async def export_final_reports(self) -> None:
        return None

    def get_total_scraped_count(self) -> int:
        return 0

    def set_task_id(self, task_id: int) -> None:
        return None

    async def record_page_success(
        self,
        domain: str,
        url: str,
        status_code: int = 200,
    ) -> None:
        return None

    async def record_page_failure(
        self,
        domain: str,
        url: str,
        status_code: int,
        error_msg: str,
    ) -> None:
        return None

    async def record_interaction(self, domain: str, interaction_data: dict) -> None:
        return None

    async def record_result_batch(
        self,
        domain: str,
        source_page: str,
        new_file_urls: List[str],
    ) -> None:
        return None


class CanaryMockStrategy(SiteCrawlStrategy):
    """
    端到端合成任务：走 SiteRunner → Crawlee → 与生产一致的反爬前置，
    页面级处理替换为靶场占位断言（page.title 等）。
    """

    is_canary_strategy: bool = True

    def get_strategy_name(self) -> str:
        return "金丝雀体检（合成任务）"

    @staticmethod
    def _safe_page_url(page: Any) -> Optional[str]:
        if page is None:
            return None
        try:
            u = getattr(page, "url", None)
            return str(u).strip() if u else None
        except Exception:
            return None

    @staticmethod
    def _canary_safe_user_seed(request: Any) -> Optional[str]:
        if request is None:
            return None
        ud = getattr(request, "user_data", None)
        if ud is None:
            return None
        try:
            raw = ud.get(_CANARY_USER_SEED_KEY) if hasattr(ud, "get") else ud[_CANARY_USER_SEED_KEY]
        except Exception:
            return None
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None

    @staticmethod
    def _canary_url_candidates(request: Any, page_url: Optional[str]) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []

        def push(u: Optional[str]) -> None:
            if not u or not isinstance(u, str):
                return
            u = u.strip()
            if not u or u in seen:
                return
            seen.add(u)
            out.append(u)

        if request is not None:
            push(CanaryMockStrategy._canary_safe_user_seed(request))
            push(getattr(request, "url", None))
            push(getattr(request, "loaded_url", None))
        push(page_url)
        return out

    def _quadrant_from_seed_hosts(
        self,
        hosts: List[str],
    ) -> Optional[Tuple[str, List[str]]]:
        """精确 host 未命中表项时，用语种子的注册域后缀匹配（子域 / 部分 CDN 跳转）。"""
        for ch in hosts:
            for i, seed in enumerate(DEFAULT_CANARY_SEED_URLS):
                sh = _normalize_canary_hostname(urlparse(seed).hostname)
                if not sh:
                    continue
                if ch == sh or ch.endswith("." + sh):
                    if i < len(_CANARY_QUADRANT_GROUPS):
                        g, items = _CANARY_QUADRANT_GROUPS[i]
                        return (g, list(items))
        return None

    def _quadrant_from_request(
        self,
        request: Any,
        page_url: Optional[str],
        *,
        extra_first: Optional[List[str]] = None,
    ) -> Tuple[str, List[str], bool]:
        """
        综合 user_data 原始种子、request.url、loaded_url、当前页 URL 解析象限。
        Returns:
            (group, item_ids, mapped)
        """
        seen: set[str] = set()
        urls: List[str] = []

        def push(u: Optional[str]) -> None:
            if not u or not isinstance(u, str):
                return
            u = u.strip()
            if not u or u in seen:
                return
            seen.add(u)
            urls.append(u)

        if extra_first:
            for u in extra_first:
                push(u)
        for u in self._canary_url_candidates(request, page_url):
            push(u)

        for u in urls:
            g, items, mapped = self._resolve_quadrant_for_url(u)
            if mapped:
                return g, items, True

        hosts: List[str] = []
        for u in urls:
            try:
                h = _normalize_canary_hostname(urlparse(u).hostname)
            except Exception:
                h = ""
            if h and h not in hosts:
                hosts.append(h)

        suf = self._quadrant_from_seed_hosts(hosts)
        if suf is not None:
            return (*suf, True)

        worst = (
            urls[0]
            if urls
            else (getattr(request, "url", None) if request is not None else None)
            or page_url
            or "Unknown"
        )
        _log.warning(
            "[CanaryMockStrategy] 未在金丝雀域名表中命中（已试种子/重定向/后缀匹配）: {}",
            str(worst)[:300],
        )
        g, items = _CANARY_UNMAPPED_HOST_QUADRANT
        return g, list(items), False

    def _canary_fsm_saw_error(self) -> bool:
        """本次任务内 FSM 是否曾进入 ERROR（终态常为 STOPPED，须查历史）。"""
        sm = self._state_manager
        if sm is None:
            return False
        for _ts, _old, new_st, _reason in sm.get_history():
            if new_st == CrawlerState.ERROR:
                return True
        return False

    async def run(self) -> None:
        self._canary_seeds: List[str] = list(self.settings.strategy_settings.target_urls)
        n = len(self._canary_seeds)
        if n != len(_CANARY_QUADRANT_GROUPS):
            _log.warning(
                "[CanaryMockStrategy] 靶场 URL 数量({})与象限组数({})不一致，按最短长度对齐",
                n,
                len(_CANARY_QUADRANT_GROUPS),
            )
        self._canary_total = max(1, min(n, len(_CANARY_QUADRANT_GROUPS)))
        self._canary_done = 0
        reset_for_new_run()
        set_progress(0)
        try:
            await asyncio.wait_for(
                super().run(),
                timeout=_CANARY_RUN_HARD_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            _log.error(
                "[CanaryMockStrategy] 体检全局硬超时（{}s），已取消引擎协程；"
                "随后进入 Runner.teardown 释放浏览器",
                _CANARY_RUN_HARD_TIMEOUT_SECS,
            )
            mark_all_failed("体检全局硬超时，已中止")
            return
        except Exception as exc:
            _log.exception("[CanaryMockStrategy] 合成任务未捕获异常（已映射到看板）")
            mark_all_failed(f"任务异常: {exc!r}")
            raise

        if self._canary_fsm_saw_error():
            _log.warning(
                "[CanaryMockStrategy] FSM 历史含 ERROR（父类已吞 crawler 异常等），"
                "看板收敛为全项失败；进度条保持当前值（不置 100%%）",
            )
            mark_all_failed("抓取引擎发生严重异常，体检中止")
            return

        if self._canary_done < self._canary_total:
            _log.warning(
                "[CanaryMockStrategy] 页面回调未覆盖全部种子（{}/{}），"
                "不将进度置为 100%，避免误报完成",
                self._canary_done,
                self._canary_total,
            )
            return

        set_progress(100)

    async def _resolve_seed_urls(self) -> List[Any]:
        """为每粒种子注入 user_data，保留原始 URL 供 301/CDN 后象限归因。"""
        from crawlee import Request

        raw = [u.strip() for u in self.settings.strategy_settings.target_urls if u and str(u).strip()]
        return [
            Request.from_url(u, user_data={_CANARY_USER_SEED_KEY: u})
            for u in raw
        ]

    async def _create_task_record(self) -> None:
        """
        金丝雀不写 tasks 表、不向 SiteAuditCenter 注入 task_id，
        避免 scan_records / downloaded_files / tasks 等生产账本被探测流量污染。
        """
        _log.debug("[CanaryMockStrategy] 跳过 _create_task_record（金丝雀零 DB 账本）")

    async def _update_task_status(self, status: str) -> None:
        """与 _create_task_record 成对跳过，避免 UPDATE tasks。"""
        _log.debug(
            "[CanaryMockStrategy] 跳过 _update_task_status({!r})（金丝雀零 DB 账本）",
            status,
        )

    def _assemble_pipeline(self) -> None:
        """
        金丝雀轻量管线：跳过 SiteAuditCenter / Downloader / NetSniffer 等重型组件；
        保留 ErrorRegistry + context 注入所需的 _get_workspace 占位，避免父类 run() 崩溃。
        """
        strat = self.settings.strategy_settings
        task = self.settings.task_info
        ft = strat.file_type
        self._target_ext = f".{ft}" if ft not in ("img", "all") else f".{ft}"

        self._audit_center = _CanaryWorkspaceStub(task.save_directory)
        self._error_registry = ErrorRegistry()
        self._generator = SiteUrlGenerator()

        self._parser = None
        self._downloader = None
        self._action_downloader = None
        self._interactor = None
        self._net_sniffer = None
        self._strategist = None
        self._action_handler = None

        _log.info(
            "[CanaryMockStrategy] 金丝雀轻量管线已启用："
            "UrlGenerator + ErrorRegistry + 工作区占位；"
            "已跳过 SiteAuditCenter / Downloader / NetSniffer / Handlers 管线",
        )

    def _register_crawlee_handlers(self, crawler: Any) -> None:
        """
        仅注册默认页与失败回调；不注册 NEED_CLICK，杜绝 ActionHandler /
        ActionDownloader 侧向触发真实下载或交互落库。
        """
        crawler.router.default_handler(self._default_page_handler)
        if hasattr(crawler, "failed_request_handler"):
            crawler.failed_request_handler = self._failed_request_handler
        else:
            _log.debug(
                "[CanaryMockStrategy] 爬虫不支持 failed_request_handler，跳过注册",
            )

    def _resolve_quadrant_for_url(self, url: str) -> Tuple[str, List[str], bool]:
        """
        按 URL 主机名解析象限。

        Returns:
            (group, item_ids, mapped)；mapped=False 表示未命中字典，使用显式默认象限并应打告警。
        """
        try:
            host_key = _normalize_canary_hostname(urlparse(url).hostname)
        except Exception:
            host_key = ""
        if not host_key:
            return (*_CANARY_UNMAPPED_HOST_QUADRANT, False)
        group = _CANARY_HOST_TO_QUADRANT.get(host_key)
        if group is not None:
            return (*group, True)
        return (*_CANARY_UNMAPPED_HOST_QUADRANT, False)

    def _quadrant_for_url(self, url: str) -> Tuple[str, List[str]]:
        g, items, _mapped = self._quadrant_from_request(None, None, extra_first=[url])
        return g, items

    def _fail_quadrant_for_exception(
        self,
        exc: BaseException,
        *,
        request: Any = None,
        page_url: Optional[str] = None,
        fallback_url: str = "Unknown",
    ) -> None:
        msg = str(exc)[:_DESC_MAX]
        group, item_ids, mapped = self._quadrant_from_request(
            request,
            page_url,
            extra_first=[fallback_url] if fallback_url and fallback_url != "Unknown" else None,
        )
        if not mapped:
            msg = f"未映射域名: {msg}"[:_DESC_MAX]
        try:
            set_quadrant_group(
                group,
                [(iid, "fail", msg) for iid in item_ids],
            )
        except Exception as inner:
            _log.warning("[CanaryMockStrategy] 异常后写看板失败: {}", inner)

    def _record_handler_failure(self, context: Any, exc: BaseException) -> None:
        request = getattr(context, "request", None)
        page = getattr(context, "page", None)
        fb = getattr(request, "url", "Unknown") if request else "Unknown"
        self._fail_quadrant_for_exception(
            exc,
            request=request,
            page_url=self._safe_page_url(page),
            fallback_url=str(fb),
        )

    def _bump_progress(self) -> None:
        self._canary_done += 1
        set_progress(min(100, int(100 * self._canary_done / self._canary_total)))

    @staticmethod
    def _is_sannysoft_probe_url(url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        return "bot.sannysoft.com" in host or "sannysoft.com" in host

    async def _default_page_handler(self, context: Any) -> None:
        try:
            await self._canary_page_handler_core(context)
        except Exception as exc:
            _log.warning("[CanaryMockStrategy] default_handler 未捕获异常，已写入看板: {}", exc)
            self._record_handler_failure(context, exc)

    async def _canary_page_handler_core(self, context: Any) -> None:
        if not self._is_running:
            return

        page = getattr(context, "page", None)
        request = getattr(context, "request", None)
        page_url = self._safe_page_url(page)
        current_url: str = getattr(request, "url", "Unknown") if request else "Unknown"
        response = getattr(context, "response", None)
        status_code: int = getattr(response, "status", 200) if response else 200

        group, item_ids, _mapped = self._quadrant_from_request(request, page_url)

        if page is not None and self._challenge_solver is not None:
            try:
                await self._maybe_handle_challenge_page(page, status_code)
            except Exception as exc:
                _log.warning("[CanaryMockStrategy] 挑战检测异常，已写入看板: {}", exc)
                self._fail_quadrant_for_exception(
                    exc,
                    request=request,
                    page_url=page_url,
                    fallback_url=current_url,
                )

        candidates = self._canary_url_candidates(request, page_url)
        seed_tier_sanny = any(self._is_sannysoft_probe_url(u) for u in candidates)
        page_sanny = bool(page_url and self._is_sannysoft_probe_url(page_url))

        # ── bot.sannysoft.com：身份/硬件 DOM 探针（仅当当前页仍落在 Sannysoft 域上） ──
        if page is not None and status_code < 400 and seed_tier_sanny and page_sanny:
            try:
                updates = await run_sannysoft_identity_probe(page, self.settings)
                apply_sannysoft_probe_updates(updates)
            except Exception as exc:
                _log.warning("[CanaryMockStrategy] sannysoft 探针异常: {}", exc)
                detail = f"{DOM_PROBE_FAIL_DESC}: {str(exc)[:400]}"[:_DESC_MAX]
                apply_sannysoft_probe_updates(
                    [
                        ("identity_locale", "fail", detail),
                        ("viewport_fit", "fail", detail),
                        ("webgl_vendor", "fail", detail),
                        ("canvas_audio", "fail", detail),
                    ],
                )
            self._bump_progress()
            return

        if page is not None and status_code < 400 and seed_tier_sanny and not page_sanny:
            _log.warning(
                "[CanaryMockStrategy] 种子为 Sannysoft 但页面已重定向至其他域（{}），"
                "跳过 DOM 探针并标记 warn",
                (page_url or "")[:200],
            )
            redirect_note = (
                "靶场页已重定向离开 Sannysoft，跳过 DOM 探针"
            )[:_DESC_MAX]
            apply_sannysoft_probe_updates(
                [
                    ("identity_locale", "warn", redirect_note),
                    ("viewport_fit", "warn", redirect_note),
                    ("webgl_vendor", "warn", redirect_note),
                    ("canvas_audio", "warn", redirect_note),
                ],
            )
            self._bump_progress()
            return
        handler = QUADRANT_HANDLERS.get(group)
        if handler is not None:
            try:
                updates = await handler(page, response, current_url, self.settings)
                set_quadrant_group(group, updates)
            except Exception as exc:
                _log.warning(
                    "[CanaryMockStrategy] 象限 {!r} 探针异常，已降级为全 fail: {}",
                    group,
                    exc,
                )
                detail = f"探针异常: {str(exc)[:400]}"[:_DESC_MAX]
                try:
                    set_quadrant_group(
                        group,
                        [(iid, "fail", detail) for iid in item_ids],
                    )
                except Exception as inner:
                    _log.warning("[CanaryMockStrategy] 写看板失败: {}", inner)
            self._bump_progress()
            return

        state = "fail"
        desc = f"HTTP {status_code}"

        try:
            if page is not None:
                title = await page.title()
                ok = bool(title) and status_code < 400
                state = "pass" if ok else "fail"
                desc = f"status={status_code}, title={title[:120]!r}"
            elif status_code < 400:
                state = "pass"
                desc = f"status={status_code}（无 Page，占位通过）"
        except Exception as exc:
            state = "fail"
            desc = f"断言异常: {str(exc)[:400]}"[:_DESC_MAX]

        try:
            set_quadrant_group(
                group,
                [(iid, state, desc[:_DESC_MAX]) for iid in item_ids],
            )
        except Exception as exc:
            _log.warning("[CanaryMockStrategy] 写看板失败: {}", exc)

        self._bump_progress()

    async def _failed_request_handler(self, context: Any) -> None:
        try:
            await self._canary_failed_request_core(context)
        except Exception as exc:
            _log.warning("[CanaryMockStrategy] failed_request_handler 异常，已写入看板: {}", exc)
            self._record_handler_failure(context, exc)

    async def _canary_failed_request_core(self, context: Any) -> None:
        page = getattr(context, "page", None)
        if page is not None and self._challenge_solver is not None:
            try:
                await self._maybe_handle_challenge_page(page, 403)
            except Exception as exc:
                _log.warning("[CanaryMockStrategy] failed_request 挑战检测异常: {}", exc)
                req = getattr(context, "request", None)
                pu = self._safe_page_url(page)
                fb = getattr(req, "url", "Unknown") if req else "Unknown"
                self._fail_quadrant_for_exception(
                    exc,
                    request=req,
                    page_url=pu,
                    fallback_url=str(fb),
                )

        request = getattr(context, "request", None)
        if request is None:
            return
        page_url = self._safe_page_url(page)
        url: str = getattr(request, "url", "Unknown")
        err = getattr(request, "error_message", "请求失败") or "请求失败"
        group, item_ids, _m = self._quadrant_from_request(request, page_url)
        try:
            set_quadrant_group(
                group,
                [(iid, "fail", err[:_DESC_MAX]) for iid in item_ids],
            )
            self._bump_progress()
        except Exception as exc:
            _log.warning("[CanaryMockStrategy] failed_request 写看板失败: {}", exc)
