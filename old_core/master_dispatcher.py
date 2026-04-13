# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: master_dispatcher.py
# 功能描述: 系统总指挥 (任务路由器)
# 核心职责:
#   1. 接收外部 (UI/API) 传入的任务配置字典，拦截并解析任务意图。
#   2. 识别任务模式 (如：站点遍历 site 或 搜索引擎 search)。
#   3. 动态唤醒并调度对应的独立采集线 (切片实例)，实现业务逻辑的绝对物理隔离。
#   4. 统一管理生命周期，向下级正在运行的实例透传“紧急停止”和“大屏数据拉取”指令。
# 输入: 任务配置字典 (config_dict)
# 输出: 无直接输出，负责整体生命周期调度和实例托管
# ==============================================================================

import asyncio
import logging

# 导入切片线路（Search 线暂时注释掉，等我们后面写好再打开）
from core.site_runner import SiteCrawlerRunner
from core.search_runner import SearchCrawlerRunner

class MasterDispatcher:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.current_runner = None  # 记录当前正在干活的线路实例

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[总指挥] {message}", level)
        else:
            print(f"[{level.upper()}] [总指挥] {message}")

    async def run_with_config(self, config_dict):
        """
        统一入口：拦截配置，判断模式，派发任务
        """
        task_info = config_dict.get("task_info", {})
        # 假设你的前端配置里有一个 mode 字段区分："site" 或 "search"
        task_mode = task_info.get("mode", "site") 
        
        self._log(f"接收到新任务，已识别模式: {task_mode.upper()}", "info")
        
        if task_mode == "site":
            self.current_runner = SiteCrawlerRunner(self.log_callback)
        elif task_mode == "search":
            self.current_runner = SearchCrawlerRunner(self.log_callback)
        else:
            self._log(f"❌ 未知的任务模式: {task_mode}", "error")
            return
            
        # 唤醒对应线路，开始干活
        await self.current_runner.run(config_dict)

    async def stop(self):
        """
        统一出口：停止当前正在运行的线路
        """
        if self.current_runner:
            self._log("接收到停止指令，正在向作业线路下达撤退命令...", "warning")
            await self.current_runner.stop()
        else:
            self._log("当前没有正在运行的任务。", "info")
            
    def get_dashboard_data(self):
        """
        统一监控透传：直接向前端返回当前正在干活的线路的数据
        """
        if self.current_runner:
            return self.current_runner.get_dashboard_data()
        
        # 默认空数据模板
        return {
            "requests_total": 0, "requests_finished": 0, "requests_failed": 0,
            "crawler_runtime": 0, "avg_success_duration": 0, "avg_failed_duration": 0,
            "requests_per_minute": 0, "failed_per_minute": 0, "retry_count": 0,
            "files_found": 0, "files_downloaded": 0, "files_active": 0
        }