"""
db — 数据持久层。

对外仅暴露 DatabaseManager 和异步工厂函数 get_db。
DDL / schema 初始化函数（init_db、drop_all_tables 等）由 database.py
在内部调用，属于实现细节，不在此重导出。
"""

from src.db.database import DatabaseManager, get_db

__all__ = [
    "DatabaseManager",
    "get_db",
]
