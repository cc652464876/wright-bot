# PrismPDF 爬虫核心架构升级与重构指南

**文档性质**：Migration & Upgrade Guide（供二次安全与规范审核）  
**依据**：`migration/old_core_vs_src_paths_and_error_snapshots.md` 与当前 `src/` 目录事实结构  
**读者**：接手未迁移模块的开发者 / 自动化重构代理

---

## 1. 架构演进概述 (Architecture Overview)

### 1.1 旧版 `core` / `old_core` 的主要痛点

- **存储与并发耦合**：审计数据以 `defaultdict(asyncio.Lock)` + 内存字典 + 高频 JSON/JSONL 原子写为主，路径拼接分散在 Runner、Handler、AuditCenter 多处，易出现竞态与 I/O 放大。
- **配置形态不统一**：业务大量消费「扁平 `config_dict`」，`task_info` / `strategy_settings` / `engine_settings` 边界靠约定而非类型约束，跨模块复制 `save_directory`、`strategy_prefix` 易漂移。
- **可观测性与 UI 契约隐式**：`manifest.json`、`interactions.json`、`scanned_urls.jsonl`、`scan_errors_log.txt` 等文件布局即契约；重构时若未同步导出层，前端或脚本会静默失效。
- **错误快照与上下文脱节风险**：`error_interceptor` 依赖 `ContextVar`（`current_registry` + `current_error_workspace_resolver` 或显式 `current_error_dir`）；若在策略入口未注入，拦截器逻辑存在但**永不生效**（对照报告已记录 `old_core` 内未见 `.set()`）。
- **防反爬与引擎逻辑堆叠**：浏览器启动、下载目录、指纹/会话等参数与「站点工厂」揉在同一模块（如旧 `site_engines.py`），不利于替换后端与单测。

### 1.2 新版 `src` 架构的核心优势

- **分层清晰**：`src/config`（`PrismSettings` / `get_app_config`）→ `src/app`（`dispatcher`、`runner`、`bridge`）→ `src/modules/*`（按业务策略）→ `src/engine`（Crawlee 与浏览器）→ `src/db`（持久化契约）。
- **状态机显式化**：`src/engine/state_manager.py` 与策略的 `run()` / `cleanup()` 配合，便于统一停机、错误态与收尾顺序（审计导出、队列 drain）。
- **审计与路径黑盒保留、实现替换**：`SiteAuditCenter` 仍暴露同名 Hook，但运行时以 **SQLite WAL + 单写者** 为主；`export_final_reports()` 承担对旧文件格式的**兼容快照**职责。
- **防反爬「工具箱」归位**：`src/engine/anti_bot/` 下聚类——`stealth/`（Playwright / Rebrowser / Camoufox 等后端）、`behavior/`（点击策略可插拔）、`proxy_rotator.py`、`challenge_solver.py`、`fingerprint.py`；与业务 Handler 解耦，由 `CrawleeEngineFactory` / `BrowserFactory` 组装。
- **错误登记与快照可测试**：`SiteCrawlStrategy.run()` 内**显式** `current_registry.set` 与 **`current_error_workspace_resolver.set(audit._get_workspace)`**；`error_interceptor` 按当前请求 URL 解析域名，快照写入 **`{save_directory}/{prefix}_{domain}/errors/`**。`cleanup()` 按域导出 **`error_report.md`**（过滤条目，见 `ErrorRegistry.export_to_markdown_for_domain`）。

### 1.3 架构原则（迁移时必须遵守）

