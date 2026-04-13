"""
modules.search — 搜索引擎爬取策略子包。

对外暴露 SearchCrawlStrategy；模块内部辅助函数（如 _is_external_result）
为实现细节，不在此层重导出。
"""

from src.modules.search.strategy import SearchCrawlStrategy

__all__ = [
    "SearchCrawlStrategy",
]
