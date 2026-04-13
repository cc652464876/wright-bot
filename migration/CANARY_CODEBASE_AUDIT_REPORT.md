# 代码库审核报告与实施路径

**依据规格**：《金丝雀系统构建指南_V1.txt》（文档内标题：PrismPDF 金丝雀系统 UI 设计与开发规格书 v2.0）  
**对照仓库**：`WebPDF_V12`（审核时点与 `src/`、`UI/` 实际代码一致）

---

## 一、总体结论：技术可行性

**可行，但当前仓库与规格之间存在明显的「编排层 + 契约层 + 探针层」断层。**

底层已具备：多浏览器后端（Chromium / Rebrowser / Camoufox）、指纹生成与启动参数（含 WebRTC 相关 flag）、挑战检测（含 Cloudflare 5s）、贝塞尔鼠标模拟等**能力碎片**；规格要求的是把这些能力**串成一次独占式体检**并通过 **`fetch_canary_dashboard` 结构化 JSON** 驱动独立看板 UI，这部分**尚未存在**。

---

## 二、架构级约束：全局执行锁（Gap 最大项）

### 2.1 现状（可引用代码）

- **`MasterDispatcher`** 仅有**爬虫任务互斥**：`self._running`，在 `run_with_config` 入口防止**第二个爬虫任务**并发；任务结束在 `finally` 中清零。这与「主任务 vs 金丝雀」的**浏览器资源全局锁**不是同一概念。

```104:119:src/app/dispatcher.py
        # 互斥保护：同一时刻只允许一个任务运行
        if self._running:
            self._log(
                "[Dispatcher] 已有任务正在运行，请先停止当前任务再启动新任务",
                "warning",
            )
            return

        self._running = True
        try:
            await self._execute_task(config_dict)
        finally:
            # 无论任务如何结束，确保引用被清空，为下一次任务做准备
            self._strategy = None
            self._runner   = None
            self._running  = False
```

- **`PrismAPI.get_status`** 只反映调度器是否在跑爬虫，**没有** `locked`、也没有金丝雀占用态。

```216:223:src/app/bridge.py
    def get_status(self) -> str:
        """
        UI 心跳（ui-status.js）：与后端调度器任务占用状态对齐。

        Returns:
            ``'running'`` 当 MasterDispatcher 正在执行任务；否则 ``'idle'``。
        """
        return "running" if self._dispatcher.is_task_running() else "idle"
```

### 2.2 结论

- **没有**规格中的**跨子系统全局浏览器执行锁**（主界面自动爬虫 ⟷ 金丝雀体检互斥）。
- **`_running` 不可直接当「浏览器锁」复用**：金丝雀若独立协程启动浏览器，仍可能与 `run_with_config` 并行（除非金丝雀也走同一入口并被同一把锁拦住）。

### 2.3 侵入性最小的加锁方案（建议方向）

1. **单一事实源**：在 `bridge` 可访问的共享对象上增加**显式三态或二态 + 原因**：例如 `idle | crawl_running | canary_running`（或 `browser_lock_holder: None | "crawl" | "canary"`），由 **`PrismAPI`** 在 `start_task` / 拟议的 `start_canary` 前**原子检查并设置**，在任务 `finally` 与金丝雀 `finally` 中**释放**。
2. **调度入口**：爬虫侧已在 `dispatcher.run_with_config` 有互斥；金丝雀侧应对称地在**启动浏览器前**申请同一把锁，避免只改前端置灰而后端仍可双开。
3. **API 面**：扩展 `get_status` 或新增 `get_system_lock_state()`，使主界面与 `canary.html` 的轮询/心跳能区分 **`running` vs `locked`**（与规格 JSON 中 `system_state: locked` 对齐）。
4. **可选**：若希望严格串行化 asyncio 内所有浏览器启动，可在上述标志之外再加 **`asyncio.Lock`**，但**标志位 + 前后端契约**仍是 UI 互斥与 Tooltip 的必需条件。

---

## 三、前端 UI 与组件复用

### 3.1 组件命名与能力（`sp-dropdown` vs 现有）

- 规格写 **`sp-dropdown`**；当前主界面引擎/策略使用的是 **`sp-picker` + `sp-menu-item`**（Spectrum Web Components 常见模式），例如 `stealth-engine`（`UI/index.html`）。
- **`sp-button`**：已广泛使用。
- **`sp-progress-bar`**：**已在 `UI/monitor.html` 使用**，样式与规格「底部细进度条」可复用同一组件与 `UI/style_components.css` 中的定制。

