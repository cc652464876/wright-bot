"""
utils — 横切关注点工具层。

对外暴露日志三件套、四个 AOP 装饰器，以及从 tenacity 统一转出的重试原语。
其他模块应优先从本包导入，而非直接深入 logger / decorators 子模块，
以便将来替换底层实现时只需改动此处。
"""

from src.utils.logger import get_logger, set_level, setup_logger
from src.utils.decorators import (
    async_timeout,
    log_call,
    singleton,
    timeit,
    # tenacity re-exports — 统一在此单点管控，方便全项目替换
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_fixed,
    before_sleep_log,
)

__all__ = [
    # 日志
    "setup_logger",
    "get_logger",
    "set_level",
    # AOP 装饰器
    "log_call",
    "timeit",
    "singleton",
    "async_timeout",
    # tenacity re-exports
    "retry",
    "retry_if_exception_type",
    "stop_after_attempt",
    "wait_exponential",
    "wait_fixed",
    "before_sleep_log",
]
