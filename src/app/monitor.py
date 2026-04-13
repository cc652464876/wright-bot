"""
@Layer   : App 层（第五层 · 应用编排）
@Role    : 爬虫任务实时统计与仪表盘数据聚合
@Pattern : Data Aggregator（合并多源统计） + Memento Pattern（冻结快照防止 UI 清零）
@Description:
    SiteMonitor 负责将两个独立数据源的统计合并为 UI 仪表盘所需的单一数据快照：
    1. Crawlee 引擎统计（crawler.statistics.state）：
       请求总数 / 完成 / 失败 / RPM / 平均耗时 / 重试柱状图 / 运行时长
    2. Downloader 统计（strategy.get_stats()）：
       files_found / files_downloaded / files_active

    核心设计：
    - 爬虫运行时：动态读取 Crawlee 统计对象，实时更新。
    - 爬虫停止后：从 _frozen_crawler_stats 冻结快照中读取 Crawlee 部分，
      防止 Crawlee 内部状态清零导致 UI 仪表盘数据突然归零（Memento 模式）。
    - Downloader 数据永远实时读取（即使爬虫已停止，后台下载可能仍在进行）。

    Pattern: Data Aggregator + Memento（冻结快照）
"""

from __future__ import annotations

import copy
import threading
from datetime import timedelta
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.modules.base_strategy import BaseCrawlStrategy


