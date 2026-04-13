# 金丝雀（Canary）监控系统 — 架构整合与重构方案

> **角色定位**：工业级高并发爬虫体系中，金丝雀负责「指纹配置 × 目标站点」反爬有效性的独占式体检与结构化看板展示；主线路（`SiteRunner` + `SiteCrawlStrategy` + `SiteAuditCenter`）继续承担批量站点抓取与审计账本职责。  
> **文档目的**：在不大改既有分层的前提下，给出高内聚、低耦合的目录归属、接口契约冻结策略、异步与持久化约束，以及可落地的集成代码骨架。

---

## 一、工作区现状盘点（与金丝雀相关）

| 区域 | 路径 | 说明 |
|------|------|------|
| API / Facade | `src/app/bridge.py` | 模块级 `_CANARY_LOG_BUFFER`、`_append_canary_log`、`_build_canary_dashboard_payload`；`PrismAPI` 暴露 `fetch_canary_logs` / `append_canary_log` / `fetch_canary_dashboard` / `run_canary_checkup` / `toggle_canary_window` / `raise_canary_window` 及与引擎同步的 `get_stealth_engine` / `set_stealth_engine` |
| 调度 | `src/app/dispatcher.py` | `is_task_running()` → `self._running`，金丝雀占位逻辑用其推导 `system_state=locked` |
| 主线路 | `src/app/runner.py`（`SiteRunner`）、`src/modules/site/strategy.py`、`src/modules/site/audit/audit_center.py` | 主爬取生命周期与 DB 账本；**当前无金丝雀探针挂载** |
| 持久化 | `src/db/database.py`、`src/db/schema.py` | **唯一**写路径：`DatabaseManager` 内 `asyncio.Queue` + `_writer_loop`；`SiteAuditCenter` 明确不建第二层写队列 |
| 状态机 | `src/engine/state_manager.py` | 主爬虫 FSM（`CrawlerState`）；金丝雀可有独立轻量状态模型，**禁止**与主 FSM 混用同一实例导致竞态 |
| 前端 | `UI/canary.html`、`UI/canary.js`、`UI/ui-main.js` | 轮询 `fetch_canary_dashboard`、`fetch_canary_logs`；调用 `run_canary_checkup`、`set_stealth_engine` |
| 规格/审计备忘 | `migration/CANARY_*.md` | 需求与缺口说明，非运行时代码 |

**结论**：金丝雀已具备 **API 面 + 独立日志缓冲 + Dashboard JSON 占位 + 第二窗口**；缺的是 **模块化的编排/探针/持久化模型**，以及 **与调度器/引擎的非侵入式衔接**。

---

## 二、职责划分与目录归属

### 2.1 推荐顶层决策

| 组件 | 建议归属 | 理由 |
|------|----------|------|
| 金丝雀编排器、探针协议、看板 DTO 组装、DB 写入封装（Repository） | **`src/modules/canary/`**（新建包） | 与 `site` / `search` 并列，属于**垂直业务子域**；高内聚（体检全流程在一包内），且不污染 `engine` 的通用浏览器抽象 |
| 浏览器启动、指纹、Stealth 后端、挑战检测 | **继续留在 `src/engine/`** | 金丝雀通过**工厂/已有 API**复用，不在 `engine` 内堆「四象限 UI 字段」 |
| 全局执行锁、与主任务互斥、pywebview API | **`src/app/`**（`bridge` + 可选 `canary_api.py` 薄委托） | App 层负责线程模型与对外契约；实现细节下沉到 `modules/canary` |
| Dashboard / 静态资源 | **`UI/`** | 保持不变 |
| 配置项（超时、默认靶站、feature flag） | **`src/config/settings.py`** 或 `src/config/canary.py` | 与 `PrismSettings` 扩展字段一致，避免魔法常量散落在探针内 |

**不推荐**把金丝雀整体塞进 `SiteAuditCenter`：`SiteAuditCenter` 的 Hook 语义绑定 **`task_id` + 站点爬取账本**（`scan_records` / `downloaded_files` / `error_log`），与「一次独占体检 run」的生命周期不同；强行合并会导致账本方法与体检方法交织、违反单一职责。

**可选折中**：在 `schema` 增加 **`canary_*` 表**，由 `src/modules/canary/repository.py` 调用 `get_db()` → `enqueue_write` / `query`；**不要**为金丝雀新增 `SiteAuditCenter` 的 Hook（除非产品明确要求把体检结果并入任务维度统计）。

### 2.2 建议包内文件切分（`src/modules/canary/`）

