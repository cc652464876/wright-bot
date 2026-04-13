# PrismPDF 金丝雀能力清单 vs 代码库对照审查报告

**审查范围**：`WrightBot_V1` 仓库内与 **Crawlee (Python)**、**Playwright**、表单交互、隐身/指纹、挑战检测、代理相关的实现与脚本级测试。  
**对照基准**：用户提供的《金丝雀测试靶场与能力清单》（Tier 1–4）及桌面文件 `爬虫能力自检清单.py` 中的靶场分层说明。  
**结论摘要**：生产管线已具备 **注册墙表单粗粒度突破**、**下载事件竞态**、**多后端隐身启动参数**、**Cloudflare / Turnstile / hCaptcha / reCAPTCHA 的 DOM 级检测与部分等待型应对**、**代理列表注入 Crawlee**。清单中要求的 **拟人化输入**、**复杂下拉**、**API 级 PDF 拦截**、**playwright-stealth 类全量抹平**、**验证码打码移交接口**、**针对 Sannysoft/CreepJS 的结构化金丝雀断言** 等 **基本未以自动化测试或独立模块形式覆盖**。

---

## 全局总结

| 维度 | 结论（一句话） |
| :--- | :--- |
| **已有能力** | 业务向的「点击下载 + 注册墙 Faker 填表 + `wait_for_event('download')`」与引擎向的「启动参数 + 上下文指纹 + `FingerprintInjector` + `ChallengeDetector/Solver` + `ProxyConfiguration`」已形成闭环，但**不等于**清单中的靶场级回归测试。 |
| **主要缺口** | 无 **DemoQA / Practice Automation / Sannysoft / 2Captcha Demo** 等固定 URL 的 **pytest（或等价）金丝雀用例**；无 **`press_sequentially` / blur 校验链**；无 **非原生下拉** 工具库；无 **`expect_response` 抓 PDF 流**；依赖中 **无 `playwright-stealth`**。 |
| **重复与张力** | **三套「指纹」叙事并存**：`browser_new_context_options`（干净）、`FingerprintInjector`（`add_init_script` + `Object.defineProperty`）、Crawlee `DefaultFingerprintGenerator`（`use_fingerprint=False`）——设计上有互斥，但与清单「避免低级全局注入被识别」存在 **策略张力**。 |
| **代理与 BANNED** | `ChallengeSolver` 失败会转 **BANNED**；`Dispatcher._on_state_change` 对 BANNED **仅打日志**，注释中的 `proxy_rotator.get_next()` **与 `ProxyRotator` 实际 API 不一致**（类中无 `get_next`），轮换实质依赖 **Crawlee SessionPool + 多代理 URL**，而非显式「换 IP 再重试」编排。 |

---

## 1. 已有能力映射 (Implemented)

### 1.1 Tier 1：基础 UI 与表单交互

| 清单项 | 实现情况 | 代码锚点 |
| :--- | :--- | :--- |
| 常规输入（拟人化 delay / `press_sequentially`） | **未实现**。默认 `locator.fill()` 瞬时填充。 | `PlaywrightBehaviorSimulator.fill` → `element.fill()`（`src/engine/anti_bot/behavior/playwright_simulator.py`） |
| 格式校验突破（Email/手机 + blur） | **未系统化**。注册墙路径用 Faker email，无 blur/分步输入。 | `ActionDownloader._fill_fake_form`（`src/modules/site/handlers/action_downloader.py`） |
| 复杂下拉（原生 `select` / 非原生模糊匹配） | **未实现**。 | — |
| 动态下载（`expect_download` / `expect_response`） | **部分**。使用 `page.wait_for_event("download")` 与表单检测 **竞速**，非 `expect_download()` 上下文管理器；**无** `expect_response`。 | `ActionDownloader.execute`（`action_downloader.py`） |

**补充（业务级能力，仍属 Tier 1 相关）**：

- **注册墙识别**：`_FORM_DETECTION_SELECTOR` 基于 email/firstname 等选择器（`action_downloader.py`）。
- **拟人化延迟**：点击前 `random.uniform(0.5, 2.2)` 秒（`action_downloader.py`）。
- **下载按钮侦察**：`Interactor.DOWNLOAD_BUTTON_SELECTORS` + NEED_CLICK 队列（`src/modules/site/handlers/interactor.py`，`action.py`）。

### 1.2 Tier 2：浏览器指纹与隐蔽性

