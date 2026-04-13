# 金丝雀系统 Bug 审查与代码证据收集报告

**审查范围：** `src/modules/canary/` 全量源码；`src/app/dispatcher.py`、`src/app/runner.py`、`src/app/bridge.py` 中与金丝雀调度/看板相关的路径；金丝雀继承链上的 `src/modules/site/strategy.py`（`SiteCrawlStrategy.run` / `cleanup` / Crawlee 挂载）。  
**审查方法：** 静态代码阅读与调用链推导；**未对仓库做任何代码修改**。  
**日期：** 2026-04-13  

---

## 1. 执行摘要

金丝雀（`CanaryMockStrategy`）通过调度器以 `task_info.is_canary=True` 走与站点任务相同的 `MasterDispatcher` → `SiteRunner` → `SiteCrawlStrategy.run()` 通路，复用 `CrawleeEngineFactory` 与 Playwright/Crawlee 生命周期。页面级逻辑由子类覆盖的 handler 替代，探针层对 `bot.sannysoft.com` 有独立 DOM 评估与超时封装。

**总体结论：**

- **异步与资源：** 金丝雀仍执行父类完整 `_assemble_pipeline()`，创建审计中心、下载器、NetSniffer 等重量级组件；虽默认 handler 不触发下载管线，但 **DB/磁盘/队列组件仍被构造**，存在资源与语义污染风险。Crawlee 与 Playwright 的关闭依赖 `crawler.run()` 结束后的 `cleanup()`，与主线路一致；未发现明显的「未 `await` 的裸 `create_task`」留在金丝雀专用 handler 路径上。
- **异常：** 页面 handler 与 sannysoft 探针外层大量 `except Exception`，极端情况下 **普通异常多被吞掉并仅打 debug**，可能导致 Crawlee 将失败请求记为成功处理；**`BaseException` 子类**（如 `KeyboardInterrupt`）不在 `Exception` 捕获范围内，理论上仍可中断事件循环。`CanaryMockStrategy.run` 在顶层异常时会 **更新看板后重新抛出**，由 `SiteRunner` / `Dispatcher` 记录，一般不会导致进程静默退出。
- **契约与逻辑：** `contracts.py` 的 `TypedDict` 与 `dashboard.build_payload` 及 UI（`UI/canary.js`）在字段形状上基本一致；`state` 在运行时可取 `warn`（探针合成逻辑），与 UI 的 `dot-warn` 一致。主要逻辑风险在于 **URL 与象限映射**、**种子数量与象限组不一致时的对齐策略**，以及 **`system_state=running` 与调度器 `_running` 标志的语义耦合**。

以下按审查重点分类给出 **文件路径、行号、证据片段与推导**。

---

## 2. 风险分类 A：异步并发与生命周期（Crawlee / Playwright）

### A1. 金丝雀仍装配完整 Site 管线（资源与副作用面）

**风险简述：** `CanaryMockStrategy` 未覆盖 `run()` 中除首尾看板逻辑外的 `SiteCrawlStrategy.run()` 主体，因此仍会调用 `_assemble_pipeline()`，实例化 `SiteAuditCenter`、`Downloader`、`NetSniffer` 等。金丝雀虽跳过 `_create_task_record` / `_update_task_status`，但 **审计目录、DB 连接、NetSniffer 队列** 等仍可能被创建并在 `cleanup()` 中走导出/停止逻辑，与「零生产账本」目标存在张力；高并发或磁盘慢时可能放大与主任务相同的 drain/cleanup 时序问题。

**证据：**

```174:201:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\site\strategy.py
    async def run(self) -> None:
        ...
        # ── Step 2: 装配管线 ─────────────────────────────────────────
        self._assemble_pipeline()

        # ── Step 3: 生成种子 URL（子类可覆盖 _resolve_seed_urls，如金丝雀直出靶场列表） ─
        seeds = await self._resolve_seed_urls()
```

