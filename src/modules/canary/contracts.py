"""
金丝雀看板 JSON 契约（与 UI/canary.js 轮询字段对齐）。
"""

from __future__ import annotations

from typing import List, TypedDict


class QuadrantItemDict(TypedDict):
    id: str
    label: str
    state: str
    desc: str


class QuadrantsDict(TypedDict):
    network: List[QuadrantItemDict]
    identity: List[QuadrantItemDict]
    hardware: List[QuadrantItemDict]
    combat: List[QuadrantItemDict]


class CanaryDashboardDict(TypedDict):
    """fetch_canary_dashboard 返回体。"""

    system_state: str
    current_engine: str
    progress_percent: int
    quadrants: QuadrantsDict
