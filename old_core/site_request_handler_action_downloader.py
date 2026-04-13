# -*- coding: utf-8 -*-

import os
import asyncio
import random
from core.site_utils import get_core_domain

class ActionDownloader:
    def __init__(self, main_downloader, log_callback):
        """
        动作下载引擎：专门接管复杂的点击、表单突破、跨窗口拦截与严格落盘。
        """
        self.main_downloader = main_downloader
        self.log_callback = log_callback
        
        # 共享主下载器的核心配置
        self.save_dir = main_downloader.save_dir
        # 确保此处的 strategy_prefix 在上游传入时为英文（如 'sitemap', 'all'）
        self.strategy_prefix = main_downloader.strategy_prefix 
        self.target_ext = main_downloader.target_ext
        self.semaphore = main_downloader.semaphore
        
        # [同步更新] 继承主下载器的物理 I/O 互斥锁，防止并发动作下写同一文件崩溃
        self.io_lock = getattr(main_downloader, 'io_lock', asyncio.Lock())
        
        # [核心修复] 彻底干掉旧的全局静态目录引用，改为在落盘时动态计算

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[Action引擎] {message}", level)
        else:
            print(f"[{level.upper()}] [Action引擎] {message}")

    def _is_target_file(self, url, headers):
        """绝对过滤防线：坚决阻挡图片等杂音，仅放行目标 PDF 画册流"""
        content_type = headers.get("content-type", "").lower()
        url_lower = url.lower()
        
        # 1. 明确拒绝所有常见图片格式
        if any(img_ext in url_lower for img_ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']):
            return False
        if "image/" in content_type:
            return False

        # 2. 严格核验 PDF 身份
        if "application/pdf" in content_type:
            return True
        if url_lower.endswith('.pdf') or url_lower.endswith(self.target_ext):
            return True
        # 兜底：处理强制下载的二进制流，但要求 URL 必须带有 pdf 痕迹
        if "application/octet-stream" in content_type and ('.pdf' in url_lower or self.target_ext in url_lower):
             return True
             
        return False

    async def execute(self, page, browser_context, btn, source_url):
        """核心枢纽：原生流驱动的点击、防线穿透与落盘 (Playwright Native 重构版)"""
        async with self.semaphore:
            self.main_downloader.files_active += 1
            await asyncio.sleep(random.uniform(0.5, 2.2))

            try:
                # 1. 声明原生下载事件监听（不阻塞协程）
                download_promise = page.wait_for_event("download", timeout=20000)
                
                # 2. 触发核心动作
                await btn.click()

                # 3. 探针竞速：等待下载触发 vs 等待表单出现
                form_indicator = page.locator('input[placeholder*="First Name" i], input[name*="name" i], button:has-text("SEND"), form')
                form_wait_task = asyncio.create_task(form_indicator.first.wait_for(state="visible", timeout=4000))
                download_wait_task = asyncio.create_task(download_promise)
                
                done, pending = await asyncio.wait(
                    [form_wait_task, download_wait_task], 
                    return_when=asyncio.FIRST_COMPLETED
                )

                # 💡 核心优化：主动取消未完成的挂起任务，避免后台抛出 TimeoutError
                for task in pending:
                    task.cancel()

                download_obj = None

                # 4. 路由分发
                if download_wait_task in done and not download_wait_task.exception():
                    download_obj = download_wait_task.result()
                    self._log("⚡ 按钮点击后直接触发了目标原生流下载！", "info")
                    
                elif form_wait_task in done and not form_wait_task.exception():
                    self._log("🛡️ 检测到注册墙，启动 Faker 伪装机制...", "warning")
                    await self._fill_fake_form(page)
                    
                    send_btn = page.locator('button[type="submit"], button:has-text("SEND"), input[type="submit"]').first
                    if await send_btn.count() > 0:
                        self._log("🚀 伪装完毕，发送表单并等待原生流响应...", "info")
                        # 重新挂载纯净的 expect_download 拦截表单提交
                        try:
                            async with page.expect_download(timeout=20000) as download_info:
                                await send_btn.click()
                            download_obj = await download_info.value
                        except Exception:
                            self._log("⏳ 表单提交后等待原生下载流超时。", "error")
                    else:
                        raise Exception("未找到表单的 SEND 按钮")
                else:
                    self._log("未检测到表单，等待可能的延迟原生流响应...", "info")
                    try:
                        download_obj = await download_wait_task
                    except Exception:
                        self._log("⏳ 等待原生流超时，可能已被阻断或只是普通跳转页面。", "error")

                # 5. 终点防线：原生落盘
                if download_obj:
                    await self._process_and_save_payload(download_obj, source_url)

            except Exception as e:
                self._log(f"❌ 动作执行异常: {str(e)[:150]}", "error")
            finally:
                self.main_downloader.files_active -= 1

    async def _fill_fake_form(self, page):
        """独立抽出的智能表单填充逻辑"""
        try:
            from faker import Faker
            fake = Faker('en_US')
        except ImportError:
            class DummyFake:
                def first_name(self): return "John"
                def last_name(self): return "Doe"
                def city(self): return "New York"
                def company_email(self): return "studio@arch-design.com"
            fake = DummyFake()

        for selector, val in [
            ('input[name*="name" i], input[placeholder*="First Name" i]', fake.first_name()),
            ('input[name*="last" i], input[placeholder*="Last Name" i]', fake.last_name()),
            ('input[name*="city" i], input[placeholder*="City" i], input[placeholder*="Town" i]', fake.city()),
            ('input[type="email"], input[name*="email" i], input[placeholder*="E-mail" i]', fake.company_email())
        ]:
            target_input = page.locator(selector).first
            if await target_input.count() > 0 and await target_input.is_visible():
                await target_input.fill(val)

        checkboxes = page.locator('input[type="checkbox"]')
        for i in range(await checkboxes.count()):
            if await checkboxes.nth(i).is_visible():
                try: await checkboxes.nth(i).check(force=True)
                except: pass
                    
    async def _process_and_save_payload(self, download_obj, source_url):
        """全面接管 Playwright 原生 Download 对象的严格落盘逻辑 (解耦与并发同步版)"""
        suggested_filename = download_obj.suggested_filename
        
        # 绝对过滤防线：坚决阻挡图片等杂音，仅放行目标 PDF 画册文件
        if not suggested_filename.lower().endswith('.pdf') and not suggested_filename.lower().endswith(self.target_ext):
            self._log(f"⚠️ [终极过滤拦截] 抛弃非PDF异常文件: {suggested_filename}", "warning")
            await download_obj.cancel()
            return

        # [修复战役二] 底层流去重：跨越伪装按钮，直击真实的下载源 URL
        actual_url = download_obj.url
        if actual_url in self.main_downloader.downloaded_urls:
            self._log(f"♻️ [流去重] 该底层流已下载过，直接丢弃: {actual_url}", "info")
            await download_obj.cancel()
            return
        self.main_downloader.downloaded_urls.add(actual_url)

        # 根据当前触发下载的源 URL，动态计算专属域名目录
        domain = get_core_domain(source_url)
        target_dir = os.path.join(self.save_dir, f"{self.strategy_prefix}_{domain}")
        os.makedirs(target_dir, exist_ok=True) 
        # 🌟 删除了独立的 error_log_path 静态拼接，拥抱中心化管理
        
        # [同步更新] 引入异步互斥锁与物理占坑机制
        async with self.io_lock:
            final_save_path = os.path.join(target_dir, suggested_filename)
            file_root, file_ext = os.path.splitext(suggested_filename)
            counter = 1
            
            # 冲突处理：如遇同名文件则自动增加数字后缀 (如 _1.pdf)
            while os.path.exists(final_save_path):
                new_filename = f"{file_root}_{counter}{file_ext}"
                final_save_path = os.path.join(target_dir, new_filename)
                counter += 1
                
            # [物理占坑优化] 瞬间创建文件并立即释放系统句柄，防止后续 save_as 发生 I/O 竞态锁死
            try:
                open(final_save_path, 'a').close()
            except Exception as e:
                self._log(f"⚠️ [物理占位失败] {str(e)}", "warning")

        self._log(f"⏳ [动作流落盘准备] 正在写入本地: {os.path.basename(final_save_path)}...", "info")
            
        # 彻底落盘
        try:
            await download_obj.save_as(final_save_path)
            
            # [同步更新] 物理落盘质检 (校验文件存在且体积大于 1KB)
            if os.path.exists(final_save_path) and os.path.getsize(final_save_path) > 1024:
                self._log(f"🎯 [原生流获取成功] 已落盘: {os.path.basename(final_save_path)}", "success")
                self.main_downloader.files_downloaded += 1
            else:
                self._log(f"❌ [落盘异常] 文件损坏或体积为 0，拒绝计数: {suggested_filename}", "error")
                if os.path.exists(final_save_path):
                    os.remove(final_save_path)
                    
        except Exception as e:
            err_msg = f"Action动作落盘失败: {str(e)}"
            self._log(f"❌ {err_msg} | 源 URL: {source_url}", "error")
            
            # 🌟 向上穿透，调用大哥 (main_downloader) 的审计大脑统一落盘
            audit_center = getattr(self.main_downloader, 'audit_center', None)
            if audit_center:
                await audit_center.record_download_failure(domain, source_url, err_msg)