```353:404:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\site\strategy.py
    def _assemble_pipeline(self) -> None:
        ...
        self._audit_center = SiteAuditCenter(
            db_path=app_cfg.db_path,
            base_save_dir=task.save_directory,
            ...
        )
        ...
        self._generator = SiteUrlGenerator()
        ...
        self._downloader = Downloader(...)
        ...
        self._net_sniffer = NetSniffer(...)
```

```79:91:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
    async def _create_task_record(self) -> None:
        ...
        _log.debug("[CanaryMockStrategy] 跳过 _create_task_record（金丝雀零 DB 账本）")

    async def _update_task_status(self, status: str) -> None:
        ...
        _log.debug(
            "[CanaryMockStrategy] 跳过 _update_task_status({!r})（金丝雀零 DB 账本）",
            status,
        )
```

**推导：** 「跳过 DB 任务行」不等于「跳过审计中心与文件系统工作区」；`_assemble_pipeline` 仍会创建 `SiteAuditCenter` 等。若外部架构师希望金丝雀 **完全隔离** 生产数据面，当前实现仅在任务表维度短路，**管线级隔离不完整**。

---

### A2. 双重「下载排空」轮询（时序冗余与长尾等待）

**风险简述：** `SiteCrawlStrategy.run()` 在 `crawler.run()` 之后已按 `files_active` 轮询排空；`SiteRunner.teardown()` 再次调用 `_drain_active_downloads()`。对金丝雀而言 `files_active` 多为 0，通常快速返回；但若 Downloader 因异常或竞态残留计数，**总等待上限可叠加**（策略内 120s + Runner 内 120s 量级），拉长事件循环占用时间，虽不典型构成死锁，但属于 **收尾路径重复与长尾风险**。

**证据：**

```269:273:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\site\strategy.py
        waited = 0.0
        while self.files_active > 0 and waited < _DRAIN_MAX_WAIT_SECS:
            await asyncio.sleep(_DRAIN_POLL_INTERVAL)
            waited += _DRAIN_POLL_INTERVAL
```

```326:327:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\app\runner.py
        # ── Step 1: 等待活跃下载归零（最长 _DRAIN_MAX_WAIT_SECS 秒）──────
        await self._drain_active_downloads()
```

**推导：** 非金丝雀专有问题，但金丝雀共享同一路径；在「体检应短平快」的产品预期下，**重复 drain 与审计 cleanup 可能使一次体检耗时高于直觉**。

---

### A3. `cleanup()` 与 Playwright 浏览器释放依赖 `SiteUrlGenerator.close()`

**风险简述：** 浏览器/Playwright 资源由 `SiteCrawlStrategy.cleanup()` 调用 `self._generator.close()` 释放。金丝雀同样走该路径。若 `close()` 内部在极端错误下阻塞或重入，会影响 **下一次** `run_with_config` 的 `self._running` 复位（`run_with_config` 的 `finally` 在 `await _execute_task` 返回后执行）。此问题与主线路共享，金丝雀不单独放大，但 **体检频繁触发时更易暴露**。

**证据：**

```298:347:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\site\strategy.py
    async def cleanup(self) -> None:
        ...
        # ── 4. 释放 Playwright 浏览器（generator） ───────────────────
        if self._generator is not None:
            try:
                await self._generator.close()
            except Exception as exc:
                _log.debug(f"[SiteCrawlStrategy.cleanup] generator.close() 异常: {exc!r}")
```

**推导：** 异常被吞在 debug 级别；资源是否真正释放依赖 `close()` 实现，审查时应在 `SiteUrlGenerator` 层继续向下追踪（本次范围未展开 generator 源文件）。

---

### A4. 跨线程投递：`run_coroutine_threadsafe` 无 Future 结果消费

**风险简述：** `run_canary_checkup` 将 `run_with_config` 投递到后台事件循环，**未保存 Future、未注册 done 回调**。当前 `run_with_config` / `_execute_task` 对 `run_task()` 异常有捕获，Future 多数情况下会正常完成；若未来改动使协程未捕获异常外溢，可能出现 **「Future 异常未检索」** 的 asyncio 警告或静默失败，GUI 侧仅看到「已提交」而任务未真实运行。

