# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: site_runner.py
# 功能描述: 站点采集线主引擎 (终极瘦身版)
# 核心职责:
#   1. 统筹调用本线路专属的组件实例化与生命周期管理。
#   2. 初始化 SiteAuditCenter，并挂载外部处理器 (RequestHandler)。
#   3. 设置全局底层失败路由 (failed_request_handler) 进行防漏兜底。
# ==============================================================================

import asyncio
import os
import inspect  # 🛡️ 新增导入，用于替代被弃用的 asyncio 方法
from core.site_generator import SiteUrlGenerator
from core.site_parser import SiteDataParser
from core.site_engines import SiteCrawlerFactory

from core.site_utils import backup_config, get_core_domain
from core.site_monitor import SiteMonitor
from core.site_request_handler import SiteRequestHandler

# 🌟 新增导入大一统审计中心
from core.site_audit_center import SiteAuditCenter

class SiteCrawlerRunner:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.is_running = False
        
        # 🌟 删除了原本所有的字典 (domain_results等) 和并发锁 (manifest_lock等)
        # 统统交由 audit_center 内部私有化管理
        self.audit_center = None 
        
        self.crawler = None
        self.request_handler = None
        self.monitor = SiteMonitor()

    @property
    def scraped_count(self):
        """动态获取已抓取数量：通过对外 API 获取，保障线程/协程安全"""
        if not self.audit_center:
            return 0
        # 移交权力：由审计中心自己去安全地统计数量
        # 🛡️ 柔性防御：防止 audit_center 尚未完成升级时导致 UI 轮询崩溃
        get_count_func = getattr(self.audit_center, "get_total_scraped_count", None)
        return get_count_func() if get_count_func else 0

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[Site线] {message}", level)
        else:
            print(f"[{level.upper()}] [Site线] {message}")

    async def stop(self):
        """安全中止所有子组件的运行"""
        if not self.is_running:
            return 
            
        self.is_running = False 
        self._log("🛑 正在执行站点遍历紧急停止程序...", "warning") 

        if self.crawler:
            self._log("正在通知 Crawlee 停止 Site 线路调度...", "info")
            # 🛡️ 显式触发底层引擎的停止机制 (修复 Python 版 API 差异)
            pool = getattr(self.crawler, 'autoscaled_pool', None)
            if pool:
                await pool.abort()  # 强制中止并发任务池
            elif hasattr(self.crawler, 'stop'):
                stop_method = self.crawler.stop
                # 🛡️ 智能类型防御：判断 stop 到底是异步还是同步
                if inspect.iscoroutinefunction(stop_method):
                    await stop_method()
                elif callable(stop_method):
                    stop_method()

        self._log("✨ 站点采集引擎已安全退出。", "success")

    def get_dashboard_data(self):
        """UI 状态接管：委托 Monitor 从 request_handler 读取同体下载的状态"""
        return self.monitor.get_dashboard_data(self.is_running, self.crawler, self.request_handler)

    async def run(self, config_dict):
        # 1. 初始化状态与监控
        self.is_running = True
        self.monitor.reset()
        
        task_info = config_dict.get("task_info", {})
        save_dir = task_info.get("save_directory", "./downloads")
        strategy_prefix = config_dict.get("strategy_settings", {}).get("strategy_prefix", "site")
        
        # 2. 备份配置 
        backup_config(config_dict, save_dir)

        # 🌟 3. 实例化大一统审计中心
        self.audit_center = SiteAuditCenter(base_save_dir=save_dir, strategy_prefix=strategy_prefix)

        # 4. 实例化干活的组件
        generator = SiteUrlGenerator(self.log_callback)
        parser = SiteDataParser(self.log_callback)
        factory = SiteCrawlerFactory(self.log_callback)
        self.crawler = factory.create_crawler(config_dict)

        # 5. 生成种子链接
        start_urls = await generator.generate(
            config_dict.get("strategy_settings", {}),
            max_targets=100
        )
        
        if not start_urls:
            self._log("❌ 未生成有效种子链接，任务中止。", "error")
            self.is_running = False
            return

        self._log(f"📁 识别到 {len(start_urls)} 个初始目标，准备起航", "info")
        # 🌟 删除了冗长的“防崩预创建双保险”循环 os.makedirs，全权交由 AuditCenter 惰性创建

        # 6. 核心：挂载外部独立的路由处理器 (极简传参)
        self.request_handler = SiteRequestHandler(
            config_dict=config_dict,
            parser=parser,
            audit_center=self.audit_center,  # 🌟 唯一的数据依赖传入
            log_callback=self.log_callback,
            check_running_func=lambda: self.is_running
        )
        
        # --- 路由分发逻辑：明线与暗线 ---
        @self.crawler.router.default_handler
        async def route_wrapper(context):
            if self.request_handler: 
                await self.request_handler.default_handler(context)

        @self.crawler.router.handler('NEED_CLICK')
        async def action_wrapper(context):
            if self.request_handler: 
                await self.request_handler.action_handler(context)
            
        # 🌟 核心重构：全局底层兜底路由 (拦截所有重试耗尽的彻底失败链接)
        @self.crawler.failed_request_handler
        async def failed_request_wrapper(context, error: Exception):  # 🛠️ 修复：必须保留 context 和 error 两个参数！
            url = context.request.url
            domain = get_core_domain(url)
            
            # 提取报错信息 (优先使用最后一次 error_messages，否则提取抛出的 error 对象)
            error_msgs = getattr(context.request, 'error_messages', [])
            err_msg = error_msgs[-1] if error_msgs else str(error)
            
            # 移除静默跳过，改用严格异常抛出 (防生产环境 -O 优化参数导致 assert 语句被抹除失效)
            if self.audit_center is None:
                raise RuntimeError("致命错误：AuditCenter 尚未初始化或已丢失！生命周期严重错乱。")
                
            await self.audit_center.record_page_failure(
                domain=domain, 
                url=url, 
                status_code=500,  # 底层崩溃统一标 500
                error_msg=f"底层重试耗尽彻底放弃: {err_msg}"
            )

        # 7. 启动 Crawlee 引擎
        self._log("🚀 Site 爬虫引擎启动...", "warning")
        try:
            await self.crawler.run(start_urls)
        except Exception as e:
            self._log(f"❌ 运行出错: {e}", "error")

        # 8. 收尾工作
        self._log("🏁 网页遍历结束，等待后台任务队列清空...", "warning")
        
        # 1. 等待原生文件下载流清空
        while self.request_handler and getattr(self.request_handler, 'files_active', 0) > 0:
            await asyncio.sleep(0.5)
            if not self.is_running:
                break
                
        # 🌟 [闭环修复] 2. 等待网络探针的异步落盘队列清空 (Graceful Shutdown)
        if self.request_handler and hasattr(self.request_handler, 'network_monitor'):
            probe_queue = getattr(self.request_handler.network_monitor, '_record_queue', None)
            if probe_queue:
                self._log("⏳ 正在等待网络探针日志安全落盘...", "info")
                # 严格等待队列中所有任务执行完毕 (依赖 Worker 内部调用 task_done)
                await probe_queue.join()
                # 发送毒药丸(None)，通知后台死循环的 Worker 安全退出
                await probe_queue.put(None)
                    
        # 🌟 极简收尾：加上安全判空消除 Pylance 报错
        if self.audit_center:
            await self.audit_center.export_final_reports()
        
        # ⚠️ 必须在队列彻底清空后，再将 is_running 设为 False
        # 这样 network_monitor 里的独立 Worker 才会安详地退出死循环
        self.is_running = False
        self._log("🎉 Site 任务完成！", "success")