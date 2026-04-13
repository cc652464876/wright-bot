"""
@Layer   : App 层（第五层 · 应用编排）
@Role    : 系统网络状态实时监控（网速 + 延迟探针）
@Pattern : Active Object Pattern（后台守护线程 + 定时推送） + Proxy Mode（代理模式感知）
@Description:
    NetworkMonitor 在后台守护线程中持续运行两类监控：
    1. 网速监控（每 1s）：通过 psutil.net_io_counters() 差分计算
       上传 / 下载速率（KB/s），推送给 pywebview 窗口的 updateNetSpeedUI JS 函数。
    2. 延迟探针（每 3s）：向百度（国内连通性）和 Google generate_204（国际连通性）
       发送 HTTP 探测请求，测量往返延迟（ms），推送给 updateNetStatusUI JS 函数。
    支持三种代理模式（来自 UI 设置）：
    - 'A'（自动/跟随系统 VPN）: 使用默认系统代理
    - 'B'（直连）             : 强制不走代理（ProxyHandler({})）
    - 'C'（手动节点）         : 使用用户指定的 proxy_ip
    psutil 和 urllib.request 均采用懒加载（在线程内部导入），
    避免主线程启动时的重型 C 扩展加载开销。

    Pattern: Active Object（后台守护线程）+ Proxy Mode（三种代理感知）
"""

from __future__ import annotations

import threading
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import webview