**证据：**

```654:657:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\app\bridge.py
        asyncio.run_coroutine_threadsafe(
            self._dispatcher.run_with_config(config_dict),
            self._loop,
        )
```

```334:340:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\app\dispatcher.py
        try:
            await self._runner.run_task()
        except Exception as exc:
            ...
            self._log(f"[Dispatcher] run_task() 顶层异常（已收尾）: {exc!r}", "error")
```

**推导：** 在**当前**代码版本下，顶层异常多半已被吸收；风险为 **维护演进时的脆弱性**，而非当下必现 Bug。

---

## 3. 风险分类 B：异常捕获与进程稳定性

### B1. 默认页面 handler 吞掉所有 `Exception`，错误不向 Crawlee 冒泡

**风险简述：** `CanaryMockStrategy._default_page_handler` 将 `_canary_page_handler_core` 包在 `try/except Exception` 中，仅 `_log.debug`。Crawlee 可能将请求标为处理完成，**重试/失败统计与真实 DOM/网络故障脱节**；在强反爬或协议错误场景下，**看板可能长时间保持 `idle` 或部分更新**，与 `failed_request_handler` 是否触发取决于 Crawlee 是否将其归类为 failed request。

**证据：**

```128:132:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
    async def _default_page_handler(self, context: Any) -> None:
        try:
            await self._canary_page_handler_core(context)
        except Exception as exc:
            _log.debug("[CanaryMockStrategy] default_handler 已吞异常（不冒泡至 Crawlee）: {}", exc)
```

**推导：** 这是 **刻意的静默策略**，利于不崩溃，但代价是 **可观测性与 Crawlee 语义不一致**；外部架构师需权衡是否要在金丝雀模式下 **选择性向上抛出** 或 **显式标记 request 失败**（需查 Crawlee Python 的 API）。

---

### B2. Sannysoft 探针：内层超时与异常兜底较完整，外层仍仅 debug

**风险简述：** `_extract_page_bundle` 使用 `asyncio.wait_for` 包裹 `page.evaluate`，超时返回 `{"error": "timeout"}`；`run_sannysoft_identity_probe` 外层 `except Exception` 返回四行 `fail`。因此 **一般 Python 异常不会击穿到主进程**。但若 Playwright/CDP 在极底层抛出 **非 `Exception` 继承** 的罕见错误（极少见），或事件循环级故障，仍可能逃逸（属框架级假设）。

**证据：**

```598:611:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\probes_sannysoft.py
async def _extract_page_bundle(page: Any) -> Dict[str, Any]:
    try:
        raw = await asyncio.wait_for(
            page.evaluate(_SANNYSOFT_EXTRACT_JS),
            timeout=_EVAL_TIMEOUT_SECS,
        )
        ...
    except asyncio.TimeoutError:
        return {"error": "timeout"}
    except Exception as exc:
        _log.debug("[sannysoft] evaluate 异常: {}", exc)
        return {"error": str(exc)}
```

```614:634:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\probes_sannysoft.py
async def run_sannysoft_identity_probe(
    page: Any,
    settings: PrismSettings,
) -> List[Tuple[str, str, str]]:
    try:
        expected = _expected_profile(settings)
        bundle = await _extract_page_bundle(page)
        return build_sannysoft_probe_updates(bundle, expected)
    except Exception as exc:
        _log.debug("[sannysoft] run_sannysoft_identity_probe 兜底: {}", exc)
        fd = DOM_PROBE_FAIL_DESC
        return [
            ("identity_locale", "fail", fd),
            ...
        ]
```

**推导：** 探针路径对 **可预见的网络/DOM/超时** 防御较好；与 B1 结合，**策略层仍可能吞掉探针外的其它异常**（见 B1）。

---