| 清单项 | 实现情况 | 代码锚点 |
| :--- | :--- | :--- |
| `playwright-stealth` 抹平 webdriver 等 | **未引入依赖**。采用 **`--disable-blink-features=AutomationControlled`** + 可选 **rebrowser-patches** + **Camoufox**。 | `requirements.txt`；`_ANTIBOT_CHROMIUM_ARGS` / `playwright_backend.py`；`rebrowser_backend.py` |
| Headless 伪装 / 规避 | **部分**。`window_mode`: `headless` / `minimized` / `normal`（minimized 用窗口位置兜底）。 | `CrawleeEngineFactory._create_playwright_crawler`（`src/engine/crawlee_engine.py`）；`PlaywrightBackend._build_launch_options` |
| 干净交互环境、少全局注入 | **部分矛盾**：文档强调 context 层注入；`use_fingerprint=True` 时仍 **`FingerprintInjector` 注入 Navigator/Screen/Canvas/WebGL 等**（`add_init_script`）。 | `fingerprint.py`（`FingerprintInjector`）；`crawlee_engine.py`（`pre_navigation_hook`） |

### 1.3 Tier 3：验证码与阻断

| 清单项 | 实现情况 | 代码锚点 |
| :--- | :--- | :--- |
| iframe 内常见 CAPTCHA 识别 | **部分**。DOM/iframe URL 特征检测 **Turnstile / hCaptcha / reCAPTCHA v2**。 | `ChallengeDetector`（`src/engine/anti_bot/challenge_solver.py`） |
| 打码 API / 上下文移交 | **未实现**。Solver 对 hCaptcha/reCAPTCHA 走 **False**，无挂起队列与外部 API 钩子。 | `ChallengeSolver.solve` 中 `_dispatch` 分支（`challenge_solver.py`） |

### 1.4 Tier 4：WAF / 5 秒盾

| 清单项 | 实现情况 | 代码锚点 |
| :--- | :--- | :--- |
| IP / 代理池切换与重试 | **部分**。`ProxyRotator` → `ProxyConfiguration(proxy_urls=...)`；轮换语义 **委托 Crawlee**；BANNED 时 **无强制换代理 API 调用**。 | `proxy_rotator.py`；`crawlee_engine.py`（`_build_proxy_configuration`）；`dispatcher.py`（`_on_state_change`） |
| JS Challenge 等待穿透 | **部分**。Cloudflare 5s：**轮询 title**；Turnstile：**轮询隐藏字段**。 | `_solve_cloudflare_5s`、`_solve_cloudflare_turnstile`（`challenge_solver.py`） |

### 1.5 请求拦截（与清单「动态 PDF / 拦截」相关）

| 能力 | 实现情况 | 代码锚点 |
| :--- | :--- | :--- |
| 资源级 route | **有**（Generator 拉取 HTML 时屏蔽图片等）。 | `SiteGenerator._fetch_via_playwright` → `page.route("**/*", _route_filter)`（`src/modules/site/generator.py`） |
| PDF 响应体拦截 | **无**（`src` 下未见 `expect_response`）。 | — |

### 1.6 现有「测试」脚本（非靶场回归）

| 文件 | 作用 |
| :--- | :--- |
| `test_browser_env.py` | Rebrowser/Camoufox 访问 httpbin、截图。 |
| `test_useragent_override.py` | 指纹 + **`FingerprintInjector.inject`** 后检查 `navigator.userAgent`。 |
| `scripts/smoke_antidetect.py` | Crawlee 工厂指纹开关、遗留 `core.site_engines` 路径、Rebrowser 告警、可选真机启动。 |

**注意**：仓库内 **无 `core/` 包**，`smoke_antidetect.smoke_legacy_site_engines` 中 `from core.site_engines import SiteCrawlerFactory` 在仅克隆 `src` 结构时 **可能无法运行**，属于测试债务。

---

## 2. 缺失能力诊断 (Missing)

### 2.1 高优先级（与清单 Tier 1/2 强相关）

1. **DemoQA / Practice Automation 类端到端用例**（固定 URL + 断言）：无。
2. **`press_sequentially` 或分段 delay 输入** + **`blur` 触发校验**：无。
3. **非原生下拉**（`div`/`ul` 模拟 + 全局文本模糊匹配）与 **原生 `select_option`** 封装：无。
4. **`page.expect_download()`**（与 `wait_for_event` 并存时的统一抽象）及 **`expect_response` 捕获 `application/pdf`**：无。
5. **Sannysoft / CreepJS 结构化探针**（解析页面或关键 `evaluate` 子集输出 JSON）：无；`test_useragent_override` 仅 about:blank。
6. **`playwright-stealth`（Python 生态）或等价脚本注入策略** 与当前 `FingerprintInjector` 的 **A/B 对照测试**：未做。

### 2.2 高优先级（Tier 3/4）

