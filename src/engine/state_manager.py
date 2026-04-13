"""
@Layer   : Engine 层（第三层 · 引擎基础设施）
@Role    : 爬虫运行时状态机
@Pattern : State Machine Pattern（有限状态机 FSM） + Observer Pattern（状态变更监听器）
@Description:
    将爬虫运行期间的复杂状态流转（IDLE → INITIALIZING → RUNNING → CHALLENGE → BANNED 等）
    显式建模为有限状态机，替代原有散落在各模块的 is_running / is_stopped 布尔标志。
    CrawlerState 枚举定义全部合法状态；
    VALID_TRANSITIONS 字典集中声明合法的状态转移边，非法转移直接抛出 StateTransitionError；
    CrawlerStateManager 作为 FSM 执行器，持有当前状态并在每次转移时
    通知所有已注册的监听器（Observer Pattern），
    可驱动 UI 更新、anti_bot 策略切换或日志记录等横切关注点。
"""

from __future__ import annotations

import asyncio
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Set

from src.utils.logger import get_logger

_fsm_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# 状态枚举
# ---------------------------------------------------------------------------

class CrawlerState(Enum):
    """
    爬虫运行时状态枚举（FSM 状态集合）。

    状态语义：
    - IDLE         : 初始态，无任务在运行。
    - INITIALIZING : 正在加载配置、生成种子 URL、构建引擎。
    - RUNNING      : 正常抓取中。
    - CHALLENGE    : 遭遇反爬挑战（5s 盾 / CAPTCHA），正在尝试解决。
    - BANNED       : IP 被封禁，等待代理轮换后重试。
    - PAUSED       : 用户主动暂停（保留现场，可恢复）。
    - STOPPING     : 收到停止信号，正在等待下载队列清空。
    - STOPPED      : 已完全停止，资源已释放。
    - ERROR        : 不可恢复的致命错误，需要人工介入。
    """

    IDLE = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    CHALLENGE = auto()
    BANNED = auto()
    PAUSED = auto()
    STOPPING = auto()
    STOPPED = auto()
    ERROR = auto()


# ---------------------------------------------------------------------------
# 合法转移表（FSM 边集合）
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: Dict[CrawlerState, Set[CrawlerState]] = {
    CrawlerState.IDLE: {
        CrawlerState.INITIALIZING,
    },
    CrawlerState.INITIALIZING: {
        CrawlerState.RUNNING,
        CrawlerState.ERROR,
    },
    CrawlerState.RUNNING: {
        CrawlerState.CHALLENGE,
        CrawlerState.BANNED,
        CrawlerState.PAUSED,
        CrawlerState.STOPPING,
        CrawlerState.ERROR,
    },
    CrawlerState.CHALLENGE: {
        CrawlerState.RUNNING,    # 挑战解决，恢复正常
        CrawlerState.BANNED,     # 挑战解决失败，升级为封禁
        CrawlerState.ERROR,
    },
    CrawlerState.BANNED: {
        CrawlerState.RUNNING,    # 代理切换成功，恢复抓取
        CrawlerState.STOPPING,   # 代理耗尽，放弃任务
        CrawlerState.ERROR,
    },
    CrawlerState.PAUSED: {
        CrawlerState.RUNNING,    # 用户恢复
        CrawlerState.STOPPING,   # 用户停止
    },
    CrawlerState.STOPPING: {
        CrawlerState.STOPPED,
    },
    CrawlerState.STOPPED: {
        CrawlerState.IDLE,       # 任务结束后重置，准备接受下一个任务
    },
    CrawlerState.ERROR: {
        CrawlerState.IDLE,       # 手动清理后重置
        CrawlerState.STOPPING,   # 尝试优雅退出
    },
}

# 状态变更监听器的类型别名：(old_state, new_state, reason) -> None
StateListener = Callable[[CrawlerState, CrawlerState, str], None]


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------

class StateTransitionError(Exception):
    """
    非法状态转移异常。
    当 transition_to() 的目标状态不在当前状态的 VALID_TRANSITIONS 集合中时抛出。
    """
    pass


# ---------------------------------------------------------------------------
# FSM 执行器
# ---------------------------------------------------------------------------