```
src/modules/canary/
  __init__.py          # 导出稳定工厂函数，如 get_canary_service()
  contracts.py         # Protocol / TypedDict：Dashboard JSON、锁查询端口
  state.py             # 体检专用状态：pending / running / blocked / done（内存 + 可选 DB 镜像）
  service.py           # CanaryService：编排探针、更新 state、写日志、调 Repository
  probes/              # 各象限探针（纯 asyncio，失败隔离）
    __init__.py
    base.py
    ...
  repository.py        # 仅通过 DatabaseManager 持久化；无自建 Queue
  dashboard.py         # 从 state + settings + dispatcher 端口构建与 UI 契约一致的 dict
```

---

## 三、接口契约与解耦设计（防「空壳」与 Pylance 报错）

### 3.1 必须冻结的 JS ↔ Python 公开 API（签名与返回形状）

以下 **`PrismAPI` 方法名与参数类型** 已被 `UI/canary.js` 依赖；重构时**只能改实现体，不能删方法或改参数/返回类型语义**：

- `fetch_canary_logs(self, limit: int = 200) -> List[Dict[str, str]]`
- `append_canary_log(self, message: str, level: str = "INFO") -> Dict[str, Any]`
- `fetch_canary_dashboard(self) -> Dict[str, Any]`
- `run_canary_checkup(self) -> Dict[str, Any]`
- `toggle_canary_window(self) -> bool`
- `raise_canary_window(self) -> None`
- `get_stealth_engine(self) -> str`
- `set_stealth_engine(self, engine: str) -> Dict[str, Any]`

`fetch_canary_dashboard` 返回的 **键集合**（`system_state`, `current_engine`, `progress_percent`, `quadrants`）应与 `_build_canary_dashboard_payload` 当前占位保持一致或**向后兼容扩展**（只增键、不改类型），避免前端渲染 `undefined`。

### 3.2 与 `SiteRunner` / `SiteAuditCenter` 的松耦合方式

**原则**：金丝雀**不继承**、**不修改** `SiteRunner` / `SiteAuditCenter` 的既有公开方法签名；通过 **端口（Protocol）+ 可选订阅** 交互。

| 需求 | 做法 |
|------|------|
| 知道主任务是否占用 | 依赖 `Callable[[], bool]` 或 `Protocol` 暴露 `is_task_running()`（由 `MasterDispatcher` 实现）；**禁止**金丝雀直接读 `SiteRunner` 私有字段 |
| 可选：主任务侧「旁路观测」 | 在 `SiteCrawlStrategy` 或具体 Handler 中 **可选** 调用 `canary_port.emit_crawl_hint(event)`（若 `None` 则 no-op）；**默认不注入**，避免生产路径耦合 |
| 持久化 | 金丝雀 **独立 Repository** → `await get_db(path)` → `enqueue_write`；**不**把体检行塞进 `record_page_success` 除非 Schema 明确扩展 |

### 3.3 核心解耦代码示例（Protocol + 服务入口）

**`src/modules/canary/contracts.py`**（节选）

```python
from __future__ import annotations

from typing import Any, Callable, Dict, List, Protocol, TypedDict


class QuadrantItem(TypedDict):
    id: str
    label: str
    state: str
    desc: str


class CanaryDashboardPayload(TypedDict, total=False):
    system_state: str
    current_engine: str
    progress_percent: int
    quadrants: Dict[str, List[QuadrantItem]]


class DispatcherLockPort(Protocol):
    def is_task_running(self) -> bool: ...


class CanaryLogSink(Protocol):
    def append(self, level: str, message: str) -> None: ...
```

**`src/modules/canary/service.py`**（编排骨架；异常不向上冒泡到主爬虫）

```python
from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Optional

from src.modules.canary.contracts import DispatcherLockPort, CanaryLogSink
from src.utils.logger import get_logger

_log = get_logger(__name__)


class CanaryService:
    """与 bridge.start_task 一致：GUI 线程只投递 coroutine，不 await Future。"""

    def __init__(
        self,
        lock_port: DispatcherLockPort,
        log_sink: CanaryLogSink,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._lock = lock_port
        self._log_sink = log_sink
        self._loop = loop
        self._last_future: Optional[concurrent.futures.Future] = None

    def request_checkup(self) -> dict:
        if self._lock.is_task_running():
            self._log_sink.append("WARNING", "[异常] 主控任务运行中，无法进行金丝雀体检")
            return {"success": False, "message": "主控任务运行中，无法进行金丝雀体检"}
        if self._last_future is not None and not self._last_future.done():
            return {"success": False, "message": "金丝雀体检已在运行中"}

        async def _body() -> None:
            try:
                # await probe_pipeline.run()
                self._log_sink.append("INFO", "[系统] 体检流水线完成（示例）")
            except Exception as exc:
                _log.exception("金丝雀体检失败（已隔离，不影响主任务）")
                self._log_sink.append("ERROR", f"[异常] 体检失败: {exc!r}")

        self._last_future = asyncio.run_coroutine_threadsafe(_body(), self._loop)
        return {"success": True, "message": "体检已提交至后台事件循环"}
```

