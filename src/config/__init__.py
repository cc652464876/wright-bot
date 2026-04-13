"""
config — 应用配置层。

对外只暴露顶层配置模型和三个单例访问函数；各 Config 子模型（TaskInfoConfig、
StrategyConfig 等）属于内部实现细节，不在此重导出。

新增浏览器内核本地化隔离导出：
    BrowserCoresConfig、_init_browser_env
    供 main.py 和 scripts/ 中的工具脚本在最早时刻调用，确保
    PLAYWRIGHT_BROWSERS_PATH / REBROWSER_BROWSERS_PATH / CAMOUFOX_CACHE_DIR
    在任何 Playwright / Camoufox 模块 import 之前被正确设置。
"""

from src.config.settings import (
    AppConfig,
    BrowserCoresConfig,
    PrismSettings,
    _init_browser_env,
    get_app_config,
    get_settings,
    update_settings,
)

__all__ = [
    # 顶层配置模型
    "PrismSettings",
    "AppConfig",
    # 浏览器内核本地化隔离
    "BrowserCoresConfig",
    "_init_browser_env",
    # 单例访问 / 更新函数
    "get_settings",
    "get_app_config",
    "update_settings",
]
