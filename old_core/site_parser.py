# -*- coding: utf-8 -*-
# ==============================================================================
# 模块名称: site_parser.py
# 功能描述: 站点文件数据清洗器 (分析师)
# 核心职责:
#   1. 专职负责网站 DOM 结构中的 URL 标准化 (补全 http, 处理相对路径)。
#   2. 智能文件名生成 (从 URL 提取, 哈希兜底)。
#   3. 文件名安全性清洗 (移除 Windows 非法字符)。
#   4. 链接过滤 (判断后缀名是否符合要求)。
# 输入: 原始 href 字符串, 当前页面 URL
# 输出: 清洗后的字典 {'source_page': '...', 'file_url': '...', 'file_name': '...'} 或 None
# ==============================================================================

import hashlib
import re
from urllib.parse import urljoin

class SiteDataParser:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback

    def _log(self, message, level="info"):
        if self.log_callback:
            self.log_callback(f"[SiteParser] {message}", level)

    def parse_link(self, base_url, raw_href, target_ext=".pdf"):
        """
        核心方法：将站点上抓取到的原始 href 转换为可下载的任务对象
        """
        if not raw_href:
            return None

        # 1. 扩展名预检 (快速过滤)
        if target_ext.lower() not in raw_href.lower():
            return None

        # 2. URL 拼接与标准化
        full_url = urljoin(base_url, raw_href)

        # 3. 文件名提取与清洗
        safe_name = self._generate_safe_filename(full_url, target_ext)

        return {
            "source_page": base_url,
            "file_url": full_url,
            "file_name": safe_name
        }

    def _generate_safe_filename(self, file_url, extension):
        """
        内部工具：生成操作系统安全的文件名
        """
        try:
            raw_name = file_url.split('/')[-1].split('?')[0]
            
            if len(raw_name) < 5 or not raw_name.lower().endswith(extension):
                short_hash = hashlib.md5(file_url.encode('utf-8')).hexdigest()[:8]
                raw_name = f"doc_{short_hash}{extension}"

            safe_name = re.sub(r'[^\w\s\.\-]', '', raw_name).strip()
            
            if not safe_name.lower().endswith(extension):
                safe_name += extension
                
            return f"_{safe_name}"

        except Exception:
            fallback_hash = hashlib.md5(file_url.encode('utf-8')).hexdigest()[:10]
            return f"_unknown_{fallback_hash}{extension}"