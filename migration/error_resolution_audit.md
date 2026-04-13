# WrightBot_V1 — Pyright / Pylance 与运行时风险审核报告

**生成日期**：2026-04-13  
**扫描范围**：`src/` 全量（重点复核 `src/modules/site/audit`、`src/modules/canary`、`src/engine`）  
**扫描方式**：在项目根目录执行 `python -m pyright src`（Pyright 语言服务器与 VS Code Pylance 共享同一分析内核，结论可直接对照 IDE 诊断）。  
**说明**：按你的要求，本文件**仅记录问题与方案**，不在业务源码中注入修复；人工 Code Review 通过后再改代码。

---

## 执行摘要

| 指标 | 结果 |
|------|------|
| `pyright src` 报错数 | **53**（0 warnings） |
| `pyright src/modules/site/audit` 单独扫描 | **0**（该子包当前无独立类型错误） |
| 已发现的**纯运行时缺陷**（与 Pyright 一致） | **1**：`ProxyRotator.build()` 调用不存在的 `ProxyConfiguration.create`（当前仓库内未被调用，属**潜伏死代码路径**） |

**错误来源归类（高层）**

1. **契约与注解不一致**（接口重构 / TypedDict / 可选组件未收窄）：`bridge`、`site/strategy`、`dispatcher`、`canary/strategy`、`crawlee_engine` 等。  
2. **第三方存根与运行时 API 漂移**：`pywebview` 对话框常量类型、`crawlee` 的 `ProxyConfiguration` 工厂方法。  
3. **依赖未安装或类型存根缺失**：`tzlocal`、`rebrowser_patches`（可选依赖）。  
4. **抽象基类未声明统一构造签名**：`AbstractBrowserBackend` 导致 `BrowserFactory` 对具体后端的实例化被 Pyright 拒绝。  
5. **刻意使用 `object` 弱化上下文类型**：`Interactor` / `ActionHandler` 等与 Crawlee / Playwright 上下文相关的 API 触发大量属性访问报错。  
6. **无效类型注解形式**：`get_logger` 的返回注解 `"logger"` 与 Loguru 的 `Logger` 类型混淆。

**异步 I/O 调用链（抽样结论）**

- `bridge.PrismAPI` 中对 `MasterDispatcher` 的 `run_with_config` / `stop` 使用 `asyncio.run_coroutine_threadsafe(..., self._loop)`，与文档描述一致，**未发现**在 GUI 线程直接 `asyncio.run()` 任务主体的模式问题。  
- `DatabaseManager` 采用单写者队列 + `aiosqlite`，设计层面合理；Pyright 报错来自 **`_conn` 在类型上仍为 `Optional`**，与 `_writer_loop` 启动时序相关。

**与「禁止掩耳盗铃」相关的存量技术债（供 Review 时一并处理）**

仓库内已存在多处 `# type: ignore`、`# noqa`（例如 `site/handlers/action.py`、`crawlee_engine.py` 对 `_proxies` 的私有访问等）。本次审核**不新增**此类屏蔽；建议在后续修复中**用正确类型与公开 API 替换**，而非扩大 suppress 范围。

---

## 分项审核（标准化结构）

### 1. `src/app/bridge.py`

#### 1.1 金丝雀看板返回类型

- **Target File**：`src/app/bridge.py`（及 `src/modules/canary/dashboard.py`）  
- **Error Message**：`reportReturnType` — 类型 `CanaryDashboardDict` 不可分配给返回类型 `Dict[str, Any]`（`_build_canary_dashboard_payload` / `fetch_canary_dashboard` 相关行）。  
- **Root Cause**：**接口契约与注解不一致**。`build_payload()` 已返回精确契约 `CanaryDashboardDict`（见 `src/modules/canary/contracts.py`），但桥接层仍声明宽松 `Dict[str, Any]`，TypedDict 与 `Dict[str, Any]` 在 Pyright 下**非双向兼容**（返回值位置逆变）。  
- **Proposed Architectural Solution**：将 `_build_canary_dashboard_payload` 与 `PrismAPI.fetch_canary_dashboard` 的返回注解改为 `CanaryDashboardDict`（或 `Mapping[str, object]` 若必须保持只读抽象）。前端仍要求 JSON 可序列化：TypedDict 运行时即普通 `dict`，无需改 JS。导入建议：`from src.modules.canary.contracts import CanaryDashboardDict`。

