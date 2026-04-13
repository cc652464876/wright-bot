# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: site_request_handler_downloader.py
# 功能描述: 站点采集线 - 独立下载器与并发控制器
# 核心职责:
#   1. 并发节流阀管理 (Semaphore)
#   2. 状态统计 (UI 数据源)
#   3. Chromium 纯原生流式落盘 (链接触发与按钮触发)
#   4. 严格的文件类型过滤拦截
# ==============================================================================

import os
import asyncio
import random
import urllib.parse  # 🌟 [优化升级] 引入 URL 解析库，用于剥离动态参数
import aiofiles      # TODO(Fix3): 替换同步 open/write，避免阻塞 asyncio 事件循环
from core.site_utils import get_core_domain  # 🌟 引入核心域名提取工具

class SiteRequestHandlerDownloader:
    # 🌟 新增 audit_center=None 参数
    def __init__(self, config_dict, log_callback, check_running_func, save_dir, strategy_prefix, audit_center=None):
        """
        初始化独立下载核心，接管并发状态，剥离本地报错 I/O
        """
        self.config_dict = config_dict
        self.log_callback = log_callback
        self.is_running = check_running_func
        
        self.save_dir = save_dir
        self.strategy_prefix = strategy_prefix
        self.audit_center = audit_center  # 🌟 挂载全能审计大脑
        
        # UI 状态接管
        self.files_found = 0
        self.files_downloaded = 0
        self.files_active = 0
        
        # 核心：并发节流阀，保护本地 I/O 与目标服务器（设定为 5 并发）
        self.semaphore = asyncio.Semaphore(5)
        
        # [新增修复] 增加物理文件读写排他锁，防止高并发下的重名文件竞态碰撞
        self.io_lock = asyncio.Lock()
        
        # [修复战役二] 引入全局大脑：内存级 URL 去重集合，防止跨页面重复下载同一文件
        self.downloaded_urls = set()
        
        # 基础路径与策略配置
        self.target_ext = f".{config_dict.get('strategy_settings', {}).get('file_type', 'pdf').lower()}"

    def get_stats(self):
        return {
            "files_found": self.files_found,
            "files_downloaded": self.files_downloaded,
            "files_active": self.files_active
        }   

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[下载核心] {message}", level)
        else:
            print(f"[{level.upper()}] [下载核心] {message}")

    async def native_download_task(self, page, url, save_path, item):
        """后台异步原生下载：受 Semaphore 保护的 Chromium 原生底层网络请求拦截落盘"""
        if not self.is_running():
            return

        # TODO(Fix4): 礼貌延迟应在获取 semaphore 槽位之前执行，
        #   否则线程槽位被 sleep 占死，其他下载任务无法进入。
        #   正确位置：await asyncio.sleep(random.uniform(0.5, 2.2))  ← 移到此行
        await asyncio.sleep(random.uniform(0.5, 2.2))  # ← 待移至 semaphore 外

        async with self.semaphore:
            self.files_active += 1
            
            try:
                self._log(f"🚀 [原生上下文直连] 准备获取流数据: {url}", "info")
                
                # 🌟 [优化升级] 设置拦截关卡：剥离 Query 参数的严格去重
                # 解决带有随机时间戳或 Session 尾巴 (?t=123) 导致防重失效的问题
                parsed_url = urllib.parse.urlparse(url)
                clean_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
                
                if clean_url in self.downloaded_urls:
                    self._log(f"♻️ [全局去重] 核心文件已下载，跳过动态直连: {clean_url}", "info")
                    return
                # 存入集合的也是干净的 URL
                self.downloaded_urls.add(clean_url)
                
                # 💡 核心修复：动用 Playwright 共享上下文的底层请求 API
                # 100% 继承当前防爬状态，彻底绕过 DOM 和 PDF 渲染器的抢夺
                response = await page.context.request.get(
                    url,
                    timeout=45000,
                    headers={
                        "Referer": page.url, # 继承当前页面的防盗链上下文
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,application/pdf,*/*;q=0.8"
                    }
                )

                if not response.ok:
                    raise Exception(f"HTTP 状态异常: {response.status}")

                # 提取头部信息，进行严格类型拦截
                content_type = response.headers.get("content-type", "").lower()
                url_lower = url.lower()
                
                if any(img_ext in url_lower for img_ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']) or "image/" in content_type:
                    self._log(f"⚠️ [类型拦截] 发现图片链接，拒绝落盘: {url}", "warning")
                    return
                
                # 从 URL 或 Header 中提取文件名 (兜底逻辑)
                suggested_filename = url.split('/')[-1].split('?')[0]
                if not suggested_filename.endswith(self.target_ext):
                    suggested_filename = f"downloaded_document_{random.randint(1000,9999)}{self.target_ext}"

                # 提取目标的父目录，准备物理锁落盘
                target_dir = os.path.dirname(save_path)
                
                async with self.io_lock:
                    final_save_path = os.path.join(target_dir, suggested_filename)
                    file_root, file_ext = os.path.splitext(suggested_filename)
                    counter = 1
                    
                    while os.path.exists(final_save_path):
                        new_filename = f"{file_root}_{counter}{file_ext}"
                        final_save_path = os.path.join(target_dir, new_filename)
                        counter += 1
                        
                    try:
                        # TODO(Fix3): async with aiofiles.open(final_save_path, 'wb'): pass
                        with open(final_save_path, 'wb') as placeholder:
                            pass
                    except Exception as e:
                        self._log(f"⚠️ [物理占位失败] {str(e)}", "warning")

                self._log(f"⏳ [落盘准备] 正在写入本地: {os.path.basename(final_save_path)}...", "info")
                
                # 💡 直接读取二进制 Response Buffer 并写入物理文件
                file_bytes = await response.body()

                # TODO(Fix3): async with aiofiles.open(final_save_path, 'wb') as f:
                #                 await f.write(file_bytes)
                with open(final_save_path, 'wb') as f:
                    f.write(file_bytes)
                
                # 落盘质检
                if os.path.exists(final_save_path) and os.path.getsize(final_save_path) > 1024:
                    self.files_downloaded += 1
                    self._log(f"📥 [原生流落盘成功] 已落盘: {os.path.basename(final_save_path)}", "success")
                else:
                    self._log(f"❌ [落盘异常] 文件损坏或体积过小，拒绝计数: {suggested_filename}", "error")
                    if os.path.exists(final_save_path):
                        os.remove(final_save_path)
                        
            except Exception as e:
                domain = get_core_domain(url)
                if isinstance(e, asyncio.TimeoutError) or "timeout" in str(e).lower():
                    err_msg = "底层请求超时"
                    self._log(f"❌ [下载挂起] 服务器无响应，已释放并发槽: {url}", "error")
                else:
                    err_msg = f"未知请求异常: {str(e)}"
                    self._log(f"❌ [请求异常] {str(e)[:100]} | {url}", "error")
                
                # 🌟 调用审计大脑统一落盘，剥离本地脆皮 I/O
                audit_center = getattr(self, 'audit_center', None)
                if audit_center:
                    await audit_center.record_download_failure(domain, url, err_msg)
            finally:
                self.files_active -= 1