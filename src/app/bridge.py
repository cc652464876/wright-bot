"""
@Layer   : App 层（第五层 · 应用编排）
@Role    : pywebview JS ↔ Python API 桥接层
@Pattern : Facade Pattern（将复杂的后端组件聚合为 JS 可调用的简单 API 集合）
@Description:
    PrismAPI 是 pywebview 的 js_api 对象，前端 JS 通过
    window.pywebview.api.xxx() 调用此类的公开方法，实现双向通信。

    职责边界：
    - 所有对 MasterDispatcher / NetworkMonitor / SiteMonitor 的调用
      都必须经由此类中转，前端永远不直接接触后端业务对象。
    - 触发异步操作的方法（start_task / stop_task / request_preview_robots_txt）
      通过 asyncio.run_coroutine_threadsafe(coro, self._loop) 将协程投递到
      后台守护线程中运行的 asyncio 事件循环，彻底避免 pywebview GUI 线程与
      asyncio 事件循环线程之间的跨线程崩溃问题。
    - 纯同步方法（get_dashboard_data / update_proxy 等）直接返回结果，
      不经过事件循环，确保 UI 轮询低延迟。

    create_app() 是模块的唯一公开工厂函数，由 main.py 调用，
    负责组装所有后端组件并返回配置好 js_api 的 pywebview Window 实例。

    Pattern: Facade（API 聚合）+ Thread-Safe Coroutine Dispatch（asyncio 跨线程调度）
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import webview

from src.modules.canary.contracts import CanaryDashboardDict
from src.utils.logger import get_logger

_log_bridge = get_logger(__name__)

if TYPE_CHECKING:
    from src.app.dispatcher import MasterDispatcher
    from src.app.net_monitor import NetworkMonitor


# ---------------------------------------------------------------------------
# 模块级内存日志缓冲区
# ---------------------------------------------------------------------------
# loguru sink 回调将日志条目追加到此双端队列，deque 的 maxlen 上限防止内存膨胀。
# _install_log_sink() 在 create_app() 中调用，时机位于 main.py 已完成
# setup_logger()（包含 logger.remove()）之后，因此 sink 不会被清除。

_LOG_BUFFER: collections.deque = collections.deque(maxlen=1000)
_LOG_SINK_LOCK: threading.Lock  = threading.Lock()   # 保护 _LOG_SINK_INSTALLED
_LOG_SINK_INSTALLED: bool = False

# 金丝雀体检窗口专用日志（与任务看板 _LOG_BUFFER / loguru 内存流完全隔离）
_CANARY_LOG_BUFFER: collections.deque = collections.deque(maxlen=500)
_CANARY_LOG_LOCK = threading.Lock()

# 解析 loguru 文件日志行的正则表达式（用于降级读取日志文件）
# 格式：{YYYY-MM-DD HH:mm:ss.SSS} | {LEVEL:<8} | {name}:{func}:{line} - {message}
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?"
    r"\s*\|\s*(\w+)\s*\|[^-]*-\s*(.+)$"
)


def _install_log_sink() -> None:
    """
    向 loguru 注册内存 sink，将新日志条目实时追加到 _LOG_BUFFER。

    设计约束：
    - 必须在 setup_logger()（包含 logger.remove()）调用之后执行，
      否则新注册的 sink 将被 remove() 清除。create_app() 的调用时机满足此约束。
    - 幂等：多次调用只注册一次。
    - GIL 保护已足够防止 _LOG_BUFFER.append() 的并发问题（deque 是线程安全的），
      _LOG_SINK_LOCK 仅用于保护 _LOG_SINK_INSTALLED 标志的读写。
    """
    global _LOG_SINK_INSTALLED
    with _LOG_SINK_LOCK:
        if _LOG_SINK_INSTALLED:
            return

        from loguru import logger as _loguru

        def _memory_sink(message: Any) -> None:
            record = message.record
            _LOG_BUFFER.append({
                "time":    record["time"].strftime("%H:%M:%S"),
                "level":   record["level"].name,
                "message": record["message"],
            })

        _loguru.add(_memory_sink, level="DEBUG")
        _LOG_SINK_INSTALLED = True


def _read_log_file_tail(limit: int) -> List[Dict[str, str]]:
    """
    从最新的 loguru 日志文件中读取最后 `limit` 条可解析的日志条目。
    供 _LOG_BUFFER 为空（应用刚启动还未记录日志）时降级使用。

    Returns:
        日志条目列表（最新条目在前），每条含 time / level / message 字段。
    """
    try:
        from src.config.settings import get_app_config
        log_dir = Path(get_app_config().log_dir)
        if not log_dir.exists():
            return []

        # 按文件名排序取最新一个（格式 app_YYYY-MM-DD.log）
        log_files = sorted(log_dir.glob("app_*.log"), reverse=True)
        if not log_files:
            return []

        entries: List[Dict[str, str]] = []
        with open(log_files[0], encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                m = _LOG_LINE_RE.match(raw_line.rstrip())
                if m:
                    dt, level, message = m.groups()
                    entries.append({
                        "time":    dt[11:],          # 取 HH:MM:SS 部分
                        "level":   level.strip(),
                        "message": message.strip(),
                    })

        # 返回最新 `limit` 条，且保持"最新在前"顺序
        return entries[-limit:][::-1]

    except Exception:
        _log_bridge.debug("读取日志文件尾部失败", exc_info=True)
        return []


def _append_canary_log(level: str, message: str) -> None:
    """写入金丝雀专属缓冲区（仅供 fetch_canary_logs / 体检探针使用）。"""
    entry = {
        "time":    datetime.now().strftime("%H:%M:%S"),
        "level":   (level or "INFO").upper(),
        "message": message,
    }
    with _CANARY_LOG_LOCK:
        _CANARY_LOG_BUFFER.append(entry)


def _snapshot_canary_logs(limit: int) -> List[Dict[str, str]]:
    """返回最新 ``limit`` 条，顺序为最新在前（与 get_log_entries 一致）。"""
    with _CANARY_LOG_LOCK:
        snap = list(_CANARY_LOG_BUFFER)
    if not snap:
        return []
    return snap[-limit:][::-1]


def _build_canary_dashboard_payload(dispatcher: Any) -> CanaryDashboardDict:
    """
    组装金丝雀 JSON 契约：合并内存象限态 + 调度器占用语义。
    - idle: 无任务
    - running: 金丝雀合成任务执行中
    - locked: 普通爬取任务占用调度器
    """
    from src.config.settings import get_settings
    from src.modules.canary.dashboard import build_payload

    engine = str(get_settings().stealth.stealth_engine)
    return build_payload(
        dispatcher_running=dispatcher.is_task_running(),
        is_canary_active=dispatcher.is_canary_run_active(),
        current_engine=engine,
    )


# ---------------------------------------------------------------------------
# PrismAPI
# ---------------------------------------------------------------------------

class PrismAPI:
    """
    pywebview JS API 桥接对象（Facade）。

    所有公开方法均可从前端 JS 通过
    window.pywebview.api.<method_name>(...) 直接调用。
    返回值须为 JSON 可序列化类型（dict / list / str / int / float / bool / None）。

    跨线程调度说明：
        pywebview 在 GUI 线程回调此类方法；凡需触发 asyncio 协程的方法，
        必须通过 asyncio.run_coroutine_threadsafe(coro, self._loop) 投递，
        严禁在此线程中直接 asyncio.run() 或 loop.run_until_complete()。

    Pattern: Facade —— 将 Dispatcher / NetMonitor / DB 聚合为统一 API 接口。
    """

    def __init__(
        self,
        dispatcher: "MasterDispatcher",
        net_monitor: "NetworkMonitor",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """
        Args:
            dispatcher : MasterDispatcher 实例（任务调度核心）。
            net_monitor: NetworkMonitor 实例（网速 / 延迟监控）。
            loop       : 后台 asyncio 守护线程中运行的事件循环引用，
                         供 run_coroutine_threadsafe 跨线程调度使用。
        """
        self._dispatcher = dispatcher
        self._net_monitor = net_monitor
        self._loop = loop
        self._monitor_window: Any = None
        self._canary_window: Any = None
        self._main_window: Any = None

    def bind_main_window(self, window: Any) -> None:
        """主窗口创建后注入，供 robots 异步回调等非阻塞 evaluate_js 使用。"""
        self._main_window = window

    def _notify_main_panel_button_inactive(self, panel: str) -> None:
        """子窗口关闭（含原生 X）后通知主界面将 Log / Canary 按钮重置为非激活样式。"""
        main = self._main_window
        if main is None:
            return
        try:
            token = json.dumps(panel)
            main.evaluate_js(
                f"window.setPanelButtonInactive && window.setPanelButtonInactive({token})"
            )
        except Exception:
            _log_bridge.debug(
                "主面板按钮状态通知 evaluate_js 失败 panel=%r", panel, exc_info=True
            )

    def raise_monitor_window(self) -> None:
        """若日志看板窗口仍存在，则恢复并提到前台；不创建新窗口。"""
        try:
            w = self._monitor_window
            if w is None or w not in webview.windows:
                return
            try:
                w.restore()
            except Exception:
                _log_bridge.debug("监控窗口 restore 失败", exc_info=True)
            try:
                w.show()
            except Exception:
                _log_bridge.debug("监控窗口 show 失败", exc_info=True)
        except Exception:
            _log_bridge.debug("raise_monitor_window 失败", exc_info=True)

    def raise_canary_window(self) -> None:
        """若金丝雀窗口仍存在，则恢复并提到前台；不创建新窗口。"""
        try:
            w = self._canary_window
            if w is None or w not in webview.windows:
                return
            try:
                w.restore()
            except Exception:
                _log_bridge.debug("金丝雀窗口 restore 失败", exc_info=True)
            try:
                w.show()
            except Exception:
                _log_bridge.debug("金丝雀窗口 show 失败", exc_info=True)
        except Exception:
            _log_bridge.debug("raise_canary_window 失败", exc_info=True)

    # ------------------------------------------------------------------
    # 任务控制 API（前端按钮触发）
    # ------------------------------------------------------------------

    def start_task(self, config_json: str) -> Dict[str, Any]:
        """
        前端「开始」按钮触发：将 UI 配置 JSON 字符串解析后投递给 Dispatcher 执行。

        通过 asyncio.run_coroutine_threadsafe() 在后台事件循环中非阻塞提交协程，
        立即返回给 JS 调用方；任务实际在后台事件循环中异步执行。

        Args:
            config_json: 前端 collectUiConfig() 返回的 JSON 字符串。
                task_info.enable_realtime_jsonl_export（可选，默认 false）：
                为 true 时站点策略启用 RealtimeFileExporter，向各域名工作区实时追加
                scanned_urls.jsonl / scan_errors_log.txt / interactions.jsonl。
        Returns:
            {'success': bool, 'message': str}
        """
        try:
            config_dict = json.loads(config_json)
        except json.JSONDecodeError as exc:
            return {"success": False, "message": f"配置 JSON 解析失败: {exc}"}

        asyncio.run_coroutine_threadsafe(
            self._dispatcher.run_with_config(config_dict),
            self._loop,
        )
        return {"success": True, "message": "任务已提交至后台事件循环"}

    def stop_task(self) -> Dict[str, Any]:
        """
        前端「停止」按钮触发：向 Dispatcher 发送停止信号（非阻塞投递）。

        Returns:
            {'success': bool, 'message': str}
        """
        asyncio.run_coroutine_threadsafe(self._dispatcher.stop(), self._loop)
        return {"success": True, "message": "已向 Dispatcher 发送停止信号"}

    def get_status(self) -> str:
        """
        UI 心跳（ui-status.js）：与后端调度器任务占用状态对齐。

        Returns:
            ``'running'`` 当 MasterDispatcher 正在执行任务；否则 ``'idle'``。
        """
        return "running" if self._dispatcher.is_task_running() else "idle"

    # ------------------------------------------------------------------
    # 监控数据 API（UI 定时轮询）
    # ------------------------------------------------------------------

    def get_dashboard_data(self) -> Dict[str, Any]:
        """
        UI 仪表盘定时轮询接口（约每 1s 被 JS 调用一次）。
        同步调用 Dispatcher.get_dashboard_data()，无 I/O，保证低延迟。

        Returns:
            包含所有仪表盘字段的数据字典（JSON 可序列化）。
        """
        return self._dispatcher.get_dashboard_data()

    # ------------------------------------------------------------------
    # 代理设置 API
    # ------------------------------------------------------------------

    def update_proxy(self, mode: str, ip: str = "") -> None:
        """
        前端代理切换触发：校验代理格式后更新 NetworkMonitor 的代理模式。

        校验规则：
        - mode 必须为 'A'（跟随系统）/ 'B'（直连）/ 'C'（手动节点）之一；
          其他值静默忽略，不抛出异常（防止前端传入脏数据使监控线程崩溃）。
        - mode='C' 时 ip 不能为空，且须符合 host:port 格式
          （允许带 scheme，如 http://1.2.3.4:8080 或纯 1.2.3.4:8080）。

        Args:
            mode: 代理模式 'A'（自动）/ 'B'（直连）/ 'C'（手动节点）。
            ip  : 手动节点 IP:PORT 字符串（mode='C' 时有效）。
        """
        _VALID_MODES = frozenset({"A", "B", "C"})
        mode = (mode or "").strip().upper()

        if mode not in _VALID_MODES:
            return  # 非法 mode：静默拒绝，不抛异常

        ip = (ip or "").strip()

        if mode == "C":
            # 手动节点：ip 不能为空且须含端口分隔符
            # 允许格式：'1.2.3.4:1080' / 'http://1.2.3.4:1080'
            check = ip.split("://")[-1] if "://" in ip else ip
            if not check or ":" not in check:
                # 格式不合法：静默拒绝，不更新（防止写入损坏的代理地址）
                return

        # 热更新 NetworkMonitor（影响延迟探针的 HTTP 请求代理）
        self._net_monitor.update_proxy_settings(mode, ip)

    # ------------------------------------------------------------------
    # 文件系统工具 API
    # ------------------------------------------------------------------

    def open_folder_dialog(self) -> Optional[str]:
        """
        打开系统文件夹选择对话框，供用户选择保存路径。
        通过 webview.windows[0].create_file_dialog() 实现原生对话框。

        此方法在 pywebview GUI 线程中同步执行，webview 的对话框 API
        要求在 GUI 线程调用，因此无需投递到 asyncio 事件循环。

        Returns:
            用户选择的文件夹绝对路径；取消选择时返回 None。
        """
        try:
            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if result and len(result) > 0:
                return str(result[0])
            return None
        except Exception:
            _log_bridge.debug("open_folder_dialog 失败", exc_info=True)
            return None

    def select_folder(self) -> Optional[str]:
        """JS 侧 `select_folder()` 与 `open_folder_dialog()` 同义（ui-main.js）。"""
        return self.open_folder_dialog()

    def select_file(self, extensions: str = "") -> Optional[str]:
        """
        打开系统文件选择对话框。`extensions` 为逗号分隔后缀，如 ``json,txt,xlsx``。

        Returns:
            所选文件的绝对路径；取消时返回 None。
        """
        exts = [
            e.strip().lower().lstrip(".")
            for e in (extensions or "").split(",")
            if e.strip()
        ]
        if exts:
            desc = ";;".join(f"{e.upper()} (*.{e})" for e in exts)
            file_types_str = f"{desc};;All files (*.*)"
        else:
            file_types_str = "All files (*.*)"
        try:
            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=(file_types_str,),
            )
            if result and len(result) > 0:
                return str(result[0])
            return None
        except Exception:
            _log_bridge.debug("select_file 对话框失败", exc_info=True)
            return None

    def open_save_directory(self, path: str) -> Dict[str, Any]:
        """
        在操作系统文件管理器中打开指定目录（Windows: explorer / macOS: open / Linux: xdg-open）。

        Args:
            path: 要打开的目录绝对路径。
        Returns:
            {'success': bool, 'message': str}
        """
        if not path or not path.strip():
            return {"success": False, "message": "路径为空"}

        path = path.strip()
        if not os.path.isdir(path):
            return {
                "success": False,
                "message": f"目录不存在或路径无效: {path!r}",
            }

        try:
            if sys.platform == "win32":
                startfile = getattr(os, "startfile", None)
                if callable(startfile):
                    startfile(path)
                else:
                    return {"success": False, "message": "当前环境不支持 os.startfile"}
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                # Linux / FreeBSD / 其他 POSIX 系统
                subprocess.Popen(["xdg-open", path])

            return {"success": True, "message": f"已在文件管理器中打开: {path}"}

        except Exception as exc:
            return {"success": False, "message": f"打开目录失败: {exc!r}"}

    def request_preview_robots_txt(self, url: str, request_id: str) -> Dict[str, Any]:
        """
        非阻塞请求 robots.txt：立即返回，完成后通过 ``evaluate_js`` 调用
        ``window.__resolveRobotsPreview(payload)``，避免在 GUI 线程上使用
        ``future.result()`` 长时间阻塞。

        Args:
            url: 目标网站 URL 或域名。
            request_id: 前端生成的关联 ID，用于 Promise 配对。

        Returns:
            ``{'accepted': bool, 'message': str}`` — 仅表示是否已受理，非抓取结果。
        """
        url = (url or "").strip()
        rid = (request_id or "").strip()
        if not url:
            return {"accepted": False, "message": "URL 不能为空"}
        if not rid:
            return {"accepted": False, "message": "request_id 不能为空"}

        async def _fetch(target_url: str) -> Optional[str]:
            from src.modules.site.generator import SiteUrlGenerator
            gen = SiteUrlGenerator()
            try:
                return await asyncio.wait_for(
                    gen.preview_robots_txt(target_url),
                    timeout=30.0,
                )
            finally:
                await gen.close()

        def _on_done(fut: concurrent.futures.Future) -> None:
            try:
                content: Optional[str] = fut.result()
            except asyncio.TimeoutError:
                self._emit_robots_preview_result(
                    rid, False, None, "请求超时（30s），请检查网络或目标站点可达性",
                )
                return
            except Exception as exc:
                self._emit_robots_preview_result(
                    rid, False, None, f"获取 robots.txt 失败: {exc!r}",
                )
                return

            if content is None:
                self._emit_robots_preview_result(
                    rid,
                    False,
                    None,
                    "无法获取 robots.txt，目标站点可能不可达或不存在该文件",
                )
            else:
                self._emit_robots_preview_result(rid, True, content, "获取成功")

        future = asyncio.run_coroutine_threadsafe(_fetch(url), self._loop)
        try:
            future.add_done_callback(_on_done)
        except Exception as exc:
            return {"accepted": False, "message": f"提交后台任务失败: {exc!r}"}

        return {"accepted": True, "message": "已提交后台抓取"}

    def _emit_robots_preview_result(
        self,
        request_id: str,
        ok: bool,
        content: Optional[str],
        message: str,
    ) -> None:
        """在主窗口上执行 JS 回调；窗口未就绪时静默忽略。"""
        win = self._main_window
        if win is None:
            try:
                wins = getattr(webview, "windows", None)
                if wins and len(wins) > 0:
                    win = wins[0]
            except Exception:
                _log_bridge.debug(
                    "_emit_robots_preview_result 获取 webview.windows 失败",
                    exc_info=True,
                )
                win = None
        if win is None:
            return
        payload = json.dumps(
            {"id": request_id, "ok": ok, "content": content, "message": message},
            ensure_ascii=False,
        )
        try:
            win.evaluate_js(
                f"window.__resolveRobotsPreview && window.__resolveRobotsPreview({payload})"
            )
        except Exception:
            _log_bridge.debug(
                "robots 预览 evaluate_js 失败 request_id=%r", request_id, exc_info=True
            )

    # ------------------------------------------------------------------
    # 日志 API
    # ------------------------------------------------------------------

    def fetch_logs(self, limit: int = 200) -> List[Dict[str, str]]:
        """供 monitor.html 轮询：返回纯列表，与 `get_log_entries` 的 entries 一致。"""
        payload = self.get_log_entries(limit)
        if not payload.get("success"):
            return []
        return list(payload.get("entries") or [])

    def fetch_canary_logs(self, limit: int = 200) -> List[Dict[str, str]]:
        """
        金丝雀窗口专用日志（与全局 loguru / fetch_logs 隔离）。
        仅包含显式写入 _CANARY_LOG_BUFFER 的体检相关条目。
        """
        limit = max(1, min(int(limit) if limit else 200, 500))
        return _snapshot_canary_logs(limit)

    def append_canary_log(self, message: str, level: str = "INFO") -> Dict[str, Any]:
        """前端或探针向金丝雀日志区追加一行（写入独立缓冲区）。"""
        msg = (message or "").strip()
        if not msg:
            return {"success": False, "message": "message 为空"}
        _append_canary_log(level or "INFO", msg)
        return {"success": True}

    def fetch_statistics(self) -> Dict[str, Any]:
        """供 monitor.html 仪表盘轮询；与 `get_dashboard_data` 一致。"""
        return self.get_dashboard_data()

    def fetch_canary_dashboard(self) -> CanaryDashboardDict:
        """
        金丝雀看板轮询（UI/canary.html）。

        拉取金丝雀看板数据（内存态 quadrants + 进度 + 调度占用语义）。
        目前已实装 Sannysoft 探针（Identity/Hardware）；Network 与 Combat 象限已接入
        结构化脚手架（extract/build/run），部分底层特征（如 JA3、完整 WebRTC 泄漏探测）
        处于 warn 待扩展状态。
        """
        return _build_canary_dashboard_payload(self._dispatcher)

    def get_stealth_engine(self) -> str:
        """当前运行时 stealth_engine，与主界面「设备伪装」一致。"""
        from src.config.settings import get_settings

        return str(get_settings().stealth.stealth_engine)

    def set_stealth_engine(self, engine: str) -> Dict[str, Any]:
        """
        从金丝雀窗口更新引擎并尝试同步主窗口 ``#stealth-engine`` 的 sp-picker。
        """
        from src.config.settings import apply_stealth_engine_patch

        engine = (engine or "").strip().lower()
        if not apply_stealth_engine_patch(engine):
            return {"success": False, "message": f"非法引擎值: {engine!r}"}

        win = self._main_window
        if win is None:
            try:
                wins = getattr(webview, "windows", None)
                if wins and len(wins) > 0:
                    win = wins[0]
            except Exception:
                _log_bridge.debug("set_stealth_engine 获取 webview.windows 失败", exc_info=True)
                win = None
        if win is not None:
            try:
                safe = json.dumps(engine)
                win.evaluate_js(
                    f"(function(){{var el=document.getElementById('stealth-engine');"
                    f"if(el){{el.value={safe};}}}})();"
                )
            except Exception:
                _log_bridge.debug(
                    "set_stealth_engine 同步主窗 #stealth-engine 失败", exc_info=True
                )
        return {"success": True, "message": "已更新 stealth_engine"}

    def run_canary_checkup(self) -> Dict[str, Any]:
        """
        「运行体检」：组装 is_canary=True 的合成站点任务，走 MasterDispatcher 主通路。
        """
        if self._dispatcher.is_task_running():
            _append_canary_log("WARNING", "[异常] 调度器占用中，无法启动金丝雀合成任务")
            return {
                "success": False,
                "message": "调度器占用中（请先结束当前任务后再运行体检）",
            }
        from src.config.settings import get_settings
        from src.modules.canary.dashboard import reset_for_new_run
        from src.modules.canary.strategy import DEFAULT_CANARY_SEED_URLS

        reset_for_new_run()
        _append_canary_log("INFO", "[系统] 已提交金丝雀合成任务（与主线路共享 Crawlee 与反爬栈）")

        base = get_settings().model_dump()
        task_info = dict(base.get("task_info") or {})
        task_info["mode"] = "site"
        task_info["is_canary"] = True
        task_info["task_name"] = f"Canary_Checkup_{int(time.time() * 1000)}"
        task_info["max_pdf_count"] = max(len(DEFAULT_CANARY_SEED_URLS) + 10, 50)
        task_info["enable_realtime_jsonl_export"] = False

        strat = dict(base.get("strategy_settings") or {})
        strat["crawl_strategy"] = "direct"
        strat["target_urls"] = list(DEFAULT_CANARY_SEED_URLS)

        perf = dict(base.get("performance") or {})
        perf["max_concurrency"] = 1

        config_dict = {
            **base,
            "task_info": task_info,
            "strategy_settings": strat,
            "performance": perf,
        }

        asyncio.run_coroutine_threadsafe(
            self._dispatcher.run_with_config(config_dict),
            self._loop,
        )
        return {
            "success": True,
            "message": "金丝雀合成任务已提交至后台事件循环",
        }

    def toggle_canary_window(self) -> bool:
        """
        打开或关闭金丝雀体检窗口（UI/canary.html）。

        Returns:
            True 表示窗口已打开；False 表示已关闭。
        """
        ui_dir = Path(__file__).resolve().parent.parent.parent / "UI"
        canary_path = ui_dir / "canary.html"

        try:
            if self._canary_window is not None:
                try:
                    alive = self._canary_window in webview.windows
                except Exception:
                    _log_bridge.debug("金丝雀窗口存活检测失败", exc_info=True)
                    alive = False
                if not alive:
                    self._canary_window = None

            if self._canary_window is not None:
                try:
                    self._canary_window.destroy()
                except Exception:
                    _log_bridge.debug("金丝雀窗口 destroy 失败", exc_info=True)
                self._canary_window = None
                return False

            def _on_closed() -> None:
                self._canary_window = None
                self._notify_main_panel_button_inactive("canary")

            win = webview.create_window(
                title="PrismPDF · 金丝雀体检",
                url=str(canary_path),
                js_api=self,
                width=920,
                height=720,
            )
            if win is None:
                _log_bridge.error("金丝雀窗口 webview.create_window 返回 None")
                self._canary_window = None
                return False
            events = getattr(win, "events", None)
            if events is None:
                _log_bridge.error("金丝雀窗口缺少 events，无法注册 closed 回调")
                self._canary_window = None
                return False
            try:
                events.closed += _on_closed
            except Exception:
                _log_bridge.debug("金丝雀窗口注册 closed 回调失败", exc_info=True)
            self._canary_window = win
            _append_canary_log("INFO", "[系统] 金丝雀专属日志通道已就绪（与任务看板日志隔离）")
            return True
        except Exception:
            _log_bridge.debug("toggle_canary_window 失败", exc_info=True)
            self._canary_window = None
            return False

    def toggle_monitor_window(self) -> bool:
        """
        打开或关闭独立日志监控窗口（UI/monitor.html）。

        Returns:
            True 表示监控窗口已打开；False 表示已关闭。
        """
        ui_dir = Path(__file__).resolve().parent.parent.parent / "UI"
        monitor_path = ui_dir / "monitor.html"

        try:
            if self._monitor_window is not None:
                try:
                    alive = self._monitor_window in webview.windows
                except Exception:
                    _log_bridge.debug("监控窗口存活检测失败", exc_info=True)
                    alive = False
                if not alive:
                    self._monitor_window = None

            if self._monitor_window is not None:
                try:
                    self._monitor_window.destroy()
                except Exception:
                    _log_bridge.debug("监控窗口 destroy 失败", exc_info=True)
                self._monitor_window = None
                return False

            def _on_closed() -> None:
                self._monitor_window = None
                self._notify_main_panel_button_inactive("log")

            win = webview.create_window(
                title="PrismPDF · 日志",
                url=str(monitor_path),
                js_api=self,
                width=900,
                height=640,
            )
            if win is None:
                _log_bridge.error("监控窗口 webview.create_window 返回 None")
                self._monitor_window = None
                return False
            events = getattr(win, "events", None)
            if events is None:
                _log_bridge.error("监控窗口缺少 events，无法注册 closed 回调")
                self._monitor_window = None
                return False
            try:
                events.closed += _on_closed
            except Exception:
                _log_bridge.debug("监控窗口注册 closed 回调失败", exc_info=True)
            self._monitor_window = win
            return True
        except Exception:
            _log_bridge.debug("toggle_monitor_window 失败", exc_info=True)
            self._monitor_window = None
            return False

    def get_log_entries(self, limit: int = 200) -> Dict[str, Any]:
        """
        获取最近 N 条日志记录供 UI 日志面板展示。

        读取策略（优先级递减）：
        1. 内存缓冲区 _LOG_BUFFER（由 _install_log_sink() 实时填充）：
           实时性最高，包含应用启动后到现在的所有日志条目。
        2. 降级读取最新日志文件尾部（_LOG_BUFFER 为空时，如应用刚启动 / sink 未安装）：
           扫描 {log_dir}/app_*.log，解析最后 `limit` 行。

        Args:
            limit: 最多返回的日志条数（上限 500 防止 JSON 负载过大）。
        Returns:
            {'success': bool, 'entries': List[dict]}
            每条 entry：{'time': str, 'level': str, 'message': str}
        """
        limit = max(1, min(limit, 500))  # 上限 500 防止 JSON 负载过大

        try:
            # ── 优先从内存缓冲区读取 ──────────────────────────────────
            # _LOG_BUFFER 是 deque，append 是线程安全的（CPython GIL）；
            # list() 创建快照防止迭代期间被修改
            snapshot = list(_LOG_BUFFER)

            if snapshot:
                # 返回最新 `limit` 条，且以"最新在前"顺序排列
                entries = snapshot[-limit:][::-1]
                return {"success": True, "entries": entries}

            # ── 降级：从日志文件读取（缓冲区尚无内容） ────────────────
            entries = _read_log_file_tail(limit)
            return {"success": True, "entries": entries}

        except Exception as exc:
            return {
                "success": False,
                "entries": [],
                "message": f"读取日志失败: {exc!r}",
            }


# ---------------------------------------------------------------------------
# 工厂函数（替代原 monitor_app.create_app）
# ---------------------------------------------------------------------------

def create_app() -> webview.Window:
    """
    应用工厂函数（唯一公开入口，由 main.py 调用）。

    组装流程：
    1. 创建新 asyncio 事件循环，通过守护线程持续运行（loop.run_forever()）。
       守护线程确保主进程退出时自动终止，无需手动清理。
    2. 实例化 MasterDispatcher、NetworkMonitor。
    3. 创建 PrismAPI 桥接对象，注入事件循环引用供跨线程调度使用。
    4. 定位 UI/index.html，创建 pywebview Window（js_api=api）。
    5. 注册 window.events.loaded 回调：通过 run_coroutine_threadsafe
       在后台事件循环中启动 NetworkMonitor。
    6. 返回 Window 实例供 main.py 调用 webview.start()。

    Returns:
        配置完毕的 pywebview Window 实例。
    """
    # 本地导入避免循环依赖（dispatcher / net_monitor 同属 src.app 包）
    from src.app.dispatcher import MasterDispatcher
    from src.app.net_monitor import NetworkMonitor

    # ── 1. 启动后台 asyncio 事件循环守护线程 ─────────────────────────────
    loop = asyncio.new_event_loop()
    _loop_thread = threading.Thread(
        target=_run_event_loop,
        args=(loop,),
        daemon=True,
        name="asyncio-event-loop",
    )
    _loop_thread.start()

    # ── 2. 实例化后端核心组件 ────────────────────────────────────────────
    dispatcher = MasterDispatcher()
    net_monitor = NetworkMonitor()

    # ── 3. 创建 JS API 桥接对象（注入事件循环引用）──────────────────────
    api = PrismAPI(dispatcher=dispatcher, net_monitor=net_monitor, loop=loop)

    # ── 4. 定位 UI 目录并创建 pywebview Window ───────────────────────────
    ui_path = Path(__file__).resolve().parent.parent.parent / "UI" / "index.html"
    window = webview.create_window(
        title="PrismPDF",
        url=str(ui_path),
        js_api=api,
        width=1200,
        height=800,
        min_size=(900, 600),
    )
    if window is None:
        raise RuntimeError("主窗口 webview.create_window 返回 None，无法启动应用")
    api.bind_main_window(window)

    # ── 5. 页面加载完成后启动 NetworkMonitor（注入主窗口以便 evaluate_js） ─
    def _on_loaded() -> None:
        asyncio.run_coroutine_threadsafe(net_monitor.start(window=window), loop)

    main_events = getattr(window, "events", None)
    if main_events is None:
        raise RuntimeError("主窗口缺少 events，无法注册 loaded 回调")
    main_events.loaded += _on_loaded

    # ── 6. 安装内存日志 sink（必须在 setup_logger() / logger.remove() 之后调用）
    #       此时 main.py 已完成日志初始化，sink 不会被清除。
    _install_log_sink()

    return window


def _run_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """
    在守护线程中持续运行 asyncio 事件循环（供 create_app 内部使用）。

    通过 asyncio.set_event_loop(loop) 将此循环绑定为当前线程的默认循环，
    使得在此线程内创建的协程和 Task 均归属于同一循环实例。
    loop.run_forever() 阻塞直到 loop.stop() 被调用（应用退出时触发）。

    Args:
        loop: 待运行的 asyncio 事件循环实例。
    """
    asyncio.set_event_loop(loop)
    loop.run_forever()
