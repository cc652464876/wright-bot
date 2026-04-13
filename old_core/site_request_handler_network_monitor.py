# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: site_request_handler_network_monitor.py
# 功能描述: 站点采集线 - 网络探针 (Network Interception)
# 核心职责:
#   监听底层网络请求与响应，绕过 DOM，直接截获通过 AJAX/Fetch 加载的目标文件。
# ==============================================================================

import os
import asyncio

class SiteRequestHandlerNetworkMonitor:
    def __init__(self, parser, target_ext, log_callback, check_running_func):
        """
        初始化网络探针
        """
        self.parser = parser
        self.target_ext = target_ext
        self.log_callback = log_callback
        self.is_running = check_running_func
        
        # 🌟 [优化升级: 方案B] 引入异步队列，解耦底层事件与耗时 I/O
        self._record_queue = asyncio.Queue()
        self._worker_task = None

    async def _queue_worker(self):
        """独立后台 Worker：专门负责消费队列中的数据并落盘"""
        # 即使 is_running 为 False，也要把队列里残留的数据消费完
        while self.is_running() or not self._record_queue.empty():
            try:
                # 阻塞等待队列数据
                item = await self._record_queue.get()
                
                # 接收到“毒药丸”(Sentinel Value)，安全退出循环
                if item is None:
                    self._record_queue.task_done()
                    break
                    
                domain, source_page, file_urls, audit_manager = item
                
                # 真正执行耗时的 I/O 写盘操作 (脱离 page 生命周期)
                await audit_manager.record_result_batch(
                    domain=domain, 
                    source_page=source_page, 
                    new_file_urls=file_urls
                )
                self._record_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"后台落地队列异常: {e}", "error")

    async def stop(self):
        """全局调用：安全停止网络探针的工作队列"""
        if self._worker_task and not self._worker_task.done():
            # 向队列投入“毒药丸”
            await self._record_queue.put(None)
            # 等待队列消费完毕并关闭 Task
            await self._record_queue.join()
            self._worker_task.cancel()

    def _log(self, message, level="info"):
        """内部专属日志打印"""
        if self.log_callback:
            self.log_callback(f"[网络探针] {message}", level)
        else:
            print(f"[{level.upper()}] [网络探针] {message}")

    async def attach_probe(self, context, domain: str, domain_workspace: str, audit_manager, downloader):
        """
        在当前页面布置网络拦截探针
        """
        if not hasattr(context, 'page') or not self.is_running():
            return

        async def handle_response(response):
            try:
                if response.status != 200: 
                    return
                    
                content_type = response.headers.get('content-type', '').lower()
                
                # 检查是否命中目标扩展名 (如 PDF)
                if self.target_ext == '.pdf' and 'application/pdf' in content_type:
                    # 修复 API 歧义：使用 context.page.url 替代 context.request.url 获取母页面地址
                    source_page_url = context.page.url 
                    
                    # 使用传入的 parser 解析数据
                    item = self.parser.parse_link(source_page_url, response.url, self.target_ext)
                    if not item: 
                        return
                    
                    # [已移除] 越权读取审计中心字典的去重逻辑
                    # 去重职责完全交还给 audit_manager.record_result_batch 内部处理

                    # 更新下载器统计指标 (注意：这里的统计可能会包含重复项，精确统计建议在中心处理)
                    downloader.files_found += 1 
                    target_sub_folder = os.path.basename(domain_workspace)
                    self._log(f"🕵️‍♂️ 探针截获数据流: {item['file_name']} -> [{target_sub_folder}]", "success")
                    
                    # 触发降噪写入：改为 put_nowait 非阻塞投递
                    self._record_queue.put_nowait((
                        domain, 
                        source_page_url, 
                        [item['file_url']], 
                        audit_manager
                    ))
            except Exception as e:
                # 修复危险的静默吞噬：将异常降级为 DEBUG/INFO 打印，防止掩盖代码级 BUG
                self._log(f"网络探针处理响应时发生异常: {str(e)}", "debug")

        # 🌟 惰性启动独立 Worker (确保挂载时 event_loop 是运行状态)
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._queue_worker())

        # 绑定到 Playwright/Crawlee 的 page response 事件上
        context.page.on("response", handle_response)