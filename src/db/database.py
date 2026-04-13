"""
@Layer   : Core 层（第三层 · 数据持久化）
@Role    : SQLite 异步安全数据库管理器（模块级懒加载）
@Pattern : Single-Writer Queue Pattern（单消费者写入队列） + 读写分离
@Description:
    放弃原有 @singleton 装饰器（存在 async 初始化与同步 __new__ 的根本矛盾），
    改用模块级懒加载单例（get_db() + asyncio.Lock 保护并发初始化）。

    并发安全策略（解决 SQLite "database is locked" 的根本方案）：

    写操作（INSERT / UPDATE / DELETE）：
        所有爬虫协程通过 enqueue_write() / enqueue_write_many() 将写请求
        推入 asyncio.Queue；后台唯一的 _writer_loop() 协程串行消费队列，
        从根本上消除多协程并发写入的 SQLite 锁争用。
        调用方 await future 等待写入结果，失败异常从此处透传。

    读操作（SELECT）：
        直接 await aiosqlite，支持并发执行（WAL 模式允许多读并发）。

    初始化：
        get_db() 通过模块级 asyncio.Lock 保护，确保 initialize() 只被调用一次；
        asyncio 单线程模型保证 _db_lock 的赋值本身是原子的（无需额外同步）。

    Pattern: Single-Writer Queue（写串行化） + WAL 并发读
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite


# ---------------------------------------------------------------------------
# 内部：写入请求数据类（队列消息体）
# ---------------------------------------------------------------------------

@dataclass
class _WriteRequest:
    """
    写入队列的单条消息体，封装一次 DML 操作及其结果回调 Future。

    Attributes:
        sql   : 参数化 SQL 语句（? 占位符）。
        params: execute 模式为 Tuple；executemany 模式为 List[Tuple]。
        many  : True 表示 executemany 批量写入，False 表示单条 execute。
        future: 写入完成后由 _writer_loop 设置 result / exception，
                供 enqueue_write 调用方通过 await future 感知写入结果。
    """

    sql: str
    params: Any
    many: bool
    future: asyncio.Future


# ---------------------------------------------------------------------------
# 数据库管理器
# ---------------------------------------------------------------------------

class DatabaseManager:
    """
    SQLite 异步数据库管理器。

    实例由 get_db() 模块级懒加载创建并缓存，业务代码不应直接实例化此类，
    始终通过 ``db = await get_db()`` 获取。

    并发安全策略：
    - 写操作：单消费者 _writer_loop() 协程串行执行，消除锁争用。
    - 读操作：直接 await aiosqlite，WAL 模式下多协程可并发 SELECT。

    Pattern: Single-Writer Queue（写串行化） + WAL 并发读（读操作并行）
    """

    def __init__(self, db_path: str = "./data/prism.db") -> None:
        """
        同步初始化：设置路径与数据结构，不打开数据库连接。
        真正的连接与后台写入任务由 initialize() 启动。

        Args:
            db_path: SQLite 数据库文件的相对或绝对路径。
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_queue: asyncio.Queue[_WriteRequest] = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """
        异步初始化：打开 aiosqlite 连接，配置 WAL / 外键约束，
        并将 _writer_loop() 作为后台 asyncio.Task 启动。

        由 get_db() 在首次访问时自动调用，不应由业务代码直接调用。
        """
        from src.db.schema import init_db

        # 打开连接；row_factory 使游标返回 sqlite3.Row，支持 dict(row) 转换
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row

        # 开启 WAL 模式（允许多协程并发 SELECT）并强制外键约束
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.commit()

        # 建表（DDL 在 writer_loop 启动前直接操作 _conn，避免鸡蛋问题）
        await init_db(self)

        # 启动后台单写者循环
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name="db-writer"
        )

    async def _writer_loop(self) -> None:
        """
        单消费者写入循环（后台 asyncio.Task 持续运行）。

        逐条从 _write_queue 取出 _WriteRequest，按 many 标志选择
        execute / executemany，提交事务后通过 future 通知调用方。
        异常时通过 future.set_exception() 透传给 enqueue_write() 的调用方。
        使用 task_done() 以支持 queue.join() 实现优雅关闭。
        """
        while True:
            request = await self._write_queue.get()
            try:
                if request.many:
                    await self._conn.executemany(request.sql, request.params)
                else:
                    await self._conn.execute(request.sql, request.params)
                await self._conn.commit()
            except asyncio.CancelledError:
                # close() 取消任务时可能正好在处理一个请求；
                # 通知调用方此请求被取消，然后 re-raise 退出循环
                if not request.future.done():
                    request.future.cancel()
                raise
            except Exception as exc:
                if not request.future.done():
                    request.future.set_exception(exc)
            else:
                if not request.future.done():
                    request.future.set_result(None)
            finally:
                self._write_queue.task_done()

    async def close(self) -> None:
        """
        优雅关闭：等待写入队列完全消费，取消后台 Task，关闭 aiosqlite 连接。
        通常由 bridge.py 的 on_closing 回调触发。
        """
        # 等待队列中所有已入队的写请求全部被消费（task_done 计数归零）
        await self._write_queue.join()

        # 取消后台写入任务（此时队列已空，cancel 打断的是 get() 的阻塞等待）
        if self._writer_task and not self._writer_task.done():
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # 公开：写入入口（单写者队列 —— 所有 DML 必须经由此接口）
    # ------------------------------------------------------------------

    async def enqueue_write(
        self,
        sql: str,
        params: Tuple[Any, ...] = (),
    ) -> None:
        """
        将单条 DML（INSERT / UPDATE / DELETE）推入写入队列。

        当前协程阻塞等待后台 _writer_loop 执行完毕后才返回；
        写入失败时 aiosqlite.Error 从此处透传给调用方。

        Args:
            sql   : 使用 ? 占位符的参数化 SQL。
            params: 与占位符一一对应的参数元组。
        Raises:
            aiosqlite.Error: 后台写入失败时透传。
            RuntimeError   : 数据库尚未初始化（_conn 为 None）时抛出。
        """
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._write_queue.put(
            _WriteRequest(sql=sql, params=params, many=False, future=future)
        )
        await future

    async def enqueue_write_many(
        self,
        sql: str,
        params_list: List[Tuple[Any, ...]],
    ) -> None:
        """
        将批量 DML 推入写入队列（executemany 模式，后台单事务批量提交）。

        Args:
            sql        : 使用 ? 占位符的参数化 SQL。
            params_list: 参数元组列表，每项对应一次 SQL 执行。
        Raises:
            aiosqlite.Error: 后台批量写入失败时透传。
        """
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._write_queue.put(
            _WriteRequest(sql=sql, params=params_list, many=True, future=future)
        )
        await future

    # ------------------------------------------------------------------
    # 公开：DML 便捷方法（委托单写者队列，接口与原同步版本对齐）
    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        """
        执行单条 DML，内部委托 enqueue_write()。

        Args:
            sql   : 参数化 SQL。
            params: 绑定参数元组。
        """
        await self.enqueue_write(sql, params)

    async def executemany(
        self,
        sql: str,
        params_list: List[Tuple[Any, ...]],
    ) -> None:
        """
        批量执行 DML，内部委托 enqueue_write_many()。

        Args:
            sql        : 参数化 SQL。
            params_list: 参数元组列表。
        """
        await self.enqueue_write_many(sql, params_list)

    # ------------------------------------------------------------------
    # 公开：查询操作（直接 await aiosqlite，支持并发执行）
    # ------------------------------------------------------------------

    async def query(
        self,
        sql: str,
        params: Tuple[Any, ...] = (),
    ) -> List[Dict[str, Any]]:
        """
        执行 SELECT 查询，以字典列表形式返回结果集（并发安全）。
        WAL 模式下多个 SELECT 可同时执行，不经过写入队列。

        Args:
            sql   : SELECT 语句（支持 ? 占位符）。
            params: 绑定参数元组。
        Returns:
            每行结果为 {column_name: value} 的字典列表；无数据时返回空列表。
        Raises:
            aiosqlite.Error: 查询执行失败时上抛。
        """
        if self._conn is None:
            raise RuntimeError("DatabaseManager 尚未初始化，请先 await get_db()")
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            # sqlite3.Row 支持 keys()；dict(row) 将其转为普通字典
            return [dict(row) for row in rows]

    async def query_one(
        self,
        sql: str,
        params: Tuple[Any, ...] = (),
    ) -> Optional[Dict[str, Any]]:
        """
        执行 SELECT 查询，仅返回第一行结果（若无结果则返回 None）。

        Args:
            sql   : SELECT 语句。
            params: 绑定参数元组。
        Returns:
            第一行结果字典；无数据时返回 None。
        """
        if self._conn is None:
            raise RuntimeError("DatabaseManager 尚未初始化，请先 await get_db()")
        async with self._conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 模块级懒加载（替代 @singleton + threading 双重检查锁反模式）
