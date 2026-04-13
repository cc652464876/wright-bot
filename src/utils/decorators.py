"""
@Layer   : Utils 层（第二层 · 横切关注点）
@Role    : AOP 横切关注点装饰器库
@Pattern : AOP（面向切面编程）+ Decorator Pattern + Singleton（@singleton）
@Description:
    提供一组可复用的函数装饰器，以无侵入方式将重试、日志、计时、单例等
    横切关注点"织入"任意业务函数，而无需修改函数本身的实现代码。
    所有装饰器均同时支持同步函数和 asyncio 协程函数，并与 loguru 日志系统集成。

    ── @retry ──────────────────────────────────────────────────────────────
    直接复用 requirements.txt 中已有的 tenacity 库，不自造轮子。
    使用方按需组合 tenacity 的装饰器原语：
        from tenacity import (
            retry, stop_after_attempt, wait_exponential,
            wait_fixed, retry_if_exception_type, before_sleep_log,
        )
    常用示例：
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(TimeoutError),
        )
        async def fetch_page(url: str) -> str: ...
    ────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from typing import Any, Callable, Optional, TypeVar

from src.utils.logger import get_logger

_log = get_logger(__name__)

# @retry：直接从 tenacity 导入，项目中不再维护自定义实现。
# 各模块按需 from tenacity import retry, stop_after_attempt, wait_exponential …
from tenacity import (  # noqa: F401  — 统一在此 re-export，方便全项目单点替换  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_fixed,
    before_sleep_log,
)

# 泛型变量：代表被装饰的原始可调用对象类型
F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# 第一部分：@log_call —— 函数调用日志切面
# ---------------------------------------------------------------------------

def log_call(
    level: str = "DEBUG",
    log_args: bool = True,
    log_result: bool = False,
    log_elapsed: bool = True,
) -> Callable[[F], F]:
    """
    函数调用日志装饰器工厂（同步 & 异步通用）。
    Pattern: AOP —— 在函数进入/退出处自动织入日志记录，无需手动 print/log。

    Args:
        level     : 日志输出级别，默认 DEBUG（生产环境不污染 INFO 流）。
        log_args  : 是否记录入参（*args, **kwargs）；敏感接口可设为 False。
        log_result: 是否记录返回值；返回值较大时建议关闭。
        log_elapsed: 是否在退出时记录函数耗时（毫秒）。

    Usage:
        @log_call(level="INFO", log_result=True)
        def build_crawler(config: dict) -> Crawler: ...
    """
    def decorator(func: F) -> F:

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            """同步函数日志包装器：记录入参、返回值、耗时。"""
            entry = f"→ {func.__qualname__}"
            if log_args:
                entry += f"  args={args!r}  kwargs={kwargs!r}"
            _log.log(level, entry)

            t0 = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception:
                elapsed = (time.perf_counter() - t0) * 1000
                _log.log(level, f"✗ {func.__qualname__} raised after {elapsed:.1f}ms")
                raise

            elapsed = (time.perf_counter() - t0) * 1000
            exit_msg = f"← {func.__qualname__}"
            if log_elapsed:
                exit_msg += f"  elapsed={elapsed:.1f}ms"
            if log_result:
                exit_msg += f"  result={result!r}"
            _log.log(level, exit_msg)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            """异步协程日志包装器：记录入参、返回值、耗时。"""
            entry = f"→ {func.__qualname__}"
            if log_args:
                entry += f"  args={args!r}  kwargs={kwargs!r}"
            _log.log(level, entry)

            t0 = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
            except Exception:
                elapsed = (time.perf_counter() - t0) * 1000
                _log.log(level, f"✗ {func.__qualname__} raised after {elapsed:.1f}ms")
                raise

            elapsed = (time.perf_counter() - t0) * 1000
            exit_msg = f"← {func.__qualname__}"
            if log_elapsed:
                exit_msg += f"  elapsed={elapsed:.1f}ms"
            if log_result:
                exit_msg += f"  result={result!r}"
            _log.log(level, exit_msg)
            return result

        # 根据 func 是否为协程函数决定返回 async_wrapper 或 sync_wrapper
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore[return-value]
        return sync_wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# 第二部分：@timeit —— 轻量计时切面
# ---------------------------------------------------------------------------

def timeit(func: F) -> F:
    """
    轻量计时装饰器（同步 & 异步通用，无需括号直接装饰）。
    Pattern: AOP —— 最简形式的性能监测切面，仅输出耗时，不记录参数。
    执行完毕后通过 loguru DEBUG 级别输出函数名与耗时（毫秒）。

    Usage:
        @timeit
        async def run_crawl_session(): ...
    """
    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        result = await func(*args, **kwargs)
        elapsed = (time.perf_counter() - t0) * 1000
        _log.debug(f"[timeit] {func.__qualname__}  {elapsed:.1f}ms")
        return result

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = (time.perf_counter() - t0) * 1000
        _log.debug(f"[timeit] {func.__qualname__}  {elapsed:.1f}ms")
        return result

    if asyncio.iscoroutinefunction(func):
        return async_wrapper  # type: ignore[return-value]
    return sync_wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 第三部分：@singleton —— 类级别单例切面
# ---------------------------------------------------------------------------

def singleton(cls: type) -> type:
    """
    类级别单例装饰器（线程安全，双重检查锁）。
    Pattern: Singleton Pattern —— 通过装饰器实现，无需修改类内部结构。
    确保被装饰的类在整个进程生命周期内只实例化一次；
    后续调用 cls() 均返回缓存的同一实例。

    Usage:
        @singleton
        class DatabaseManager: ...
    """
    _instance: Optional[Any] = None
    _lock = threading.Lock()

    @functools.wraps(cls)
    def get_instance(*args: Any, **kwargs: Any) -> Any:
        nonlocal _instance
        # 第一次检查（无锁，快速路径）
        if _instance is None:
            with _lock:
                # 第二次检查（持锁，防止竞态）
                if _instance is None:
                    _instance = cls(*args, **kwargs)
        return _instance

    return get_instance  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 第四部分：@async_timeout —— 协程超时切面
# ---------------------------------------------------------------------------

def async_timeout(seconds: float) -> Callable[[F], F]:
    """
    异步协程超时装饰器工厂。
    Pattern: AOP —— 将 asyncio.wait_for 超时保护织入协程外层，
    超时后抛出 asyncio.TimeoutError，可与 tenacity @retry 组合使用。

    Args:
        seconds: 允许的最大执行时间（秒）；超出则取消协程并抛出异常。

    Usage:
        @async_timeout(30.0)
        @retry(stop=stop_after_attempt(2), retry=retry_if_exception_type(asyncio.TimeoutError))
        async def navigate_page(url: str) -> None: ...
    """
    def decorator(func: F) -> F:

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """在 asyncio.wait_for 包裹下执行协程，超时则抛出 TimeoutError。"""
            return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)

        return wrapper  # type: ignore[return-value]

    return decorator
