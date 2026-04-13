"""
@Layer   : Core 层（第三层 · 数据持久化）
@Role    : 数据库表结构 DDL 定义与初始化
@Pattern : Data Mapper（表结构集中管理） + Migration-Ready（DDL 集中维护）
@Description:
    将所有 CREATE TABLE / CREATE INDEX DDL 集中在此模块，
    作为数据库结构的唯一权威定义（SSOT for Schema）。
    init_db() 是唯一初始化入口，幂等安全（使用 IF NOT EXISTS），
    可在应用启动和单元测试中重复调用。
    当前定义四张核心表：
      - tasks          : 爬取任务记录（每次 run 对应一行）
      - downloaded_files: 已成功下载的文件元数据
      - scan_records   : 页面扫描历史（成功 / 失败 / 重定向）
      - error_log      : 去重错误日志（对应原 ErrorRegistry 功能）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from src.db.database import DatabaseManager

# 删除顺序（反向外键依赖：子表先删，父表 tasks 最后）
_DROP_ORDER: List[str] = [
    "DROP TABLE IF EXISTS error_log;",
    "DROP TABLE IF EXISTS scan_records;",
    "DROP TABLE IF EXISTS downloaded_files;",
    "DROP TABLE IF EXISTS tasks;",
]


# ---------------------------------------------------------------------------
# DDL 常量：每张表独立一个字符串，便于版本管理和迁移
# ---------------------------------------------------------------------------

DDL_TABLE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name     TEXT    NOT NULL,
    mode          TEXT    NOT NULL CHECK(mode IN ('site', 'search')),
    strategy      TEXT    NOT NULL,
    save_directory TEXT   NOT NULL,
    file_type     TEXT    NOT NULL DEFAULT 'pdf',
    status        TEXT    NOT NULL DEFAULT 'pending'
                          CHECK(status IN ('pending', 'running', 'stopped', 'finished', 'error')),
    started_at    TEXT,
    finished_at   TEXT,
    config_json   TEXT
);
"""

DDL_TABLE_DOWNLOADED_FILES = """
CREATE TABLE IF NOT EXISTS downloaded_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    domain        TEXT    NOT NULL,
    source_page   TEXT    NOT NULL,
    file_url      TEXT    NOT NULL,
    file_name     TEXT    NOT NULL,
    file_size_kb  REAL,
    saved_path    TEXT,
    downloaded_at TEXT    NOT NULL,
    UNIQUE(task_id, file_url)
);
"""

DDL_TABLE_SCAN_RECORDS = """
CREATE TABLE IF NOT EXISTS scan_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    domain        TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK(status IN ('success', 'failed', 'redirect')),
    status_code   INTEGER,
    error_msg     TEXT,
    scanned_at    TEXT    NOT NULL,
    UNIQUE(task_id, url)
);
"""

DDL_TABLE_ERROR_LOG = """
CREATE TABLE IF NOT EXISTS error_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    domain        TEXT,
    error_type    TEXT    NOT NULL,
    error_msg     TEXT    NOT NULL,
    url           TEXT,
    fingerprint   TEXT    NOT NULL UNIQUE,
    first_seen    TEXT    NOT NULL,
    last_seen     TEXT    NOT NULL,
    count         INTEGER NOT NULL DEFAULT 1
);
"""

# 索引 DDL：提升高频查询性能
DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_downloaded_files_task_domain ON downloaded_files(task_id, domain);",
    "CREATE INDEX IF NOT EXISTS idx_scan_records_task_domain ON scan_records(task_id, domain);",
    "CREATE INDEX IF NOT EXISTS idx_error_log_fingerprint ON error_log(fingerprint);",
]

# 按执行顺序排列的所有 DDL（外键依赖：tasks 须在子表之前）
_ALL_DDL: List[str] = [
    DDL_TABLE_TASKS,
    DDL_TABLE_DOWNLOADED_FILES,
    DDL_TABLE_SCAN_RECORDS,
    DDL_TABLE_ERROR_LOG,
    *DDL_INDEXES,
]


# ---------------------------------------------------------------------------
# 公开初始化接口
# ---------------------------------------------------------------------------

async def init_db(db: "DatabaseManager") -> None:
    """
    执行所有建表 DDL，初始化数据库结构（幂等，可重复调用）。
    应在应用启动时（bridge.py 或 main.py）调用一次。

    直接使用 db._conn 执行 DDL，绕过写入队列——此函数在 _writer_loop
    启动之前被 initialize() 调用，队列尚不可用。

    Args:
        db: 已建立 aiosqlite 连接的 DatabaseManager 实例。
    """
    assert db._conn is not None, "initialize() 必须先创建 _conn 再调用 init_db()"
    for ddl in _ALL_DDL:
        await db._conn.execute(ddl)
    await db._conn.commit()


def get_all_ddl_statements() -> List[str]:
    """
    返回所有 DDL 语句列表的副本。
    供数据库迁移工具、单元测试或文档生成器使用。

    Returns:
        包含所有 CREATE TABLE / CREATE INDEX 语句的字符串列表。
    """
    return list(_ALL_DDL)


async def drop_all_tables(db: "DatabaseManager") -> None:
    """
    删除所有受管理的表（仅用于测试环境重置，生产环境禁止调用）。
    按外键依赖的反向顺序执行 DROP TABLE IF EXISTS。

    直接使用 db._conn 执行，确保与 init_db 对称并在队列之外操作。

    Args:
        db: 已初始化的 DatabaseManager 单例。
    """
    assert db._conn is not None, "DatabaseManager 尚未初始化"
    for stmt in _DROP_ORDER:
        await db._conn.execute(stmt)
    await db._conn.commit()
