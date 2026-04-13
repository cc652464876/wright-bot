"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : 域名级审计中心（扫描记录、成果清单、并发安全写入）
@Pattern : Singleton-like（任务期间全局唯一实例） + Repository Pattern（DatabaseManager 持久层）
@Description:
    SiteAuditCenter 是爬虫任务期间的"数据账本"，完全接管所有域名维度的
    状态存储与持久化 I/O，将并发控制和路径管理从业务代码（handlers）中彻底剥离。

    ── 存储方案（V10.1 重构方向）────────────────────────────────────────────
    放弃 core/ 中的 defaultdict(asyncio.Lock) + 手写 JSON 原子写方案，
    改用 aiosqlite（requirements.txt 中已有）+ src/db/schema.py 中已定义的表：

        scan_records      ← 对应 Hook1/2（record_page_success / failure）
        downloaded_files  ← 对应 Hook4（record_result_batch）
        error_log         ← 对应 Hook2/3（scan_errors / download_errors）

    持久化与并发（详见 src/db/database.py DatabaseManager）：
    - 所有 DML（INSERT/UPDATE/DELETE）经单写者 asyncio.Queue 入队，由后台 _writer_loop
      串行执行并逐条 commit，避免多协程直接争用 SQLite 写锁；本类不再自建第二层写队列。
    - WAL（PRAGMA journal_mode=WAL）配合上述队列：读操作（SELECT）可走连接并发执行。
    - 去重通过 UNIQUE 约束 + INSERT OR IGNORE 在 SQL 层保证，替代内存 set。
    - 断点续传通过查询已存在记录实现，替代手动从 JSON 文件重建内存状态。
    - UI 轮询的 O(1) 计数通过维护内存计数器 + 必要时 SELECT COUNT(*) 校准实现。

    ── 文件落盘（manifest.json 兼容层）────────────────────────────────────
    若上游消费方仍需要 manifest.json 文件格式（如现有 UI 读取），
    export_final_reports() 在任务结束时从 DB 导出一次 JSON 快照即可，
    无需在每次 record_result_batch() 时原子写文件。

    ── 实时文件流（可选，RealtimeFileExporter 委托）──────────────────────
    当 task_info.enable_realtime_jsonl_export 为 True 时，由策略层注入
    RealtimeFileExporter，在写 DB 成功后再追加 scanned_urls.jsonl /
    scan_errors_log.txt / interactions.jsonl（交互为 JSONL 新契约，见迁移文档）。

    ── 对外 API 不变 ───────────────────────────────────────────────────────
    5 个 Hook 方法签名与 core/site_audit_center.py 完全一致，Runner 层零修改。

    Pattern: Repository（DB 封装）+ DatabaseManager 单写者队列 + WAL 并发读 + 可选实时文件委托
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, TYPE_CHECKING

import aiofiles  # export_final_reports 异步写 manifest.json

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.db.database import DatabaseManager
    from src.modules.site.audit.realtime_file_exporter import RealtimeFileExporter

_log = get_logger(__name__)

# SQL 常量，与 schema.py 中 DDL 字段严格对齐
_SQL_INSERT_SCAN_SUCCESS = """
INSERT OR IGNORE INTO scan_records
    (task_id, domain, url, status, status_code, scanned_at)
VALUES (?, ?, ?, 'success', ?, datetime('now'))
"""

_SQL_INSERT_SCAN_FAILURE = """
INSERT OR IGNORE INTO scan_records
    (task_id, domain, url, status, status_code, error_msg, scanned_at)
VALUES (?, ?, ?, 'failed', ?, ?, datetime('now'))
"""

_SQL_UPSERT_ERROR_LOG = """
INSERT INTO error_log
    (task_id, domain, error_type, error_msg, url, fingerprint, first_seen, last_seen, count)
VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), 1)
ON CONFLICT(fingerprint) DO UPDATE SET
    last_seen = datetime('now'),
    count     = count + 1
"""