#### 1.2 `create_file_dialog` 的 `dialog_type` 实参

- **Target File**：`src/app/bridge.py`  
- **Error Message**：`reportArgumentType` — 无法将 `module_property` 类型参数赋给 `int` 类型参数 `dialog_type`（`webview.FOLDER_DIALOG`、`webview.OPEN_DIALOG` 调用处）。  
- **Root Cause**：**第三方类型存根与实现不一致**（或存根将模块级常量标成了 `property`/`Proxy`）。运行时 pywebview 通常接受这些常量；类型检查器按存根解析为错误。  
- **Proposed Architectural Solution**（择一，均**不**使用 `type: ignore`）：  
  - **A**：在**项目内**增加 `typings/pywebview/__init__.pyi`（或 `webview` 的 partial stub），将 `FOLDER_DIALOG`、`OPEN_DIALOG` 声明为 `int` 或 `Literal[...]`，并在 `pyrightconfig.json` / `pyproject.toml` 中配置 `extraPaths` / `stubPath`。  
  - **B**：使用存根认可的入口，例如若文档/存根规定通过 `webview.enums` 或具体 `int` 枚举值，则改为该 API（需对照你安装的 `pywebview` 版本文档）。

#### 1.3 `Window.events` 可能为 `None`

- **Target File**：`src/app/bridge.py`  
- **Error Message**：`reportOptionalMemberAccess` — `events` 不是 `None` 的已知属性（金丝雀窗 / 监控窗 / `create_app` 主窗的 `window.events.loaded` / `closed` 等）。  
- **Root Cause**：**存根将 `events` 标为可选**；业务代码在未收窄前直接访问。  
- **Proposed Architectural Solution**：在绑定前显式收窄，例如：  
  `events = getattr(window, "events", None)`；若 `events is None` 则记录错误并跳过回调注册（或 `raise RuntimeError` 若视为不可恢复）。对 `create_app()` 中主窗口同理。这样既满足类型检查，也符合防御性编程（避免静默 `try/except: pass` 掩盖配置错误）。

#### 1.4 `create_app()` 返回类型与 `webview.create_window`

- **Target File**：`src/app/bridge.py`  
- **Error Message**：`reportReturnType` — `Window | None` 不可分配给 `Window`。  
- **Root Cause**：**存根声明 `create_window` 可返回 `None`**，与注解 `-> webview.Window` 冲突。  
- **Proposed Architectural Solution**：在赋值后断言或显式分支：`window = webview.create_window(...)`；若 `window is None` 则记录致命日志并 `raise RuntimeError("无法创建主窗口")`。将返回值收窄为 `Window` 后再 `return window`。

#### 1.5（维护性）大量裸 `except Exception: pass`

- **Target File**：`src/app/bridge.py`（如 `_notify_main_panel_button_inactive`、`raise_monitor_window`、`toggle_*_window`、对话框等）  
- **Error Message**：非 Pyright 报错；属**可观测性与调试风险**。  
- **Root Cause**：异常被吞掉，生产环境难以区分「用户取消」与「底层失败」。  
- **Proposed Architectural Solution**：在不大改行为的前提下，至少使用 `logger.debug(..., exc_info=True)` 或按异常类型分支处理；**避免**无条件的 `pass`（与你给出的工程规范一致）。

---

### 2. `src/app/dispatcher.py`

#### 2.1 `SiteRunner` / `SearchRunner` 的 `strategy` 参数类型

- **Target File**：`src/app/dispatcher.py`  
- **Error Message**：`reportArgumentType` — `BaseCrawlStrategy | None` / `BaseCrawlStrategy` 不可分配给 `SiteCrawlStrategy` / `SearchCrawlStrategy`。  
- **Root Cause**：**类型状态未随控制流收窄**。运行时 `_execute_task` 在调用 `_create_runner(mode)` 前已执行 `self._strategy = self._select_strategy(settings)` 且失败则 `return`，故实际上 `_strategy` 非 `None` 且模式与具体策略一致；但 Pyright 仍视 `_strategy` 为 `Optional[BaseCrawlStrategy]`。  
- **Proposed Architectural Solution**（择一）：  
  - **A**：在 `_create_runner` 开头使用**用户可见的失败路径**：`if self._strategy is None: raise RuntimeError("策略未初始化")`，随后 `assert isinstance(self._strategy, SiteCrawlStrategy)`（site 分支）等 —— 将可选性收窄为具体策略类型。  
  - **B**：将 `_create_runner` 改为接收显式参数 `_create_runner(self, mode: str, strategy: BaseCrawlStrategy)`，由 `_execute_task` 传入局部变量 `strategy = self._strategy`，避免在工厂方法内读取 Optional 字段。  
  **接口契约**：`SiteRunner` 只接受 `SiteCrawlStrategy`，`SearchRunner` 只接受 `SearchCrawlStrategy`；调度器负责在编译期/运行期保证与 `mode` 一致。

