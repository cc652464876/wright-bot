# old_core 与 src 路径、日志与错误快照对照报告

> 检索范围：`old_core/`、`src/`。用途：向外部专家提供诊断上下文。  
> 生成说明：基于代码静态检索整理。

---

## 第一部分：`old_core` 实现

### 1. 目录树与四类日志落盘

#### 基准路径 + `strategy_prefix` + 域名 → 任务子目录

`SiteCrawlerRunner` 从 `task_info.save_directory` 与 `strategy_settings.strategy_prefix`（默认 `"site"`）构造 `SiteAuditCenter`：

```88:96:old_core/site_runner.py
        task_info = config_dict.get("task_info", {})
        save_dir = task_info.get("save_directory", "./downloads")
        strategy_prefix = config_dict.get("strategy_settings", {}).get("strategy_prefix", "site")
        
        # 2. 备份配置 
        backup_config(config_dict, save_dir)

        # 🌟 3. 实例化大一统审计中心
        self.audit_center = SiteAuditCenter(base_save_dir=save_dir, strategy_prefix=strategy_prefix)
```

#### 黑盒工作区（即 `保存路径/{strategy_prefix}_{domain}/`）

```61:70:old_core/site_audit_center.py
    def _get_workspace(self, domain: str) -> str:
        """路径黑盒化：外部不再允许随意 os.path.join (带内存级防抖缓存)"""
        workspace = os.path.join(self.base_save_dir, f"{self.strategy_prefix}_{domain}")
        
        # 💡 只有内存中没有记录过，才去触碰磁盘创建文件夹
        if workspace not in self._initialized_workspaces:
            os.makedirs(workspace, exist_ok=True)
            self._initialized_workspaces.add(workspace)
            
        return workspace
```

#### 下载/探针侧与同构路径（与审计中心一致）

```133:134:old_core/site_request_handler.py
        # 🌟 临时保留计算 domain_workspace，专门供给尚未重构的 Downloader 和 NetworkMonitor
        domain_workspace = os.path.join(self.save_dir, f"{self.strategy_prefix}_{domain}")
```

#### 四类文件如何进入任务目录

| 文件 | 路由方式（均在 `_get_workspace(domain)` 下） |
|------|---------------------------------------------|
| `manifest.json` | `record_result_batch` → `os.path.join(workspace, "manifest.json")` |
| `interactions.json` | `record_interaction` → 同上 |
| `scanned_urls.jsonl` | `record_page_success` / `record_page_failure` → 同上 |
| `scan_errors_log.txt` | `record_page_failure` 在写 jsonl 后追加 TXT |

代表性片段：

```201:228:old_core/site_audit_center.py
    async def record_page_success(self, domain: str, url: str, status_code: int = 200):
        ...
                file_path = os.path.join(self._get_workspace(domain), "scanned_urls.jsonl")
                await self._append_jsonl_unlocked(file_path, record)

    async def record_page_failure(self, domain: str, url: str, status_code: int, error_msg: str):
        ...
                jsonl_path = os.path.join(workspace, "scanned_urls.jsonl")
                await self._append_jsonl_unlocked(jsonl_path, record)

        ...
        txt_path = os.path.join(self._get_workspace(domain), "scan_errors_log.txt")
        await self._append_txt_safe(txt_path, log_msg, domain)
```

```269:280:old_core/site_audit_center.py
            workspace = self._get_workspace(domain)
            file_path = os.path.join(workspace, "manifest.json")
            await self._write_json_atomic_unlocked(file_path, dump_data)

    async def record_interaction(self, domain: str, interaction_data: dict):
        ...
            workspace = self._get_workspace(domain)
            file_path = os.path.join(workspace, "interactions.json")
            await self._write_json_atomic_unlocked(file_path, self._domain_interactions[domain])
```

#### Playwright 浏览器下载临时目录（在「保存根目录」下，不是任务子目录）

```87:97:old_core/site_engines.py
        task_info = config.get("task_info", {})
        base_save_dir = task_info.get("save_directory", os.path.join(os.getcwd(), "downloads"))
        
        # 我们在主保存路径下划出一个专门的 "_temp_playwright" 作为引擎底层接水盘
        download_dir = os.path.join(base_save_dir, "_temp_playwright")
        ...
        os.makedirs(download_dir, exist_ok=True)
```

---

### 2. 错误快照（`old_core`）

#### 拦截器本体

新错误 → 截图 + HTML，路径为「调用方注入的 `error_dir`」，文件名带 `err_id`：