| 原则 | 说明 |
|------|------|
| **单一配置源** | 业务代码只读 `PrismSettings` / `get_app_config()`，禁止在新代码中手写 `os.getcwd()` + `"./downloads"` 作为默认保存根。 |
| **路径只从 AuditCenter 或 Strategy 推导** | 域名工作区一律 `audit_center._get_workspace(domain)` 或封装方法；禁止 Handler 内重复拼接 `{prefix}_{domain}` 规则。 |
| **文件契约显式化** | 若 UI/脚本仍依赖某 JSON/JSONL，必须在文档与 `export_*` 或适配层中列出；不可假设「写 DB 即等价于旧文件树」。 |
| **ContextVar 与 async 边界** | 任何依赖 `ContextVar` 的能力（错误快照、请求上下文）必须在明确的生命周期内 set/reset（`try/finally`），且文档注明生效范围。站点线快照目录由 **`current_error_workspace_resolver(domain→workspace)`** 推导，**禁止**在多域任务中把单一全局 `errors` 路径写入 `current_error_dir`（除非刻意兼容旧单测）。 |
| **最终报告 vs 实时日志流** | **最终报告（Manifest）**：任务收尾时由 `export_final_reports()` 从 DB 导出各域名 `manifest.json`，表示「成果快照」，非高频写入。**实时日志流（JSONL/TXT）**：默认关闭；仅当 `task_info.enable_realtime_jsonl_export == True` 时，由 `RealtimeFileExporter` 在 Hook 内与 DB **双写** `scanned_urls.jsonl`、`scan_errors_log.txt`、**`interactions.jsonl`**。需要旧式「边跑边 tail 文件」的 UI 必须在启动任务 JSON 中显式打开该开关。 |

**（UI 必读 —— 交互日志文件名与格式已变）** **`interactions.json`（旧版：单个 JSON 数组文件）已不再作为实时流输出。** 开启 `enable_realtime_jsonl_export` 后，交互事件写入 **`interactions.jsonl`**（**后缀 `.jsonl`**），每行一个独立 JSON 对象，且行内会包含 **`domain`** 字段。UI 若仍用 `JSON.parse` 整文件读取或硬编码 `interactions.json`，将**失败**；请改为按行解析 JSONL（例如 `readline` + `JSON.parse` 每行）或改为轮询 DB/API。

---

## 2. 核心路径映射表 (Key Path Mappings)

格式：**`[旧路径]` → `[新路径]`**：迁移说明 / 功能变化。

| 旧路径（`old_core` / `core` 习惯） | 新路径（`src`） | 迁移说明 / 功能变化 |
|-----------------------------------|-----------------|---------------------|
| `site_audit_center.py` → `SiteAuditCenter` | `src/modules/site/audit/audit_center.py` + `audit/realtime_file_exporter.py` | 构造参数增加 `db_path`、可选 `realtime_exporter`。运行时以 **DB** 为主（`scan_records`、`downloaded_files`、`error_log`）；`record_interaction` 默认仍无 DB 表。收尾 **`export_final_reports()`** 导出各域名 **`manifest.json`**。若 `task_info.enable_realtime_jsonl_export=True`，委托 **`RealtimeFileExporter`** 追加 **`scanned_urls.jsonl` / `scan_errors_log.txt` / `interactions.jsonl`**（交互为 JSONL，**非**旧版 `interactions.json`）。扫描类 Hook 需 **`set_task_id()`** 成功后才会写 DB 与上述实时文件。 |
| `site_error_system.py` → `ErrorRegistry` / `error_interceptor` | `src/modules/site/audit/error_registry.py` | 登记、去重指纹、快照、re-raise。站点线通过 **`current_error_workspace_resolver`** 绑定 `audit._get_workspace`；快照目录为 **`{save_directory}/{prefix}_{domain}/errors/`**（按当前页 URL 解析 domain）。仍支持显式 **`current_error_dir`** 覆盖（兼容/测试）。收尾按域 **`errors/error_report.md`**（`export_to_markdown_for_domain`）。超时：`asyncio.wait_for(..., 5.0)`。 |
| `site_runner.py` → `SiteCrawlerRunner` | `src/modules/site/strategy.py` → `SiteCrawlStrategy` + `src/app/runner.py` / `dispatcher.py` | Runner 职责拆为：**策略**（装配管线、种子、Crawlee、ContextVar、FSM）与 **应用入口**（调度多模式任务）。不再直接持有巨型 `config_dict` 贯穿全局，改为 `PrismSettings`。 |
| `master_dispatcher.py` | `src/app/dispatcher.py` | 任务路由与生命周期托管；应对接 `BaseCrawlStrategy` 子类而非具体 `SiteCrawlerRunner` 类名硬编码。 |
| `site_engines.py` → `SiteCrawlerFactory` | `src/engine/crawlee_engine.py` → `CrawleeEngineFactory` + `src/engine/anti_bot/stealth/*.py` | 浏览器启动、`_temp_playwright`、`downloads_path`、指纹/会话等迁入工厂与 Stealth 后端；配置来自 `settings` + `get_app_config().download_temp_subdir`。 |
| `site_request_handler.py` + `site_request_handler_*.py` | `src/modules/site/handlers/`（`downloader`、`interactor`、`strategist`、`action`、`action_downloader`、`net_sniffer`） | 职责链节点化；依赖通过构造函数注入（`settings`、`audit_center`、`is_running` 等）。 |
| `site_generator.py` / `site_parser.py` / `site_utils.py`（域名等） | `src/modules/site/generator.py`、`parser.py`（及 `strategy` 内聚逻辑） | 保持「纯业务」与引擎层分离；工具函数集中或收拢到 `src/utils`，避免循环 import。 |
| `site_monitor.py`（若存在） | `src/app/monitor.py`、`net_monitor.py` | 监控与 UI 桥接走 `bridge`；统计优先读 **DB / audit 公开 API**，而非直接扫磁盘 JSON。 |
| 策略前缀 `strategy_settings.strategy_prefix`（默认 `site`） | `strategy_settings.crawl_strategy`（`direct` / `full` / `sitemap`）传入 `SiteAuditCenter` 作目录前缀 | **子目录命名规则变更**：由 `site_<domain>` 可能变为 `full_<domain>` 等。UI 与脚本硬编码 `site_` 会失效；需配置对齐或兼容映射层。 |

