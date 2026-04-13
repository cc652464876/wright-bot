# -*- coding: utf-8 -*-

# ==============================================================================
# 模块名称: site_utils.py
# 功能描述: 站点采集线的纯工具函数集合
# 核心职责:
#   1. 提供不依赖于爬虫状态的纯逻辑处理
#   2. 处理字符串映射、URL 解析、本地文件读写等辅助工作
# ==============================================================================

import os
import json
from urllib.parse import urlparse

def get_strategy_prefix(config_dict):
    """将中文策略名转换为英文前缀"""
    # 假设前端传来的策略名在 task_info 里的 strategy 字段
    strategy_name = config_dict.get("task_info", {}).get("strategy", "全站历遍") 
    mapping = {
        "全站历遍": "site",
        "地图采集": "sitemap",
        "Google搜索": "google",
        "Bing搜索": "bing",
        "DuckDuckGo搜索": "duckduckgo"
    }
    return mapping.get(strategy_name, "site") # 默认 fallback 为 site

def get_core_domain(url):
    """从 URL 中提取核心域名，例如 www.poliform.it -> poliform"""
    try:
        netloc = urlparse(url).netloc.split(':')[0]
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        parts = netloc.split('.')
        # 如果是二级域名如 uk.minotti.com，取 minotti；如果是 poliform.it 取 poliform
        if len(parts) > 2:
            return parts[-2]
        return parts[0]
    except Exception:
        return "unknown"

def backup_config(config_dict, save_dir):
    """备份任务配置字典到下载目录"""
    try:
        path = os.path.join(save_dir, "config.backup.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=4)
    except:
        pass