**Gap**：仓库中**无**独立 **`canary.html`**；金丝雀四象限 + 状态灯 CSS 需**新建页面**（背景 `#121212` 与 `monitor.html` 一致，可抄布局思路但内容不同）。

**组件 Gap**：若坚持规格标签名 `sp-dropdown`，需确认 `prism-ui-bundle` 是否注册该标签；**务实做法**是与主界面一致继续用 **`sp-picker`**，仅在文档层把「下拉框」映射为同一交互。

### 3.2 轮询逻辑复用（`fetch_canary_dashboard`）

- **`UI/monitor.js`** 已具备成熟模式：`setInterval` + `window.pywebview.api.fetch_statistics()` / `fetch_logs()`，解析字典更新 DOM。
- **`UI/ui-status.js`** 对 **`get_status`** 的 1s 轮询可扩展为读取 **`locked`** 后禁用 **`btn-toggle-crawl`** 并设置 Tooltip。

**结论**：**轮询范式可无缝复用**；需在 `bridge.py` 增加 **`fetch_canary_dashboard()`**（或等价命名）返回规格 JSON，金丝雀页复制 `monitor.js` 的异步轮询骨架即可。

### 3.3 引擎选择与主界面「双向绑定」

- 主界面引擎值来自 **`UI/ui-config.js` 的 `collectUiConfig()`**（`stealth-engine`）。

**Gap**：跨窗口实时同步需**新增机制**（例如：金丝雀页修改后调用 `pywebview.api.set_stealth_engine` 写回配置 + 主窗口 `evaluate_js` 更新 picker，或共享配置文件/内存状态），当前代码中**未见**跨金丝雀窗口的同步实现。

---

## 四、后端契约与四象限能力映射

**说明**：仓库中**无** `canary_strategy.py`；下列按规格四象限对照**现有模块**。

| 规格项 | 现有能力 | 断层 |
|--------|----------|------|
| **TLS / JA3** | `src/engine/anti_bot/proxy_rotator.py` 注释层解释 SOCKS5 vs HTTP 对 JA3 的影响 | **无** Python 侧 JA3 计算、无与「目标指纹库」比对逻辑 |
| **HTTP / IP 连通** | 正常爬虫请求路径 | **无** 专用「Headers 顺序 / HeadlessChrome 标记」结构化检测与 Pass/Warn 规则 |
| **WebRTC 泄露** | `playwright_backend.py` / `rebrowser_backend.py` / `crawlee_engine.py` 等启动参数 `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` 等 | **无** 页面内 RTCPeerConnection / ICE 候选与出口 IP **实测**与结果结构化 |
| **身份/语言/时区** | `fingerprint.py` 生成 `FingerprintProfile`（含本机时区、语言等），上下文通过 `new_context` 注入 | **无** 「页面 `navigator` 与注入配置**逐项对照**」的自动化断言与 UI 文案生成 |
| **视口与物理屏** | `fingerprint.py` 中 `_get_screen_metrics()`、分辨率相关字段 | **无** 运行时 `window.innerWidth/outerWidth` vs 物理屏一致性检测流水线 |
| **WebGL 厂商/渲染** | **OS 侧** GPU → 推断 Chrome/ANGLE 风格 `webgl_vendor` / `webgl_renderer`（`_HardwareInfo`） | **无** 浏览器内 `getParameter(UNMASKED_VENDOR_WEBGL)` **实测**；无法单独用现有逻辑判断 **SwiftShader/Mesa** 等软件渲染暴露 |
| **Canvas/音频哈希** | 未见专门探针模块 | **无** 噪声注入校验与哈希对比 |
| **Cloudflare 5s** | `challenge_solver.py` 中 `ChallengeDetector._check_cloudflare_5s` 等 | **未** 封装为金丝雀 `quadrants.combat[].state/desc`；**无** 统一耗时（如 3.2s）结构化输出 |
| **CDP / cdc_** | `rebrowser_backend.py` 文档与 `rebrowser_patches.patch()` | **无** 运行时探测 「cdc_ 是否存在」的 **evaluate 脚本 + 结果枚举** |
| **仿生轨迹评分** | `pyautogui_simulator.py` 内 **`PlaywrightHumanMouseSimulator`**：贝塞尔 + `page.mouse` | **无** 对外 **0~1 打分**；无对接 antcpt 类靶场的固定流程 |

**无缝复用（指「可调用/可参考」，非「已满足规格 JSON」）**：