**关键调度流（站点线）**：`Dispatcher` → `SiteCrawlStrategy.validate/run/cleanup` → `CrawleeEngineFactory.create()` → 注册 handler → `crawler.run(seeds)`（期间 **`current_registry` + `current_error_workspace_resolver`**）→ `cleanup`（`export_final_reports`、**分域** `error_report.md`、NetSniffer stop、generator close）。

---

## 3. 典型错误与避坑指南 (Common Pitfalls & Error Snapshots)

以下分类覆盖对照报告中「路径 / 快照 / 契约」类问题，并扩展到迁移期高频工程问题。

### 3.1 错误快照（Error Snapshots）从不生成

| 子类 | 现象 | 根因 | 标准修复 |
|------|------|------|----------|
| **ContextVar 未注入** | DOM 异常抛出但无 `*_screenshot.png` / `*_page.html` | `registry is None`，或未设置 **`current_error_workspace_resolver`** 且未设置显式 **`current_error_dir`**，导致无法解析 `err_dir`。 | 站点线在 `crawler.run()` 外 `set` **`current_error_workspace_resolver`**（绑定 `_get_workspace`）+ `current_registry`；`finally` **reset**。 |
| **异常发生在拦截器外** | Crawlee `failed_request_handler` 仅记日志/DB，无图 | 重试耗尽、路由失败等路径未包在 `async with error_interceptor` 内。 | 对需取证的关键路径补拦截器，或在 failed_handler 内调用**显式**快照 API（需 page 仍存活）；文档注明「仅 handler 内异常可快照」。 |
| **快照二次失败** | 日志含「快照保存失败」 | `TargetClosedError`、超时、磁盘权限、页面已销毁。 | 保持短超时（≤5s）、快照失败只打日志不吞主异常；必要时降 `full_page`、捕获后仍 `raise` 原异常。 |

### 3.2 UI / 外部工具「找不到文件」或目录名不一致

