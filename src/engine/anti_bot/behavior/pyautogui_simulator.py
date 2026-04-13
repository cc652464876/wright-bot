"""
@Layer   : Engine 层（第三层 · 反爬工具箱 · 行为模拟）
@Role    : Playwright page.mouse + 贝塞尔曲线浏览器级仿真鼠标行为模拟器
@Pattern : Strategy Pattern（AbstractBehaviorSimulator 具体策略）
@Description:
    通过 Playwright 原生 page.mouse API 模拟带贝塞尔曲线轨迹的鼠标操作。
    与 PlaywrightBehaviorSimulator 的区别在于：本模拟器在每次点击/悬停前
    先沿贝塞尔曲线插值路径移动鼠标，产生真实的 MouseEvent.movementX/Y 变化，
    绕过部分 WAF 对"鼠标轨迹为零"的自动化检测。

    ── 技术实现 ──────────────────────────────────────────────────────────
    - page.mouse.move(x, y)      : 逐步按贝塞尔轨迹点移动鼠标（纯浏览器内）。
    - page.mouse.click(x, y)     : 在目标坐标执行最终点击。
    - element.bounding_box()     : 获取 DOM 元素视口坐标，无需屏幕偏移换算。
    - 贝塞尔曲线插值（三次 Bézier）: 生成自然弯曲轨迹，模拟手部运动。
    - asyncio.sleep(frame_delay) : 控制帧率（≈ 60fps），保持移动平滑性。

    ── 与旧版 PyAutoGUI 方案的对比 ────────────────────────────────────────
    旧方案（已废弃）：
        PyAutoGUI → OS 级 WM_MOUSEMOVE → 只在 headless=False + 前台窗口有效
        需要 win32gui 计算窗口偏移 → Windows 专属，跨平台失效
        影响用户桌面（鼠标被强制移动）→ 无法在后台静默运行

    新方案（本文件）：
        page.mouse.move() → CDP 发送 Input.dispatchMouseEvent → 在任意 headless 模式有效
        坐标直接来自 element.bounding_box() → 无需屏幕偏移，跨平台，零桌面干扰
        movementX/Y 随轨迹自然变化 → 通过 WAF 对轨迹连续性的检测

    ── 适用场景 ───────────────────────────────────────────────────────────
    headless=True  : 完全有效（CDP 直接注入鼠标事件）。
    headless=False : 完全有效（CDP 优先级高于 OS 鼠标消息）。
    isTrusted      : 通过 CDP 发送的事件 isTrusted=false，
                     仅极少数顶级 WAF（如 DataDome v3+）检测此字段；
                     绝大多数场景下此模拟器的检测绕过效果优于 PlaywrightBehaviorSimulator。

    ── 依赖 ──────────────────────────────────────────────────────────────
    无额外依赖，仅需 playwright（已在 requirements.txt）。
    可选：pip install numpy（加速贝塞尔点位计算；无 numpy 时退化为纯 Python 实现）。

    Pattern: Strategy —— 实现 AbstractBehaviorSimulator，
             代表"Playwright page.mouse 贝塞尔曲线仿真鼠标策略"。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

from src.engine.anti_bot.behavior.base import AbstractBehaviorSimulator

if TYPE_CHECKING:
    from playwright.async_api import Page, Locator


class PlaywrightHumanMouseSimulator(AbstractBehaviorSimulator):
    """
    Playwright page.mouse + 贝塞尔曲线仿真鼠标模拟器。

    所有点击、悬停操作均通过：
    DOM 坐标（bounding_box）→ 贝塞尔曲线轨迹生成 → page.mouse.move() 逐帧移动 → page.mouse.click()

    Pattern: Strategy（AbstractBehaviorSimulator 具体实现）
    """

    def __init__(
        self,
        move_duration_range: Tuple[float, float] = (0.3, 0.8),
        curve_control_jitter: float = 0.3,
        post_click_delay_range: Tuple[float, float] = (0.05, 0.2),
        frame_rate: int = 60,
    ) -> None:
        """
        Args:
            move_duration_range   : 鼠标移动总耗时范围（秒），在此区间内随机取值，
                                    模拟不同手速的用户行为。
            curve_control_jitter  : 贝塞尔曲线控制点的随机偏移比例（0.0 ~ 1.0），
                                    值越大轨迹越弯曲，值越小越接近直线。
            post_click_delay_range: 点击后的短暂停留时间范围（秒），模拟人工确认反应。
            frame_rate            : 鼠标移动帧率（fps），决定轨迹点采样密度。
                                    60fps 约每 16ms 发送一次 page.mouse.move()。
        """
        self._move_duration_range = move_duration_range
        self._curve_control_jitter = curve_control_jitter
        self._post_click_delay_range = post_click_delay_range
        self._frame_rate = frame_rate
        # 记录上次鼠标位置，作为贝塞尔曲线起点（初始值取视口典型中心）
        self._current_x: float = 640.0
        self._current_y: float = 400.0

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
        通过贝塞尔曲线轨迹移动鼠标并执行浏览器级点击。

        实现流程：
        1. await element.bounding_box() 获取元素视口坐标 {x, y, width, height}。
        2. 计算元素中心点，加入 ±3px 随机抖动模拟非精确定位。
        3. 调用 _move_with_bezier(page, target_x, target_y) 沿贝塞尔轨迹移动鼠标。
        4. 调用 await page.mouse.click(target_x, target_y, **kwargs) 完成点击。
        5. await asyncio.sleep(随机 post_click_delay) 模拟点击后停顿。

        Args:
            element: Playwright Locator 实例（用于获取 bounding_box）。
            page   : 当前 Playwright Page 实例（必须提供）。
            **kwargs: 传递给 page.mouse.click()（如 button='right'）。
        """
        if page is None:
            raise ValueError("page 参数是必须的，PlaywrightHumanMouseSimulator.click() 需要通过 page.mouse 发送事件。")
        target_x, target_y = await self._get_element_viewport_coords(element)
        await self._move_with_bezier(page, target_x, target_y)
        await page.mouse.click(target_x, target_y, **kwargs)
        await asyncio.sleep(random.uniform(*self._post_click_delay_range))

    async def hover(
        self,
        element: Any,
        page: Optional["Page"] = None,
        **kwargs: Any,
    ) -> None:
        """
        通过贝塞尔曲线轨迹移动鼠标到目标元素上方（不点击）。

        实现流程：
        1. 获取目标视口坐标（与 click 相同）。
        2. 调用 _move_with_bezier(page, target_x, target_y) 移动鼠标。
        3. 短暂停留（asyncio.sleep）模拟人工悬停确认。

        Args:
            element: Playwright Locator 实例。
            page   : 当前 Playwright Page 实例（必须提供）。
        """
        if page is None:
            raise ValueError("page 参数是必须的，PlaywrightHumanMouseSimulator.hover() 需要通过 page.mouse 发送事件。")
        target_x, target_y = await self._get_element_viewport_coords(element)
        await self._move_with_bezier(page, target_x, target_y)
        await asyncio.sleep(random.uniform(0.05, 0.15))

    async def fill(
        self,
        element: Any,
        value: str,
        page: Optional["Page"] = None,
        **kwargs: Any,
    ) -> None:
        """
        先点击目标输入框（触发焦点），再通过 page.keyboard.type() 逐字输入文本。

        实现流程：
        1. 调用 self.click(element, page) 使输入框获得焦点（产生真实鼠标轨迹）。
        2. 对 value 的每个字符：
           a. await page.keyboard.type(char, delay=随机字符间隔毫秒) 模拟逐字输入。
           b. 间歇性插入随机停顿（模拟思考/输入停顿）。

        Args:
            element: Playwright Locator 实例（input / textarea）。
            value  : 要填入的文本字符串。
            page   : 当前 Playwright Page 实例（必须提供）。
            **kwargs: delay_range=(min, max) 覆盖默认字符输入间隔范围（秒）。
        """
        if page is None:
            raise ValueError("page 参数是必须的，PlaywrightHumanMouseSimulator.fill() 需要通过 page.keyboard 发送事件。")
        # 提取可选参数，避免传递给 click
        delay_range: Tuple[float, float] = kwargs.pop("delay_range", (0.04, 0.14))

        # 先点击输入框获取焦点，产生真实鼠标轨迹
        await self.click(element, page)

        # 逐字符输入，delay 以毫秒为单位
        for i, char in enumerate(value):
            char_delay_ms = random.uniform(delay_range[0], delay_range[1]) * 1000
            await page.keyboard.type(char, delay=char_delay_ms)
            # 每 5~15 个字符以 5% 概率插入较长停顿（模拟思考或手误后停顿）
            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(0.3, 0.8))

    async def scroll(
        self,
        page: "Page",
        direction: str = "down",
        amount: int = 300,
    ) -> None:
        """
        通过 page.mouse.wheel() 执行带随机分段的页面滚动。

        实现流程：
        1. 将 amount 分解为多次小幅度滚动（模拟真实滚轮非线性加速度）。
        2. 每次调用 await page.mouse.wheel(delta_x, delta_y)。
        3. 每次滚动间插入随机微小延迟（asyncio.sleep）。

        Args:
            page     : 当前 Playwright Page 实例。
            direction: 'down'（正值 deltaY）/ 'up'（负值 deltaY）/
                       'right'（正值 deltaX）/ 'left'（负值 deltaX）。
            amount   : 逻辑滚动距离（像素），内部拆分为多段执行。
        """
        axis_map = {
            "down":  (0,  1),
            "up":    (0, -1),
            "right": (1,  0),
            "left":  (-1, 0),
        }
        dx_sign, dy_sign = axis_map.get(direction, (0, 1))

        remaining = amount
        while remaining > 0:
            # 非线性分段：每次滚动 60~120px，模拟滚轮加速/减速
            chunk = min(random.randint(60, 120), remaining)
            await page.mouse.wheel(dx_sign * chunk, dy_sign * chunk)
            remaining -= chunk
            await asyncio.sleep(random.uniform(0.02, 0.07))

    # ------------------------------------------------------------------
    # 私有：坐标计算与轨迹生成
    # ------------------------------------------------------------------

    async def _get_element_viewport_coords(
        self,
        element: Any,
    ) -> Tuple[float, float]:
        """
        获取 DOM 元素中心点在视口中的坐标（无需屏幕偏移换算）。

        实现步骤：
        1. await element.bounding_box() 获取 {x, y, width, height}。
        2. 计算中心点：(x + width/2, y + height/2)。
        3. 在中心点附近添加微小随机抖动（±3px），模拟人工点击非精确定位。

        Args:
            element: Playwright Locator 实例。
        Returns:
            (viewport_x, viewport_y) 视口坐标元组（浮点数）。
        Raises:
            ValueError: element.bounding_box() 返回 None（元素不在视口内）时抛出。
        """
        bbox = await element.bounding_box()
        if bbox is None:
            raise ValueError(
                "element.bounding_box() 返回 None，目标元素不在当前视口中，请先滚动到可见区域。"
            )
        # 中心点 + ±3px 随机抖动，模拟人工点击非精确定位
        cx = bbox["x"] + bbox["width"]  / 2.0 + random.uniform(-3.0, 3.0)
        cy = bbox["y"] + bbox["height"] / 2.0 + random.uniform(-3.0, 3.0)
        return cx, cy

    async def _move_with_bezier(
        self,
        page: "Page",
        target_x: float,
        target_y: float,
    ) -> None:
        """
        通过三次贝塞尔曲线生成鼠标移动轨迹并逐帧调用 page.mouse.move()。

        实现步骤：
        1. 通过 page.evaluate("() => ({x: window.mouseX, y: window.mouseY})")
           或记录上次移动坐标作为起点 P0（初始为页面中心）。
        2. 以 (target_x, target_y) 为终点 P3。
        3. 在起终点之间随机生成两个控制点 P1、P2（加入 curve_control_jitter 偏移）。
        4. 调用 _generate_bezier_points(p0, p3, n_points) 获取轨迹点序列。
        5. 逐帧调用 await page.mouse.move(x, y) 移动到每个轨迹点。
        6. await asyncio.sleep(1 / frame_rate) 控制帧率。

        Args:
            page    : 当前 Playwright Page 实例。
            target_x: 目标视口 X 坐标。
            target_y: 目标视口 Y 坐标。
        """
        p0 = (self._current_x, self._current_y)
        p3 = (target_x, target_y)

        duration  = random.uniform(*self._move_duration_range)
        n_points  = max(2, int(duration * self._frame_rate))
        frame_delay = 1.0 / self._frame_rate

        points = self._generate_bezier_points(p0, p3, n_points)

        for x, y in points:
            await page.mouse.move(x, y)
            await asyncio.sleep(frame_delay)

        # 更新当前鼠标位置记录
        self._current_x = target_x
        self._current_y = target_y

    def _generate_bezier_points(
        self,
        p0: Tuple[float, float],
        p3: Tuple[float, float],
        n_points: int,
    ) -> List[Tuple[float, float]]:
        """
        生成三次贝塞尔曲线的 n_points 个均匀采样轨迹点。

        控制点生成策略：
        - P1 在 P0→P3 向量的 1/3 处附近，加入垂直方向随机偏移（curve_control_jitter）。
        - P2 在 P0→P3 向量的 2/3 处附近，同样加入垂直方向随机偏移。
        - 偏移量 = 起终点距离 × curve_control_jitter × random.uniform(-1, 1)。

        可选优化：若 numpy 可用，使用向量化计算替代 Python 循环：
            t = np.linspace(0, 1, n_points)
            points = ((1-t)**3)[:,None]*p0 + 3*((1-t)**2*t)[:,None]*p1 + ...

        Args:
            p0      : 起点 (x, y)。
            p3      : 终点 (x, y)。
            n_points: 采样点数量（决定轨迹平滑度，由 move_duration * frame_rate 决定）。
        Returns:
            轨迹点列表 [(x1,y1), (x2,y2), ...]（浮点数，page.mouse.move 接受浮点坐标）。
        """
        if n_points < 2:
            return [p0, p3]

        dx   = p3[0] - p0[0]
        dy   = p3[1] - p0[1]
        dist = (dx * dx + dy * dy) ** 0.5
        jitter = dist * self._curve_control_jitter

        # 两个控制点在起终点连线 1/3 和 2/3 处附近，加入垂直方向随机扰动
        p1 = (
            p0[0] + dx / 3.0 + random.uniform(-1.0, 1.0) * jitter,
            p0[1] + dy / 3.0 + random.uniform(-1.0, 1.0) * jitter,
        )
        p2 = (
            p0[0] + 2.0 * dx / 3.0 + random.uniform(-1.0, 1.0) * jitter,
            p0[1] + 2.0 * dy / 3.0 + random.uniform(-1.0, 1.0) * jitter,
        )

        # 尝试 numpy 向量化加速；不可用时退化为纯 Python
        try:
            import numpy as np
            t   = np.linspace(0.0, 1.0, n_points)
            mt  = 1.0 - t
            xs  = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
            ys  = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
            return list(zip(xs.tolist(), ys.tolist()))
        except ImportError:
            pass

        # 纯 Python 三次贝塞尔采样
        points: List[Tuple[float, float]] = []
        for i in range(n_points):
            t  = i / (n_points - 1)
            mt = 1.0 - t
            x  = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
            y  = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
            points.append((x, y))
        return points
