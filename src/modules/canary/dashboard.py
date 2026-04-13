"""
金丝雀看板内存态：供 Crawlee 探针在后台更新，bridge.fetch_canary_dashboard 轮询读取。
线程安全（与 pywebview 主线程 + asyncio 线程交互）。
"""

from __future__ import annotations

import copy
import threading
from typing import Dict, List, Tuple, cast

from src.modules.canary.contracts import CanaryDashboardDict, QuadrantsDict

# ---------------------------------------------------------------------------
# 默认四象限骨架（与 bridge 历史契约一致）
# ---------------------------------------------------------------------------

def _default_quadrants() -> Dict[str, List[Dict[str, str]]]:
    def _item(
        item_id: str,
        label: str,
        state: str = "idle",
        desc: str = "等待检测 / 尚未执行",
    ) -> Dict[str, str]:
        return {"id": item_id, "label": label, "state": state, "desc": desc}

    return {
        "network": [
            _item("tls_ja3", "TLS / JA3 指纹校验"),
            _item("http_headers", "HTTP 报文与 IP 连通性"),
            _item("webrtc_leak", "WebRTC 真实 IP 泄露"),
        ],
        "identity": [
            _item("identity_locale", "综合身份与语言时区"),
            _item("viewport_fit", "视口逻辑与物理尺寸对齐"),
        ],
        "hardware": [
            _item("webgl_vendor", "WebGL 厂商与渲染引擎"),
            _item("canvas_audio", "画布与音频哈希噪点"),
        ],
        "combat": [
            _item("cf_shield", "Cloudflare 隐形质询 (5s盾)"),
            _item("cdp_automation", "CDP 协议与自动化漏洞"),
            _item("behavior_score", "仿生行为与轨迹评分"),
        ],
    }


_lock = threading.Lock()
_quadrants: Dict[str, List[Dict[str, str]]] = _default_quadrants()
_progress_percent: int = 0


def reset_for_new_run() -> None:
    """新一次金丝雀合成任务开始前重置看板。"""
    global _quadrants, _progress_percent
    with _lock:
        _quadrants = _default_quadrants()
        _progress_percent = 0


def set_progress(percent: int) -> None:
    with _lock:
        global _progress_percent
        _progress_percent = max(0, min(100, int(percent)))


def set_quadrant_group(
    group: str,
    updates: List[Tuple[str, str, str]],
) -> None:
    """
    按象限批量更新若干检测项。

    Args:
        group: quadrants 顶层键（network / identity / hardware / combat）。
        updates: (item_id, state, desc) 列表。
    """
    with _lock:
        items = _quadrants.get(group)
        if not items:
            return
        by_id = {row["id"]: row for row in items}
        for item_id, state, desc in updates:
            row = by_id.get(item_id)
            if row is not None:
                row["state"] = state
                row["desc"] = desc


def mark_all_failed(reason: str) -> None:
    """致命错误时将所有项置为失败（防御性兜底）。"""
    with _lock:
        for _g, rows in _quadrants.items():
            for row in rows:
                row["state"] = "fail"
                row["desc"] = reason[:500]


def snapshot_quadrants_progress() -> Tuple[Dict[str, List[Dict[str, str]]], int]:
    with _lock:
        return copy.deepcopy(_quadrants), _progress_percent


def build_payload(
    *,
    dispatcher_running: bool,
    is_canary_active: bool,
    current_engine: str,
) -> CanaryDashboardDict:
    """
    组装供 fetch_canary_dashboard 返回的 JSON（含 system_state 语义）。

    - idle: 无任务占用调度器。
    - running: 当前为金丝雀合成任务执行中（可看进度与象限）。
    - locked: 调度器被普通爬取任务占用，金丝雀不应再投递。
    """
    quads, prog = snapshot_quadrants_progress()
    if is_canary_active:
        system_state = "running"
    elif dispatcher_running:
        system_state = "locked"
    else:
        system_state = "idle"
    return {
        "system_state": system_state,
        "current_engine": current_engine,
        "progress_percent": prog,
        "quadrants": cast(QuadrantsDict, quads),
    }