| 子类 | 现象 | 根因 | 标准修复 |
|------|------|------|----------|
| **缺失 JSONL/TXT** | 无 `scanned_urls.jsonl`、`scan_errors_log.txt` | 默认仅写 **DB**，不刷实时文件。 | 在任务配置中设置 **`task_info.enable_realtime_jsonl_export: true`**；或改 UI 读 DB/Bridge；或在收尾批量导出。 |
| **缺失交互日志文件** | 找不到 `interactions.json` 或 tail 无输出 | 默认不写盘；开启实时流后文件名为 **`interactions.jsonl`**。 | UI 按 **JSONL 行协议**读取；若必须数组 JSON，需在适配层聚合或等待 `interactions` 表 + 导出任务。 |
| **子目录前缀变化** | 脚本找 `site_*`，实际为 `full_*` | `strategy_prefix` 与 `crawl_strategy` 语义合并/重命名。 | 配置层提供 **`legacy_prefix` 映射** 或 UI 可配置「导出用前缀」；文档列出公式：`{base}/{prefix}_{domain}`。 |
| **错误目录位置假设错误** | 脚本在 **`{save_directory}/errors`** 找快照或报告 | **新版（站点线）已改为按域隔离**：**`{save_directory}/{prefix}_{domain}/errors/`**（与 manifest / JSONL 工作区同前缀）。`error_report.md` 为**分域过滤导出**（每域一份，仅含与该域 URL 相关的条目；无 URL 上下文归入 **`unknown_domain`** 工作区）。若仍见全局目录，多为旧文档或未走站点策略。 |

### 3.3 导包与运行环境

| 子类 | 现象 | 根因 | 标准修复 |
|------|------|------|----------|
| **`core.*` 残留** | `ModuleNotFoundError: core` | 仍引用旧包名。 | 全量替换为 `src.*` 或项目约定的根包；用 `rg "from core\."` / `rg "import core"` 门禁。 |
| **循环导入** | 启动即 ImportError | 新分层下 handler ↔ engine 互相顶层 import。 | 依赖注入 + `TYPE_CHECKING` + 函数内延迟 import；引擎接口放 `protocol` 或窄抽象模块。 |

### 3.4 配置与路径硬编码

| 子类 | 现象 | 根因 | 标准修复 |
|------|------|------|----------|
| **保存目录落到 cwd** | 文件出现在意外根目录 | 未传 `task_info.save_directory` 或默认 `./downloads` 与 UI 不一致。 | 单一入口校验 `validate()` 中 `makedirs`；Bridge/API 必须传绝对路径。 |
| **`_temp_playwright` 不可写** | Playwright 下载失败 | 子目录未创建或权限问题。 | 沿用 `CrawleeEngineFactory._build_download_dir()`；勿绕开工厂私自改 `downloads_path`。 |

### 3.5 审计 Hook 「静默无写入」

| 子类 | 现象 | 根因 | 标准修复 |
|------|------|------|----------|
| **`task_id` 未注入** | `_guard_task_id` 警告，DB 无行 | `SiteAuditCenter` 须在 DB 任务行创建后 `set_task_id`。 | 在 `SiteCrawlStrategy._create_task_record()` 成功后立即调用；集成测试断言 `task_id is not None`。 |

---

## 4. 重构执行的 SOP（标准作业程序）

适用于「将剩余 `core`/`old_core` 模块迁入 `src`」或「新增一条业务策略」。

### 4.1 前置盘点（必须）

1. 列出模块职责：纯业务 / I/O / 引擎 / UI 桥接，标红**禁止**下沉到 Handler 的代码（如浏览器启动细节）。
2. 标出所有**外部契约**：读写的文件名、目录结构、环境变量、配置键名。
3. 标出所有 `async` 边界与是否依赖 `ContextVar`。

### 4.2 Import 与包结构

1. 新代码路径落在 `src/` 下明确层级：`config` / `app` / `modules/<domain>` / `engine` / `db` / `utils`。
2. 将 `from core.xxx` 改为 `from src....`，并运行静态检查与最小启动测试。
3. 禁止在新模块顶层 import 重量级引擎（Playwright/Crawlee），除非该文件本身即为 factory。

### 4.3 依赖注入调整

1. **构造器注入**：`settings: PrismSettings`、`audit_center`、`log`/`get_logger`、可选 `state_manager`、`proxy_rotator`、`challenge_solver`。
2. **禁止**在业务类内部 `get_settings()` 单例（除非项目明确为进程级单例且可测）；若使用，须在测试中可替换。
3. Crawlee 相关对象仅由 `CrawleeEngineFactory` 或 `BrowserFactory` 创建；业务只拿「已配置好的 crawler / context 工厂」。

