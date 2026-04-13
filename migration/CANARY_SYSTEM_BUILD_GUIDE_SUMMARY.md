# 金丝雀系统构建指南（总结）

**文档性质**：对桌面版《金丝雀系统构建技术指南》的浓缩摘要，供 WebPDF_V12 / PrismPDF 迁移与实现时查阅。  
**原文位置**：`金丝雀系统构建指南.txt`（用户本地桌面，非仓库内文件）。

---

## 1. 目标与范围

- **伪装维度（9 项）**：UA、平台、语言、屏幕、并发、时区、WebGL、附加请求头、画布噪声。  
- **浏览器后端（3 种）**：标准 Chromium、CDP 魔改 Rebrowser、Camoufox。  
- **金丝雀职责**：任务执行前串行访问固定靶场，在 DOM/网络层采集原始数据，清洗为 UI 可用的 pass / fail / warning，并支持按引擎对比结果。

---

## 2. 靶场与维度映射（速查）

| 靶场 | 主要验证维度 | Pass 预期（摘要） |
|------|----------------|-------------------|
| `https://httpbin.org/anything` | UA、语言、附加请求头 | 头域干净；无 `HeadlessChrome`；`Sec-Ch-Ua` 与宣称 UA 一致。 |
| `https://tls.peet.ws/api/all`（或等价 TLS/JA3 探针） | TLS 栈 / 指纹 | JA3 接近常规浏览器，无 Python/Crawlee 等默认客户端特征。 |
| `https://bot.sannysoft.com/` | UA、平台、语言、屏幕、时区、WebGL、WebDriver 等 | 页面检测项通过；`WebDriver` 为 missing；WebGL Vendor 避免出现明显软件渲染特征（如 SwiftShader/Mesa，具体阈值可与 Profile 对齐）。 |
| `browserleaks.com/canvas`（可选） | 画布噪声 | 按产品目标定义：随机指纹下签名应变化；固定 Profile 下宜稳定、与配置一致（勿混用两种判据）。 |

**说明**：「并发」等维度通常无法由 Sannysoft 单页覆盖，需在指南外单独设计探针或标为未覆盖。

---

## 3. 策略实现要点：`CanaryCrawlStrategy`

**建议路径**：`src/modules/canary_strategy.py`（以本仓库实际模块布局为准，用 `@` 引用真实基类与 dispatcher）。

1. **快速探针（API / 无完整 DOM）**  
   使用**已注入指纹的浏览器上下文**发起请求，拉取 httpbin 与 TLS 探针的 JSON；不必等待整页渲染。

2. **深度探针（DOM）**  
   `page.goto` Sannysoft → `wait_for_selector`（如 `table`）→ `page.evaluate` 解析表格中的关键单元格（UA、WebDriver、WebGL Vendor 等）。

3. **诊断层**  
   将原始数据映射为结构化结果（例如 `status` + `message`），并与 UI 规划的模块（如四大红绿灯区块）对齐。

**关键约束**：TLS/JA3 必须使用**浏览器同源请求**（如 Playwright context / `page.request`），禁止使用独立 Python `requests`/`httpx` 替代，否则会测到错误 TLS 指纹。

---

## 4. 跨引擎调度

在 API/Bridge 层（如 `start_canary_check`）允许 UI 传入引擎与代理，示例配置形态：

```python
canary_config = {
    "mode": "canary",
    "stealth_engine": ui_selected_engine,  # 'chromium' | 'rebrowser' | 'camoufox'
    "use_proxy": ui_proxy_setting,
    # 可强制使用一套 FingerprintProfile 做回归对比
}
```

用于同一 Profile 下对比「裸 Chromium 红 / 魔改引擎绿」等差异。

---

## 5. 落地清单（实现顺序）

1. 新增 `CanaryCrawlStrategy`，继承现有 `BaseCrawlStrategy`（名称以仓库为准）。  
2. `run()`：阶段 A httpbin（+ TLS 探针）→ 阶段 B Sannysoft（+ 可选 Canvas）。  
3. 内部方法 `_diagnose_results(...)`：输出统一 `status` / `message`（及可选 `evidence`）。  
4. 在 `dispatcher`（或等价路由）中：`task_info.mode == "canary"` 时路由到该策略。  
5. 复用现有日志、状态管理与指纹模块（如 `fingerprint.py`）。

**运维建议**：对外部靶场加重试、超时与降级（单站失败标 warning/unknown）；页面结构变更时准备可替换选择器或自建最小探针页。

---

## 6. Cursor 提示词（精简版，路径请按仓库替换）

将下列路径改为本仓库真实文件后使用：

> 参考 `@dispatcher` `@base_strategy` `@fingerprint` `@bridge`：实现金丝雀健康检查。新建 `canary_strategy.py`，`run()` 中先 GET `https://httpbin.org/anything` 解析 `headers`，再打开 `https://bot.sannysoft.com/` 等待 `table` 后用 `page.evaluate` 提取 UA/WebDriver/WebGL 等；实现 `_diagnose_results` 返回 pass/fail/warning；dispatcher 在 `mode == 'canary'` 时路由到此策略；复用现有日志与状态管理。若项目已有 TLS 探针约定，一并接入浏览器上下文请求。

---

## 7. 与迁移文档的关系

金丝雀验证的是 **`src/engine/anti_bot/`** 指纹与多浏览器后端组装是否正确；实现时应遵守 `MIGRATION_AND_UPGRADE_GUIDE.md` 中的配置源、路径与 ContextVar 等原则，避免在策略内硬编码下载根目录或绕过 `PrismSettings`。