class SiteMonitor:
    """
    爬虫任务仪表盘数据聚合器。

    职责：
    - get_dashboard_data() 是唯一公开接口，由 MasterDispatcher 代理给 bridge.py。
    - reset() 在每次新任务开始前调用，清空冻结快照。

    Pattern: Data Aggregator + Memento（_frozen_crawler_stats）
    """

    #: 仪表盘数据字段默认值模板（含全部 UI 所需的 key）
    DEFAULT_SNAPSHOT: Dict[str, Any] = {
        "requests_total": 0,
        "requests_finished": 0,
        "requests_failed": 0,
        "crawler_runtime": 0.0,
        "avg_success_duration": 0.0,
        "avg_failed_duration": 0.0,
        "requests_per_minute": 0.0,
        "failed_per_minute": 0.0,
        "retry_count": 0,
        "files_found": 0,
        "files_downloaded": 0,
        "files_active": 0,
        "scraped_count": 0,
        "state": "idle",
    }

    def __init__(self) -> None:
        """
        初始化监控器，冻结快照为空字典。
        """
        # Memento 存储：保存爬虫停止前最后一次 Crawlee 统计快照
        self._frozen_crawler_stats: Dict[str, Any] = {}
        # 并发保护：get_dashboard_data 可能被 UI 轮询线程与爬虫协程同时访问
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """
        清空冻结的 Crawlee 统计快照。
        应在每次新任务启动前（SiteRunner.setup() 阶段）调用，
        防止旧任务的统计数据污染新任务的仪表盘。
        """
        with self._lock:
            self._frozen_crawler_stats = {}

    def get_dashboard_data(
        self,
        is_running: bool,
        crawler: Any,
        strategy: Optional["BaseCrawlStrategy"] = None,
    ) -> Dict[str, Any]:
        """
        获取合并后的完整仪表盘数据快照。

        合并逻辑：
        1. 从 DEFAULT_SNAPSHOT 克隆基础模板（保证所有 key 存在）。
        2. 若 is_running=True 且 crawler.statistics 可访问：
           动态读取 Crawlee stats.state，更新 Crawlee 相关字段，
           并将结果（不含 files_ 字段）存入 _frozen_crawler_stats。
        3. 若 is_running=False：用 _frozen_crawler_stats 覆盖对应字段。
        4. 若 strategy 非 None：调用 strategy.get_dashboard_data()，
           合并 files_found / files_downloaded / files_active / scraped_count。

        Args:
            is_running: 当前爬虫是否处于运行状态。
            crawler   : Crawlee 爬虫实例（可为 None）。
            strategy  : 当前活跃策略实例（可为 None）。
        Returns:
            包含全部仪表盘字段的数据字典。
        """
        # 步骤 1：以 DEFAULT_SNAPSHOT 为基础克隆，保证所有 key 始终存在
        snapshot: Dict[str, Any] = copy.deepcopy(self.DEFAULT_SNAPSHOT)

        with self._lock:
            if is_running and crawler is not None:
                # 步骤 2：爬虫运行中 —— 动态读取并冻结 Crawlee 统计
                crawlee_stats = self._extract_crawlee_stats(crawler)
                if crawlee_stats:
                    snapshot.update(crawlee_stats)
                    # Memento：仅存储 Crawlee 独有字段（排除策略层字段）
                    self._frozen_crawler_stats = {
                        k: v
                        for k, v in crawlee_stats.items()
                        if k not in ("files_found", "files_downloaded",
                                     "files_active", "scraped_count", "state")
                    }
            else:
                # 步骤 3：爬虫已停止 —— 从冻结快照恢复，防止 UI 数据清零
                if self._frozen_crawler_stats:
                    snapshot.update(self._frozen_crawler_stats)

        # 步骤 4：Downloader / strategy 数据永远实时读取（后台下载可能仍在进行）
        if strategy is not None:
            try:
                strat_data = strategy.get_dashboard_data()
                for key in ("files_found", "files_downloaded", "files_active", "scraped_count"):
                    if key in strat_data:
                        snapshot[key] = strat_data[key]
                # state 字段由 strategy 的状态机权威管理
                if "state" in strat_data:
                    snapshot["state"] = strat_data["state"]
            except Exception:
                pass  # 防御：strategy 异常不应拖垮仪表盘轮询

        return snapshot

    # ------------------------------------------------------------------
    # 私有：Crawlee 统计读取
    # ------------------------------------------------------------------

    def _extract_crawlee_stats(self, crawler: Any) -> Dict[str, Any]:
        """
        从 crawler.statistics.state 对象中安全读取所有 Crawlee 统计字段。
        使用 getattr + 默认值防御，避免 Crawlee 版本升级导致字段缺失时崩溃。
        timedelta 类型字段自动转换为秒数（float）。
        retry_histogram 统一处理 list 和 dict 两种格式。

        Args:
            crawler: Crawlee 爬虫实例（BeautifulSoupCrawler 或 PlaywrightCrawler）。
        Returns:
            包含 Crawlee 统计字段的字典（不含 files_ 相关字段）。
        """
        try:
            statistics = getattr(crawler, "statistics", None)
            if statistics is None:
                return {}
            state = getattr(statistics, "state", None)
            if state is None:
                return {}
        except Exception:
            return {}

        def _get(attr: str, default: Any = 0) -> Any:
            """安全读取属性，timedelta 自动转为秒数。"""
            val = getattr(state, attr, default)
            if val is None:
                return default
            if isinstance(val, timedelta):
                return val.total_seconds()
            return val

        # ── 核心计数器 ──────────────────────────────────────────────────
        requests_total    = int(_get("requests_total", 0))
        requests_finished = int(_get("requests_finished", 0))
        requests_failed   = int(_get("requests_failed", 0))

        # ── 运行时长（timedelta 或 float 秒） ───────────────────────────
        runtime_sec = float(_get("crawler_runtime", 0.0))

        # ── 平均耗时（可能为 timedelta 或 float ms/s） ──────────────────
        avg_success = float(_get("requests_finished_duration_mean", 0.0))
        avg_failed  = float(_get("requests_failed_duration_mean",  0.0))

        # ── 速率指标 ────────────────────────────────────────────────────
        crawlee_rpm = _get("requests_finished_per_minute", None)
        crawlee_fpm = _get("requests_failed_per_minute",   None)

        rpm = self._calc_requests_per_minute(requests_finished, runtime_sec, crawlee_rpm)

        if crawlee_fpm is not None and float(crawlee_fpm) > 0:
            fpm = round(float(crawlee_fpm), 1)
        elif runtime_sec > 0:
            fpm = round(requests_failed / (runtime_sec / 60.0), 1)
        else:
            fpm = 0.0

        # ── 重试统计（list 或 dict 两种格式兼容） ───────────────────────
        retry_histogram = getattr(state, "retry_histogram", None)
        if isinstance(retry_histogram, list):
            retry_count = int(sum(retry_histogram))
        elif isinstance(retry_histogram, dict):
            retry_count = int(sum(retry_histogram.values()))
        else:
            retry_count = 0

        return {
            "requests_total":        requests_total,
            "requests_finished":     requests_finished,
            "requests_failed":       requests_failed,
            "crawler_runtime":       round(runtime_sec, 1),
            "avg_success_duration":  round(avg_success, 3),
            "avg_failed_duration":   round(avg_failed,  3),
            "requests_per_minute":   rpm,
            "failed_per_minute":     fpm,
            "retry_count":           retry_count,
        }

    def _calc_requests_per_minute(
        self,
        requests_finished: int,
        runtime_sec: float,
        crawlee_rpm: Optional[float],
    ) -> float:
        """
        计算每分钟请求数（RPM）。
        优先使用 Crawlee 内置的 requests_finished_per_minute（若 > 0）；
        否则按 requests_finished / (runtime_sec / 60) 手动计算。

        Args:
            requests_finished: 已完成请求数。
            runtime_sec      : 运行时长（秒）。
            crawlee_rpm      : Crawlee 内置 RPM 值（可为 None）。
        Returns:
            每分钟请求数（保留 1 位小数）。
        """
        if crawlee_rpm is not None and float(crawlee_rpm) > 0:
            return round(float(crawlee_rpm), 1)
        if runtime_sec > 0:
            return round(requests_finished / (runtime_sec / 60.0), 1)
        return 0.0
