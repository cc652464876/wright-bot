# -*- coding: utf-8 -*-
import asyncio
from .site_request_handler_action_downloader import ActionDownloader
from core.site_error_system import error_interceptor
from core.site_utils import get_core_domain  # [新增] 引入域名提取工具

# ==============================================================================
# 模块名称: site_request_handler_action.py
# 功能描述: 站点采集线 - 敢死队交互器 (Action Handler)
# 核心职责:
#   作为独立的沙箱防线，专门处理带有 NEED_CLICK 标签的高危点击任务。
#   继承合法状态空降，一击脱离，将致命报错（如页面崩溃、重定向）隔离在沙箱内。
# ==============================================================================

class SiteRequestHandlerAction:
    def __init__(self, interactor, downloader, log_callback, check_running_func, record_interaction=None):
        """
        初始化敢死队，接收指挥官分配的武器 (新增埋点汇报函数)
        """
        self.interactor = interactor
        self.downloader = downloader
        self.log_callback = log_callback
        self.is_running = check_running_func
        self.record_interaction = record_interaction  # [新增] 接收审计埋点回调

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[Action敢死队] {message}", level)
        else:
            print(f"[{level.upper()}] [Action敢死队] {message}")

    async def handle_action(self, context) -> None:
        """
        独立的沙箱上下文，继承合法状态空降，仅执行单一按钮的点击与接盘。
        """
        if not self.is_running():
            return

        user_data = context.request.user_data
        target_index = user_data.get('target_index')
        current_url = context.request.url
        
        self._log(f"🪂 敢死队空降完毕: 目标页面 {current_url} | 准备狙击按钮 #{target_index}", "warning")

        # 🚀 [修改] 穿上防弹衣：不再静默吞咽异常，接管报错、拍照并抛出供 Crawlee 重试
        async with error_interceptor(context.page, current_url):
            # 1. 战前清理：确保页面上没有 Cookie 遮罩层挡住我们要点的按钮
            if hasattr(context, 'page'):
                await self.interactor.clear_cookie_banners(context.page)

            # 2. 重新锁定目标：必须与侦察兵 (Interactor) 使用完全一致的特征选择器，确保 target_index 绝对精准对应！
            selectors = [
                'button:has-text("PDF"), button:has-text("Download"), button:has-text("下载"), button:has-text("View"), button:has-text("查看")',
                '[role="button"]:has-text("PDF"), [role="button"]:has-text("Download"), [role="button"]:has-text("下载"), [role="button"]:has-text("View"), [role="button"]:has-text("查看")',
                'a:not([href]):has-text("PDF"), a:not([href]):has-text("Download"), a:not([href]):has-text("下载"), a:not([href]):has-text("View"), a:not([href]):has-text("查看")',
                'a[href=""]:has-text("PDF"), a[href=""]:has-text("Download"), a[href=""]:has-text("下载"), a[href=""]:has-text("View"), a[href=""]:has-text("查看")',
                'a[href="#"]:has-text("PDF"), a[href="#"]:has-text("Download"), a[href="#"]:has-text("下载"), a[href="#"]:has-text("View"), a[href="#"]:has-text("查看")',
                'a[href^="javascript:" i]:has-text("PDF"), a[href^="javascript:" i]:has-text("Download"), a[href^="javascript:" i]:has-text("下载"), a[href^="javascript:" i]:has-text("View"), a[href^="javascript:" i]:has-text("查看")',
                'button.pdf-download-btn, button.download-btn, button.pdf-icon',
                'a:not([href]).pdf-download-btn, a[href="#"].pdf-download-btn, a[href^="javascript:" i].pdf-download-btn, a[href=""].pdf-download-btn'
            ]
            button_locators = context.page.locator(', '.join(selectors))
            
            # 确保按钮元素已加载且存在
            count = await button_locators.count()
            if target_index >= count:
                self._log(f"⚠️ 坐标失效: 找不到索引为 {target_index} 的按钮，目标可能已动态消失，紧急撤离。", "warning")
                return

            btn = button_locators.nth(target_index)

            # 3. 致命一击与接盘：将复杂连招移交给专属的 ActionDownloader
            self._log(f"💥 敢死队开火: 移交指挥权，启动专属动作下载引擎执行按钮 #{target_index} 流程...", "info")
            
            # [埋点] 记录高危按钮点击事件 - [修复战役一] 移除 create_task，改用 await 同步生命周期
            if self.record_interaction:
                domain = get_core_domain(current_url)
                await self.record_interaction(domain, {
                    "url": current_url,
                    "action": "execute_click",
                    "target_index": target_index,
                    "description": "敢死队执行深度下载交互"
                })

            # 实例化全新剥离的动作引擎，共享主下载器的并发锁和配置
            action_downloader = ActionDownloader(self.downloader, self.log_callback)
            
            # 移交执行权：传入 page, browser_context, 按钮实例，以及当前的源 URL
            await action_downloader.execute(context.page, context.page.context, btn, current_url)
            
            # 🚁 任务顺利执行到底的日志
            self._log(f"🚁 敢死队任务 #{target_index} 终结，沙箱安全销毁。", "success")