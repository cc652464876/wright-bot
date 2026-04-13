"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 页面链接解析与文件名清洗工具类
@Pattern : Utility / Pure Function（无副作用的纯数据转换）
@Description:
    SiteDataParser 是一个无状态的纯工具类，负责将从 DOM 或网络响应中
    捕获到的原始 href / URL 字符串转换为可直接下载的标准化任务对象。
    核心职责：
    1. URL 标准化：补全相对路径、修复协议头（urljoin）。
    2. 扩展名过滤：快速预检目标后缀（如 .pdf / .jpg），过滤无关链接。
    3. 安全文件名生成：从 URL 提取文件名，移除 Windows / macOS 非法字符，
       过短或无扩展名时用 MD5 哈希兜底。
    4. 核心域名提取：从任意 URL 提取 domain 字段，供 audit_center 分域存储。
    Pattern: Pure Function —— 所有方法仅接受输入、返回输出，无任何 I/O 副作用。
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

# 操作系统文件名非法字符正则（Windows + macOS 并集）
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

# 最短合法文件名长度（含扩展名）；短于此阈值时用 MD5 兜底
_MIN_FILENAME_LEN = 5

# 禁止透传给下载器的 URL 伪协议前缀
_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "#", "void(")


class SiteDataParser:
    """
    页面链接解析与文件元数据清洗器（无状态工具类）。

    对外暴露两个主要接口：
    - parse_link()      : 单条 href → 任务对象字典（或 None）。
    - get_core_domain() : URL → 纯净域名字符串。
    其余为私有工具方法，供上述接口内部调用。

    Pattern: Utility Class —— 无状态、无副作用，可安全地在多协程间共享同一实例。
    """

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def parse_link(
        self,
        base_url: str,
        raw_href: str,
        target_ext: str = ".pdf",
    ) -> Optional[Dict[str, str]]:
        """
        将页面上抓取到的原始 href 转换为可下载的任务对象字典。

        处理流程：
        1. 快速扩展名预检（raw_href 不含 target_ext 则立即返回 None）。
        2. 通过 urljoin 将相对路径补全为绝对 URL。
        3. 调用 _generate_safe_filename() 生成安全文件名。

        Args:
            base_url   : 当前页面的绝对 URL（用于相对路径补全）。
            raw_href   : 从 DOM a[href] 提取的原始 href 字符串。
            target_ext : 目标文件扩展名（含点号，如 '.pdf'）。
        Returns:
            包含 source_page / file_url / file_name 的字典；
            不满足条件时返回 None。
        """
        if not raw_href:
            return None

        # Step 1: 快速预检——若原始 href 完全不含目标扩展名则立即放弃
        # 大小写不敏感（允许 .PDF / .Pdf 等变体）
        if target_ext.lower() not in raw_href.lower():
            return None

        # Step 2: 绝对 URL 补全与合法性校验
        abs_url = self.normalize_url(base_url, raw_href)
        if abs_url is None:
            return None

        # 二次确认扩展名（path 部分大小写不敏感）
        path_lower = urlparse(abs_url).path.lower()
        if not path_lower.endswith(target_ext.lower()):
            return None

        # Step 3: 生成操作系统安全文件名
        filename = self._generate_safe_filename(abs_url, target_ext)

        return {
            "source_page": base_url,
            "file_url":    abs_url,
            "file_name":   filename,
        }

    @staticmethod
    def get_core_domain(url: str) -> str:
        """
        从任意 URL 中提取纯净核心域名（不含协议头和路径）。
        用于 audit_center 的分域存储键名 和 downloader 的目录分组。

        Args:
            url: 任意格式的 URL 字符串。
        Returns:
            纯净域名字符串（如 'example.com' 或 'sub.example.co.uk'）；
            解析失败时返回 'unknown_domain'。
        """
        if not url:
            return "unknown_domain"
        try:
            netloc = urlparse(url).netloc
            # 剥离 user:password@ 前缀和 :port 后缀
            netloc = netloc.split("@")[-1].split(":")[0].strip()
            return netloc if netloc else "unknown_domain"
        except Exception:
            return "unknown_domain"

    def normalize_url(self, base_url: str, raw_href: str) -> Optional[str]:
        """
        将 raw_href 补全为绝对 URL 并做基础合法性校验。
        剔除 javascript: 伪协议、mailto:、tel: 以及空字符串。

        Args:
            base_url : 当前页面绝对 URL。
            raw_href : 原始 href 值。
        Returns:
            标准化的绝对 URL；不合法时返回 None。
        """
        if not raw_href or not raw_href.strip():
            return None

        stripped = raw_href.strip()

        # 快速剔除禁止协议
        lower = stripped.lower()
        if any(lower.startswith(s) for s in _SKIP_SCHEMES):
            return None

        try:
            abs_url = urljoin(base_url, stripped)
        except Exception:
            return None

        parsed = urlparse(abs_url)
        # 必须有 scheme 和 netloc 才是合法 HTTP(S) URL
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None

        return abs_url

    def filter_links_by_ext(
        self,
        links: List[str],
        target_ext: str,
    ) -> List[str]:
        """
        从 URL 列表中筛选包含目标扩展名的条目（大小写不敏感）。

        Args:
            links     : 待过滤的 URL 字符串列表。
            target_ext: 目标扩展名（含点号，如 '.pdf'）。
        Returns:
            过滤后仅含目标扩展名 URL 的列表。
        """
        ext_lower = target_ext.lower()
        return [
            url for url in links
            if ext_lower in url.lower()
        ]

    # ------------------------------------------------------------------
    # 私有：文件名生成
    # ------------------------------------------------------------------

    def _generate_safe_filename(self, file_url: str, extension: str) -> str:
        r"""
        从 file_url 中提取文件名，并执行操作系统安全性清洗。

        清洗规则：
        1. 取 URL path 的最后一段（去除 query string）。
        2. 若文件名过短（< 5 字符）或不以 extension 结尾，
           则用 MD5(file_url)[:8] 生成 'doc_{hash}{ext}' 兜底名。
        3. 用正则移除 Windows / macOS 非法字符（\ / : * ? " < > |）。
        4. 在文件名前添加下划线前缀（与原 core/site_parser.py 保持兼容）。

        Args:
            file_url : 目标文件的完整 URL。
            extension: 目标扩展名（含点号）。
        Returns:
            操作系统安全的文件名字符串（如 '_research_paper.pdf'）。
        """
        try:
            path = urlparse(file_url).path
            # 取路径最后一段，剔除空段（如尾部斜杠导致的空字符串）
            raw_name = path.rstrip("/").split("/")[-1] if "/" in path else path
            # 去掉 query string 残留（path 里通常已无 query，但双重保险）
            raw_name = raw_name.split("?")[0]
        except Exception:
            raw_name = ""

        ext_lower = extension.lower()

        # 兜底条件：文件名过短或不以目标扩展名结尾
        if len(raw_name) < _MIN_FILENAME_LEN or not raw_name.lower().endswith(ext_lower):
            url_hash = hashlib.md5(file_url.encode("utf-8")).hexdigest()[:8]
            raw_name = f"doc_{url_hash}{extension}"

        # 清洗非法字符
        safe_name = self._sanitize_filename(raw_name)

        # 添加下划线前缀（保持与 core/ 版本兼容）
        if not safe_name.startswith("_"):
            safe_name = f"_{safe_name}"

        return safe_name

    @staticmethod
    def _sanitize_filename(raw_name: str) -> str:
        """
        移除文件名中的操作系统非法字符，保留字母、数字、空格、点号和连字符。

        Args:
            raw_name: 原始文件名字符串。
        Returns:
            清洗后的安全文件名字符串。
        """
        # 替换所有非法字符为下划线
        sanitized = _ILLEGAL_FILENAME_CHARS.sub("_", raw_name)
        # 压缩连续下划线
        sanitized = re.sub(r"_+", "_", sanitized)
        # 移除开头和结尾的空白/下划线（文件名不应以这些字符开始/结束）
        sanitized = sanitized.strip("_ ")
        return sanitized if sanitized else "unnamed"
