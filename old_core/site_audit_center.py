# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: site_audit_center.py
# 功能描述: PrismPDF 审计与日志系统 (全能核心大脑)
# 核心职责:
#   1. 完全接管并发状态字典与多线程锁。
#   2. 黑盒化所有 os.path 路径拼接。
#   3. 提供原子化 JSON 写入与防占用 TXT 崩溃保护。
#   4. 实现数据降噪聚合。
# ==============================================================================

import os
import json
import asyncio
import aiofiles  # 💡 新增：异步文件操作库
from datetime import datetime
from collections import defaultdict # 💡 新增：用于按域名自动分配锁

class SiteAuditCenter:
    def __init__(self, base_save_dir: str, strategy_prefix: str = "crawl"):
        """
        初始化大一统审计中心 (高并发重构版)
        """
        self.base_save_dir = base_save_dir
        self.strategy_prefix = strategy_prefix

        # 1. 状态全面私有化接管 (按域名隔离)
        self._domain_manifests = {}      # 内部改为 Dict，实现 O(1) 查找
        self._domain_scanned_urls_set = {} 
        self._domain_interactions = {}   

        # 2. 内部异步锁接管 (💡 重构为按域名细粒度锁，消除全局排队瓶颈)
        self._manifest_locks = defaultdict(asyncio.Lock)
        self._scanned_urls_locks = defaultdict(asyncio.Lock)
        self._interactions_locks = defaultdict(asyncio.Lock)
        self._txt_log_locks = defaultdict(asyncio.Lock)
        
        # 💡 新增：专门用于防并发初始化的锁，防止多个Hook同时读盘覆盖
        self._init_locks = defaultdict(asyncio.Lock)
        
        # 3. 性能优化：记录已创建的目录，防抖避免高频磁盘 I/O
        self._initialized_workspaces = set()
        
        # 💡 新增：记录完全初始化完毕的域名，替代脆弱的字典存在性双重校验
        self._fully_initialized_domains = set()
        
        # 💡 新增：全局去重文件总数计数器 (用于 O(1) 极速查询，避免 UI 轮询时耗费 CPU 遍历庞大字典)
        self._total_scraped_count = 0

    def get_total_scraped_count(self) -> int:
        """
        对外暴露的安全统计 API：O(1) 极速返回当前所有域名下已抓取的去重文件总数。
        """
        return self._total_scraped_count

    # ==========================================
    # 🛠️ 底层机制 (路径黑盒与安全 I/O)
    # ==========================================
    
    def _get_workspace(self, domain: str) -> str:
        """路径黑盒化：外部不再允许随意 os.path.join (带内存级防抖缓存)"""
        workspace = os.path.join(self.base_save_dir, f"{self.strategy_prefix}_{domain}")
        
        # 💡 只有内存中没有记录过，才去触碰磁盘创建文件夹
        if workspace not in self._initialized_workspaces:
            os.makedirs(workspace, exist_ok=True)
            self._initialized_workspaces.add(workspace)
            
        return workspace

    async def _write_json_atomic_unlocked(self, file_path: str, data: list):
        """核心：原子写入 JSON (无锁版，调用前必须由外层业务加锁)"""
        tmp_path = file_path + ".tmp"
        try:
            # 💡 优化：将纯 CPU 密集的 dumps 操作推入线程池执行，防止数据量大时卡死整个 Asyncio 事件循环
            json_str = await asyncio.to_thread(json.dumps, data, ensure_ascii=False, indent=2)
            
            async with aiofiles.open(tmp_path, 'w', encoding='utf-8') as f:
                await f.write(json_str)
            
            # os.replace 是极速的元数据修改，不会造成严重阻塞，可保留
            os.replace(tmp_path, file_path) 
        except Exception as e:
            print(f"⚠️ [AuditCenter] 原子写入 JSON 失败 ({os.path.basename(file_path)}): {e}")

    async def _append_txt_safe(self, file_path: str, message: str, domain: str):
        """核心：安全追加 TXT，防 Excel/记事本占用导致的爬虫崩溃"""
        # 💡 使用域名细粒度锁
        async with self._txt_log_locks[domain]:
            try:
                # 💡 替换为 aiofiles
                async with aiofiles.open(file_path, 'a', encoding='utf-8') as f:
                    await f.write(message + "\n")
            except PermissionError:
                print(f"⚠️ [AuditCenter] 警告: 日志文件被占用，无法写入，已静默跳过 ({os.path.basename(file_path)})")
            except Exception as e:
                print(f"⚠️ [AuditCenter] 警告: 写入日志异常 ({os.path.basename(file_path)}): {e}")
                
    async def _append_jsonl_unlocked(self, file_path: str, data_dict: dict):
        """核心：单行追加 JSONL (无锁版，外层须加锁，内置防占用保护)"""
        try:
            # 💡 替换为 aiofiles
            async with aiofiles.open(file_path, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(data_dict, ensure_ascii=False) + "\n")
        except PermissionError:
            print(f"⚠️ [AuditCenter] 警告: JSONL文件被占用，无法写入，已静默跳过 ({os.path.basename(file_path)})")
        except Exception as e:
            print(f"⚠️ [AuditCenter] 追加写入 JSONL 异常 ({os.path.basename(file_path)}): {e}")

    async def _init_domain_state(self, domain: str):
        """惰性初始化域名的内存状态 + 🌟 异步断点续传历史加载 (并发安全版)"""
        async with self._init_locks[domain]:
            # 💡 优化：使用专属集合进行双重检查锁定
            if domain in self._fully_initialized_domains:
                return
                
            workspace = self._get_workspace(domain)

            # ==========================================
            # 1. 加载 Manifest 历史数据
            # ==========================================
            if domain not in self._domain_manifests: 
                self._domain_manifests[domain] = {}
                manifest_path = os.path.join(workspace, "manifest.json")
                if os.path.exists(manifest_path):
                    try:
                        async with aiofiles.open(manifest_path, 'r', encoding='utf-8') as f:
                            content = await f.read()
                            if content:
                                old_data = json.loads(content)
                                for item in old_data:
                                    page = item.get("source_page")
                                    urls = item.get("file_urls", [])
                                    if page:
                                        urls_set = set(urls)
                                        self._domain_manifests[domain][page] = urls_set
                                        # ✅ 修复缩进：确保在循环内逐个累加，且不会触发 UnboundLocalError
                                        self._total_scraped_count += len(urls_set) 
                                print(f"🔄 [AuditCenter] 唤醒 Manifest 资产: 恢复 {len(old_data)} 个父页面记录")
                    except Exception as e:
                        backup_path = manifest_path + ".corrupted"
                        os.replace(manifest_path, backup_path)
                        print(f"⚠️ [AuditCenter] 恢复 manifest 失败，已备份避免覆写: {e}")

            # ==========================================
            # 2. 加载 Interactions
            # ==========================================
            if domain not in self._domain_interactions: 
                self._domain_interactions[domain] = []
                interactions_path = os.path.join(workspace, "interactions.json")
                if os.path.exists(interactions_path):
                    try:
                        async with aiofiles.open(interactions_path, 'r', encoding='utf-8') as f:
                            content = await f.read()
                            if content:
                                self._domain_interactions[domain] = json.loads(content)
                    except Exception as e:
                        backup_path = interactions_path + ".corrupted"
                        os.replace(interactions_path, backup_path)
                        print(f"⚠️ [AuditCenter] 恢复 interactions 失败，已备份避免覆写: {e}")

            # ==========================================
            # 3. 加载 Scanned URLs (保持原有逻辑，但已被包含在锁内)
            # ==========================================
            if domain not in self._domain_scanned_urls_set:
                self._domain_scanned_urls_set[domain] = set()
                jsonl_path = os.path.join(workspace, "scanned_urls.jsonl")
                json_path = os.path.join(workspace, "scanned_urls.json") 
                loaded_count = 0
                
                if os.path.exists(json_path):
                    try:
                        async with aiofiles.open(json_path, 'r', encoding='utf-8') as f:
                            content = await f.read()
                            if content:
                                for item in json.loads(content):
                                    self._domain_scanned_urls_set[domain].add(item.get("url"))
                                    loaded_count += 1
                    except Exception: pass
                    
                if os.path.exists(jsonl_path):
                    try:
                        async with aiofiles.open(jsonl_path, 'r', encoding='utf-8') as f:
                            async for line in f:
                                if line.strip():
                                    self._domain_scanned_urls_set[domain].add(json.loads(line).get("url"))
                                    loaded_count += 1
                    except Exception: pass
                    
                if loaded_count > 0:
                    print(f"🔄 [AuditCenter] 触发断点续传: 成功从本地唤醒 {loaded_count} 条 URL 记录")

            # ✅ 修复竞态条件：在锁内完成所有初始化后，打上完结标记
            self._fully_initialized_domains.add(domain)

    # ==========================================
    # 🎯 业务标准 API Hooks (对外仅暴露这几个入口)
    # ==========================================

    async def record_page_success(self, domain: str, url: str, status_code: int = 200):
        """Hook 1: 记录成功扫描的页面"""
        record = {"url": url, "status": "success", "code": status_code, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        
        # 💡 使用域名专属锁
        async with self._scanned_urls_locks[domain]:
            await self._init_domain_state(domain) # 💡 加上 await
            if url not in self._domain_scanned_urls_set[domain]:
                self._domain_scanned_urls_set[domain].add(url)
                file_path = os.path.join(self._get_workspace(domain), "scanned_urls.jsonl")
                await self._append_jsonl_unlocked(file_path, record)

    async def record_page_failure(self, domain: str, url: str, status_code: int, error_msg: str):
        """Hook 2: 记录页面解析或请求彻底失败"""
        record = {"url": url, "status": "failed", "code": status_code, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        
        async with self._scanned_urls_locks[domain]:
            await self._init_domain_state(domain)
            if url not in self._domain_scanned_urls_set[domain]:
                self._domain_scanned_urls_set[domain].add(url)
                workspace = self._get_workspace(domain)
                jsonl_path = os.path.join(workspace, "scanned_urls.jsonl")
                await self._append_jsonl_unlocked(jsonl_path, record)

        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{time_str}] URL: {url} | Status: {status_code} | Error: {error_msg}"
        txt_path = os.path.join(self._get_workspace(domain), "scan_errors_log.txt")
        await self._append_txt_safe(txt_path, log_msg, domain) # 💡 传入 domain 获取锁

    async def record_download_failure(self, domain: str, url: str, error_msg: str):
        """Hook 3: 接管下载器的文件损毁/超时报错逻辑"""
        txt_path = os.path.join(self._get_workspace(domain), "download_errors_log.txt")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{time_str}] File: {url} | Error: {error_msg}"
        await self._append_txt_safe(txt_path, log_msg, domain) # 💡 传入 domain 获取锁

    async def record_result_batch(self, domain: str, source_page: str, new_file_urls: list):
        """Hook 4: 记录最终成果 (🌟 全局高可用去重版)"""
        if not new_file_urls:
            return

        async with self._manifest_locks[domain]:
            await self._init_domain_state(domain)
            manifest_dict = self._domain_manifests[domain]

            # 💡 核心修复：实现全局域名级去重
            # 利用 Python 底层 set.union 极速合并当前域名下所有页面已有的 URL
            all_existing_urls = set().union(*manifest_dict.values()) if manifest_dict else set()
            
            # 过滤出真正全新的 URL
            truly_new_urls = [url for url in new_file_urls if url not in all_existing_urls]
            
            # 如果全部都是重复的，直接返回，避免触发无意义的磁盘 I/O 写盘
            if not truly_new_urls:
                return

            if source_page not in manifest_dict:
                manifest_dict[source_page] = set()
                
            # 只把真正全新的 URL 录入到当前 source_page 节点下
            manifest_dict[source_page].update(truly_new_urls)

            # 准备落盘：将 {source_page: set} 转换为 [{source_page: url, file_urls: []}] 格式
            dump_data = [
                {"source_page": page, "file_urls": list(urls)} 
                for page, urls in manifest_dict.items()
            ]

            workspace = self._get_workspace(domain)
            file_path = os.path.join(workspace, "manifest.json")
            await self._write_json_atomic_unlocked(file_path, dump_data)

    async def record_interaction(self, domain: str, interaction_data: dict):
        """Hook 5: 记录具体的 DOM 交互行为"""
        async with self._interactions_locks[domain]:
            await self._init_domain_state(domain) 
            self._domain_interactions[domain].append(interaction_data)
            workspace = self._get_workspace(domain)
            file_path = os.path.join(workspace, "interactions.json")
            await self._write_json_atomic_unlocked(file_path, self._domain_interactions[domain])
            
        action_name = interaction_data.get('action', 'unknown')
        print(f"📝 [AuditCenter] 记录 DOM 交互落盘: {action_name} -> {domain}")


    async def export_final_reports(self):
        """主引擎收尾调用的清理 API"""
        print("[AuditCenter] 审计中心数据流已安全落盘，任务完结。")