class NetworkMonitor:
    """
    系统网络状态实时监控器（后台守护线程）。

    start() 启动后台线程，stop() 设置退出标志。
    线程内部的 _monitor_loop() 持续运行，通过 window.evaluate_js()
    将速率和延迟数据推送到 pywebview 前端。
    """

    def __init__(self, window: Optional["webview.Window"] = None) -> None:
        """
        Args:
            window: pywebview Window 实例（可在 start() 时再传入）。
                    用于调用 window.evaluate_js() 向前端推送数据。
        """
        self._window = window
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None

        # 代理状态（GIL 保护的简单赋值，无需加锁）
        self.proxy_mode: str = "A"   # 'A'=跟随系统 / 'B'=直连 / 'C'=手动节点
        self.proxy_ip:   str = ""    # 仅 mode='C' 时有效，格式 'host:port'

    # ------------------------------------------------------------------
    # 公开：代理设置（由 bridge.py 在 UI 切换代理时调用）
    # ------------------------------------------------------------------

    def update_proxy_settings(self, mode: str, ip: str = "") -> None:
        """
        接收来自前端的代理模式切换，同步更新内部状态。
        线程安全：proxy_mode / proxy_ip 均为简单赋值，GIL 保护足够。

        Args:
            mode: 代理模式字符串，'A'（自动）/ 'B'（直连）/ 'C'（手动节点）。
            ip  : 手动节点 IP:PORT 字符串（mode='C' 时有效）。
        """
        self.proxy_mode = mode
        self.proxy_ip   = ip

    # ------------------------------------------------------------------
    # 公开：线程生命周期
    # ------------------------------------------------------------------

    async def start(self, window: Optional["webview.Window"] = None) -> None:
        """
        启动后台网络监控守护线程（幂等：若已在运行则静默跳过）。

        实现为 async 方法，以便 bridge.py 通过
        asyncio.run_coroutine_threadsafe(net_monitor.start(), loop) 跨线程调度。
        方法体本身为纯同步操作（启动 daemon 线程），无需 await。

        Args:
            window: 可选的 pywebview Window 实例（覆盖构造时传入的值）。
        """
        if window is not None:
            self._window = window

        # 幂等保护：线程已存活则不重复启动
        if self._running and self._thread is not None and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="network-monitor",
        )
        self._thread.start()

    def stop(self) -> None:
        """
        设置停止标志，通知后台线程在下次循环检查时退出。
        非阻塞：不等待线程实际退出。
        """
        self._running = False

    # ------------------------------------------------------------------
    # 私有：后台线程主循环
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """
        后台守护线程主循环。
        1. 懒加载 psutil，获取初始网卡计数器（last_io）。
        2. while _running：每 1s 计算网速差分并推送 updateNetSpeedUI。
        3. 每 3 个 tick 触发一次 _update_latency_status()（在独立守护子线程中执行）。
        初始化失败（psutil 不可用）时打印错误并退出。
        """
        # 懒加载 psutil，避免主线程启动时的 C 扩展加载开销
        try:
            import psutil  # type: ignore[import-untyped]
        except ImportError:
            print("[NetworkMonitor] psutil 不可用，网速监控已停止（pip install psutil）")
            return

        # 获取基准网卡计数器
        try:
            last_io = psutil.net_io_counters()
        except Exception as exc:
            print(f"[NetworkMonitor] 初始化网卡计数器失败，退出监控: {exc!r}")
            return

        tick = 0
        while self._running:
            time.sleep(1.0)
            if not self._running:
                break

            # ── 差分计算网速 ────────────────────────────────────────────
            try:
                curr_io = psutil.net_io_counters()
                up_bytes   = max(0, curr_io.bytes_sent - last_io.bytes_sent)
                down_bytes = max(0, curr_io.bytes_recv - last_io.bytes_recv)
                last_io = curr_io

                up_kb   = round(up_bytes   / 1024.0, 1)
                down_kb = round(down_bytes / 1024.0, 1)
                self._push_net_speed(up_kb, down_kb)
            except Exception:
                pass  # 单次读取失败不应终止整个监控循环

            # ── 每 3 tick 触发一次延迟探针（独立子线程，防止阻塞主循环） ──
            tick += 1
            if tick % 3 == 0:
                latency_thread = threading.Thread(
                    target=self._update_latency_status,
                    daemon=True,
                    name="latency-probe",
                )
                latency_thread.start()

    def _update_latency_status(self) -> None:
        """
        探测国内 + 国际网络延迟并推送到 UI（在独立守护线程中调用，防止阻塞主监控循环）。
        探测目标：
        - 国内：http://www.baidu.com（timeout=1.5s）
        - 国际：http://www.google.com/generate_204（timeout=2.0s）
        调用 window.evaluate_js('updateNetStatusUI(cn_ms, glb_ms)')。
        """
        cn_ms  = self._check_http_latency("http://www.baidu.com",             timeout=1.5)
        glb_ms = self._check_http_latency("http://www.google.com/generate_204", timeout=2.0)

        if self._window is not None:
            try:
                self._window.evaluate_js(f"updateNetStatusUI({cn_ms}, {glb_ms})")
            except Exception:
                pass  # 窗口尚未就绪或已销毁时静默忽略

    def _check_http_latency(self, url: str, timeout: float = 2.0) -> int:
        """
        向目标 URL 发送 HTTP GET 请求，测量响应延迟。
        根据 proxy_mode 动态构建 urllib ProxyHandler：
        - 'A': 不传 ProxyHandler（跟随系统）
        - 'B': ProxyHandler({})（强制直连）
        - 'C': ProxyHandler({'http': ..., 'https': ...})（手动节点）

        Args:
            url    : 探测目标 URL。
            timeout: 超时时间（秒）。
        Returns:
            往返延迟（毫秒整数）；超时或连接失败时返回 -1。
        """
        import urllib.request  # 懒加载，避免主线程冷启动开销

        try:
            # ── 根据代理模式构建 opener ──────────────────────────────────
            if self.proxy_mode == "B":
                # 强制直连：传入空 ProxyHandler 覆盖系统代理
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                )
            elif self.proxy_mode == "C" and self.proxy_ip:
                # 手动节点：规范化 URL 格式后注入
                proxy_url = (
                    self.proxy_ip
                    if "://" in self.proxy_ip
                    else f"http://{self.proxy_ip}"
                )
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({
                        "http":  proxy_url,
                        "https": proxy_url,
                    })
                )
            else:
                # 'A'（自动）或未知模式：跟随系统代理，不注入额外 handler
                opener = urllib.request.build_opener()

            t_start = time.perf_counter()
            opener.open(url, timeout=timeout)
            elapsed_ms = int((time.perf_counter() - t_start) * 1000)
            return elapsed_ms

        except Exception:
            return -1  # 超时 / 连接拒绝 / DNS 失败均统一返回 -1

    def _push_net_speed(self, up_kb: float, down_kb: float) -> None:
        """
        将网速数据通过 evaluate_js 推送到 pywebview 前端。
        调用：window.updateNetSpeedUI(up_kb, down_kb)

        Args:
            up_kb  : 上传速率（KB/s，保留 1 位小数）。
            down_kb: 下载速率（KB/s，保留 1 位小数）。
        """
        if self._window is not None:
            try:
                self._window.evaluate_js(f"updateNetSpeedUI({up_kb}, {down_kb})")
            except Exception:
                pass  # 窗口尚未就绪或已销毁时静默忽略
