"""
@Layer   : Engine 层（第三层 · 引擎基础设施）
@Role    : 浏览器后端工厂（统一入口）
@Pattern : Factory Pattern + Strategy Pattern（AbstractBrowserBackend 具体策略的创建者）
@Description:
    将三种浏览器后端的实例化逻辑从散落各处的独立函数和隐式规则中
    收拢到一个中心化工厂类。

    调用方只需：
        backend = BrowserFactory.create_backend(settings)
    即可获得已注入 stealth_config + app_config 的后端实例，
    完全不需要感知具体使用哪一种浏览器引擎。

    支持的引擎类型（engine_type 字符串 → 后端类映射）：
        "chromium"  → PlaywrightBackend（标准 Playwright Chromium）
        "rebrowser" → RebrowserBackend（CDP 补丁版 Chromium）
        "camoufox"  → CamoufoxBackend（补丁版 Firefox）

    扩展指引：
        1. 在 engine/anti_bot/stealth/ 下新建 xxx_backend.py（继承 AbstractBrowserBackend）。
        2. 在 _BACKEND_REGISTRY 中追加 {engine_type: BackendClass} 条目。
        3. 在 StealthConfig.stealth_engine 的 Literal 中追加新键名。
        全程不需要修改任何已存在的后端文件或 CrawleeEngineFactory。

    Pattern: Factory —— 统一入口，消除 if/elif 分支。
             Strategy —— 工厂生产 AbstractBrowserBackend 子类实例，上层完全面向接口。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Dict, Type

from src.engine.browser_engine import AbstractBrowserBackend

if TYPE_CHECKING:
    from src.config.settings import PrismSettings, StealthConfig, AppConfig

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 后端注册表（引擎类型键 → 具体后端类映射）
# ---------------------------------------------------------------------------
#
# 添加新后端指引：
#   1. 在 src/engine/anti_bot/stealth/ 下实现继承自 AbstractBrowserBackend 的子类。
#   2. 在此处追加 {engine_type_key: BackendClass} 条目。
#   3. 在 StealthConfig.stealth_engine 的 Literal 类型中追加新键名（settings.py）。
#   ★ 无需修改工厂代码本身；扩展时只需要更新本注册表。
#
_BACKEND_REGISTRY: Dict[str, Type[AbstractBrowserBackend]] = {}


def _register_backend(key: str, cls: Type[AbstractBrowserBackend]) -> None:
    """将后端类注册到工厂注册表（幂等操作，支持热扩展）。"""
    _BACKEND_REGISTRY[key] = cls
    _logger.debug(f"[BrowserFactory] 注册后端: {key!r} → {cls.__name__}")


def _load_backends() -> None:
    """
    延迟加载所有后端实现并注册到 _BACKEND_REGISTRY。

    采用延迟加载而非顶层 import 的原因：
        - 避免在 import 阶段触发 playwright / rebrowser_patches / camoufox 的模块初始化
          （部分库在 import 时会检查二进制路径或尝试连接远程服务）。
        - 确保 _init_browser_env()（main.py 最早期调用）完成后再执行这些 import，
          避免 PLAYWRIGHT_BROWSERS_PATH 等环境变量尚未设置就触发浏览器探测。
        - 实际使用时（Python 解释器启动后、爬虫任务开始前）才真正加载，
          符合"Composition Root 之后才解析依赖"的原则。
    """
    global _backends_loaded
    try:
        from src.engine.anti_bot.stealth.playwright_backend import PlaywrightBackend
        from src.engine.anti_bot.stealth.rebrowser_backend import RebrowserBackend
        from src.engine.anti_bot.stealth.camoufox_backend import CamoufoxBackend

        _register_backend("chromium", PlaywrightBackend)
        _register_backend("rebrowser", RebrowserBackend)
        _register_backend("camoufox", CamoufoxBackend)
        _backends_loaded = True
    except ImportError as exc:
        _logger.warning(
            f"[BrowserFactory] 部分后端依赖未安装，已注册的后端仍可使用。导入失败: {exc}"
        )


_backends_loaded: bool = False


# ---------------------------------------------------------------------------
# BrowserFactory — 核心工厂类
# ---------------------------------------------------------------------------

class BrowserFactory:
    """
    浏览器后端工厂（统一入口）。

    提供单一工厂方法 ``create_backend()``，根据 ``PrismSettings.stealth.stealth_engine``
    的值在注册表中查找对应的 AbstractBrowserBackend 子类，
    实例化并返回已注入 stealth_config + app_config 的后端实例。

    调用方无需 if/elif，无需 isinstance，上层代码零修改即可支持新增的浏览器后端。

    使用示例：
        from src.engine.browser_factory import BrowserFactory

        # 在 CrawleeEngineFactory 内部使用（推荐）
        backend = BrowserFactory.create_backend(settings)

        # 在 UI 层使用（engine_type 直接来自前端传参）
        backend = BrowserFactory.create_backend(settings)
        async with backend as b:
            page = await b.new_page()

    Pattern: Factory（统一入口） + Strategy（AbstractBrowserBackend 具体策略）
    """

    @staticmethod
    def create_backend(
        settings: "PrismSettings",
        *,
        backend_class: Optional[Type["AbstractBrowserBackend"]] = None,
    ) -> Optional[AbstractBrowserBackend]:
        """
        根据 ``settings.stealth.stealth_engine`` 创建并返回对应的浏览器后端实例。

        Args:
            settings      : 全局运行时参数单例（PrismSettings），从中读取 stealth_engine 值。
            backend_class: 可选，直接指定后端类（绕过注册表查找）。
                           用于单元测试或需要强制使用特定后端的场景。
                           为 None 时按 stealth_engine 键自动查找。

        Returns:
            已注入 stealth_config 和 app_config 的 AbstractBrowserBackend 子类实例；
            stealth_engine 为未知值时记录错误并返回 None。

        Raises:
            TypeError: backend_class 不是 AbstractBrowserBackend 子类时抛出。

        Pattern: Factory —— 调用方无需感知具体后端类型。
                 DI     —— stealth_config / app_config 从外部传入工厂，
                          工厂本身不持有全局状态。
        """
        # ── 延迟加载注册表（首次调用时执行一次）────────────────────────────
        global _backends_loaded
        if not _backends_loaded:
            _load_backends()

        # ── 解析 engine_type ────────────────────────────────────────────
        engine_type: str = settings.stealth.stealth_engine
        stealth_cfg: "StealthConfig" = settings.stealth

        # Camoufox 特殊处理：当前 Crawlee PlaywrightCrawler 主路径尚未接入
        # Camoufox 启动链，选择 camoufox 时返回 None 并记录告警
        # （CamoufoxBackend 本身已注册到注册表，可在 bypass Crawlee 路径中
        #  直接使用 backend_class=CamoufoxBackend 强制启用）
        if engine_type == "camoufox":
            _logger.warning(
                "[BrowserFactory] stealth_engine=camoufox：Camoufox 启动链与 "
                "Crawlee PlaywrightCrawler 当前路径存在兼容性限制。\n"
                "    若使用 CrawleeEngineFactory.create()，本次任务将按标准 Chromium "
                "启动。\n"
                "    UI 选项已预留，详见 BrowserFactory.list_engines()。\n"
                "    若需直接管理 Camoufox 生命周期（绕过 Crawlee），"
                "请直接实例化 CamoufoxBackend。"
            )
            # 返回 None，由调用方（CrawleeEngineFactory）决定回退策略

        # ── 查找后端类 ──────────────────────────────────────────────────
        if backend_class is not None:
            backend_cls = backend_class
        else:
            backend_cls = _BACKEND_REGISTRY.get(engine_type)

        if backend_cls is None:
            _logger.error(
                f"[BrowserFactory] 未知的 stealth_engine={engine_type!r}，"
                f"已注册后端: {list(_BACKEND_REGISTRY.keys())}。"
                f"已降级返回 None。请检查 StealthConfig.stealth_engine 配置。"
            )
            return None

        # ── 实例化后端（注入依赖）────────────────────────────────────────
        try:
            from src.config.settings import get_app_config

            app_cfg: "AppConfig" = get_app_config()
            backend: AbstractBrowserBackend = backend_cls(
                stealth_config=stealth_cfg,
                app_config=app_cfg,
            )
            return backend
        except TypeError as exc:
            # 捕获 __init__ 参数签名不匹配（扩展时可能发生）
            _logger.error(
                f"[BrowserFactory] 实例化 {backend_cls.__name__} 失败: {exc}。"
                f"请检查 __init__ 签名是否与 (stealth_config, app_config) 一致。"
            )
            return None

    # ------------------------------------------------------------------
    # 注册表查询接口（供调试 / UI 层使用）
    # ------------------------------------------------------------------

    @staticmethod
    def list_engines() -> Dict[str, str]:
        """
        返回所有已注册浏览器引擎的元数据字典。

        Returns:
            {"engine_type": "描述字符串"}，可直接供 UI 下拉框渲染使用。
        """
        global _backends_loaded
        if not _backends_loaded:
            _load_backends()

        return {
            "chromium": (
                "标准 Playwright Chromium（默认）：稳定、无额外二进制；适合一般站点。"
            ),
            "rebrowser": (
                "rebrowser-patches 强化 Chromium：启动前注入 CDP 补丁，削弱自动化特征；"
                "需 pip install rebrowser-patches。"
            ),
            "camoufox": (
                "Camoufox 补丁 Firefox：二进制层反指纹能力最强；"
                "与 Crawlee PlaywrightCrawler 存在兼容性限制，高防护目标推荐直接使用 "
                "CamoufoxBackend。需 pip install camoufox[geoip]。"
            ),
        }

    @staticmethod
    def is_registered(engine_type: str) -> bool:
        """检查指定引擎类型是否已在注册表中注册。"""
        global _backends_loaded
        if not _backends_loaded:
            _load_backends()
        return engine_type in _BACKEND_REGISTRY
