"""
金丝雀 Combat 象限探针（CF/CDN 拦截特征、CDP、行为分）。

extract → build → run；行为与 CDP 深层审计以诚实 warn + 待扩展为主。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Tuple

from src.config.settings import PrismSettings
from src.utils.logger import get_logger

_log = get_logger(__name__)

_DESC_MAX = 500

_BEHAVIOR_WARN = "未接入鼠标/键盘轨迹打分引擎"
_CDP_WARN = "未接入 CDP 层面的自动化指纹审计，待扩展"


def _headers_dict_from_response(response: Any) -> Dict[str, str]:
    if response is None:
        return {}
    raw = getattr(response, "headers", None)
    if raw is None:
        return {}
    out: Dict[str, str] = {}
    try:
        if isinstance(raw, Mapping):
            for k, v in raw.items():
                out[str(k).lower()] = str(v)
            return out
        items = getattr(raw, "items", None)
        if callable(items):
            pairs_obj = items()
            if isinstance(pairs_obj, Iterable) and not isinstance(
                pairs_obj, (str, bytes, bytearray)
            ):
                for pair in pairs_obj:
                    if not isinstance(pair, tuple) or len(pair) != 2:
                        continue
                    k, v = pair
                    out[str(k).lower()] = str(v)
    except Exception:
        return {}
    return out


async def _extract_combat_bundle(page: Any, response: Any) -> Dict[str, Any]:
    """提取标题、HTTP 状态与响应头（小写键），供纯函数判定。"""
    bundle: Dict[str, Any] = {
        "error": None,
        "title": "",
        "http_status": None,
        "headers_lower": _headers_dict_from_response(response),
    }
    if response is not None:
        try:
            st = getattr(response, "status", None)
            bundle["http_status"] = int(st) if st is not None else None
        except (TypeError, ValueError):
            bundle["http_status"] = None
    if page is not None:
        try:
            bundle["title"] = str(await page.title() or "")
        except Exception as exc:
            bundle["title"] = ""
            bundle["title_error"] = str(exc)
    return bundle


def build_combat_probe_updates(bundle: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """
    纯函数：combat bundle → cf_shield / cdp_automation / behavior_score 三行。
    """
    if bundle.get("error"):
        fd = str(bundle["error"])[:_DESC_MAX]
        return [
            ("cf_shield", "fail", fd),
            ("cdp_automation", "fail", fd),
            ("behavior_score", "fail", fd),
        ]

    status = bundle.get("http_status")
    title = str(bundle.get("title") or "")
    tl = title.lower()
    hdrs: Dict[str, str] = bundle.get("headers_lower") or {}

    # ── cf_shield：标题/状态码/CF 响应头启发式（不做完整挑战求解判定） ──
    cdn_edge_hint = any(
        k in hdrs
        for k in (
            "cf-ray",
            "cf-cache-status",
            "x-cache",
            "x-served-by",
            "fastly-client-ip",
        )
    )
    server_val = (hdrs.get("server") or "").lower()
    looks_cf_server = "cloudflare" in server_val

    interstitial = any(
        x in tl
        for x in (
            "just a moment",
            "attention required",
            "checking your browser",
            "ddos protection",
        )
    )

    if interstitial or (isinstance(status, int) and status in (403, 503, 429)):
        cf_desc = (
            f"疑似拦截/质询上下文: status={status!r}, title[:80]={title[:80]!r}"
        )[:_DESC_MAX]
        cf_row: Tuple[str, str, str] = ("cf_shield", "warn", cf_desc)
    elif isinstance(status, int) and status >= 400:
        cf_row = (
            "cf_shield",
            "fail",
            (f"HTTP {status}，非典型 CF 标题但仍属错误响应")[:_DESC_MAX],
        )
    elif looks_cf_server or "cf-ray" in hdrs:
        cf_row = (
            "cf_shield",
            "pass",
            (
                "响应经 Cloudflare 边缘（cf-ray/server），当前页非典型质询标题"
            )[:_DESC_MAX],
        )
    elif cdn_edge_hint:
        cf_row = (
            "cf_shield",
            "pass",
            ("未检测到典型质询标题；存在常见 CDN/边缘响应头特征")[:_DESC_MAX],
        )
    else:
        cf_row = (
            "cf_shield",
            "pass",
            (
                "未检测到典型 CF 质询标题或拦截状态码（不代表无其他防护）"
            )[:_DESC_MAX],
        )

    if bundle.get("title_error"):
        cdp_row: Tuple[str, str, str] = (
            "cdp_automation",
            "warn",
            f"{_CDP_WARN}；title 读取失败: {bundle.get('title_error')}"[:_DESC_MAX],
        )
    else:
        cdp_row = ("cdp_automation", "warn", _CDP_WARN)

    behavior_row: Tuple[str, str, str] = ("behavior_score", "warn", _BEHAVIOR_WARN)

    return [cf_row, cdp_row, behavior_row]


async def run_combat_probe(
    page: Any,
    response: Any,
    url: str,
    settings: PrismSettings,
) -> List[Tuple[str, str, str]]:
    """
    Combat 象限入口：抽取 bundle → 纯函数生成三行；异常时三行 fail 兜底。

    ``settings`` 预留扩展；``url`` 与当前页 URL 对齐供日志/后续规则使用。
    """
    _ = settings
    _ = url
    try:
        bundle = await _extract_combat_bundle(page, response)
        return build_combat_probe_updates(bundle)
    except Exception as exc:
        _log.debug("[combat] run_combat_probe 兜底: {}", exc)
        detail = f"探针异常: {str(exc)[:400]}"[:_DESC_MAX]
        return [
            ("cf_shield", "fail", detail),
            ("cdp_automation", "fail", detail),
            ("behavior_score", "fail", detail),
        ]
