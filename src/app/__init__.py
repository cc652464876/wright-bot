"""
app — 应用层（第五层 · 进程入口与 UI 桥接）。

对外暴露 pywebview 应用工厂函数 create_app、核心调度器、
以及站点与网络两个监控服务；内部实现类（PrismAPI、CrawlerRunner、
SiteRunner、SearchRunner 等）不在此层重导出。
"""

from src.app.bridge import create_app
from src.app.dispatcher import MasterDispatcher
from src.app.monitor import SiteMonitor
from src.app.net_monitor import NetworkMonitor

__all__ = [
    "create_app",
    "MasterDispatcher",
    "SiteMonitor",
    "NetworkMonitor",
]
