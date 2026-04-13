"""
engine.anti_bot.stealth — 隐身后端子包。

导出三个可互换的浏览器隐身后端实现，供引擎工厂按配置选择注入。

同时导出 BrowserCoresConfig 和 _init_browser_env，
供需要浏览器路径隔离的脚本（scripts/）使用。
"""

from src.config.settings import BrowserCoresConfig, _init_browser_env
from src.engine.anti_bot.stealth.camoufox_backend import CamoufoxBackend
from src.engine.anti_bot.stealth.playwright_backend import PlaywrightBackend
from src.engine.anti_bot.stealth.rebrowser_backend import RebrowserBackend

__all__ = [
    # 浏览器后端（策略实现）
    "PlaywrightBackend",
    "CamoufoxBackend",
    "RebrowserBackend",
    # 浏览器内核本地化隔离
    "BrowserCoresConfig",
    "_init_browser_env",
]