---

### 3. `src/db/database.py`

#### 3.1 `_writer_loop` 中 `_conn` 的可选性

- **Target File**：`src/db/database.py`  
- **Error Message**：`reportOptionalMemberAccess` — `executemany` / `execute` / `commit` 不是 `None` 的已知属性（约第 136–139 行）。  
- **Root Cause**：`self._conn` 注解为 `Optional[aiosqlite.Connection]`。逻辑上 `_writer_loop` 仅在 `initialize()` 赋值 `_conn` 之后启动，但类型系统**未继承该不变量**。若未来有人错误地提前调度 `_writer_loop`，运行时会 `AttributeError`。  
- **Proposed Architectural Solution**：在 `_writer_loop` 开头加入 `conn = self._conn`；`if conn is None: raise RuntimeError("DatabaseManager 未 initialize")`；循环内使用局部 `conn` 调用。或把 `_conn` 拆成 `NonOptional` 私有字段在 `initialize` 末尾赋值（需调整关闭路径）。**不改变**现有异步队列语义。

---

### 4. `src/engine/anti_bot/fingerprint.py`

#### 4.1 `tzlocal` 无法解析导入

- **Target File**：`src/engine/anti_bot/fingerprint.py`  
- **Error Message**：`reportMissingImports` — 无法解析导入 `tzlocal`。  
- **Root Cause**：**分析环境未安装依赖**或 **Pyright 未使用项目 venv**（`requirements.txt` 已声明 `tzlocal>=4.0`）。  
- **Proposed Architectural Solution**：在用于类型检查的 Python 环境中执行 `pip install -r requirements.txt`；在 IDE 中选择同一解释器；若使用 Pyright CLI，确保 `venvPath`/`venv` 指向该环境。无需改业务逻辑。

---

### 5. `src/engine/anti_bot/proxy_rotator.py`

#### 5.1 `ProxyConfiguration.create` 不存在

- **Target File**：`src/engine/anti_bot/proxy_rotator.py`  
- **Error Message**：`reportAttributeAccessIssue` — 无法访问 `ProxyConfiguration.create`。  
- **Root Cause**：**Crawlee 库 API 与代码假设不一致（接口漂移）**。在当前环境实测：`ProxyConfiguration` **无** `create` 类方法；构造签名为 `__init__(self, *, proxy_urls: list[str | None] | None = None, ...)`。  
- **Proposed Architectural Solution**：将 `build()` 改为与 `crawlee_engine._build_proxy_configuration` 一致，例如：  
  `return ProxyConfiguration(proxy_urls=[p.url for p in self._proxies])`  
  若必须异步预留扩展点，可保留 `async def build` 但内部使用同步构造（或未来 Crawlee 若提供真正的 async 工厂再切换）。**接口契约**：`build()` 返回 `Optional[ProxyConfiguration]`，空池返回 `None`。  
- **运行时备注**：当前仓库内**未发现**对 `ProxyRotator.build()` 的调用（引擎注释明确规避 async `build`），但一旦调用即会在旧实现上失败；修复可消除潜伏缺陷。

---

### 6. `src/engine/anti_bot/stealth/rebrowser_backend.py`

#### 6.1 `rebrowser_patches` 无法解析导入

- **Target File**：`src/engine/anti_bot/stealth/rebrowser_backend.py`  
- **Error Message**：`reportMissingImports` — 无法解析导入 `rebrowser_patches`（静态分析路径）。  
- **Root Cause**：**可选依赖未安装**（`requirements.txt` 中为注释说明的 `rebrowser-playwright` / patches 包）。源码已在 `ImportError` 分支告警降级。  
- **Proposed Architectural Solution**：开发全功能环境执行 `pip install rebrowser-patches`（或项目文档指定的包名）；或为该包添加 **PEP 561 存根** / `typings/rebrowser_patches.pyi` 空模块以消除 `reportMissingImports`，同时保持运行时 `ImportError` 行为不变。