- 启动指定后端：`AbstractBrowserBackend` 体系（含 `RebrowserBackend`）。
- 指纹与上下文参数：`FingerprintGenerator` / `FingerprintInjector`（`fingerprint.py`）。
- CF 挑战识别逻辑：`ChallengeDetector`（`challenge_solver.py`）。
- 行为轨迹：`PlaywrightHumanMouseSimulator`（`pyautogui_simulator.py`）。
- 日志流：`_LOG_BUFFER` + `fetch_logs` / `get_log_entries`（`bridge.py`），可给金丝雀底部日志区复用或打专用前缀。

**需要新增的底层能力（摘要）**：

1. **金丝雀编排器**：独占锁下按象限顺序跑页面脚本 / 导航靶站 / 计时，写共享 `CanaryDashboardState`。
2. **页面内探针 JS**：WebGL、Canvas/Audio（若要做）、WebRTC 候选收集、可选 `navigator.webdriver` / 已知 cdc 探测脚本。
3. **TLS/JA3**：若规格硬性要求，需引入 **外部指纹抓取**（如独立进程/库）或与代理出口联动说明，当前栈内**没有**。
4. **评分与规则引擎**：将原始探测结果映射为 `pass|warn|fail|idle|loading` 与中文 `desc`。

---

## 五、可复用 vs 需新增（汇总）

| 类别 | 内容 |
|------|------|
| **可复用** | `sp-theme` / `sp-picker` / `sp-button` / `sp-progress-bar`；`monitor.js` 式轮询；`bridge` 暴露新 API 的模式；`fetch_logs`；多后端启动与 WebRTC 启动 flag；`ChallengeDetector`；贝塞尔鼠标模拟器；指纹生成与 context 注入。 |
| **需新增** | 全局浏览器执行锁 + API 状态；`canary.html` 与四象限 UI；`fetch_canary_dashboard` 及内存模型；金丝雀异步任务生命周期；多数四象限项的**运行时探针与规则**；主窗与金丝雀的引擎选择同步。 |

---

## 六、分阶段实施建议（3～4 阶段）

**阶段 1 — 契约与锁骨架（最小可演示）**  
- 定义共享锁状态与 `get_status` / `fetch_canary_dashboard` 的 **`system_state`（含 `locked`）** 字段。  
- 主界面：`ui-status.js` 根据状态禁用自动爬虫按钮 + Tooltip。  
- 占位 `canary.html`：静态四象限 + 轮询 Mock JSON + 进度条，验证 pywebview 第二窗口（可参考 `toggle_monitor_window` 模式）。

**阶段 2 — 引擎同步与真实轮询**  
- 金丝雀页 `sp-picker` 与 `#stealth-engine` 双向同步（bridge 读写 `PrismSettings` 或等价配置源）。  
- `fetch_canary_dashboard` 返回真实 `current_engine`、`progress_percent`、日志指针；**暂不**跑重 probe 或仅跑轻量 `idle/loading` 状态机。

**阶段 3 — 浏览器内探针落地（优先高价值、低开销）**  
- 在独占锁下启动所选后端，顺序执行：`navigator`/UA/语言/时区一致性、WebGL UNMASKED、简单 webdriver/cdc 脚本、WebRTC 候选（若代理环境允许）。  
- 将 `ChallengeDetector` 挂到指定测试 URL，输出 CF 相关 `state/desc` 与耗时。

**阶段 4 — 硬项与评分（可选加深）**  
- TLS/JA3、Headers 顺序、Canvas/Audio 指纹、第三方「轨迹打分」靶场：按合规与依赖成本逐项引入；`PlaywrightHumanMouseSimulator` 可扩展为记录移动点数/时长等**启发式分数**，与规格中的「0.85」对齐需在 PRD 中明确打分定义。

---

## 七、相关仓库路径速查

| 主题 | 路径 |
|------|------|
| 桥接 / 轮询 API | `src/app/bridge.py` |
| 爬虫互斥 | `src/app/dispatcher.py` |
| Runner 骨架 | `src/app/runner.py` |
| 主界面 | `UI/index.html`, `UI/ui-config.js`, `UI/ui-status.js` |
| 监控窗轮询范例 | `UI/monitor.html`, `UI/monitor.js` |
| 指纹生成 | `src/engine/anti_bot/fingerprint.py` |
| Rebrowser / CDP 补丁 | `src/engine/anti_bot/stealth/rebrowser_backend.py` |
| 贝塞尔鼠标 | `src/engine/anti_bot/behavior/pyautogui_simulator.py` |
| CF 等挑战检测 | `src/engine/anti_bot/challenge_solver.py` |

---

*本文件由代码库对照规格书审核整理，供迁移与金丝雀实施引用。*
