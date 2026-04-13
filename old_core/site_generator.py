# -*- coding: utf-8 -*-
# ==============================================================================
# 模块名称: site_generator.py
# 功能描述: 站点/地图 URL 生成器 (全浏览器内核版 - 抗屏蔽最强)
# 核心职责:
#   1. 专职负责 "direct", "full", "sitemap" 三种站点级别的抓取策略。
#   2. 自动嗅探目标域名的 robots.txt 寻找 Sitemap 入口。
#   3. 使用自带的 Chromium 内核强行渲染并解析多层级的 Sitemap XML 结构。
#   4. 输出供 Crawlee 消费的种子 URL 列表。
#   (注：已移除所有搜索引擎相关的逻辑，实现完全解耦)
# 输入: 抓取策略配置 (strategy_cfg)
# 输出: 起始 URL 列表 (List[str])
# ==============================================================================

import asyncio
import os
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext

class SiteUrlGenerator:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        # 预先计算 Chrome 路径
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.chrome_path = os.path.join(base_dir, "chromium-1208", "chrome.exe")
        
        # 💡 资源复用池：确保整个前置解析周期内，只发生一次冷启动
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def _ensure_browser(self):
        """单例模式：需要时启动一次浏览器，后续所有请求复用该内核"""
        if not self._playwright:
            self._log("⏳ 正在初始化 Generator 专属持久化内核...", "info")
            self._playwright = await async_playwright().start()
            
            # 💡 优雅修复：放弃字典解包，直接根据条件显式传参，完美契合 Pylance 静态检查
            if os.path.exists(self.chrome_path):
                self._browser = await self._playwright.chromium.launch(
                    headless=True, 
                    executable_path=self.chrome_path
                )
            else:
                self._browser = await self._playwright.chromium.launch(
                    headless=True
                )
            self._context = await self._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1280, 'height': 800},
                locale='en-US'
            )

    async def close(self):
        """彻底释放 Generator 内核资源"""
        if self._context: await self._context.close()
        if self._browser: await self._browser.close()
        if self._playwright: await self._playwright.stop()
        self._context = self._browser = self._playwright = None

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[SiteGenerator] {message}", level)
        else:
            print(f"[{level.upper()}] [SiteGenerator] {message}")

    async def generate(self, strategy_cfg, max_targets=100):
        strategy = strategy_cfg.get("crawl_strategy", "direct")
        
        # 1. 提取 target_urls 数组
        target_urls = strategy_cfg.get("target_urls", [])
        
        if not target_urls:
            self._log("❌ 错误：未提供任何目标网址", "error")
            return []

        self._log(f"🔍 正在执行 Site 专属策略: {strategy}, 共 {len(target_urls)} 个目标", "info")

        all_start_urls = []
        
        # 2. 必须用 for 循环依次处理每个网址
        for url in target_urls:
            if strategy in ["direct", "full"]:
                all_start_urls.extend(self._get_direct_urls(url))
            elif strategy == "sitemap":
                sitemap_urls = await self._get_sitemap_urls(url, max_targets)
                all_start_urls.extend(sitemap_urls)
            else:
                self._log(f"⚠️ 未知的 Site 策略类型或非法传入搜索策略: {strategy}", "warning")
                
        # 3. 任务结束，彻底释放 Generator 专属内核，去重并返回
        await self.close()
        return list(dict.fromkeys(all_start_urls))

    async def preview_robots_txt(self, input_url):
        """专门给 UI 监控使用的接口"""
        if not input_url.startswith('http'): input_url = f'https://{input_url}'
        base_domain = "/".join(input_url.split("/")[:3])
        robots_url = f"{base_domain}/robots.txt"
        
        self._log(f"🔎 UI 请求检测: {robots_url}", "info")
        content = await self._fetch_via_playwright(robots_url, timeout=15000)
        
        if content:
            text = content.decode('utf-8', errors='ignore')
            await self.close()
            return text
            
        await self.close()
        return None

    async def _fetch_via_playwright(self, url, timeout=60000, max_retries=2):
        """复用持久化内核，增加请求拦截、动态等待与重试机制，告别频繁超时"""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        import asyncio

        content = None
        
        for attempt in range(max_retries + 1):
            page = None
            try:
                await self._ensure_browser()  # 确保内核已就绪
                
                if self._context is None:
                    raise RuntimeError("Browser context 尚未正确初始化，无法创建新页面。")
                    
                page = await self._context.new_page()
                
                # 🚀 修复 2: 动态调整 wait_until 策略 (需提前判断，供智能路由使用)
                is_raw_file = url.lower().endswith(('.xml', '.txt'))
                wait_strategy = "commit" if is_raw_file else "domcontentloaded"

                # 🚀 修复 1 进阶版: 智能资源拦截
                async def smart_route(route):
                    # 场景A: 如果是明确的 robots 或 sitemap 纯文本文件，丢弃除了 document 以外的所有请求 (追求极致速度)
                    if is_raw_file:
                        if route.request.resource_type != "document":
                            return await route.abort()
                        return await route.continue_()
                    
                    # 场景B: 如果是普通网页 (可能遇到了 Cloudflare 5秒盾，或 direct 策略抓首页)
                    # 放行 stylesheet, font 和 script 以应对前端反爬指纹检测，仅拦截极度占带宽的图片和视频
                    if route.request.resource_type in ["image", "media"]:
                        return await route.abort()
                    
                    return await route.continue_()

                await page.route("**/*", smart_route)

                # 发起请求
                response = await page.goto(url, timeout=timeout, wait_until=wait_strategy)
                
                if response and response.status == 200:
                    content = await response.body()
                    break  # 成功获取，跳出重试循环
                else:
                    status = response.status if response else 'Unknown'
                    self._log(f"⚠️ 请求被拦截或响应异常 [{status}]: {url}", "warning")
                    # 如果遇到明确的反爬封禁 (403, 429)，直接退出重试以防被彻底拉黑
                    if status in [403, 429]: 
                        break

            except PlaywrightTimeoutError:
                self._log(f"⏳ [尝试 {attempt + 1}/{max_retries + 1}] 页面加载超时: {url}", "warning")
            except asyncio.CancelledError:
                self._log(f"🛑 [尝试 {attempt + 1}/{max_retries + 1}] 异步任务被取消 (CancelledError): {url}", "warning")
                raise  # CancelledError 通常由上层任务控制器引发，应当继续向上抛出
            except Exception as e:
                # 捕获 ERR_ABORTED 等其他底层连接错误
                self._log(f"❌ [尝试 {attempt + 1}/{max_retries + 1}] 页面加载错误 ({type(e).__name__}): {str(e)}", "error")
            finally:
                if page:
                    await page.close()  # 用完只关页面，不关浏览器
            
            # 🚀 修复 3: 失败重试前的退避等待 (防止被目标服务器防火墙当作疯狂 CC 攻击)
            if attempt < max_retries:
                await asyncio.sleep(2)

        return content

    def _get_direct_urls(self, target_url):
        if not target_url: return []
        if not target_url.startswith(('http://', 'https://')): target_url = 'https://' + target_url
        self._log(f"🎯 锁定单一站点目标: {target_url}", "success")
        return [target_url]

    async def _detect_sitemap_from_robots(self, domain_url):
        if not domain_url.startswith('http'): domain_url = f'https://{domain_url}'
        base_domain = "/".join(domain_url.split("/")[:3])
        robots_url = f"{base_domain}/robots.txt"
        
        self._log(f"🕵️ 检索 Robots 协议: {robots_url}", "info")
        content_bytes = await self._fetch_via_playwright(robots_url, timeout=30000)
        
        found_sitemaps = []
        if content_bytes:
            text = content_bytes.decode('utf-8', errors='ignore')
            for line in text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    url = line.split(":", 1)[1].strip()
                    found_sitemaps.append(url)
        
        if found_sitemaps:
            self._log(f"🕵️ 成功识别到 {len(found_sitemaps)} 个 Sitemap 入口", "success")
            return found_sitemaps
            
        return [f"{base_domain}/sitemap_index.xml"]

    async def _get_sitemap_urls(self, target_url, max_limit):
        target_list = []
        if target_url.lower().endswith('.xml'):
            target_list = [target_url]
        else:
            target_list = await self._detect_sitemap_from_robots(target_url)

        self._log(f"🗺️ 启动站点批量解析，待处理队列: {len(target_list)}", "info")

        extracted_urls = []
        
        async def recursive_extract(url_to_fetch, depth=0):
            if depth > 5: return 
            xml_data = await self._fetch_via_playwright(url_to_fetch)
            if not xml_data: return

            try:
                soup = BeautifulSoup(xml_data, 'xml')
                locs = soup.find_all('loc')
                new_count = 0
                for loc in locs:
                    link = loc.text.strip()
                    if not link: continue

                    if link.lower().endswith('.xml'):
                        if link not in extracted_urls:
                            self._log(f"📂 [层级{depth}] 发现子 Sitemap: {link}", "info")
                            await recursive_extract(link, depth + 1)
                    else:
                        if link not in extracted_urls:
                            if len(extracted_urls) < (max_limit or 99999):
                                extracted_urls.append(link)
                                new_count += 1
                self._log(f"⏳ [层级{depth}] 解析结束，新增 {new_count} 个链接", "info")
            except Exception as e:
                self._log(f"❌ 解析错误: {e}", "error")

        try:
            for sitemap_index, sitemap_url in enumerate(target_list):
                self._log(f"🚀 [任务 {sitemap_index + 1}/{len(target_list)}] 正在处理: {sitemap_url}", "info")
                await recursive_extract(sitemap_url)
            
            # 去重并切片
            extracted_urls = list(dict.fromkeys(extracted_urls))
            if max_limit:
                extracted_urls = extracted_urls[:max_limit]

            self._log(f"✅ Sitemap 解析完成，共提取有效链接: {len(extracted_urls)} 个", "success")
            return extracted_urls

        except Exception as e:
            self._log(f"❌ 批量解析发生错误: {e}", "error")
            return []