### 4.4 路径与存储

1. 所有「按域名落盘」路径经 **`SiteAuditCenter._get_workspace(domain)`** 或 Strategy 封装方法。
2. 若需保留旧文件契约：在 **`export_final_reports` 或 cleanup 阶段**批量导出，避免恢复「每次 Hook 写大 JSON」除非性能已评估。
3. Playwright 下载临时目录仅使用 **`save_directory` + `download_temp_subdir`**（默认 `_temp_playwright`），与域名工作区分离。
4. **实时 JSONL/TXT** 一律经 **`RealtimeFileExporter`**（`src/modules/site/audit/realtime_file_exporter.py`）写入；**禁止**在 Handler 内手写同路径文件，避免锁与去重逻辑分裂。

### 4.5 错误快照与可观测性

1. 凡使用 `error_interceptor` 的代码路径，须保证调用栈外层存在 **已文档化的** `current_registry` 生命周期；站点线还须设置 **`current_error_workspace_resolver`**（或显式 `current_error_dir`）。
2. 新增异步后台任务时，检查是否在 `reset` ContextVar **之后**仍运行（会导致快照目录错乱）；必要时在子任务内复制所需路径字符串而非依赖 Var。

### 4.6 防反爬与目录安置（最佳实践）

1. **Stealth**：新浏览器后端放在 `src/engine/anti_bot/stealth/`，实现统一接口，由 `BrowserFactory` 注册表选择。
2. **行为模拟**：新鼠标/键盘策略放在 `src/engine/anti_bot/behavior/`，实现 `AbstractBehaviorSimulator`，由 `Interactor` 注入。
3. **指纹/代理/挑战**：分别落在 `fingerprint.py`、`proxy_rotator.py`、`challenge_solver.py`，**不**在 `modules/site/handlers` 内硬编码厂商 SDK。
4. 配置项进入 `PrismSettings` / `AppConfig`，并在 `settings.py` 中注明默认值与 UI 映射键。

### 4.7 向后兼容与灰度

1. **实时文件双写**（可选）：`task_info.enable_realtime_jsonl_export`（默认 `False`）。为 `True` 时由 `RealtimeFileExporter` 与 DB 同步追加；**关闭时无磁盘风暴**。旧 UI 依赖 tail 时必须打开。
2. **兼容导出**：`manifest.json` 仍由收尾导出；格式保持与旧工具一致；字段变更时提供版本号或并列文件 `manifest.v2.json`。
3. 在 `migration/` 下维护对照表与本指南的**变更日志**（日期、PR、行为差异）。

### 4.8 验收清单（PR 合并前）

- [ ] `validate()` 覆盖保存目录可写、`crawl_strategy` 合法。
- [ ] `set_task_id` 在首条审计写入前完成。
- [ ] `cleanup()` 顺序：队列 drain → `export_final_reports` → ErrorRegistry Markdown → 关闭浏览器。
- [ ] 全文检索无 `from core.`（或仅隔离在 shim 层且已标记弃用）。
- [ ] 文档更新：`Key Path Mappings` 与 UI 契约一致；若变更 `errors/` 位置或 **`interactions.jsonl`** 格式，发布说明中 **加粗** 提醒。
- [ ] 若 UI 依赖实时 tail：已在配置中验证 **`enable_realtime_jsonl_export`** 与 **`.jsonl` 解析**路径。

---

## 附录：与本指南配套的仓库内文档

| 文件 | 用途 |
|------|------|
| `migration/old_core_vs_src_paths_and_error_snapshots.md` | 路径与快照行为的事实对照（含代码锚点） |
| `migration/MIGRATION_AND_UPGRADE_GUIDE.md` | 本文：迁移规则、避坑、SOP |

---

*本文档由架构迁移上下文自动生成整理，提交审核前请结合当前 `main` 分支代码再核对一遍行号与符号名。*