class CrawlerStateManager:
    """
    爬虫运行时有限状态机（FSM）执行器。

    职责：
    1. 维护当前状态（_state）。
    2. 通过 transition_to() 执行状态转移，并在转移前校验合法性。
    3. 转移成功后通知所有已注册的监听器（Observer Pattern）。
    4. 提供语义化快捷属性（is_active / is_stopped）供业务代码判断。

    Pattern: State Machine（FSM） + Observer（监听器回调）

    并发安全：所有状态转移通过 asyncio.Lock（self._lock）串行化，
    适配 Crawlee 的纯 asyncio 协程模型；不使用 threading.Lock，
    避免在 asyncio 事件循环线程中同步阻塞。
    """

    def __init__(
        self,
        initial_state: CrawlerState = CrawlerState.IDLE,
    ) -> None:
        """
        Args:
            initial_state: 初始状态，默认 IDLE。

        初始化说明：
            self._lock = asyncio.Lock()   —— 保护 transition_to / reset 的读-改-写序列。
            self._state                   —— 当前状态，仅在持锁期间修改。
            self._listeners               —— 状态变更监听器列表，锁外回调。
            self._history                 —— 状态转移历史记录（timestamp, old, new, reason）。
        """
        self._state: CrawlerState = initial_state
        self._lock: asyncio.Lock = asyncio.Lock()
        self._listeners: List[StateListener] = []
        self._history: List[tuple] = []

    # ------------------------------------------------------------------
    # 状态属性
    # ------------------------------------------------------------------

    @property
    def state(self) -> CrawlerState:
        """
        当前状态（只读）。
        asyncio 安全：协程内的属性读取在相邻 await 点之间是原子的，
        无需额外加锁（asyncio 单线程协作调度保证）。

        Returns:
            当前 CrawlerState 枚举值。
        """
        return self._state

    # ------------------------------------------------------------------
    # 状态转移
    # ------------------------------------------------------------------

    async def transition_to(self, new_state: CrawlerState, reason: str = "") -> None:
        """
        执行状态转移（asyncio.Lock 保护的协程）。

        流程：
        1. async with self._lock 获取异步锁，防止并发转移互相覆盖。
        2. 查找 VALID_TRANSITIONS[current] 校验合法性。
        3. 更新 _state，释放锁（lock 在 with 块结束时自动释放）。
        4. 锁外依次调用所有已注册监听器（避免监听器内部 await 导致持锁等待）。

        Args:
            new_state: 目标状态。
            reason: 转移原因描述（写入日志 / 传递给监听器）。
        Raises:
            StateTransitionError: 目标状态不在当前状态的合法转移集中。
        """
        import time

        async with self._lock:
            old_state = self._state
            allowed = VALID_TRANSITIONS.get(old_state, set())
            if new_state not in allowed:
                err_msg = (
                    f"非法状态转移：{old_state.name} → {new_state.name}（reason={reason!r}）。"
                    f"允许的目标状态：{[s.name for s in allowed]}"
                )
                # 先写入 loguru（内存 sink → 主界面 / 监控窗日志），再抛出；
                # 避免业务层 try/except 静默吞掉后用户完全无感知。
                _fsm_log.error(f"[FSM] {err_msg}")
                raise StateTransitionError(err_msg)
            self._state = new_state
            self._history.append((time.time(), old_state, new_state, reason))
            # 在锁内拷贝监听器列表快照，避免锁外迭代期间列表被修改
            listeners_snapshot = list(self._listeners)

        # 锁外回调，防止监听器内部 await 与锁形成死锁
        for listener in listeners_snapshot:
            try:
                listener(old_state, new_state, reason)
            except Exception as exc:
                _fsm_log.warning(
                    "[FSM] 状态监听器回调异常（{} → {}): {!r}",
                    old_state.name,
                    new_state.name,
                    exc,
                )

    # ------------------------------------------------------------------
    # 监听器管理（Observer Pattern）
    # ------------------------------------------------------------------

    def register_listener(self, listener: StateListener) -> None:
        """
        注册状态变更监听器。
        每次合法转移完成后，以 (old_state, new_state, reason) 为参数回调。
        可用于：驱动 UI 标签更新、触发 anti_bot 代理切换、写入日志等。

        Args:
            listener: 符合 StateListener 签名的可调用对象。
        """
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unregister_listener(self, listener: StateListener) -> None:
        """
        移除已注册的监听器。若监听器不存在则静默忽略。

        Args:
            listener: 待移除的监听器对象。
        """
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # 语义化快捷判断
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """
        判断当前是否处于"活跃采集中"状态（RUNNING / CHALLENGE / BANNED）。
        用于替代原有的 is_running() lambda，供 handlers 判断是否继续处理请求。

        Returns:
            True 表示爬虫正在运行（含挑战/封禁重试中）。
        """
        return self._state in {
            CrawlerState.RUNNING,
            CrawlerState.CHALLENGE,
            CrawlerState.BANNED,
        }

    def is_stopped(self) -> bool:
        """
        判断当前是否处于完全停止状态（STOPPED 或 ERROR）。

        Returns:
            True 表示爬虫已停止，无活跃任务。
        """
        return self._state in {CrawlerState.STOPPED, CrawlerState.ERROR}

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    async def reset(self) -> None:
        """
        将状态机强制重置到 IDLE 并清空所有监听器（asyncio.Lock 保护）。
        通常在任务彻底结束（STOPPED）后由 Runner 调用，为下一个任务做准备。
        """
        async with self._lock:
            self._state = CrawlerState.IDLE
            self._listeners.clear()
            self._history.clear()

    def get_history(self) -> List[tuple]:
        """
        返回自上次 reset() 以来的完整状态转移历史记录。
        每条记录为 (timestamp, old_state, new_state, reason) 元组。

        Returns:
            状态转移历史元组列表，按时间正序排列。
        """
        return list(self._history)
