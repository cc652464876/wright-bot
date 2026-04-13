"""
@Layer   : Utils 层（第二层 · 横切关注点）
@Role    : 全局日志系统统一初始化与配置
@Pattern : AOP（面向切面编程）+ Singleton（单次初始化保护）
@Description:
    基于 loguru 统一接管整个项目的日志输出。
    提供 setup_logger() 作为唯一初始化入口，配置文件 sink（按大小轮转、按天保留）
    和控制台彩色 sink；通过 InterceptHandler 将 crawlee / playwright 等第三方库
    使用的标准 logging 模块流量统一转发至 loguru，实现日志源的完全收拢。
    各模块通过 get_logger(__name__) 获取携带模块名标签的 logger 实例。
"""

from __future__ import annotations

import logging
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from loguru import Logger, logger

# 模块级存储：保留最近一次 setup_logger 的参数，供 set_level 复用
_setup_params: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# 第一部分：日志级别枚举
# ---------------------------------------------------------------------------

class LogLevel(str, Enum):
    """
    日志级别枚举，与 loguru 内置级别字符串严格对应。
    供 AppConfig 和 setup_logger() 做类型安全的级别传参。
    """

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# 第二部分：标准库 logging 拦截器
# ---------------------------------------------------------------------------

class InterceptHandler(logging.Handler):
    """
    标准库 logging → loguru 桥接处理器。
    将 crawlee、playwright、aiohttp 等第三方依赖产生的 logging.Logger 日志记录
    统一重定向到 loguru 的处理管线，实现日志格式与目标 sink 的全局一致。
    Pattern: AOP —— 以无侵入方式拦截第三方日志横切面。
    """

    def emit(self, record: logging.LogRecord) -> None:
        """
        接收标准 LogRecord，将其级别和消息转发给 loguru logger。
        通过 logger.opt(depth=...) 保留原始调用栈帧信息。
        """
        # 将 levelname 映射为 loguru 已知级别名；未知级别退回数字值
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 沿调用栈向上找到 logging 模块之外的第一帧，计算正确 depth
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


# ---------------------------------------------------------------------------
# 第三部分：初始化函数与模块级工具
# ---------------------------------------------------------------------------

def setup_logger(
    log_dir: str = "./logs",
    log_level: str = "INFO",
    rotation: str = "10 MB",
    retention: str = "7 days",
    console: bool = True,
    intercept_stdlib: bool = True,
) -> None:
    """
    初始化 loguru 日志系统（全局唯一入口，应在 main.py 最顶部调用）。

    职责：
    1. 移除 loguru 默认 sink，重新按项目规范挂载。
    2. 添加文件 sink：写入 log_dir，按 rotation 大小轮转，按 retention 时长保留。
    3. 若 console=True，添加带颜色格式的控制台 stderr sink。
    4. 若 intercept_stdlib=True，将所有标准库 logging 根处理器替换为 InterceptHandler。

    Args:
        log_dir: 日志文件输出目录（不存在时自动创建）。
        log_level: 最低记录级别，低于此级别的日志将被丢弃。
        rotation: 单个日志文件的轮转条件（如 '10 MB' 或 '1 day'）。
        retention: 旧日志文件的保留策略（如 '7 days'）。
        console: 是否同时向控制台输出彩色日志。
        intercept_stdlib: 是否拦截标准库 logging 并转发至 loguru。
    """
    global _setup_params

    # 持久化参数，供 set_level() 重新初始化时使用
    _setup_params = dict(
        log_dir=log_dir,
        log_level=log_level,
        rotation=rotation,
        retention=retention,
        console=console,
        intercept_stdlib=intercept_stdlib,
    )

    # 清除所有已存在的 sink（含 loguru 默认的 stderr sink）
    logger.remove()

    # 自动创建日志目录
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # 文件 sink：按大小轮转、按天保留、UTF-8 编码、后台异步写入
    _FILE_FMT = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
        "{name}:{function}:{line} - {message}"
    )
    logger.add(
        log_path / "app_{time:YYYY-MM-DD}.log",
        level=log_level,
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,           # 线程/异步安全的后台写入队列
        backtrace=True,
        diagnose=False,         # 生产环境关闭变量值泄露
        format=_FILE_FMT,
    )

    # 控制台 sink：彩色、简洁格式
    if console:
        _CONSOLE_FMT = (
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        )
        logger.add(
            sys.stderr,
            level=log_level,
            colorize=True,
            format=_CONSOLE_FMT,
        )

    # 将标准库 logging 根处理器替换为拦截器
    if intercept_stdlib:
        logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)


def get_logger(name: str = "") -> Logger:
    """
    获取携带模块名标签的 loguru logger 实例。
    各模块在文件顶部调用：_log = get_logger(__name__)
    通过 logger.bind(module=name) 在每条日志记录上附加 module 字段，
    方便在文件 sink 中按模块过滤。

    Args:
        name: 通常传入 __name__，即当前模块的完整包路径。

    Returns:
        绑定了 module 字段的 loguru Logger 实例。
    """
    return logger.bind(module=name)


def set_level(level: str) -> None:
    """
    运行时动态调整所有已注册 sink 的最低日志级别。
    供调试模式或 UI 级别切换按钮在程序运行期间调用。

    Args:
        level: 目标级别字符串，须为 LogLevel 枚举值之一。
    """
    if not _setup_params:
        # setup_logger 尚未调用，直接以新级别初始化一次
        setup_logger(log_level=level)
        return

    setup_logger(**{**_setup_params, "log_level": level})