---

### 7. `src/engine/browser_factory.py`

#### 7.1 后端类实例化参数

- **Target File**：`src/engine/browser_factory.py`  
- **Error Message**：`reportCallIssue` — 没有名为 `stealth_config` / `app_config` 的参数。  
- **Root Cause**：**抽象基类未声明构造契约**。`backend_cls` 静态类型为 `type[AbstractBrowserBackend]`，而 `AbstractBrowserBackend`（`browser_engine.py`）**未定义** `__init__(stealth_config, app_config)`；具体类（如 `PlaywrightBackend`）虽有该签名，Pyright 不允许向「仅知为 ABC 的类型」传入子类专有参数。  
- **Proposed Architectural Solution**（推荐）：在 `AbstractBrowserBackend` 中增加**带实现的通用构造函数**（或 `Protocol` + `TypeVar` 绑定），例如：  
  ```python
  def __init__(self, stealth_config: StealthConfig, app_config: AppConfig) -> None:
      self._stealth_config = stealth_config
      self._app_config = app_config
  ```  
  子类通过 `super().__init__(...)` 继承；仍使用 `@abstractmethod` 约束异步接口。这样 `BrowserFactory` 的 `backend_cls(...)` 调用与类型系统一致。**注意**：需核对 `BrowserContextManager` 等是否仍只需 `stealth_config` —— 可拆分为两个 ABC 层次，避免强行统一构造签名。

---

### 8. `src/engine/crawlee_engine.py`

#### 8.1 `_get_browser_factory` 返回类型

- **Target File**：`src/engine/crawlee_engine.py`  
- **Error Message**：`reportReturnType` — `type[BrowserFactory]` 不可分配给 `BrowserFactory`。  
- **Root Cause**：函数实际返回**类对象** `BrowserFactory`，注解却写为实例类型 `BrowserFactory`。  
- **Proposed Architectural Solution**：将返回注解改为 `type[BrowserFactory]`，或重命名为 `_get_browser_factory_cls` 并同步所有调用点，避免「工厂」语义歧义。

#### 8.2 `profile` 可能未绑定

- **Target File**：`src/engine/crawlee_engine.py`  
- **Error Message**：`reportPossiblyUnboundVariable` — `profile` 可能未绑定（指纹闭包 `_fp = profile` 处）。  
- **Root Cause**：`profile` 仅在 `if stealth.use_fingerprint:` 分支赋值；`else` 分支未定义，但后续 `if stealth.use_fingerprint:` 块内闭包仍引用同一符号，人类可读为安全，**类型流未合并**。  
- **Proposed Architectural Solution**：在分支前初始化 `profile: FingerprintProfile | None = None`，在 `use_fingerprint` 分支赋值；闭包内使用 `assert _fp is not None` 或仅在分支内定义嵌套函数，确保分析器可见赋值。

#### 8.3 `ProxyConfiguration(proxy_urls=list[str])` 不变性

- **Target File**：`src/engine/crawlee_engine.py`  
- **Error Message**：`reportArgumentType` — `list[str]` 不可分配给 `list[str | None] | None`（`list` 不变性）。  
- **Root Cause**：**泛型不变性**：`list[str]` 不是 `list[str | None]` 的子类型。  
- **Proposed Architectural Solution**（择一）：  
  - 传参时使用显式类型：`list[str | None](urls)` 或 `[u for u in urls]` 带注解 `list[str | None]`；  
  - 或 `cast(list[str | None], urls)` 在确认无 `None` 需求时作为窄化（优于退化为 `Any`）。  
  **契约**：与 Crawlee 签名 `proxy_urls: list[str | None] | None` 一致。

---

### 9. `src/modules/canary/probes_combat.py` & `src/modules/canary/probes_network.py`

#### 9.1 `headers.items()` 迭代

