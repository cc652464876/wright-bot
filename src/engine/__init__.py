"""
engine — 浏览器引擎根包。

对外暴露爬虫引擎工厂、状态机及其状态枚举；底层 Browser 抽象基类
（AbstractBrowserBackend、BrowserContextManager、PagePool）属于引擎内部
实现细节，由具体 backend 子模块直接引用，不在此层对外重导出。
"""

from src.engine.browser_factory import BrowserFactory
from src.engine.crawlee_engine import CrawleeEngineFactory
from src.engine.state_manager import CrawlerState, CrawlerStateManager

__all__ = [
    "BrowserFactory",
    "CrawleeEngineFactory",
    "CrawlerStateManager",
    "CrawlerState",
]
