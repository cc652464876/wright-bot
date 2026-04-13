"""
modules.site.handlers — 站点请求处理器子包。

导出全部六个 Handler 实现，供 SiteCrawlStrategy 按职责组合装配。
每个 Handler 负责一条独立的关注点：路由决策（Strategist）、
动作执行（ActionHandler / ActionDownloader）、资源下载（Downloader）、
页面交互（Interactor）、网络嗅探（NetSniffer）。
"""

from src.modules.site.handlers.action import ActionHandler
from src.modules.site.handlers.action_downloader import ActionDownloader
from src.modules.site.handlers.downloader import Downloader
from src.modules.site.handlers.interactor import Interactor
from src.modules.site.handlers.net_sniffer import NetSniffer
from src.modules.site.handlers.strategist import Strategist

__all__ = [
    "Strategist",
    "ActionHandler",
    "ActionDownloader",
    "Downloader",
    "Interactor",
    "NetSniffer",
]
