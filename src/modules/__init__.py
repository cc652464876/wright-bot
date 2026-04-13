"""
modules — 业务策略根包。

对外仅暴露所有具体策略的公共抽象基类 BaseCrawlStrategy，
具体策略实现由各子包（site/、search/）各自声明。
"""

from src.modules.base_strategy import BaseCrawlStrategy

__all__ = [
    "BaseCrawlStrategy",
]