```101:139:old_core/site_error_system.py
@asynccontextmanager
async def error_interceptor(page, current_url="Unknown_URL"):
    ...
    registry = current_registry.get()
    error_dir = current_error_dir.get()
    
    if not registry or not error_dir:
        # 如果未处于我们设定的上下文中，直接放行不做拦截
        yield
        return
        
    try:
        yield
    except Exception as e:
        ...
        if result["is_new"]:
            err_id = result["err_id"]
            try:
                # 使用独立的 5 秒超时保护，防止 TargetClosedError 导致二次崩溃
                async with asyncio.timeout(5.0):
                    screenshot_path = os.path.join(error_dir, f"{err_id}_screenshot.png")
                    html_path = os.path.join(error_dir, f"{err_id}_page.html")
                    await page.screenshot(path=screenshot_path)
                    content = await page.content()
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(content)
            except Exception as snapshot_e:
                print(f"[{err_id}] 警告: 错误快照保存失败 - {snapshot_e}")
                
        # 必须将原始异常重新抛出，交给 Crawlee 自身的 Retry 机制处理
        raise e
```

#### 使用点示例（DOM 提取 / 敢死队）

```96:101:old_core/site_request_handler_interactor.py
        elif hasattr(context, 'page') and context.page:
            # 🚀 [新增] 穿上防弹衣：保护原生 DOM 操作，且不再吞咽异常，抛出供 Crawlee 重试
            async with error_interceptor(context.page, context.request.url):
                raw_links = await context.page.eval_on_selector_all(
                    "a[href]", "elements => elements.map(el => el.getAttribute('href'))"
```

```45:47:old_core/site_request_handler_action.py
        # 🚀 [修改] 穿上防弹衣：不再静默吞咽异常，接管报错、拍照并抛出供 Crawlee 重试
        async with error_interceptor(context.page, current_url):
```

#### 重要说明（供专家诊断）

在 `old_core` 目录内全文检索 **`current_registry.set` / `current_error_dir.set` 未出现**；`site_runner.py` / `master_dispatcher.py` 也未注入。按 `error_interceptor` 实现，若上下文未设置，会 **`yield` 后直接透传，不登记、不截图**。若本地曾能截图，可能是未纳入本仓库的补丁或其它入口注入了 ContextVar。

---

## 第二部分：`src` 现状

### 1. 路径与存储

#### 审计中心构造

`base_save_dir` = `task.save_directory`，前缀 = **`crawl_strategy`**（`direct` / `full` / `sitemap`），与旧版默认 `strategy_prefix: "site"` 语义不同：

```369:374:src/modules/site/strategy.py
        # ── 1. SiteAuditCenter ────────────────────────────────────────
        self._audit_center = SiteAuditCenter(
            db_path=app_cfg.db_path,
            base_save_dir=task.save_directory,
            strategy_prefix=strat.crawl_strategy,
        )
```

#### 工作区路径

与旧版公式一致：`{base}/{prefix}_{domain}`，用于 `makedirs`、导出 `manifest`、下载落盘等：

```394:409:src/modules/site/audit/audit_center.py
    def _get_workspace(self, domain: str) -> str:
        """
        返回指定域名的物理工作目录路径并确保其存在。
        格式：{base_save_dir}/{strategy_prefix}_{domain}
        ...
        """
        workspace = os.path.join(
            self._base_save_dir,
            f"{self._strategy_prefix}_{domain}",
        )
        os.makedirs(workspace, exist_ok=True)
        return workspace
```

#### 运行时主存储（DB 为主）

文档写明以 **SQLite（`scan_records` / `downloaded_files` / `error_log`）** 为主，**不再**在每次 Hook 时写 `interactions.json` / `scanned_urls.jsonl` / `scan_errors_log.txt`：

```9:26:src/modules/site/audit/audit_center.py
    ── 存储方案（V10.1 重构方向）────────────────────────────────────────────
    放弃 core/ 中的 defaultdict(asyncio.Lock) + 手写 JSON 原子写方案，
    改用 aiosqlite（requirements.txt 中已有）+ src/db/schema.py 中已定义的表：
        scan_records      ← 对应 Hook1/2（record_page_success / failure）
        downloaded_files  ← 对应 Hook4（record_result_batch）
        error_log         ← 对应 Hook2/3（scan_errors / download_errors）
    ...
    ── 文件落盘（manifest.json 兼容层）────────────────────────────────────
    若上游消费方仍需要 manifest.json 文件格式（如现有 UI 读取），
    export_final_reports() 在任务结束时从 DB 导出一次 JSON 快照即可，
```

#### 收尾仅导出 `manifest.json` 到各 workspace

```311:338:src/modules/site/audit/audit_center.py
    async def export_final_reports(self) -> None:
        """
        任务收尾 API：从 DB 导出 manifest.json 快照文件，供外部工具读取。
        ...
        写入路径：{base_save_dir}/{strategy_prefix}_{domain}/manifest.json
        ...
            workspace = self._get_workspace(domain)
            manifest_path = os.path.join(workspace, "manifest.json")
```

#### `record_interaction`

当前仅 **DEBUG 日志**，注释写明 **无 `interactions` 表、未落盘 JSON**：

