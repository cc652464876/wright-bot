"""
@Layer   : Modules / site / audit
@Role    : 可选的「实时文件流」导出器（与 DB 主存储双写，供旧 UI tail 使用）
@Pattern : Delegation —— 由 SiteAuditCenter 持有并转发，避免审计类无限膨胀
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime
from typing import DefaultDict, Set

import aiofiles

from src.utils.logger import get_logger

_log = get_logger(__name__)


class RealtimeFileExporter:
    """
    在开启 task_info.enable_realtime_jsonl_export 时，向各域名工作区追加：

    - scanned_urls.jsonl   — 与旧版一致的行 JSON（success/failed 带去重）
    - scan_errors_log.txt  — 每次页面扫描失败追加一行文本（不去重）
    - interactions.jsonl   — **新契约**：每行一个 JSON 对象（非旧版 interactions.json 数组）

    并发：按 domain 使用单一 asyncio.Lock，避免同目录下多文件交错写。
    """

    def __init__(self, base_save_dir: str, strategy_prefix: str) -> None:
        self._base_save_dir = base_save_dir
        self._strategy_prefix = strategy_prefix
        self._domain_locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._scanned_urls_seen: DefaultDict[str, Set[str]] = defaultdict(set)

    def _workspace(self, domain: str) -> str:
        path = os.path.join(
            self._base_save_dir,
            f"{self._strategy_prefix}_{domain}",
        )
        os.makedirs(path, exist_ok=True)
        return path

    async def append_scanned_url_if_new(
        self,
        domain: str,
        url: str,
        *,
        ok: bool,
        status_code: int,
    ) -> None:
        """若该 domain 下 url 首次记录，则追加一行到 scanned_urls.jsonl。"""
        async with self._domain_locks[domain]:
            if url in self._scanned_urls_seen[domain]:
                return
            self._scanned_urls_seen[domain].add(url)
            record = {
                "url": url,
                "status": "success" if ok else "failed",
                "code": status_code,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            path = os.path.join(self._workspace(domain), "scanned_urls.jsonl")
            line = json.dumps(record, ensure_ascii=False) + "\n"
            await self._append_bytes(path, line)

    async def append_scan_error_log(
        self,
        domain: str,
        url: str,
        status_code: int,
        error_msg: str,
    ) -> None:
        """每次扫描失败追加一行（与旧版一致：与 jsonl 去重独立）。"""
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{time_str}] URL: {url} | Status: {status_code} | Error: {error_msg}"
        async with self._domain_locks[domain]:
            path = os.path.join(self._workspace(domain), "scan_errors_log.txt")
            await self._append_bytes(path, log_msg + "\n")

    async def append_interaction_jsonl(
        self,
        domain: str,
        interaction_data: dict,
    ) -> None:
        """
        追加一行 JSONL。**文件名：interactions.jsonl**（非旧版 interactions.json）。
        行内包含 domain 字段便于单文件 tail。
        """
        payload = {"domain": domain, **dict(interaction_data)}
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._domain_locks[domain]:
            path = os.path.join(self._workspace(domain), "interactions.jsonl")
            await self._append_bytes(path, line)

    async def _append_bytes(self, file_path: str, data: str) -> None:
        try:
            async with aiofiles.open(file_path, "a", encoding="utf-8") as fh:
                await fh.write(data)
        except PermissionError:
            _log.warning(
                "[RealtimeFileExporter] 文件被占用，跳过写入: {}",
                os.path.basename(file_path),
            )
        except OSError as exc:
            _log.warning(
                "[RealtimeFileExporter] 写入失败 {}: {!r}",
                os.path.basename(file_path),
                exc,
            )
