# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: site_monitor.py
# 功能描述: 站点采集线数据监控与仪表盘统计模块
# 核心职责:
#   1. 读取并格式化 Crawlee 底层的爬虫统计数据 (统计请求数、运行时长、RPM等)
#   2. 合并下载器的统计数据 (发现文件数、已下载数、活跃下载数)
#   3. 在任务结束或中止时，完美冻结数据快照，防止前端仪表盘数据归零
# ==============================================================================

class SiteMonitor:
    def __init__(self):
        # 冻结的爬虫状态字典，用于任务停止后保持前端显示
        self._frozen_crawler_stats = {}

    def reset(self):
        """任务重新启动时清空冻结数据"""
        self._frozen_crawler_stats.clear()

    def get_dashboard_data(self, is_running, crawler, request_handler):
        """
        获取合并后的仪表盘数据
        :param is_running: 当前爬虫是否正在运行 (布尔值)
        :param crawler: Crawlee 爬虫实例
        :param request_handler: SiteRequestHandler 处理器实例 (现已接管同体原生下载状态)
        """
        # 💡 优雅修复：显式声明字典的值可以是 int 或 float
        data: dict[str, int | float] = {
            "requests_total": 0, "requests_finished": 0, "requests_failed": 0,
            "crawler_runtime": 0.0, "avg_success_duration": 0.0, "avg_failed_duration": 0.0,
            "requests_per_minute": 0.0, "failed_per_minute": 0.0, "retry_count": 0,
            "files_found": 0, "files_downloaded": 0, "files_active": 0
        }

        # 1. 如果爬虫正在运行且存在统计对象，则动态读取 Crawlee 数据
        if is_running and crawler and hasattr(crawler, 'statistics'):
            stats = crawler.statistics
            s = stats.state if hasattr(stats, 'state') else None

            if s:
                try:
                    data["requests_total"] = getattr(s, 'requests_total', 0)
                    data["requests_finished"] = getattr(s, 'requests_finished', 0)
                    data["requests_failed"] = getattr(s, 'requests_failed', 0)
                    
                    runtime = getattr(s, 'crawler_runtime', None)
                    runtime_sec = runtime.total_seconds() if (runtime and hasattr(runtime, 'total_seconds')) else 0
                    data["crawler_runtime"] = runtime_sec
                    
                    crawlee_rpm = getattr(s, 'requests_finished_per_minute', None)
                    if crawlee_rpm is not None and crawlee_rpm > 0:
                         data["requests_per_minute"] = round(crawlee_rpm, 1)
                    else:
                        if runtime_sec > 1:
                            data["requests_per_minute"] = round(data["requests_finished"] / (runtime_sec / 60), 1)
                        else:
                            data["requests_per_minute"] = 0

                    avg_success = getattr(s, 'request_avg_finished_duration', None)
                    data["avg_success_duration"] = avg_success.total_seconds() if (avg_success and hasattr(avg_success, 'total_seconds')) else 0
                        
                    avg_fail = getattr(s, 'request_avg_failed_duration', None)
                    data["avg_failed_duration"] = avg_fail.total_seconds() if (avg_fail and hasattr(avg_fail, 'total_seconds')) else 0

                    # 处理重试柱状图
                    retry_hist = getattr(s, 'request_retry_histogram', [])
                    real_retries = 0
                    if isinstance(retry_hist, list):
                        for i, count in enumerate(retry_hist):
                            if i > 0: real_retries += count
                    elif isinstance(retry_hist, dict):
                        for k, v in retry_hist.items():
                            if int(k) > 0: real_retries += v
                    data["retry_count"] = real_retries

                    # 更新冻结快照 (排除由 request_handler 负责的 files_ 字段)
                    self._frozen_crawler_stats = {k: v for k, v in data.items() if not k.startswith("files_")}
                except Exception as e:
                    pass
        else:
            # 2. 如果爬虫已停止，则直接使用冻结的数据快照
            data.update(self._frozen_crawler_stats)

        # 3. 合并下载状态数据 (无论网页遍历是否停止，后台原生下载可能仍在落盘)
        if request_handler:
            data.update(request_handler.get_stats())
            
        return data