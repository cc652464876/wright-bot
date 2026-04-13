"""
@Layer   : Modules 层（site · audit）
@Role    : 策略层可替换的审计工作区契约（Protocol）
@Description:
    SiteCrawlStrategy 与金丝雀桩共用同一套类型注解，避免具体类与轻量桩
    在继承树上的赋值冲突；SiteAuditCenter 以结构子类型满足本协议。
"""

from __future__ import annotations

from typing import Protocol


class AuditWorkspaceProvider(Protocol):
    """站点策略在 run / cleanup / handler 中依赖的最小审计面。"""

    def _get_workspace(self, domain: str) -> str: ...

    async def export_final_reports(self) -> None: ...

    def get_total_scraped_count(self) -> int: ...

    def set_task_id(self, task_id: int) -> None: ...

    async def record_page_success(
        self,
        domain: str,
        url: str,
        status_code: int = 200,
    ) -> None: ...

    async def record_page_failure(
        self,
        domain: str,
        url: str,
        status_code: int,
        error_msg: str,
    ) -> None: ...

    async def record_interaction(self, domain: str, interaction_data: dict) -> None: ...

    async def record_result_batch(
        self,
        domain: str,
        source_page: str,
        new_file_urls: list[str],
    ) -> None: ...