# ---------------------------------------------------------------------------

_db_instance: Optional[DatabaseManager] = None
_db_lock: Optional[asyncio.Lock] = None


async def get_db(db_path: str = "./data/prism.db") -> DatabaseManager:
    """
    获取 DatabaseManager 模块级单例（异步懒加载）。

    首次调用时自动创建实例并 await initialize() 完成连接和后台任务启动；
    后续调用直接返回缓存实例，无额外开销。

    线程安全说明：
        - _db_lock 赋值在第一个无 await 的代码段中完成；
          asyncio 单线程协作调度保证该赋值对其他协程的可见性（无需额外同步）。
        - async with _db_lock 包裹实例化过程，防止并发首次调用时重复初始化。

    Args:
        db_path: 数据库文件路径，仅首次调用时生效，后续调用忽略此参数。
    Returns:
        已初始化并运行写入循环的 DatabaseManager 实例。
    """
    global _db_instance, _db_lock

    if _db_instance is not None:
        return _db_instance

    # asyncio 单线程：此处无 await，赋值原子完成，不会被其他协程抢占
    if _db_lock is None:
        _db_lock = asyncio.Lock()

    async with _db_lock:
        if _db_instance is None:
            db = DatabaseManager(db_path)
            await db.initialize()
            _db_instance = db

    return _db_instance