- **Target File**：`src/modules/canary/probes_combat.py`、`src/modules/canary/probes_network.py`  
- **Error Message**：`reportGeneralTypeIssues` — `object` 不可迭代（`for k, v in items():`）。  
- **Root Cause**：`items = getattr(raw, "items", None)` 后 `callable(items)` 为真时，Pyright 仍认为 `items()` 返回值为 `object`。  
- **Proposed Architectural Solution**：使用 `typing.Protocol` 定义 `SupportsItems`（含 `def items(self) -> Iterable[tuple[str, str]]: ...`），或对 `raw` 使用 `isinstance` 与 `collections.abc.Mapping` 收窄；Playwright 的 `Headers` 可单独 `isinstance` 分支处理。避免用 `Any` 掩盖。

---

### 10. `src/modules/canary/strategy.py`

#### 10.1 `_CanaryWorkspaceStub` 赋给 `_audit_center`

- **Target File**：`src/modules/canary/strategy.py`  
- **Error Message**：`reportAttributeAccessIssue` — `_CanaryWorkspaceStub` 不可分配给 `SiteAuditCenter | None`。  
- **Root Cause**：**解耦设计与继承层次类型不一致**：金丝雀策略复用 `SiteCrawlStrategy` 的 `run()` 模板，但用轻量桩替代 `SiteAuditCenter`，未在类型层声明「可替换实现」。  
- **Proposed Architectural Solution**（择一）：  
  - **A**：定义 `Protocol`（如 `AuditWorkspaceProvider`）：要求 `_get_workspace`、`set_task_id`、`record_page_success` 等**金丝雀实际会调用的最小方法集**，让 `SiteCrawlStrategy` 的 `_audit_center` 类型为该 `Protocol`（`SiteAuditCenter` 实现协议）。  
  - **B**：提取公共基类 `BaseSiteStrategy`，将 `_audit_center` 留在更泛的基类并泛型化 `T_audit`。  
  **禁止**：把 `_audit_center` 改为 `object` 或 `Any` 以消除报错。

---

### 11. `src/modules/search/strategy.py`

#### 11.1 `BrowserContext.new_page()` 与可选上下文

- **Target File**：`src/modules/search/strategy.py`  
- **Error Message**：`reportOptionalMemberAccess` — `new_page` 不是 `None` 的已知属性（约第 502 行）。  
- **Root Cause**：`self._context` 被标注为 `Optional[BrowserContext]`（或等价），在未收窄前调用 `new_page`。  
- **Proposed Architectural Solution**：依赖现有 `_ensure_playwright()` 契约：使该方法返回 `BrowserContext` 或在该方法内 `assert self._context is not None` 并抛出明确异常；调用处使用局部非可选变量。与 `generator` 同类问题保持同一模式。

---

### 12. `src/modules/site/generator.py`

#### 12.1 同上，`new_page` 可选访问

- **Target File**：`src/modules/site/generator.py`  
- **Error Message**：`reportOptionalMemberAccess` — `new_page` 不是 `None` 的已知属性（约第 238 行）。  
- **Root Cause**：与搜索策略相同 — `_context` 可选性未在 `await self._ensure_browser()` 后收窄。  
- **Proposed Architectural Solution**：`_ensure_browser()` 保证 `_context` 已赋值并返回 `BrowserContext`，或内部 `raise RuntimeError`；随后 `ctx = self._context; assert ctx is not None` 仅供类型检查（生产路径不应触发）。

---

### 13. `src/modules/site/handlers/action.py`

#### 13.1 `button_locators.count` / `nth`

- **Target File**：`src/modules/site/handlers/action.py`  
- **Error Message**：`reportAttributeAccessIssue` — 无法访问类 `object` 的属性 `count` / `nth`。  
- **Root Cause**：`_build_button_locator` 标注返回 `object`，调用方失去 Playwright `Locator` 类型信息。文件第 145 行还存在 `# type: ignore[union-attr]`，与「禁止屏蔽」的长期目标冲突。  
- **Proposed Architectural Solution**：为 `page` / 返回值引入 `playwright.async_api.Page` 与 `Locator` 类型（`from playwright.async_api import Page, Locator`），`_build_button_locator(self, page: Page) -> Locator`。若需延迟导入，使用 `TYPE_CHECKING` 块注解。删除 `type: ignore`。

---

### 14. `src/modules/site/handlers/interactor.py`

#### 14.1 `context: object` 上的 Crawlee API

