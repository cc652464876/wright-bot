"""
modules.site.audit — 站点审计子包。

导出审计中心、错误注册表及错误拦截装饰器/上下文管理器。
三者共同构成站点爬取的可观测性基础设施。
"""

from src.modules.site.audit.audit_center import SiteAuditCenter
from src.modules.site.audit.error_registry import (
    ErrorRegistry,
    current_error_workspace_resolver,
    error_interceptor,
)
from src.modules.site.audit.realtime_file_exporter import RealtimeFileExporter

__all__ = [
    "SiteAuditCenter",
    "ErrorRegistry",
    "RealtimeFileExporter",
    "current_error_workspace_resolver",
    "error_interceptor",
]
