# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: site_request_handler_strategist.py
# 功能描述: 站点采集线 - 路由与链接扩散策略器
# 核心职责:
#   1. 管理爬虫的翻页与路由推送 (基于 Crawlee 官方策略)
#   2. 维护严格的正则表达式排除网 (防止爬虫误入图片、静态资源或目标文件本身)
#   3. 根据全局策略 (full, sitemap, direct) 决定是否扩散
# ==============================================================================

import re
# from crawlee import EnqueueStrategy #

class SiteRequestHandlerStrategist:
    def __init__(self, config_dict, log_callback, check_running_func):
        """
        初始化路由策略器，接管所有链接扩散逻辑
        """
        self.config_dict = config_dict
        self.log_callback = log_callback
        self.is_running = check_running_func
        
        # 获取策略配置
        self.crawl_strategy = config_dict.get("strategy_settings", {}).get("crawl_strategy", "full")
        self.target_ext = f".{config_dict.get('strategy_settings', {}).get('file_type', 'pdf').lower()}"

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[路由策略] {message}", level)
        else:
            print(f"[{level.upper()}] [路由策略] {message}")

    def _get_exclude_patterns(self):
        """
        构建最严密的正则表达式排除网 (忽略大小写)
        核心注意：这里要把 target_ext (比如 .pdf) 也加进排除名单！
        因为前面的下载器已经原生下载了它，不能让 Crawlee 再把它当成网页去请求。
        """
        return [
            re.compile(r'(?i)\.(jpg|jpeg|png|gif|webp|svg|ico|bmp)$'), # 视觉图片
            re.compile(r'(?i)\.(css|js|woff|woff2|ttf|eot)$'),         # 前端静态资源
            re.compile(r'(?i)\.(mp4|webm|mp3|avi|mov)$'),              # 媒体文件
            re.compile(r'(?i)\.(zip|rar|7z|exe|dmg)$'),                # 压缩包与程序
            re.compile(rf'(?i)\{self.target_ext}$')                    # 排除目标文件自身
        ]

    async def enqueue_next_pages(self, context):
        """
        第三道防线：翻页与路由扩散
        """
        if not self.is_running() or not hasattr(context, 'enqueue_links'):
            return

        exclude_patterns = self._get_exclude_patterns()
        
        try:
            # 🛡️ 新增防线：在执行底层 DOM 扫描前，检查页面是否存活并等待其稳定
            if hasattr(context, 'page') and context.page:
                if context.page.is_closed():
                    self._log("⚠️ 页面已关闭，跳过本次链接扩散", "warning")
                    return
                # 尝试等待页面 DOM 稳定，防止页面正在跳转中
                try:
                    await context.page.wait_for_load_state('domcontentloaded', timeout=2000)
                except Exception:
                    pass

            # 执行扩散
            if self.crawl_strategy in ["full", "direct"]:
                await context.enqueue_links(
                    strategy='same-domain', 
                    exclude=exclude_patterns
                )
            elif self.crawl_strategy == "sitemap":
                pass 
            else:
                await context.enqueue_links(
                    strategy='same-domain', 
                    exclude=exclude_patterns
                )
        except Exception as e:
            # 🛡️ 精准拦截销毁异常，将其降级为 info 提示，防止污染错误日志
            if "Execution context was destroyed" in str(e):
                self._log("ℹ️ 页面已跳转或刷新，DOM 上下文销毁，已安全跳过本次链接扩散", "info")
            else:
                self._log(f"⚠️ 链接扩散策略执行异常: {e}", "warning")