```289:308:src/modules/site/audit/audit_center.py
    async def record_interaction(
        self,
        domain: str,
        interaction_data: dict,
    ) -> None:
        ...
        # TODO: interactions 表 DDL 需在 schema.py 中补充后解注释以下写入逻辑
        _log.debug(
            f"[SiteAuditCenter] interaction @ {domain}: "
            f"url={interaction_data.get('url')} "
            f"action={interaction_data.get('action')}"
        )
```

#### Playwright 临时下载目录

与旧版类似：在 `save_directory` 下 `_temp_playwright`：

```387:400:src/engine/crawlee_engine.py
    def _build_download_dir(self) -> str:
        """
        在 settings.task_info.save_directory 下创建 _temp_playwright 子目录，
        作为 Playwright 底层接水盘（browser_launch_options['downloads_path']）。
        ...
        """
        from src.config.settings import get_app_config
        subdir  = get_app_config().download_temp_subdir
        dl_dir  = Path(self._settings.task_info.save_directory) / subdir
        dl_dir.mkdir(parents=True, exist_ok=True)
        return str(dl_dir.resolve())
```

---

### 2. 错误快照与异常处理（`src`）

#### 在 `crawler.run()` 期间注入 Registry + 错误目录

目录为 **`{save_directory}/errors`**，全局单一目录，**不是** `{prefix}_{domain}/errors/`：

```223:259:src/modules/site/strategy.py
        # ── 设置 ErrorRegistry 上下文变量（供 error_interceptor 使用） ─
        from src.modules.site.audit.error_registry import (
            current_registry,
            current_error_dir,
        )
        error_dir = os.path.join(self.settings.task_info.save_directory, "errors")
        os.makedirs(error_dir, exist_ok=True)

        token_r = current_registry.set(self._error_registry)
        token_d = current_error_dir.set(error_dir)

        ...
        try:
            await crawler.run(seeds)
        ...
        finally:
            current_registry.reset(token_r)
            current_error_dir.reset(token_d)
```

#### 拦截器行为

`registry is None` 则完全不拦截；有 registry 时登记错误，`err_dir` 非空则快照；`asyncio.wait_for(..., 5.0)`：

```295:335:src/modules/site/audit/error_registry.py
    registry: Optional[ErrorRegistry] = current_registry.get()
    err_dir: Optional[str] = current_error_dir.get()

    # 不在受保护上下文内——直接透传，不做任何拦截
    if registry is None:
        yield
        return

    try:
        yield
    except BaseException:
        ...
        result = registry.register_error(exc_val, exc_tb, url=current_url)
        err_id: str = str(result["err_id"])

        # 仅对首次出现的新错误触发快照，避免大量重复文件
        if result.get("is_new") and err_dir is not None and page is not None:
            async def _take_snapshot() -> None:
                os.makedirs(err_dir, exist_ok=True)
                screenshot_path = os.path.join(err_dir, f"{err_id}_screenshot.png")
                html_path = os.path.join(err_dir, f"{err_id}_page.html")
                await page.screenshot(path=screenshot_path, full_page=False)
                html_content = await page.content()
                ...
            try:
                await asyncio.wait_for(_take_snapshot(), timeout=5.0)
```

#### 收尾导出 Markdown 报告

仍在 `save_directory/errors/error_report.md`：

```312:325:src/modules/site/strategy.py
        if self._error_registry is not None:
            summary = self._error_registry.get_summary()
            if summary.get("unique_errors", 0) > 0:
                error_dir = os.path.join(
                    self.settings.task_info.save_directory, "errors"
                )
                ...
                report_path = os.path.join(error_dir, "error_report.md")
```

#### Crawlee 层失败（如重试耗尽）

`strategy.py` 里注册的 handler 会调 `audit_center.record_page_failure`（进 DB），**不经过** `error_interceptor`，因此 **无自动截图**（除非另有路径）。

---

## 简要缺失对照（结论摘要）

1. **目录前缀语义**：旧版默认 `strategy_prefix="site"`；新版用 `crawl_strategy`（`direct`/`full`/`sitemap`），子目录名会与旧版 `site_域名` 不一致。

2. **四类文件**：新版运行时 **`interactions.json` / `scanned_urls.jsonl` / `scan_errors_log.txt` 不再按任务子目录持续写入**；扫描与错误进 **DB**；文件侧目前可见 **`export_final_reports` → 仅 `manifest.json`**。

3. **错误快照目录**：新版快照在 **`{save_directory}/errors`**；旧版设计依赖 ContextVar 的 `error_dir`（本仓库 `old_core` 内 **未找到 `.set()`**）。

4. **新版快照触发条件**：仅在 **`async with error_interceptor(...)` 包裹**且 **`crawler.run()` 期间** ContextVar 有效时执行；超时类失败若在拦截器外，可能只有日志/DB 而无截图。