### B3. `CanaryMockStrategy.run` 顶层异常：看板失败后 **重新抛出**

**风险简述：** `mark_all_failed` 后 `raise` 会使 `SiteRunner.execute` 再次记录并 `raise`，最终由 `Dispatcher._execute_task` 捕获。进程通常不崩溃，但 **金丝雀 run 的语义是「失败时仍抛异常」**，与「合成任务应始终温和结束」的直觉可能不一致；若上层未来去掉 `Dispatcher` 中的捕获，异常会向外传播。

**证据：**

```55:74:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
    async def run(self) -> None:
        ...
        try:
            await super().run()
            set_progress(100)
        except Exception as exc:
            _log.exception("[CanaryMockStrategy] 合成任务未捕获异常（已映射到看板）")
            mark_all_failed(f"任务异常: {exc!r}")
            raise
```

```304:317:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\app\runner.py
        except Exception as exc:
            self._log(
                f"[SiteRunner] strategy.run() 抛出顶层异常: {exc!r}",
                "error",
            )
            await _drive_to_terminal(...)
            raise
```

**推导：** 当前 **Dispatcher 捕获 `run_task` 异常** 时不会再次向上抛（见上文 A4 引用），故 **端到端仍稳定**；风险在于 **契约文档与后续调用方** 对「是否抛异常」的预期不一致。

---

### B4. `BaseException` 未在 handler 中捕获

**风险简述：** 所有 `except Exception` **不捕获** `KeyboardInterrupt`、`SystemExit`。在人工中断或嵌入环境中，若信号注入到协程栈，仍可能终止循环；属 Python 常规语义，非金丝雀独有，但审查清单要求「极端情况」时需写明。

**证据：** 同上各 `except Exception` 块（strategy/probes），Python 语言层面行为。

---

## 4. 风险分类 C：契约（contracts）与策略/看板逻辑

### C1. `contracts.py` 与 `dashboard.build_payload` 形状一致；`state` 未枚举约束

**风险简述：** `CanaryDashboardDict` 要求 `quadrants` 为四键、`QuadrantItemDict` 含 `id/label/state/desc`。`dashboard._default_quadrants()` 与 `build_payload()` 满足该形状。`build_sannysoft_probe_updates` 可产生 `state == "warn"`，**未在 TypedDict 注释中列出**，但为 `str` 类型，静态类型检查宽松；**UI `canary.js` 已支持 `warn`**（`DOT_CLASS.warn`），前后端一致。

**证据：**

```24:30:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\contracts.py
class CanaryDashboardDict(TypedDict):
    """fetch_canary_dashboard 返回体。"""

    system_state: str
    current_engine: str
    progress_percent: int
    quadrants: QuadrantsDict
```

```105:130:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\dashboard.py
def build_payload(
    *,
    dispatcher_running: bool,
    is_canary_active: bool,
    current_engine: str,
) -> CanaryDashboardDict:
    ...
    if is_canary_active:
        system_state = "running"
    elif dispatcher_running:
        system_state = "locked"
    else:
        system_state = "idle"
    return {
        "system_state": system_state,
        ...
    }
```

**推导：** **无结构性断层**；若外部系统只接受 `pass|fail|idle`，则需文档化 `warn`。

---

### C2. `system_state == "running"` 依赖「当前策略对象仍为金丝雀」且 `_running == True`

**风险简述：** `is_canary_active` 来自 `dispatcher.is_canary_run_active()`，实现为检查 `self._strategy.is_canary_strategy`。`run_with_config` 的 `finally` 会清空 `_strategy`。若在任务刚结束瞬间轮询，可能出现 **极短窗口** 内 `dispatcher_running` 与 `is_canary_active` 的组合变化；一般仅影响 UI 闪烁，不构成数据错误。

**证据：**

```156:159:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\app\dispatcher.py
    def is_canary_run_active(self) -> bool:
        strat = self._strategy
        return bool(getattr(strat, "is_canary_strategy", False))
```