7. **极验（Geetest）等国内常见 iframe 检测**：`ChallengeDetector` 未覆盖。
8. **验证码挂起**：无「检测到 CAPTCHA → 暂停 crawler → 回调/队列交给打码服务 → 注入 token → 恢复」的 **稳定接口**。
9. **BANNED → 显式代理轮换**：状态监听与 `ProxyRotator` API **文档不一致**，缺少「换 session/换代理后重放当前请求」的集成测试。

---

## 3. 功能重复与冲突检测 (Duplicates & Conflicts)

### 3.1 多套 Stealth / 指纹路径

| 路径 | 角色 | 冲突风险 |
| :--- | :--- | :--- |
| **Crawlee `DefaultFingerprintGenerator`** | `use_fingerprint=False` | 与自研指纹 **互斥**（工厂已处理，见 `crawlee_engine.py` 注释 P0-04） | 
| **`FingerprintGenerator` + `browser_new_context_options`** | UA / viewport / Client Hints | 与 Crawlee 内置 **互斥** | 
| **`FingerprintInjector`（init script）** | Navigator/Screen/Canvas/WebGL 等 | 与清单「少做低级全局注入」**目标张力**；与 **Camoufox** 文档要求「勿重复注入」**并存**（`fingerprint.py` 已说明 camoufox 应跳过） | 
| **Rebrowser patches** | CDP 层补丁 | 与纯 Chromium 启动参数 **叠加**（需用金丝雀验证是否过度修补） | 

### 3.2 两套「拟真」输入

- **Playwright 虚拟**：`PlaywrightBehaviorSimulator`（`fill` / `click`）。
- **OS 级鼠标**：`PyAutoGUIBezierSimulator` / `PlaywrightHumanMouseSimulator`（`pyautogui_simulator.py`）。

二者为 **策略切换**，非重复实现；但若表单填充只走 `fill()`，则 **与行为模拟器解耦**，易出现「点击很真、填表很假」的不一致。

### 3.3 等待与重试

| 位置 | 模式 |
| :--- | :--- |
| `ActionDownloader` | 下载 20s、表单 5s、`asyncio.wait` FIRST_COMPLETED、表单后再等下载 15s |
| `ChallengeSolver` | 5s 盾轮询 title；Turnstile 最长 15s 轮询 |
| `SiteGenerator._fetch_via_playwright` | 指数退避重试 |
| Crawlee | `max_request_retries`、`request_handler_timeout` |

**风险**：同类「等页面变正常」逻辑分散在 **Solver** 与 **业务 Downloader**，缺少统一 **超时/遥测** 配置面。

### 3.4 定位 vs JS

- **主路径**：Playwright locator + 少量 `page.evaluate`（如 Turnstile 隐藏字段、`extract_raw_links` 用 `eval_on_selector_all`）。
- **旧版**：`old_core/site_request_handler*.py` 中存在 **`expect_download` 与更复杂表单分支**，与当前 `src` **并行遗留**，易误导维护者「是否已实现」。

---

## 4. 重构与选择建议 (Recommendations)

### 4.1 Stealth / 指纹：保留什么、收敛什么

1. **保留** `browser_new_context_options` 层 UA、viewport、`extra_http_headers`、`permissions=[]` 与启动参数（WebRTC、AutomationControlled）——与现有架构一致，且注释已说明为何避免纯 `evaluate` 覆盖。
2. **对 Sannysoft/CreepJS 金丝雀**：建议 **分档策略**  
   - **档 A**：仅 context + launch args（Rebrowser **或** Camoufox 其一），**不**注入 `FingerprintInjector`，测「原生链路基线」。  
   - **档 B**：当前默认（context + `FingerprintInjector`），测「业务默认组合」。  
   这样可避免「一层叠一层无法归因」。
3. **若引入 `playwright-stealth`**：不要与 `FingerprintInjector` 的 Navigator 覆盖 **重复 patch**；优先在 **独立分支/开关** 做对比，再决定是否替换部分 `Object.defineProperty` 脚本。

### 4.2 表单与下载：建议抽象单一模块

将 **`HumanFormFiller`**（press_sequentially、blur、select、自定义下拉）与 **`DownloadCapture`**（`expect_download` + 可选 `expect_response`）从 `ActionDownloader` 中抽出，便于 DemoQA 金丝雀与生产共用，避免 `old_core` 与 `src` 两套逻辑漂移。

### 4.3 验证码：建议增加「挂起接口」

在 `ChallengeSolver` 或并行 **`CaptchaOrchestrator`** 中增加：

- `detect()` 已有 → 扩展 **Geetest** 等选择器；
- **`async def request_external_solve(page, ctype) -> bool`**：默认 `NotImplemented`，金丝雀/mock 实现；生产接 2Captcha 等。

### 4.4 代理与 BANNED

