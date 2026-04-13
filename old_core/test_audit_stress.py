# -*- coding: utf-8 -*-
import os
import asyncio
import random
import shutil
import json
from site_audit_center import SiteAuditCenter

# ==========================================
# ⚙️ 压测配置
# ==========================================
TEST_DIR = "./mock_workspace"
TEST_DOMAIN = "stress-test.mono-design.com"
CONCURRENCY_LEVEL = 1000  # 瞬间并发量（模拟上千个异步动作同时触发）

async def mock_network_probe(audit_manager, index):
    """模拟网络探针瞬间截获大量 PDF 并发落盘"""
    source_page = f"https://{TEST_DOMAIN}/page_{index % 10}" # 模拟 10 个页面，每个页面触发 100 次探针
    file_url = f"https://{TEST_DOMAIN}/files/doc_{index}.pdf"
    await audit_manager.record_result_batch(
        domain=TEST_DOMAIN,
        source_page=source_page,
        new_file_urls=[file_url]
    )

async def mock_dom_interactor(audit_manager, index):
    """模拟超高频的 DOM 点击交互"""
    interaction = {
        "action": "click",
        "selector": f"#btn_{index}",
        "timestamp": f"2026-03-21T10:00:{index%60:02d}"
    }
    await audit_manager.record_interaction(domain=TEST_DOMAIN, interaction_data=interaction)

async def mock_page_scanner(audit_manager, index):
    """模拟页面扫描的成功与失败交替写入"""
    url = f"https://{TEST_DOMAIN}/product_view_{index}.html"
    if random.random() > 0.2:
        await audit_manager.record_page_success(domain=TEST_DOMAIN, url=url)
    else:
        await audit_manager.record_page_failure(domain=TEST_DOMAIN, url=url, status_code=500, error_msg="Mock Timeout")

async def main():
    print("🧹 [压测准备] 清理旧的测试数据...")
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
        
    print(f"🚀 [压测启动] 正在拉起 {CONCURRENCY_LEVEL * 3} 个并发请求...")
    audit_manager = SiteAuditCenter(base_save_dir=TEST_DIR, strategy_prefix="stress")
    
    # 构建密集的并发任务流
    tasks = []
    for i in range(CONCURRENCY_LEVEL):
        tasks.append(asyncio.create_task(mock_network_probe(audit_manager, i)))
        tasks.append(asyncio.create_task(mock_dom_interactor(audit_manager, i)))
        tasks.append(asyncio.create_task(mock_page_scanner(audit_manager, i)))

    # 瞬间全部释放执行
    await asyncio.gather(*tasks)
    
    # 触发审计中心的收尾
    await audit_manager.export_final_reports()
    
    # ==========================================
    # 📊 质检与数据核对 (自动校验是否丢数据)
    # ==========================================
    print("\n" + "="*40)
    print("✅ [压测完成] 开始进行磁盘数据核对...")
    workspace = audit_manager._get_workspace(TEST_DOMAIN)
    
    # 1. 核对 manifest.json (预期: 10个 source_page, 总计 1000 个去重 file_urls)
    manifest_path = os.path.join(workspace, "manifest.json")
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest_data = json.load(f)
    total_files = sum(len(page['file_urls']) for page in manifest_data)
    print(f"📄 Manifest 聚合嵌套校验: 找到 {len(manifest_data)} 个母页面, 共计 {total_files} 个 PDF 链接 (预期 {CONCURRENCY_LEVEL}) -> {'[通过]' if total_files == CONCURRENCY_LEVEL else '[异常]丢数据了!'}")

    # 2. 核对 interactions.json (预期: 1000 条记录)
    interactions_path = os.path.join(workspace, "interactions.json")
    with open(interactions_path, 'r', encoding='utf-8') as f:
        interactions_data = json.load(f)
    print(f"🖱️ Interactions 交互记录校验: 成功落盘 {len(interactions_data)} 条 (预期 {CONCURRENCY_LEVEL}) -> {'[通过]' if len(interactions_data) == CONCURRENCY_LEVEL else '[异常]丢数据了!'}")

    # 3. 核对 scanned_urls.jsonl (预期: 1000 条记录)
    scanned_path = os.path.join(workspace, "scanned_urls.jsonl")
    scanned_count = sum(1 for line in open(scanned_path, 'r', encoding='utf-8') if line.strip())
    print(f"🌐 Scanned URLs JSONL 极速追加校验: 成功落盘 {scanned_count} 条 (预期 {CONCURRENCY_LEVEL}) -> {'[通过]' if scanned_count == CONCURRENCY_LEVEL else '[异常]丢数据了!'}")
    print("="*40 + "\n")

if __name__ == "__main__":
    asyncio.run(main())