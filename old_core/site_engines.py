# -*- coding: utf-8 -*-
import os
from datetime import timedelta
from crawlee.crawlers import BeautifulSoupCrawler, PlaywrightCrawler
from crawlee import ConcurrencySettings
from crawlee.sessions import SessionPool
from crawlee.fingerprint_suite import DefaultFingerprintGenerator

# ==============================================================================
# 模块名称: site_engines.py
# 功能描述: 站点爬虫工厂 (专属兵工厂)
# 核心职责:
#   1. 为 Site 线路专门定制 BeautifulSoupCrawler 或 PlaywrightCrawler 实例。
#   2. 统一配置并发控制、浏览器选项以及基础重试策略。
# 输入: 配置字典 (config_dict)
# 输出: 初始化好的 Crawlee 引擎对象
# ==============================================================================

class SiteCrawlerFactory:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[SiteEngine] {message}", level)

    def create_crawler(self, config):
        engine_cfg = config.get("engine_settings", {})
        crawler_type = engine_cfg.get("crawler_type", "playwright")
        
        common_kwargs = self._build_common_kwargs(config)

        if crawler_type == "playwright":
            self._log("🔧 正在组装 Playwright (动态渲染) 引擎...", "info")
            return self._create_playwright_engine(config, common_kwargs)
        else:
            self._log("🔧 正在组装 BeautifulSoup (静态极速) 引擎...", "info")
            return BeautifulSoupCrawler(**common_kwargs)

    def _build_common_kwargs(self, config):
        perf_cfg = config.get("performance", {})
        timeout_cfg = config.get("timeouts_and_retries", {})
        
        # =================================================================
        # 🚀 双轨制智能并发控制 (Site 专属宽泛限流策略)
        # =================================================================
        raw_max_input = perf_cfg.get("max_concurrency", "auto")

        # 1. 解析前端 UI 传来的下拉框值
        if str(raw_max_input).lower() == "auto":
            user_max = 16  # 站点线如果是 auto，给予更宽广的默认并发上限 16
        else:
            try:
                user_max = int(raw_max_input)
            except ValueError:
                user_max = 16

        # 2. 核心数学算法：防爆内存的宽泛拦截
        limit_max = 16  # Playwright 动态渲染开 16 个已经是普通电脑的物理极限
        
        final_max = min(user_max, limit_max)    # 峰值：取用户设定和物理极限(16)的较小值
        final_min = 1                           # 起步：永远保持 1
        final_desired = min(5, final_max)       # 爬升：取最佳巡航(5)和当前峰值的较小值

        concurrency_settings = ConcurrencySettings(
            min_concurrency=final_min,
            desired_concurrency=final_desired,
            max_concurrency=final_max 
        )
        
        self._log(f"⚙️ Site引擎并发策略已锁定 -> 起步:{final_min} | 爬升:{final_desired} | 峰值:{final_max}", "info")
        # =================================================================

        return {
            # 同样将配额上限解绑，以匹配我们前端允许的无尽爬取
            "max_requests_per_crawl": perf_cfg.get("max_requests_per_crawl", 9999), 
            "max_request_retries": timeout_cfg.get("max_request_retries", 3),
            "request_handler_timeout": timedelta(seconds=timeout_cfg.get("request_handler_timeout_secs", 60)),
            "concurrency_settings": concurrency_settings,
        }

    def _create_playwright_engine(self, config, common_kwargs):
        engine_cfg = config.get("engine_settings", {})
        stealth_cfg = config.get("stealth", {})
        timeout_cfg = config.get("timeouts_and_retries", {})
        
        # === 🚀 1. 解析并设置全局统一的下载“安全基座” ===
        # 统一读取前端 task_info 里的 save_directory，与 site_runner.py 保持绝对一致
        task_info = config.get("task_info", {})
        base_save_dir = task_info.get("save_directory", os.path.join(os.getcwd(), "downloads"))
        
        # 我们在主保存路径下划出一个专门的 "_temp_playwright" 作为引擎底层接水盘
        download_dir = os.path.join(base_save_dir, "_temp_playwright")
        
        # 【关键修复】：在启动 Playwright 前，必须强制由 Python 创建好这个物理文件夹
        # 否则如果文件夹不存在，Playwright 触发下载时会直接抛出路径不存在的底层异常
        os.makedirs(download_dir, exist_ok=True)

        # === ⚙️ 2. 组装浏览器启动参数 ===
        # Step 6：SSL 走 Playwright 标准键 ignore_https_errors；反自动化 / WebRTC 与 crawlee_engine 对齐
        browser_launch_options = {
            "headless": stealth_cfg.get("headless", True),
            "ignore_https_errors": stealth_cfg.get("ignore_ssl_error", True),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-webrtc-hw-decoding",
                "--disable-webrtc-hw-encoding",
            ],
            # 🚀 注入全局落盘基座：强制浏览器内核把所有下载产生的文件，第一时间全部扔进这个真实存在的文件夹
            "downloads_path": download_dir,
        }

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) 
        local_chrome = os.path.join(root_dir, "chromium-1208", "chrome.exe")
        
        if os.path.exists(local_chrome):
            self._log(f"🚀 检测到本地浏览器内核: {local_chrome}", "success")
            browser_launch_options["executable_path"] = local_chrome

        pw_kwargs = common_kwargs.copy()
        
        # === 🛡️ Site 专属增强：原生防盾装甲 (Python 版) ===
        pw_kwargs.update({
            "browser_type": engine_cfg.get("browser_type", "chromium"),
            "browser_launch_options": browser_launch_options,
            
            # 🌟 1. 激活指纹生成器欺骗 WAF 防火墙
            "fingerprint_generator": DefaultFingerprintGenerator(),
            
            # 🌟 2. 激活基础会话池 (维持 10 个身份轮换足矣)
            "use_session_pool": True,
            "session_pool": SessionPool(
                max_pool_size=10
            ),
        })

        return PlaywrightCrawler(**pw_kwargs)