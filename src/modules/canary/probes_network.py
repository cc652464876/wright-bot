"""
金丝雀 Network 象限探针（TLS / HTTP 头 / WebRTC 泄漏）。

extract → build → run 与 probes_sannysoft 对齐；JA3 等需 CDP/底层栈的特征仅作诚实 warn。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Tuple

from src.config.settings import PrismSettings
from src.utils.logger import get_logger

_log = get_logger(__name__)

_DESC_MAX = 500

_TLS_JA3_WARN = "缺少底层网络栈/CDP支持，待扩展"
_WEBRTC_WARN = "未执行 ICE/STUN 对端候选泄漏探测，待扩展"


def _headers_dict_from_response(response: Any) -> Dict[str, str]:
    """将 Playwright/Crawlee response.headers 规范为小写键的 str→str 映射。"""
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


def _http_status_from_response(response: Any) -> Any:
    if response is None:
        return None
    try:
        return getattr(response, "status", None)
    except Exception:
        return None


def _response_url_str(response: Any) -> str:
    if response is None:
        return ""
    try:
        u = getattr(response, "url", None)
        return str(u) if u is not None else ""
    except Exception:
        return ""


async def _extract_network_bundle(page: Any, response: Any) -> Dict[str, Any]:
    """
    从导航 response 抽取浅层 HTTP 元数据；可选探测 RTCPeerConnection 是否可用（非泄漏测试）。
    """
    bundle: Dict[str, Any] = {
        "error": None,
        "http_status": _http_status_from_response(response),
        "response_url": _response_url_str(response),
        "headers": _headers_dict_from_response(response),
        "webrtc": {},
    }
    if page is not None:
        try:
            has_rtc = await page.evaluate("() => typeof RTCPeerConnection !== 'undefined'")
            bundle["webrtc"] = {"rtc_peer_connection_defined": bool(has_rtc)}
        except Exception as exc:
            bundle["webrtc"] = {"error": str(exc)}
    return bundle


def build_network_probe_updates(bundle: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """
    纯函数：network bundle → tls_ja3 / http_headers / webrtc_leak 三行。
    """
    if bundle.get("error"):
        fd = str(bundle["error"])[:_DESC_MAX]
        return [
            ("tls_ja3", "fail", fd),
            ("http_headers", "fail", fd),
            ("webrtc_leak", "fail", fd),
        ]

    tls_row: Tuple[str, str, str] = ("tls_ja3", "warn", _TLS_JA3_WARN)

    status = bundle.get("http_status")
    hdrs: Dict[str, str] = bundle.get("headers") or {}
    if not isinstance(hdrs, dict):
        hdrs = {}

    if status is not None and isinstance(status, int) and status >= 400:
        keys_preview = ", ".join(sorted(hdrs.keys())[:12])
        http_desc = (
            f"HTTP {status}；响应头键预览: {keys_preview or '（无）'}"
        )[:_DESC_MAX]
        http_row: Tuple[str, str, str] = ("http_headers", "fail", http_desc)
    elif not hdrs and bundle.get("response_url") == "" and status is None:
        http_row = (
            "http_headers",
            "warn",
            "无响应对象或无法读取状态/头（可能未经过导航 response）",
        )
    else:
        # 摘要：状态、Content-Type、Server、少量键名
        ct = hdrs.get("content-type", "")[:120]
        srv = hdrs.get("server", "")[:120]
        keys_preview = ", ".join(sorted(hdrs.keys())[:16])
        parts = []
        if status is not None:
            parts.append(f"status={status}")
        if ct:
            parts.append(f"content-type={ct!r}")
        if srv:
            parts.append(f"server={srv!r}")
        if keys_preview:
            parts.append(f"keys={keys_preview}")
        ru = (bundle.get("response_url") or "")[:200]
        if ru:
            parts.append(f"url={ru!r}")
        http_desc = "; ".join(parts)[:_DESC_MAX] or "已读取响应头（摘要为空）"
        http_row = ("http_headers", "pass", http_desc)

    wrtc = bundle.get("webrtc") or {}
    if isinstance(wrtc, dict) and wrtc.get("error"):
        webrtc_desc = f"页面内探测异常: {wrtc.get('error')}"[:_DESC_MAX]
        webrtc_row: Tuple[str, str, str] = ("webrtc_leak", "warn", webrtc_desc)
    else:
        defined = False
        if isinstance(wrtc, dict):
            defined = bool(wrtc.get("rtc_peer_connection_defined"))
        suffix = (
            f" RTCPeerConnection={'可用' if defined else '不可用/未知'}；{_WEBRTC_WARN}"
        )
        webrtc_row = ("webrtc_leak", "warn", suffix[:_DESC_MAX])

    return [tls_row, http_row, webrtc_row]


async def run_network_probe(
    page: Any,
    response: Any,
    url: str,
    settings: PrismSettings,
) -> List[Tuple[str, str, str]]:
    """
    Network 象限入口：抽取 bundle → 纯函数生成三行；异常时三行 fail 兜底。

    ``settings`` 预留与代理/指纹策略联动，当前未使用。
    """
    _ = settings
    _ = url
    try:
        bundle = await _extract_network_bundle(page, response)
        return build_network_probe_updates(bundle)
    except Exception as exc:
        _log.debug("[network] run_network_probe 兜底: {}", exc)
        detail = f"探针异常: {str(exc)[:400]}"[:_DESC_MAX]
        return [
            ("tls_ja3", "fail", detail),
            ("http_headers", "fail", detail),
            ("webrtc_leak", "fail", detail),
        ]