**`bridge.py` 集成方式**：保留 `PrismAPI.run_canary_checkup` **方法签名不变**，内部改为 `return self._canary_service.request_checkup()`（`CanaryService` 在 `create_app()` 时注入 `dispatcher` + 共享日志 sink）。

---

## 四、异步并发与数据流

### 4.1 非阻塞与生命周期

- **GUI 线程**：`PrismAPI` 中凡触发体检的入口，须与 `start_task` 一致，使用 `asyncio.run_coroutine_threadsafe(..., self._loop)`，**禁止**在 pywebview 线程里 `asyncio.run()`。
- **状态迁移**：`pending → running → blocked/done` 建议在 `CanaryService` 内存状态 + `fetch_canary_dashboard` 读取；若需跨进程恢复，再镜像到 DB。
- **与主爬虫**：主线路继续使用 `CrawlerStateManager`；金丝雀**单独**状态对象，避免共用一个 FSM 实例。

### 4.2 持久化：强制复用 `DatabaseManager`

- **唯一写队列**：业务层仅调用 `DatabaseManager.enqueue_write` / `execute` / `executemany`（内部已是单消费者 `asyncio.Queue`）。
- **禁止**：在 `CanaryService`、`SiteAuditCenter`、或「中转中心」中再建 **`asyncio.Queue` 用于落库**（会造成双队列、顺序难推理、关闭时 join 顺序易死锁）。
- **推荐表**（示例，迁移时写入 `schema.py`）：`canary_runs`（run_id、started_at、finished_at、engine、target_profile_json、overall_status）、`canary_probe_results`（run_id、probe_id、status、detail_json）。写入路径统一 `await db.enqueue_write(...)`。

**Repository 示例（节选）**

```python
# src/modules/canary/repository.py
from __future__ import annotations

from typing import Any, Tuple

from src.db.database import get_db


async def insert_probe_result(db_path: str, params: Tuple[Any, ...]) -> None:
    db = await get_db(db_path)
    await db.enqueue_write(
        "INSERT INTO canary_probe_results (run_id, probe_id, status, detail_json) "
        "VALUES (?, ?, ?, ?)",
        params,
    )
```

---

## 五、防御性编程（防级联故障）

1. **边界吞异常 + 日志**：探针单测失败只更新该 `quadrant` 项为 `fail`/`warning`，并 `logger.exception`；**不**向主 `SiteRunner` 抛异常。
2. **资源隔离**：体检浏览器上下文与主任务**进程内互斥**（与现有 `dispatcher.is_task_running()` 对齐）；若未来支持并行，须独立 browser pool，仍不共享 `SiteAuditCenter` 的 `task_id` 账本。
3. **bridge 薄层**：`fetch_canary_dashboard` / `fetch_canary_logs` 内部对服务调用包一层 `try/except`，失败时返回**上次快照或占位 JSON**，避免 JS 轮询崩溃。

---

## 六、实施顺序建议（降低回归风险）

1. **新增** `src/modules/canary/`（`contracts` / `dashboard` / `service` / `repository`），把 `_build_canary_dashboard_payload` 的逻辑迁至 `dashboard.py`，`bridge` 仅委托调用 — **API 签名不变**。
2. **扩展** `schema.py` + 一次性迁移脚本（若需要历史查询）；Repository 全部走 `get_db().enqueue_write`。
3. **实现** `run_canary_checkup` 的异步流水线（`run_coroutine_threadsafe`），占位探针逐个替换为真实 `page.evaluate` 等（复用 `src/engine/` 工厂）。
4. **可选**：`task_info.mode == "canary"` 时 `MasterDispatcher._select_strategy` 路由到 `CanaryCrawlStrategy`（继承 `BaseCrawlStrategy`）— **仅当** 产品希望「体检」与「普通任务」共享同一套 `run_with_config` 生命周期；否则保持独立 `CanaryService`，减少 Dispatcher 状态 `self._running` 与体检并发的语义纠缠。
5. **静态检查**：每步运行 `pyright`/`basedpyright` 或 IDE Pylance，确保 `PrismAPI` 无未实现方法、无返回类型收窄错误。

---

## 七、小结

| 维度 | 决策 |
|------|------|
| 目录 | 核心业务放 **`src/modules/canary/`**；引擎能力复用 **`src/engine/`**；API 留在 **`src/app/bridge.py`**（委托实现） |
| 与主线路关系 | **`DispatcherLockPort` + 可选 emit**；不改 `SiteRunner`/`SiteAuditCenter` 既有 Hook 签名 |
| 持久化 | **仅** `DatabaseManager` 单写者队列；**禁止**监控层第二 `asyncio.Queue` |
| 契约 | **冻结** `PrismAPI` 金丝雀相关方法签名与 Dashboard 键；重构只移动实现 |
| 可靠性 | 探针与编排异常隔离；GUI 线程只投递协程 |

本文档路径：`migration/canary_integration_plan.md`。
