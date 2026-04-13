"""
@Layer   : Engine 层（第三层 · 反爬工具箱 · 行为模拟）
@Role    : 标准 Playwright 行为模拟器（AbstractBehaviorSimulator 默认实现）
@Pattern : Strategy Pattern（AbstractBehaviorSimulator 具体策略）
@Description:
    使用 Playwright 原生 API 执行所有交互操作（click / hover / fill / scroll）。
    这是默认的行为模拟策略，无额外依赖，适合对 WAF 鼠标事件检测要求不高的场景。

    此文件只处理"Playwright API 虚拟输入场景"，不含任何 PyAutoGUI 或
    OS 级鼠标移动逻辑。若需要真实鼠标事件，请使用 pyautogui_simulator.py——
    两者实现了相同的 AbstractBehaviorSimulator 接口，Interactor 无感切换。

    Playwright 虚拟输入的局限性（供将来判断是否需要切换策略时参考）：
        - page.click() 最终通过 CDP dispatchMouseEvent 发送，type = "mousePressed"，
          缺少真实鼠标轨迹（MouseEvent.movementX/Y 恒为 0）。
        - 部分高级 WAF（如 DataDome、PerimeterX）会检测此特征，判定为自动化操作。
        - 若遇到此类拦截，切换到 PyAutoGUIBezierSimulator 无需修改 Interactor 代码。

    Pattern: Strategy —— 实现 AbstractBehaviorSimulator，代表"Playwright 标准虚拟输入策略"。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, TYPE_CHECKING

from src.engine.anti_bot.behavior.base import AbstractBehaviorSimulator

if TYPE_CHECKING:
    from playwright.async_api import Page, Locator


class PlaywrightBehaviorSimulator(AbstractBehaviorSimulator):
    """
    标准 Playwright 行为模拟器（默认策略）。

    所有交互操作通过 Playwright locator / page.mouse API 执行。
    无需额外依赖，开箱即用。

    Pattern: Strategy（AbstractBehaviorSimulator 具体实现）
    """

    def __init__(
        self,
        click_delay_ms: int = 0,
        hover_before_click: bool = False,
    ) -> None:
        """
        Args:
            click_delay_ms    : 点击前的额外延迟毫秒数（模拟人工反应时间，默认 0 即无延迟）。
            hover_before_click: True 时在 click() 前先执行 hover()，
                                模拟人类"移动鼠标到目标再点击"的行为序列。
        """
        self._click_delay_ms = click_delay_ms
        self._hover_before_click = hover_before_click

    # ------------------------------------------------------------------
    # AbstractBehaviorSimulator 接口实现
    # ------------------------------------------------------------------

    async def click(
        self,
        element: Any,
        page: Optional["Page"] = None,
        **kwargs: Any,
    ) -> None:
        """
        通过 Playwright locator.click() 执行点击。

        实现要点：
        1. 若 hover_before_click=True，先调用 element.hover()。
        2. 若 click_delay_ms > 0，等待对应毫秒数。
        3. 调用 element.click(**kwargs)。

        Args:
            element: Playwright Locator 实例。
            page   : 本策略不需要，签名保留以满足接口契约。
            **kwargs: 传递给 locator.click()（如 button='right' / force=True）。
        """
        if self._hover_before_click:
            await element.hover()
        if self._click_delay_ms > 0:
            await asyncio.sleep(self._click_delay_ms / 1000.0)
        await element.click(**kwargs)

    async def hover(
        self,
        element: Any,
        page: Optional["Page"] = None,
        **kwargs: Any,
    ) -> None:
        """
        通过 Playwright locator.hover() 执行悬停。

        Args:
            element: Playwright Locator 实例。
            page   : 本策略不需要。
            **kwargs: 传递给 locator.hover()（如 timeout）。
        """
        await element.hover(**kwargs)

    async def fill(
        self,
        element: Any,
        value: str,
        page: Optional["Page"] = None,
        **kwargs: Any,
    ) -> None:
        """
        通过 Playwright locator.fill() 填写文本。

        Args:
            element: Playwright Locator 实例（须为 input / textarea）。
            value  : 要填入的文本。
            page   : 本策略不需要。
            **kwargs: 传递给 locator.fill()（如 timeout）。
        """
        await element.fill(value, **kwargs)

    async def scroll(
        self,
        page: "Page",
        direction: str = "down",
        amount: int = 300,
    ) -> None:
        """
        通过 page.mouse.wheel() 执行页面滚动。

        Args:
            page     : 当前 Playwright Page 实例。
            direction: 'down'（正值 deltaY）/ 'up'（负值 deltaY）/
                       'right'（正值 deltaX）/ 'left'（负值 deltaX）。
            amount   : 滚动像素数。
        """
        delta_map = {
            "down":  (0,       amount),
            "up":    (0,      -amount),
            "right": (amount,       0),
            "left":  (-amount,      0),
        }
        delta_x, delta_y = delta_map.get(direction, (0, amount))
        await page.mouse.wheel(delta_x, delta_y)