- 修正 `Dispatcher` 注释：将「`get_next()`」改为与实际行为一致（**SessionPool + 代理列表**），或 **实现**真正的「标记当前 session 失效并轮换」并与 Crawlee API 对齐。  
- 为 Tier 4 增加测试：**模拟 403 → 进入 BANNED → 断言下一次请求使用不同 proxy session**（需 mock 或本地代理桩）。

---

## 5. Crawlee 下的最佳实践代码骨架（缺失项）

以下骨架仅作 **金丝雀/模块边界** 示例，未与现有类名强绑定；运行前需按项目 `PlaywrightCrawler` 的 handler 签名调整。

### 5.1 DemoQA 风格：拟人输入 + blur + 原生 select

```python
# tests/canary/test_demoqa_form_skeleton.py  （建议路径，按需创建）
import asyncio
from crawlee.crawlers import PlaywrightCrawler

DEMOQA_TEXT = "https://demoqa.com/text-box"
DEMOQA_SELECT = "https://demoqa.com/select-menu"  # 按实际菜单 URL 调整


async def human_fill(locator, text: str, delay_ms: int = 35) -> None:
    await locator.click()
    await locator.press_sequentially(text, delay=delay_ms)


async def blur_trigger_validation(page, locator) -> None:
    await human_fill(locator, "bad-email")
    await locator.press("Tab")
    await asyncio.sleep(0.2)


async def select_native(page, label: str) -> None:
    await page.get_by_label(label).select_option(label="Ms.")  # 示例


async def handler(context):
    page = context.page
    await context.page.goto(DEMOQA_TEXT)
    await blur_trigger_validation(page, page.get_by_placeholder("name@example.com"))
    # 断言错误提示可见（按 DemoQA 实际 DOM 写 expect）
```

### 5.2 非原生下拉（模糊文本）

```python
async def pick_combobox_by_text(page, combo_selector: str, text: str) -> None:
    await page.locator(combo_selector).click()
    await page.get_by_role("option", name=text).click()
    # 若无 role=option：用 page.locator("li, div[role='option']").filter(has_text=text).first.click()
```

### 5.3 下载：`expect_download` + PDF `expect_response` 二选一封装

```python
from playwright.async_api import Page, Download, Response


async def capture_click_download(page: Page, click_coro):
    async with page.expect_download(timeout=30_000) as dl_info:
        await click_coro
    download: Download = await dl_info.value
    path = await download.path()
    return path


async def capture_pdf_response(page: Page, trigger_coro, url_glob: str = "**/*.pdf"):
    async with page.expect_response(
        lambda r: "application/pdf" in (r.headers.get("content-type") or "").lower()
    ) as resp_info:
        await trigger_coro
    resp: Response = await resp_info.value
    body = await resp.body()
    return body
```

### 5.4 Sannysoft 指纹金丝雀（结构化断言骨架）

```python
SANNYSOFT = "https://bot.sannysoft.com/"


async def run_sannysoft_baseline(page):
    await page.goto(SANNYSOFT, wait_until="networkidle", timeout=60_000)
    webdriver = await page.evaluate("navigator.webdriver")
    # 可扩展：读取页面表格 DOM 或固定 test id（若站点提供）
    return {"webdriver": webdriver, "url": page.url}
```

### 5.5 Crawlee 中注册单 URL 金丝雀爬虫

```python
from crawlee import Request


def build_single_url_crawler(factory, handler):
    crawler = factory.create()
    crawler.router.default_handler(handler)
    return crawler


async def run_canary(factory):
    crawler = build_single_url_crawler(factory, my_canary_handler)
    await crawler.run([Request.from_url(SANNYSOFT)])
```

---

## 6. 文档与代码引用索引（便于跳转）

| 主题 | 路径 |
| :--- | :--- |
| 挑战检测与等待型解决 | `src/engine/anti_bot/challenge_solver.py` |
| 注册墙 + 下载竞速 | `src/modules/site/handlers/action_downloader.py` |
| Crawlee 工厂 / 指纹互斥 / pre_navigation_hook | `src/engine/crawlee_engine.py` |
| 标准 Playwright 行为（含 `fill`） | `src/engine/anti_bot/behavior/playwright_simulator.py` |
| 指纹注入（init script） | `src/engine/anti_bot/fingerprint.py`（`FingerprintInjector`） |
| 代理适配器 | `src/engine/anti_bot/proxy_rotator.py` |
| 页面处理管线（挑战 → Cookie → 下载） | `src/modules/site/strategy.py` |
| BANNED 监听 | `src/app/dispatcher.py`（`_on_state_change`） |

---

*报告生成依据：仓库静态代码扫描；若后续增加 `tests/` 或金丝雀专用 handler，请同步更新本文件「已有能力映射」一节。*
