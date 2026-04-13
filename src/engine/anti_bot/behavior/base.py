"""
@Layer   : Engine 层（第三层 · 反爬工具箱 · 行为模拟）
@Role    : 浏览器行为模拟器抽象基类（策略接口契约）
@Pattern : Strategy Pattern（行为模拟策略可替换）
@Description:
    定义所有具体行为模拟策略必须实现的标准交互接口：
    click / hover / fill / scroll。

    Interactor（modules/site/handlers/interactor.py）面向此接口编程，
    不感知底层是 Playwright 虚拟输入还是 PyAutoGUI OS 级真实鼠标事件。

    已有的具体实现（见同目录）：
        PlaywrightBehaviorSimulator : 调用 Playwright locator.click() / page.mouse 等 API。
                                      默认策略，无额外依赖。
        PyAutoGUIBezierSimulator    : 将 DOM 元素 bounding box 坐标映射为屏幕坐标，
                                      通过 PyAutoGUI + 贝塞尔曲线生成真实鼠标轨迹并执行点击。
                                      可绕过对 CDP 鼠标事件特征（dispatch type = synthetic）的检测。

    新增策略指引：
        1. 在 engine/anti_bot/behavior/ 下新建 xxx_simulator.py。
        2. 继承 AbstractBehaviorSimulator 并实现所有 @abstractmethod。
        3. 在 StealthConfig.behavior_mode 的 Literal 中追加新值。
        4. 在 MasterDispatcher / Runner 的模拟器工厂映射中注册新值。
        ★ 全程不需要修改 Interactor 或其他模拟器文件。

    Pattern: Strategy —— 每个具体子类代表一种物理隔离的行为模拟策略。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page, Locator


class AbstractBehaviorSimulator(ABC):
    """
    浏览器行为模拟器抽象基类。

    所有方法均接受 Playwright Locator 或 ElementHandle 作为目标元素，
    以及 page: Page 作为上下文（某些策略需要通过 page 获取屏幕坐标）。

    Pattern: Strategy —— 子类即"一种行为模拟策略"，Interactor 面向此基类编程。
    """

    # ------------------------------------------------------------------
    # 核心交互方法（子类必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    async def click(
        self,
        element: Any,
        page: Optional["Page"] = None,
        **kwargs: Any,
    ) -> None:
        """
        点击目标元素。

        Args:
            element: Playwright Locator 或 ElementHandle（具体类型由子类决定）。
            page   : 当前 Playwright Page 实例。
                     PyAutoGUI 策略需要通过 page 获取元素在视口中的 bounding box，
                     进而映射为屏幕坐标；Playwright 策略可忽略此参数。
            **kwargs: 额外参数（如 force / timeout / button）由子类按需消费。
        """
        ...

    @abstractmethod
    async def hover(
        self,
        element: Any,
        page: Optional["Page"] = None,
        **kwargs: Any,
    ) -> None:
        """
        将鼠标悬停到目标元素位置。

        Args:
            element: 目标元素（Playwright Locator 或 ElementHandle）。
            page   : 当前 Playwright Page 实例（PyAutoGUI 策略需要）。
            **kwargs: 额外参数（如 timeout）由子类按需消费。
        """
        ...

    @abstractmethod
    async def fill(
        self,
        element: Any,
        value: str,
        page: Optional["Page"] = None,
        **kwargs: Any,
    ) -> None:
        """
        在目标输入元素中填写文本。

        Args:
            element: 目标输入元素（Playwright Locator 或 ElementHandle）。
            value  : 要填入的文本字符串。
            page   : 当前 Playwright Page 实例（PyAutoGUI 策略需要）。
            **kwargs: 额外参数（如 delay 模拟逐字输入）由子类按需消费。
        """
        ...

    @abstractmethod
    async def scroll(
        self,
        page: "Page",
        direction: str = "down",
        amount: int = 300,
    ) -> None:
        """
        在页面中滚动指定方向和距离。

        Args:
            page     : 当前 Playwright Page 实例。
            direction: 滚动方向，'down' / 'up' / 'left' / 'right'。
            amount   : 滚动像素数（逻辑像素）。
        """
        ...
