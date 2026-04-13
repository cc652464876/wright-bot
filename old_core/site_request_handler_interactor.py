# -*- coding: utf-8 -*-

# -*- coding: utf-8 -*-

import asyncio
from crawlee import Request  # 👈 新增引入正规的 Request 模型
from core.site_error_system import error_interceptor  # 🚀 [新增] 引入全局报错拦截器
from core.site_utils import get_core_domain  # [新增] 引入域名提取工具

# ==============================================================================
# 模块名称: site_request_handler_interactor.py
# 功能描述: 站点采集线 - DOM 页面清洗与交互器
# 核心职责:
#   1. 战前清理: 自动处理全球各语言的 Cookie 弹窗与遮罩层
#   2. DOM 解析: 提取页面内明线 a 标签链接
#   3. 深度交互: 主动识别并点击页面上的疑似“下载”按钮
# ==============================================================================

class SiteRequestHandlerInteractor:
    def __init__(self, log_callback, check_running_func, record_interaction=None):
        """
        初始化页面交互器，接管所有与 DOM 元素的视觉交互 (新增埋点汇报函数)
        """
        self.log_callback = log_callback
        self.is_running = check_running_func
        self.record_interaction = record_interaction  # [新增] 接收审计埋点回调

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[DOM交互] {message}", level)
        else:
            print(f"[{level.upper()}] [DOM交互] {message}")

    async def clear_cookie_banners(self, page):
        """
        🧹 战前清理：自动处理 Cookie 弹窗与遮罩层
        防止弹窗拦截后续的原生元素点击 (如点击下载按钮)
        """
        if not self.is_running() or not page:
            return

        try:
            # 汇总全球常见的 "接受/关闭" 按钮特征 (支持中英文)
            cookie_selectors = [
                'button:has-text("Accept All")',
                'button:has-text("Accept")',
                'button:has-text("Accept cookies")',
                'button:has-text("I Accept")',
                'button:has-text("同意")',
                'button:has-text("接受全部")',
                '#accept-cookies',
                '.cookie-accept',
                '.cookie-banner button'
            ]
            
            # 将多个选择器用逗号拼接，Playwright 会同时查找
            combined_selector = ", ".join(cookie_selectors)
            cookie_btn = page.locator(combined_selector).first
            
            # 💡 极短超时：只给 1.5 秒时间寻找弹窗。有就点，没有就立刻走人，绝不墨迹
            if await cookie_btn.is_visible(timeout=1500):
                await cookie_btn.click()
                self._log("🍪 已自动破除 Cookie 弹窗遮挡", "info")
                
                # [埋点] 记录 Cookie 弹窗清除动作 - [修复战役一] 移除 create_task 改用 await，防止生命周期脱节
                if self.record_interaction:
                    current_url = page.url
                    domain = get_core_domain(current_url)
                    await self.record_interaction(domain, {
                        "url": current_url,
                        "action": "clear_cookie_banner",
                        "description": "已自动点击同意/关闭 Cookie 弹窗"
                    })
                    
                # 稍微等个 500 毫秒，让弹窗的消失动画飞一会儿，以免后续点击落空
                await page.wait_for_timeout(500)
                
        except Exception:
            # 没有任何弹窗，或者报错，都保持绝对静默，不干扰主线任务
            pass

    async def extract_raw_links(self, context):
        """
        🔍 提取页面内明线 a 标签的 href 链接
        兼容 BeautifulSoup 解析 (如果有) 和 Playwright 原生提取
        """
        if not self.is_running():
            return []

        raw_links = []
        if hasattr(context, 'soup') and context.soup: 
            try:
                raw_links = [a['href'] for a in context.soup.find_all('a', href=True)]
            except Exception as e:
                self._log(f"⚠️ 静态链接提取微恙: {e}", "warning")
        elif hasattr(context, 'page') and context.page:
            # 🚀 [新增] 穿上防弹衣：保护原生 DOM 操作，且不再吞咽异常，抛出供 Crawlee 重试
            async with error_interceptor(context.page, context.request.url):
                raw_links = await context.page.eval_on_selector_all(
                    "a[href]", "elements => elements.map(el => el.getAttribute('href'))"
                )
            
        return raw_links

    async def trigger_download_buttons(self, context, downloader=None):
        """
        🖱️ 侦察兵模式：扫描疑似 PDF 下载按钮。
        绝对不直接点击！而是将其裂变为带独立指纹的任务，推入 Crawlee 队列，交由敢死队 (ActionHandler) 空降处理。
        """
        if not self.is_running() or not hasattr(context, 'page') or not context.page:
            return []
            
        current_url = context.request.url

        # 🚀 [修改] 穿上防弹衣：不再静默吞咽异常，而是交由拦截器拍照并抛出重试
        async with error_interceptor(context.page, current_url):
            # 🎯 宽泛特征扫描：宁可错杀，不可放过。依赖后置的下载检测逻辑来拦截非 PDF 文件
            selectors = [
                # 1. 天生需要点击交互的元素
                'button:has-text("PDF"), button:has-text("Download"), button:has-text("下载"), button:has-text("View"), button:has-text("查看")',
                '[role="button"]:has-text("PDF"), [role="button"]:has-text("Download"), [role="button"]:has-text("下载"), [role="button"]:has-text("View"), [role="button"]:has-text("查看")',
                
                # 2. “伪链接”拦截 (<a> 标签)：包括无 href、空 href、锚点和 JS 伪协议
                'a:not([href]):has-text("PDF"), a:not([href]):has-text("Download"), a:not([href]):has-text("下载"), a:not([href]):has-text("View"), a:not([href]):has-text("查看")',
                'a[href=""]:has-text("PDF"), a[href=""]:has-text("Download"), a[href=""]:has-text("下载"), a[href=""]:has-text("View"), a[href=""]:has-text("查看")',
                'a[href="#"]:has-text("PDF"), a[href="#"]:has-text("Download"), a[href="#"]:has-text("下载"), a[href="#"]:has-text("View"), a[href="#"]:has-text("查看")',
                'a[href^="javascript:" i]:has-text("PDF"), a[href^="javascript:" i]:has-text("Download"), a[href^="javascript:" i]:has-text("下载"), a[href^="javascript:" i]:has-text("View"), a[href^="javascript:" i]:has-text("查看")',
                
                # 3. 强特征类名兜底
                'button.pdf-download-btn, button.download-btn, button.pdf-icon',
                'a:not([href]).pdf-download-btn, a[href="#"].pdf-download-btn, a[href^="javascript:" i].pdf-download-btn, a[href=""].pdf-download-btn'
            ]
            
            # 将选择器合并并交由 Playwright 引擎统一寻址
            button_locators = context.page.locator(', '.join(selectors))
            
            button_count = await button_locators.count()
            if button_count == 0:
                return []

            self._log(f"🔎 侦察兵在当前页面发现 {button_count} 个疑似交互按钮，准备实施深度甄别...", "info")
            
            requests_to_add = []
            current_url = context.request.url

            for i in range(button_count):
                if not self.is_running():
                    break
                    
                btn = button_locators.nth(i)
                
                if await btn.is_visible() and await btn.is_enabled():
                    
                    # ==============================================================================
                    # 🛡️ 防嵌套装甲 (DOM 树逆向溯源)
                    # 作用：检查该元素是否被一个拥有真实静态 href 的 <a> 标签包裹。
                    # ==============================================================================
                    is_wrapped_by_real_link = await btn.evaluate('''el => {
                        const aTag = el.closest('a');
                        if (!aTag) return false;
                        const href = aTag.getAttribute('href');
                        // 如果父级 a 标签有 href，并且不是 "#", "", "javascript:" 开头，那就是真链接
                        return href && href !== '#' && href !== '' && !href.toLowerCase().startsWith('javascript:');
                    }''')

                    if is_wrapped_by_real_link:
                        # 修正：它本身就是一个被包裹的静态链接，直接跳过交互即可
                        self._log(f"🛡️ 识别并拦截伪装按钮 #{i}: 其外层已被静态真实链接包裹，直接跳过无效交互。", "info")
                        continue
                    # ==============================================================================

                    # 💡 生成带有唯一指纹 (unique_key) 的空降坐标
                    unique_key = f"{current_url}#btn{i}"
                    
                    # 👈 核心修复：使用 Request 模型包裹，并将键名修改为严格的蛇形规范
                    requests_to_add.append(Request.from_url(
                        url=current_url,
                        unique_key=unique_key,
                        user_data={
                            "label": "NEED_CLICK", 
                            "target_index": i
                        }
                    ))
                    self._log(f"📌 标记高危交互目标: 按钮 #{i} -> 已封装为标准 Request 坐标", "success")
            
            # 🚀 批量将裂变任务推入 Crawlee 请求队列
            if requests_to_add:
                await context.add_requests(requests_to_add)
                self._log(f"🚀 已成功向主控中心 (Queue) 派发 {len(requests_to_add)} 个敢死队空降任务", "success")

        return []