- **Target File**：`src/modules/site/handlers/interactor.py`  
- **Error Message**：多处 `reportAttributeAccessIssue` / `reportReturnType` — `page`、`request`、`id`、`add_requests`、`evaluate` 等；`List[Request]` 不可分配给 `List[object]`。  
- **Root Cause**：将 Crawlee 的 `PlaywrightCrawlingContext`（或 `PlaywrightPreNavCrawlingContext`）降级为 `object`，导致属性与返回值链式失效。  
- **Proposed Architectural Solution**：从 `crawlee.crawlers` / `crawlee._types` 导入官方上下文类型（与项目已用的 `PlaywrightPreNavCrawlingContext` 模式一致），将 `trigger_download_buttons` 等方法的 `context` 参数改为该类型；返回值改为 `list[Request]`。若 Crawlee 未导出稳定符号，使用 `Protocol` 精确描述所需属性（`page`, `request`, `session`, `add_requests`）。

---

### 15. `src/modules/site/strategy.py`

#### 15.1 管线组件 Optional 与 `attach_probe` 契约

- **Target File**：`src/modules/site/strategy.py`  
- **Error Message**：大量 `reportOptionalMemberAccess` 与 `reportArgumentType` — `_audit_center`、`_interactor`、`_parser`、`_downloader`、`_net_sniffer`、`_strategist` 等；`attach_probe` 收到 `SiteAuditCenter | None`、`Downloader | None`；`native_download_task` 收到 `Any | None` 的 `page`。  
- **Root Cause**：`_assemble_pipeline()` 在正常 site 任务中会填满组件，但类型上仍为 `Optional`；`_default_page_handler` 未向类型系统证明「仅在全管线就绪后注册」。金丝雀子类进一步放大与 `Optional` 的冲突。  
- **Proposed Architectural Solution**：  
  - 引入**内部非可选快照**（例如 `_pipeline: SitePipeline` 数据类，在 `_assemble_pipeline` 末尾构建并赋值），`run()` 在注册 Crawlee handler 前 `assert self._pipeline is not None`。  
  - 或将 `_default_page_handler` 拆到嵌套类，仅在构造时注入非可选依赖。  
  - 对 `page`：在 handler 开头 `if page is None: return` 后使用 `assert page is not None` 收窄为 `Page`。  
  **接口契约**：`NetSniffer.attach_probe` 需要真实 `SiteAuditCenter` 与 `Downloader`；调用方必须仅在管线完整时调度。

---

### 16. `src/utils/logger.py`

#### 16.1 `get_logger` 返回注解无效

- **Target File**：`src/utils/logger.py`  
- **Error Message**：`reportInvalidTypeForm` — 类型表达式中不允许使用变量（`-> "logger"`）。  
- **Root Cause**：注解字符串 `"logger"` 被解析为**前向引用变量名**，而非 Loguru 类型；Loguru 无该符号。  
- **Proposed Architectural Solution**：改为 `from loguru import Logger` 并使用 `-> Logger`，或 `BoundLogger`（视 `bind` 返回值而定，可用 `logger.__class__` 或官方 typing 辅助）。与 `TYPE_CHECKING` 结合避免循环导入。

---

## `src/modules/site/audit` 专项说明

单独执行 `python -m pyright src/modules/site/audit` 结果为 **0 errors**。当前 Pylance/Pyright 未在该目录报告问题。  
若全量 `src` 扫描中包含间接依赖（例如从 `site/strategy` 穿透），问题已在上文 **`site/strategy`** 条目中归类。

---

## 建议的修复优先级（供人工 Review）

1. **P0（运行时）**：修正 `ProxyRotator.build()` 与 Crawlee `ProxyConfiguration` 的真实 API 对齐。  
2. **P0（类型与契约）**：`SiteCrawlStrategy` 管线非可选化或协议化，消除整组 Optional 级联错误。  
3. **P1**：`AbstractBrowserBackend` 构造签名、`BrowserFactory` 实例化、Crawlee 上下文类型在 `Interactor` / `ActionHandler` 落地。  
4. **P1**：`bridge` 与金丝雀 TypedDict 返回类型、窗口 `events` / `create_window` 收窄。  
5. **P2**：环境依赖（`tzlocal`、rebrowser 存根）、pywebview 存根、`get_logger` 注解、`crawlee_engine` 的 `profile` / `proxy_urls` 细节。

---

## 文件生成确认

审核内容已写入：

**`migration/error_resolution_audit.md`**

请在人工 Code Review 定稿后，再安排具体代码修改与合并。
