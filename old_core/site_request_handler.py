# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: site_request_handler.py
# 功能描述: 站点采集线 - 核心路由指挥官 (极简重构版)
# 核心职责:
#   作为最高指挥中心，统筹 Audit(账本)、Network(网络监控)、Downloader(下载)、
#   Interactor(DOM交互)、Strategist(路由策略) 和 Action(敢死队)。
#   无具体业务实现，仅负责流水线生命周期的调度与环境装填。
# ==============================================================================

import os
import asyncio
from core.site_utils import get_core_domain

# 引入所有干员模块 (请确保它们在同一目录下)
from .site_request_handler_network_monitor import SiteRequestHandlerNetworkMonitor
from .site_request_handler_downloader import SiteRequestHandlerDownloader
from .site_request_handler_interactor import SiteRequestHandlerInteractor
from .site_request_handler_strategist import SiteRequestHandlerStrategist
from .site_request_handler_action import SiteRequestHandlerAction

class SiteRequestHandler:
    def __init__(self, config_dict, parser, audit_center, log_callback, check_running_func):
        """
        初始化核心路由指挥官 (极简重构版)
        """
        self.config_dict = config_dict
        self.parser = parser
        self.audit_center = audit_center  # 🌟 接入大一统审计中心
        self.log_callback = log_callback
        self.is_running = check_running_func
        
        # 从 config_dict 解析基础路径，供尚未重构的 Downloader 使用
        task_info = config_dict.get("task_info", {})
        self.save_dir = task_info.get("save_directory", "./downloads")
        self.strategy_prefix = config_dict.get("strategy_settings", {}).get("strategy_prefix", "site")
        self.target_ext = f".{config_dict.get('strategy_settings', {}).get('file_type', 'pdf').lower()}"
        
        # ==========================================
        # 🌟 干员集结：模块化装配
        # ==========================================
        # 1. 网络干员：负责底层抓包拦截
        self.network_monitor = SiteRequestHandlerNetworkMonitor(
            self.parser, self.target_ext, log_callback, check_running_func
        )
        
        # 2. 原有四大干员
        self.downloader = SiteRequestHandlerDownloader(
            config_dict, log_callback, check_running_func, self.save_dir, self.strategy_prefix
        )
        
        # 🌟 彻底移除 dummy 存根，将真正的 AuditCenter 交互记录埋点注入前线干员
        self.interactor = SiteRequestHandlerInteractor(
            log_callback, check_running_func, self.audit_center.record_interaction
        )
        
        self.strategist = SiteRequestHandlerStrategist(
            config_dict, log_callback, check_running_func
        )
        self.action_squad = SiteRequestHandlerAction(
            self.interactor, self.downloader, log_callback, check_running_func, self.audit_center.record_interaction
        )

    # 🌟 彻底删除了繁琐的 _prepare_domain_env 方法 (目录黑盒化，全权交由 AuditCenter)

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[Site指挥官] {message}", level)
        else:
            print(f"[{level.upper()}] [Site指挥官] {message}")

    # ==========================================
    # 🌟 底层拦截补丁 (Task 1 核心)
    # ==========================================
    async def _force_pdf_download_route(self, route, request):
        """
        Playwright page.route("**/*") 全局路由拦截器。
        注册后，页面发出的每一个网络请求都会经过此方法。

        ⚠️ 关键约束：route 对象必须显式 continue / fulfill / abort，
           否则请求永久挂起，整个页面渲染卡死。
           下方 await route.continue_() 是兜底安全调用，填充业务逻辑时不可删除。

        实现目标（待填充）：
        - 检测 Content-Type 或 URL 后缀为 PDF 的请求。
        - 对目标请求修改 Accept 头，强制服务器返回二进制流而非在线预览。
        - 非目标请求直接透传（await route.continue_()）。

        Args:
            route  : Playwright Route 对象，用于 continue / fulfill / abort。
            request: Playwright Request 对象，含 url / resource_type / headers。
        """
        # TODO: 在此处加入 PDF 强制下载拦截逻辑（修改 Accept 头等）
        # 兜底：所有未匹配请求必须透传，否则请求永久挂起
        await route.continue_()

    # ==========================================
    # 🌟 属性代理：透传给 Downloader
    # ==========================================
    @property
    def files_active(self): return self.downloader.files_active
    @property
    def files_found(self): return self.downloader.files_found
    @property
    def files_downloaded(self): return self.downloader.files_downloaded
    def get_stats(self): return self.downloader.get_stats()

    # ==========================================
    # 🌟 核心路由防线
    # ==========================================
    async def action_handler(self, context) -> None:
        """敢死队调度防线 (专为带有 NEED_CLICK 标签的任务提供入口)"""
        if not self.is_running(): return
        
        try: await context.page.route("**/*", self._force_pdf_download_route)
        except Exception: pass
            
        # 🌟 移除旧的环境装填，直接执行
        await self.action_squad.handle_action(context)

    async def default_handler(self, context) -> None:
        """主线调度防线"""
        if not self.is_running(): return
        
        try: await context.page.route("**/*", self._force_pdf_download_route)
        except Exception: pass
            
        # 0. 环境梳理
        current_url = context.request.url
        domain = get_core_domain(current_url)
        
        # 🌟 临时保留计算 domain_workspace，专门供给尚未重构的 Downloader 和 NetworkMonitor
        domain_workspace = os.path.join(self.save_dir, f"{self.strategy_prefix}_{domain}")
        self._log(f"🔍 扫描站点: {current_url}", "info")
        
        # 🌟 [埋点 1] 记录网页扫描历史 (直接调用全新 AuditCenter)
        asyncio.create_task(self.audit_center.record_page_success(domain, current_url))
            
        download_tasks = []

        # 🧹 第一步：战前清理
        if hasattr(context, 'page'):
            await self.interactor.clear_cookie_banners(context.page)

        # 🛡️ 第二步：布置底层网络探针 (注意：临时传 audit_center 顶替旧 manager)
        await self.network_monitor.attach_probe(
            context, domain, domain_workspace, self.audit_center, self.downloader
        )

        # 🕸️ 第三步：DOM 树提取
        raw_links = await self.interactor.extract_raw_links(context)
        new_urls_batch = []  # 🌟 降噪：改为仅收集 URL 纯净列表
        
        # 🌟 高效内存去重预备：提取该域名下所有已被记录的 file_urls
        existing_manifest = self.audit_center._domain_manifests.get(domain, [])
        existing_urls = {u for page in existing_manifest for u in page.get("file_urls", [])}
        
        for href in raw_links:
            item = self.parser.parse_link(current_url, href, self.target_ext)
            if item:
                # 🌟 隔离去重与批次内去重 (全新集合校验)
                if item['file_url'] in existing_urls or item['file_url'] in new_urls_batch:
                    continue

                new_urls_batch.append(item['file_url'])
                self.downloader.files_found += 1  
                
                target_sub_folder = f"{self.strategy_prefix}_{domain}"
                self._log(f"🎉 捕获文件: {item['file_name']} -> 将存入 [{target_sub_folder}]", "success")
                
                save_path = os.path.join(domain_workspace, item['file_name'])
                download_tasks.append(
                    self.downloader.native_download_task(context.page, item['file_url'], save_path, item)
                )
                

        # ⏳ 第四步：强制生命周期锁定与真实落盘校验
        successful_urls = []
        if download_tasks:
            self._log(f"⏳ 锁定当前页面，等待 {len(download_tasks)} 个文件原生落盘...", "info")
            # 等待所有下载协程完成，捕获异常防止崩溃
            results = await asyncio.gather(*download_tasks, return_exceptions=True)
            
            # 严格对齐 new_urls_batch 和 download_tasks 的索引
            for i, task_result in enumerate(results):
                if isinstance(task_result, Exception):
                    # 下载失败：只打印日志，绝对不加入成功列表
                    self._log(f"⚠️ 任务崩溃 (幽灵数据已拦截): {str(task_result)[:100]}", "error")
                else:
                    # 下载成功：提取对应的真实 URL
                    successful_urls.append(new_urls_batch[i])

        # 🌟 [埋点 3] 时序修复：在确保文件落地后，仅将成功的 URL 记入账本
        if successful_urls:
            # 放弃 create_task，直接 await 保证账本写入的时序绝对安全
            await self.audit_center.record_result_batch(domain, current_url, successful_urls)

        # 🖱️ 第五步：补充防线
        await self.interactor.trigger_download_buttons(context, self.downloader)

        # 🚀 第六步：翻页与路由扩散
        await self.strategist.enqueue_next_pages(context)