_SQL_INSERT_FILE = """
INSERT OR IGNORE INTO downloaded_files
    (task_id, domain, source_page, file_url, file_name, downloaded_at)
VALUES (?, ?, ?, ?, ?, datetime('now'))
"""

_SQL_COUNT_FILES = "SELECT COUNT(*) AS cnt FROM downloaded_files WHERE task_id = ?"


class SiteAuditCenter:
    """
    域名级异步审计中心（任务期间全局唯一实例）。

    存储架构（SQLite + WAL；经 get_db(db_path) 取得 DatabaseManager 单例）：
    - scan_records     表 ← record_page_success / record_page_failure
    - downloaded_files 表 ← record_result_batch（UNIQUE(task_id, file_url) 去重）
    - error_log        表 ← record_page_failure / record_download_failure

    并发安全：
    - 写操作：DatabaseManager.enqueue_write / execute / executemany → 内部 asyncio.Queue +
      唯一 _writer_loop 消费（见 database.py）；多协程可同时 await，但不会并行触碰连接写接口。
    - 读操作：DatabaseManager.query / query_one 在 WAL 下可并发 SELECT。
    - 去重由 SQL UNIQUE 约束 + INSERT OR IGNORE 保证，消除内存 set 维护成本。

    内存状态：
    - _total_scraped_count : int —— UI 轮询专用计数器，随 INSERT 成功递增，O(1) 读取。
    - _task_id             : Optional[int] —— DB 中的 tasks.id；需在任务创建后由外部设置。

    Pattern: Repository Pattern —— DB 操作封装，业务 Hook 与存储细节完全解耦。
    """

    def __init__(
        self,
        db_path: str,
        base_save_dir: str,
        strategy_prefix: str = "site",
        *,
        realtime_exporter: Optional["RealtimeFileExporter"] = None,
    ) -> None:
        """
        Args:
            db_path        : SQLite 数据库文件路径（由 DatabaseManager 统一管理，
                             通常为 {base_save_dir}/audit.db）。
            base_save_dir  : 任务根保存目录（来自 settings.task_info.save_directory）。
            strategy_prefix: 子目录前缀（如 'direct' / 'full' / 'sitemap'），
                             与域名拼接后作为最终存储子目录名（如 'full_example.com'）。
            realtime_exporter: 可选；启用 task_info.enable_realtime_jsonl_export 时
                             由策略层注入，负责 JSONL/TXT 实时追加（与 DB 双写）。

        初始化说明：
            self._db_path              : str —— SQLite 文件路径，供 _get_conn() 使用。
            self._base_save_dir        : str —— 根保存目录。
            self._strategy_prefix      : str —— 目录前缀。
            self._total_scraped_count  : int —— UI 轮询计数器（任务存续期间从 0 开始累加）。
            self._task_id              : Optional[int] —— 必须在 run() 前由外部 set_task_id() 注入。
        """
        self._db_path = db_path
        self._base_save_dir = base_save_dir
        self._strategy_prefix = strategy_prefix
        self._total_scraped_count: int = 0
        # task_id 须在 DB 创建任务行后由调用方注入，初始为 None
        self._task_id: Optional[int] = None
        self._realtime_exporter: Optional["RealtimeFileExporter"] = realtime_exporter

    # ------------------------------------------------------------------
    # 公开：task_id 注入（由 SiteCrawlStrategy 在 DB 写入任务行后调用）
    # ------------------------------------------------------------------

    def set_task_id(self, task_id: int) -> None:
        """
        注入当前任务在 tasks 表中的主键，确保 Hook 写入时外键引用有效。

        Args:
            task_id: tasks.id 主键整数。
        """
        self._task_id = task_id
        _log.debug(f"[SiteAuditCenter] task_id 已设置为 {task_id}")

    # ------------------------------------------------------------------
    # 公开统计接口
    # ------------------------------------------------------------------

    def get_total_scraped_count(self) -> int:
        """
        O(1) 返回当前任务所有域名下已去重的文件总数。
        由 UI 轮询频繁调用，须保证极低开销。
        计数器在每次 record_result_batch() 成功 INSERT 时递增，无需查询 DB。

        Returns:
            全局去重文件总计数整数。
        """
        return self._total_scraped_count

    # ------------------------------------------------------------------
    # 公开业务 Hook API（对外唯一调用入口）
    # ------------------------------------------------------------------

    async def record_page_success(
        self,
        domain: str,
        url: str,
        status_code: int = 200,
    ) -> None:
        """
        Hook 1：记录成功扫描的页面。
        INSERT OR IGNORE INTO scan_records(task_id, domain, url, status, status_code, scanned_at)
        VALUES(?, ?, ?, 'success', ?, datetime('now'))

        Args:
            domain     : 目标域名（由 SiteDataParser.get_core_domain() 提取）。
            url        : 已成功扫描的页面 URL。
            status_code: HTTP 响应状态码（默认 200）。
        """
        if not self._guard_task_id("record_page_success"):
            return
        db = await self._get_conn()
        try:
            await db.execute(_SQL_INSERT_SCAN_SUCCESS, (self._task_id, domain, url, status_code))
        except Exception as exc:
            _log.warning(f"[SiteAuditCenter] record_page_success 写入失败: {exc!r}")
            return
        if self._realtime_exporter:
            try:
                await self._realtime_exporter.append_scanned_url_if_new(
                    domain, url, ok=True, status_code=status_code
                )
            except Exception as exc:
                _log.warning(f"[SiteAuditCenter] 实时 scanned_urls.jsonl 写入失败: {exc!r}")

    async def record_page_failure(
        self,
        domain: str,
        url: str,
        status_code: int,
        error_msg: str,
    ) -> None:
        """
        Hook 2：记录页面请求或解析彻底失败。
        INSERT OR IGNORE INTO scan_records(task_id, domain, url, status, status_code, error_msg, scanned_at)
        INSERT INTO error_log ... ON CONFLICT(fingerprint) DO UPDATE ...

        Args:
            domain    : 目标域名。
            url       : 失败的页面 URL。
            status_code: HTTP 状态码（彻底失败时统一传 500）。
            error_msg : 错误描述字符串。
        """
        if not self._guard_task_id("record_page_failure"):
            return
        db = await self._get_conn()
        fingerprint = self._make_fingerprint("scan_failure", domain, url)
        try:
            await db.execute(
                _SQL_INSERT_SCAN_FAILURE,
                (self._task_id, domain, url, status_code, error_msg[:500]),
            )
            await db.execute(
                _SQL_UPSERT_ERROR_LOG,
                (self._task_id, domain, "scan_failure", error_msg[:500], url, fingerprint),
            )
        except Exception as exc:
            _log.warning(f"[SiteAuditCenter] record_page_failure 写入失败: {exc!r}")
            return
        if self._realtime_exporter:
            try:
                await self._realtime_exporter.append_scanned_url_if_new(
                    domain, url, ok=False, status_code=status_code
                )
                await self._realtime_exporter.append_scan_error_log(
                    domain, url, status_code, error_msg
                )
            except Exception as exc:
                _log.warning(f"[SiteAuditCenter] 实时扫描失败日志写入失败: {exc!r}")

    async def record_download_failure(
        self,
        domain: str,
        url: str,
        error_msg: str,
    ) -> None:
        """
        Hook 3：记录文件下载失败（由 Downloader / ActionDownloader 调用）。
        INSERT INTO error_log ... ON CONFLICT(fingerprint) DO UPDATE ...

        Args:
            domain   : 目标域名。
            url      : 下载失败的文件 URL。
            error_msg: 错误描述字符串。
        """
        if not self._guard_task_id("record_download_failure"):
            return
        db = await self._get_conn()
        fingerprint = self._make_fingerprint("download_failure", domain, url)
        try:
            await db.execute(
                _SQL_UPSERT_ERROR_LOG,
                (self._task_id, domain, "download_failure", error_msg[:500], url, fingerprint),
            )
        except Exception as exc:
            _log.warning(f"[SiteAuditCenter] record_download_failure 写入失败: {exc!r}")

    async def record_result_batch(
        self,
        domain: str,
        source_page: str,
        new_file_urls: List[str],
    ) -> None:
        """
        Hook 4：记录成功下载的文件批次（SQL 层去重 + 计数器递增）。
        对每个 url 执行：
            INSERT OR IGNORE INTO downloaded_files(task_id, domain, source_page,
                file_url, file_name, downloaded_at)
        利用 UNIQUE(task_id, file_url) 约束在 DB 层去重，rowcount > 0 时递增计数器。

        Args:
            domain       : 目标域名。
            source_page  : 文件来源的父页面 URL。
            new_file_urls: 本批次发现的文件 URL 列表（未去重，DB 层处理）。
        """
        if not self._guard_task_id("record_result_batch") or not new_file_urls:
            return

        db = await self._get_conn()
        params_list = [
            (
                self._task_id,
                domain,
                source_page,
                file_url,
                os.path.basename(file_url.split("?")[0]) or file_url[-40:],
            )
            for file_url in new_file_urls
        ]
        try:
            # executemany 在后台单写者内部单事务批量提交，高效且原子
            await db.executemany(_SQL_INSERT_FILE, params_list)
            # INSERT OR IGNORE：无法直接获知每行是否为新增行；
            # 用 DB COUNT 校准计数器（轻量 SELECT，WAL 下并发安全）
            row = await db.query_one(_SQL_COUNT_FILES, (self._task_id,))
            if row is not None:
                self._total_scraped_count = int(row["cnt"])
        except Exception as exc:
            _log.warning(f"[SiteAuditCenter] record_result_batch 写入失败: {exc!r}")

    async def record_interaction(
        self,
        domain: str,
        interaction_data: dict,
    ) -> None:
        """
        Hook 5：记录 DOM 交互行为（由 Interactor / ActionHandler 调用）。
        当前 schema.py 中尚未定义 interactions 表；
        此 Hook 以 DEBUG 日志记录数据并静默跳过 DB 写入，
        待 interactions 表 DDL 加入 schema.py 后可直接激活 INSERT 语句。

        Args:
            domain          : 目标域名。
            interaction_data: 交互记录字典（含 url / action / description 等字段）。
        """
        # TODO: interactions 表 DDL 需在 schema.py 中补充后解注释以下写入逻辑
        _log.debug(
            f"[SiteAuditCenter] interaction @ {domain}: "
            f"url={interaction_data.get('url')} "
            f"action={interaction_data.get('action')}"
        )
        if self._realtime_exporter and self._task_id is not None:
            try:
                await self._realtime_exporter.append_interaction_jsonl(
                    domain, interaction_data
                )
            except Exception as exc:
                _log.warning(f"[SiteAuditCenter] 实时 interactions.jsonl 写入失败: {exc!r}")

    async def export_final_reports(self) -> None:
        """
        任务收尾 API：从 DB 导出 manifest.json 快照文件，供外部工具读取。
        由 SiteCrawlStrategy.cleanup() 在所有队列清空后调用。

        导出格式（兼容 core/ 版本）：
            [{"source_page": url, "file_urls": [url1, url2, ...]}, ...]
        写入路径：{base_save_dir}/{strategy_prefix}_{domain}/manifest.json
        使用 aiofiles 异步写入，不阻塞事件循环。
        """
        if self._task_id is None:
            _log.warning("[SiteAuditCenter] export_final_reports: task_id 未设置，跳过导出")
            return

        db = await self._get_conn()
        try:
            domain_rows = await db.query(
                "SELECT DISTINCT domain FROM downloaded_files WHERE task_id = ?",
                (self._task_id,),
            )
        except Exception as exc:
            _log.error(f"[SiteAuditCenter] export_final_reports 查询失败: {exc!r}")
            return

        for domain_row in domain_rows:
            domain: str = domain_row["domain"]
            workspace = self._get_workspace(domain)
            manifest_path = os.path.join(workspace, "manifest.json")

            try:
                file_rows = await db.query(
                    "SELECT source_page, file_url FROM downloaded_files "
                    "WHERE task_id = ? AND domain = ? ORDER BY source_page",
                    (self._task_id, domain),
                )
            except Exception as exc:
                _log.error(
                    f"[SiteAuditCenter] export_final_reports 查询 {domain} 失败: {exc!r}"
                )
                continue

            # 按 source_page 聚合 file_url
            by_page: Dict[str, List[str]] = defaultdict(list)
            for r in file_rows:
                by_page[r["source_page"]].append(r["file_url"])

            manifest = [
                {"source_page": page, "file_urls": urls}
                for page, urls in by_page.items()
            ]

            try:
                async with aiofiles.open(manifest_path, "w", encoding="utf-8") as fh:
                    await fh.write(json.dumps(manifest, ensure_ascii=False, indent=2))
                _log.info(
                    f"[SiteAuditCenter] manifest 已导出 → {manifest_path} "
                    f"（{len(file_rows)} 条记录）"
                )
            except Exception as exc:
                _log.error(
                    f"[SiteAuditCenter] manifest 写入失败 {manifest_path}: {exc!r}"
                )

    # ------------------------------------------------------------------
    # 私有：数据库连接
    # ------------------------------------------------------------------

    async def _get_conn(self) -> "DatabaseManager":
        """
        获取 DatabaseManager 单例（get_db(self._db_path) 懒加载，进程内通常共用一个库文件）。

        DML 经写入队列串行落盘；SELECT 不经队列。WAL / 外键在 DatabaseManager.initialize()
        中配置。关闭与排空队列由应用生命周期（如 get_db().close()）负责，而非 SiteAuditCenter。

        Returns:
            已初始化且后台 writer 任务已运行的 DatabaseManager 实例。
        """
        from src.db.database import get_db
        return await get_db(self._db_path)

    # ------------------------------------------------------------------
    # 私有：路径管理（黑盒化）
    # ------------------------------------------------------------------

    def _get_workspace(self, domain: str) -> str:
        """
        返回指定域名的物理工作目录路径并确保其存在。
        格式：{base_save_dir}/{strategy_prefix}_{domain}

        Args:
            domain: 目标域名字符串。
        Returns:
            已确保存在的工作目录绝对路径。
        """
        workspace = os.path.join(
            self._base_save_dir,
            f"{self._strategy_prefix}_{domain}",
        )
        os.makedirs(workspace, exist_ok=True)
        return workspace

    # ------------------------------------------------------------------
    # 私有：辅助工具
    # ------------------------------------------------------------------

    def _guard_task_id(self, hook_name: str) -> bool:
        """
        确保 _task_id 已被注入；未注入时打印警告并返回 False，
        避免因 NOT NULL FK 约束导致 DB 写入异常。

        Args:
            hook_name: 调用方 Hook 的名称（仅用于日志）。
        Returns:
            True 表示可继续写入；False 表示应跳过。
        """
        if self._task_id is None:
            _log.warning(
                f"[SiteAuditCenter] {hook_name}: task_id 尚未注入，跳过 DB 写入。"
                " 请在任务行创建后调用 audit_center.set_task_id(task_id)。"
            )
            return False
        return True

    @staticmethod
    def _make_fingerprint(error_type: str, domain: str, url: str) -> str:
        """
        为 error_log 行生成去重指纹（MD5[:12]）。
        基于 (error_type, domain, url) 三元组，保证相同失败来源合并计数。

        Args:
            error_type: 错误类型字符串（如 'scan_failure' / 'download_failure'）。
            domain    : 目标域名。
            url       : 失败的 URL。
        Returns:
            12 位十六进制指纹字符串。
        """
        raw = f"{error_type}|{domain}|{url}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