```112:119:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\app\dispatcher.py
        self._running = True
        try:
            await self._execute_task(config_dict)
        finally:
            self._strategy = None
            self._runner   = None
            self._running  = False
```

**推导：** **语义正确**；仅 UI 层需注意轮询节奏。

---

### C3. URL → 象限映射：子串匹配与种子数不一致

**风险简述：**

1. `_quadrant_for_url` 使用 `url.startswith(base) or base in url`，**子串误匹配** 时会把页面归到错误象限（例如某 URL 偶然包含另一种子路径片段）。
2. 当 `target_urls` 数量与 `_CANARY_QUADRANT_GROUPS` 不一致时，仅打 warning，**按最短长度对齐**；多出的 URL 会落入「索引越界」分支，最终 **默认映射到第一象限组**（`return _CANARY_QUADRANT_GROUPS[0]`），造成 **网络象限承担非网络靶场** 的误判风险。

**证据：**

```106:114:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
    def _quadrant_for_url(self, url: str) -> Tuple[str, List[str]]:
        seeds = getattr(self, "_canary_seeds", []) or self.settings.strategy_settings.target_urls
        for i, seed in enumerate(seeds):
            if i >= len(_CANARY_QUADRANT_GROUPS):
                break
            base = seed.split("?")[0].rstrip("/")
            if url.startswith(base) or base in url:
                return _CANARY_QUADRANT_GROUPS[i]
        return _CANARY_QUADRANT_GROUPS[0]
```

```55:64:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
        ...
        if n != len(_CANARY_QUADRANT_GROUPS):
            _log.warning(
                "[CanaryMockStrategy] 靶场 URL 数量({})与象限组数({})不一致，按最短长度对齐",
                n,
                len(_CANARY_QUADRANT_GROUPS),
            )
```

**推导：** 属于 **逻辑正确性/可解释性** 风险，而非崩溃类 Bug；架构师修复时可考虑 **显式 URL→象限表** 或 **禁止子串 in url**。

---

### C4. Sannysoft 专用分支与其它靶场进度计数

**风险简述：** Sannysoft URL 命中时，在 `try` 末尾 `_bump_progress()` 并 `return`；其它 URL 在占位逻辑末尾也 `_bump_progress()`。若某请求 **仅走 failed_request** 而未走 default handler（或反之），**进度条与象限完成度可能不完全一致**；需结合 Crawlee 对重试/跳过的行为理解，属 **产品精度** 问题。

**证据：**

```150:174:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
        if (
            page is not None
            and status_code < 400
            and self._is_sannysoft_probe_url(current_url)
        ):
            try:
                updates = await run_sannysoft_identity_probe(page, self.settings)
                apply_sannysoft_probe_updates(updates)
            except Exception as exc:
                ...
            try:
                self._bump_progress()
            except Exception:
                pass
            return
```

```206:236:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
    async def _canary_failed_request_core(self, context: Any) -> None:
        ...
        try:
            set_quadrant_group(
                group,
                [(iid, "fail", err[:500]) for iid in item_ids],
            )
            self._bump_progress()
        except Exception as exc:
            _log.debug("[CanaryMockStrategy] failed_request 写看板失败: {}", exc)
```

**推导：** 需用端到端测试枚举「重定向、拦截、仅失败回调」等路径验证进度是否单调到 100%。

---

### C5. `bridge.run_canary_checkup` 与策略内 `reset_for_new_run` 双次重置

**风险简述：** `run_canary_checkup` 先 `reset_for_new_run()`，`CanaryMockStrategy.run` 再次 `reset_for_new_run()`。功能上无害；若两次之间 UI 轮询，可能看到 **一次空白重置**，属体验细节。

**证据：**

```637:638:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\app\bridge.py
        reset_for_new_run()
        _append_canary_log("INFO", "[系统] 已提交金丝雀合成任务（与主线路共享 Crawlee 与反爬栈）")
```

```66:67:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
        reset_for_new_run()
        set_progress(0)
```

