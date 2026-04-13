"""
engine.anti_bot.behavior — 人类行为模拟子包。

导出两个具体 Simulator 实现；抽象基类 AbstractBehaviorSimulator
属于内部约定，由具体实现类直接继承，不在此对外暴露。
"""

from src.engine.anti_bot.behavior.playwright_simulator import PlaywrightBehaviorSimulator
from src.engine.anti_bot.behavior.pyautogui_simulator import PlaywrightHumanMouseSimulator

__all__ = [
    "PlaywrightBehaviorSimulator",
    "PlaywrightHumanMouseSimulator",
]
