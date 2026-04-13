"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 爬虫策略抽象基类（策略接口契约）
@Pattern : Strategy Pattern（策略模式）—— 定义算法族的公共接口
@Description:
    作为所有具体爬取策略（SiteCrawlStrategy、SearchCrawlStrategy 等）的抽象父类，
    通过 ABC + 抽象方法强制子类实现标准生命周期接口：
    validate() → run() → cleanup()
    MasterDispatcher（src/app/dispatcher.py）作为策略模式的 Context，
    持有 BaseCrawlStrategy 引用并在运行时动态切换具体策略，
    从而将"任务调度"与"具体爬取算法"完全解耦。
    所有横切关注点（重试、日志、计时）通过 src/utils/decorators.py 的 AOP
    装饰器在子类方法上注入，基类本身保持纯净的接口定义。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Optional

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.config.settings import PrismSettings

_log = get_logger(__name__)


class BaseCrawlStrategy(ABC):
    """
    爬虫策略抽象基类。

    定义所有具体策略必须遵守的生命周期协议：
    1. validate() —— 任务启动前的配置合法性校验。
    2. run()      —— 核心爬取逻辑执行（异步）。
    3. stop()     —— 外部发出中止信号（非阻塞）。
    4. cleanup()  —— 任务结束后的资源释放（异步）。

    子类须实现所有抽象方法；非抽象方法提供默认实现，子类可按需覆盖。
    Pattern: Strategy Pattern —— 子类即"一种策略"，Context（Dispatcher）面向此基类编程。
    """

    def __init__(self, settings: "PrismSettings") -> None:
        """
        Args:
            settings: 全局运行时参数单例（PrismSettings），策略从中读取所有配置。
        """
        self.settings = settings
        self._is_running: bool = False

    # ------------------------------------------------------------------
    # 抽象方法：子类必须实现
    # ------------------------------------------------------------------

    @abstractmethod
    def validate(self) -> bool:
        """
        任务启动前的配置合法性校验。
        检查 settings 中当前策略所必需的字段是否合法（如 target_urls 非空、
        crawl_strategy 与模式匹配等）。

        Returns:
            True 表示校验通过，可以执行 run()；
            False 表示校验失败，Dispatcher 应中止任务并向 UI 报错。
        """
        pass

    @abstractmethod
    async def run(self) -> None:
        """
        核心爬取逻辑（异步，由 Runner 在事件循环中 await）。
        实现具体的种子生成 → 引擎启动 → 请求处理 → 结果持久化全流程。
        """
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        """
        任务结束后的资源释放（无论成功或异常均须调用）。
        包括：关闭数据库连接、释放浏览器资源、导出最终报告等收尾工作。
        """
        pass

    # ------------------------------------------------------------------
    # 非抽象方法：提供默认实现，子类可覆盖
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """
        外部发出中止信号（非阻塞）。
        将内部运行标志 _is_running 置为 False；
        run() 中的循环应定期检查此标志以实现优雅退出。
        子类可覆盖以添加额外的停止逻辑（如通知 Crawlee 引擎中止）。
        """
        _log.info(f"[{self.get_strategy_name()}] 收到停止信号，正在设置 _is_running=False")
        self._is_running = False

    def is_running(self) -> bool:
        """
        检查策略当前是否处于运行中状态。
        供 handlers 内部的 check_running_func lambda 调用，
        替代原有的 self.is_running 布尔属性访问模式。

        Returns:
            True 表示任务进行中；False 表示已停止或尚未启动。
        """
        return self._is_running

    def get_dashboard_data(self) -> Dict[str, Any]:
        """
        返回供 UI 轮询的实时仪表盘数据快照。
        默认返回空字典；子类应覆盖并委托给 Monitor 返回有意义的统计数据。

        Returns:
            包含 files_found / files_downloaded / status 等字段的字典。
        """
        return {}

    def get_strategy_name(self) -> str:
        """
        返回当前策略的人类可读名称（用于日志和 UI 标签）。
        默认返回类名；子类可覆盖返回更友好的中文名称。

        Returns:
            策略名称字符串，如 'site_full' 或 '网站批量抓取'。
        """
        return self.__class__.__name__