---

## 5. 风险分类 D：与主线路行为差异（安全/反爬）

### D1. 金丝雀不注册 `NEED_CLICK` handler，与 `SiteCrawlStrategy` 不一致

**风险简述：** 金丝雀 `_register_crawlee_handlers` 仅注册 `default_handler` 与 `failed_request_handler`，**不注册** `NEED_CLICK`。这是刻意的缩小攻击/下载面；但若某靶场页依赖交互才能暴露指纹特征，**体检可能偏「乐观」**。

**证据：**

```93:104:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
    def _register_crawlee_handlers(self, crawler: Any) -> None:
        ...
        crawler.router.default_handler(self._default_page_handler)
        if hasattr(crawler, "failed_request_handler"):
            crawler.failed_request_handler = self._failed_request_handler
```

对比父类：

```460:461:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\site\strategy.py
        crawler.router.default_handler(self._default_page_handler)
        crawler.router.handler("NEED_CLICK")(self._action_handler.handle_action)
```

**推导：** 设计取舍，非缺陷；需在 PRD 中写明 **金丝雀不等于完整站点抓取行为**。

---

### D2. 挑战检测在金丝雀 handler 中异常被吞

**风险简述：** `_maybe_handle_challenge_page` 在 sannysoft 分支与普通分支均被调用，但外层对 `Exception` 的吞掉可能导致 **挑战未解决仍继续后续逻辑**（视 Crawlee 页面状态而定）。

**证据：**

```144:148:c:\Users\MONO-MA14\Desktop\WrightBot_V1\src\modules\canary\strategy.py
        if page is not None and self._challenge_solver is not None:
            try:
                await self._maybe_handle_challenge_page(page, status_code)
            except Exception as exc:
                _log.debug("[CanaryMockStrategy] 挑战检测异常（忽略）: {}", exc)
```

**推导：** 与 B1 同类——**稳定性换诊断精度**。

---

## 6. 供外部架构师使用的检查清单（无代码修改）

| 编号 | 主题 | 建议方向（由架构师决策） |
|------|------|--------------------------|
| P1 | 管线重量 | 是否要为 `is_canary` 提供「轻量装配」路径，避免 SiteAuditCenter/Downloader 等在体检中实例化 |
| P2 | Handler 异常语义 | 是否在金丝雀模式下将部分异常映射为 Crawlee failed 或结构化指标，而非仅 debug |
| P3 | 顶层异常策略 | `CanaryMockStrategy.run` 是否在映射看板后改为「不 raise」以统一「合成任务永不抛」契约 |
| P4 | URL→象限 | 是否改为精确匹配或配置表，去掉 `base in url` |
| P5 | Future 观测 | `run_coroutine_threadsafe` 是否注册 done 回调写金丝雀日志或指标 |
| P6 | 引擎类型 | `crawler_type == beautifulsoup` 时无 Playwright Page，sannysoft 探针不触发；是否要在 UI 标明「仅 Playwright 全量体检」 |

---

## 7. 引用文件索引

| 路径 | 与金丝雀关系 |
|------|----------------|
| `src/modules/canary/contracts.py` | 看板 JSON TypedDict |
| `src/modules/canary/dashboard.py` | 内存态、锁、`build_payload` |
| `src/modules/canary/strategy.py` | `CanaryMockStrategy` |
| `src/modules/canary/probes_sannysoft.py` | Sannysoft DOM 探针 |
| `src/app/dispatcher.py` | 策略选择、`is_canary_run_active` |
| `src/app/runner.py` | `SiteRunner` teardown/drain |
| `src/app/bridge.py` | 看板 API、`run_canary_checkup` |
| `src/modules/site/strategy.py` | 父类 run/cleanup/Crawlee 挂载 |
| `UI/canary.js` | 前端状态渲染（含 `warn`） |

---

**报告结束。** 本文档仅含审查结论与代码证据，**不包含具体补丁或重构方案**（由接收方架构师出具）。
