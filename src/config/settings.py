"""
@Layer   : Config 层（第一层 · 最底层基石）
@Role    : 全局唯一参数真实来源（SSOT）
@Pattern : SSOT（Single Source of Truth）+ Pydantic BaseModel / BaseSettings + Singleton
@Description:
    将来自前端 UI 的完整 JSON 配置解析为强类型嵌套 Pydantic 模型（PrismSettings），
    并以模块级单例形式对外暴露，确保整个进程中任意模块读取的参数永远来自同一份权威数据。
    应用级固定参数（日志目录、数据库路径等）由 AppConfig（BaseSettings）通过
    环境变量 / .env 文件驱动，与运行时 UI 参数严格分离。

    ★ 浏览器内核本地化隔离：
    所有三种浏览器（标准 Playwright Chromium、rebrowser-patches、Camoufox）的二进制
    和缓存文件均强制存放在项目根目录的 ./browser_cores 文件夹中。
    通过 _init_browser_env() 在进程最早时刻设置环境变量实现隔离，
    详见 BrowserCoresConfig 和 _init_browser_env() 的文档。
"""

from __future__ import annotations

import math
import os
import random
import threading
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings


# ---------------------------------------------------------------------------
# 第一部分：各配置块的子模型（严格对应 ui-config.js 中的 JSON key 分组）
# ---------------------------------------------------------------------------

class TaskInfoConfig(BaseModel):
    """
    task_info 配置块：任务基础信息。
    对应 UI 的任务名、保存路径、模式（site / search）等顶层字段。

    enable_realtime_jsonl_export:
        为 True 时由各站点策略装配 RealtimeFileExporter，向域名工作区实时追加
        scanned_urls.jsonl / scan_errors_log.txt / interactions.jsonl（与 DB 双写）。
    """

    mode: Literal["site", "search"] = "site"
    task_name: str = "PrismPDF_Task"
    save_directory: str = "./downloads"
    history_file: str = ""
    max_pdf_count: int = 50
    #: 为 True 时，SiteAuditCenter 在写 DB 的同时向各域名工作区追加实时文件流
    #: （scanned_urls.jsonl / scan_errors_log.txt / interactions.jsonl），供旧 UI tail。
    enable_realtime_jsonl_export: bool = False
    #: 为 True 时表示由 UI 金丝雀「运行体检」下发的合成任务；Dispatcher 将路由到 CanaryMockStrategy。
    is_canary: bool = False


class StrategyConfig(BaseModel):
    """
    strategy_settings 配置块：抓取策略参数。
    对应 UI 的爬取模式（direct / full / sitemap / search）、目标 URL 列表、文件类型等。
    """

    crawl_strategy: Literal[
        "direct", "full", "sitemap",
        "google_search", "bing_search", "duckduckgo"
    ] = "direct"
    target_urls: List[str] = Field(default_factory=list)
    search_keyword: str = ""
    api_key: str = ""
    file_type: Literal["pdf", "img", "all"] = "pdf"


class EngineConfig(BaseModel):
    """
    engine_settings 配置块：引擎类型与浏览器启动参数。
    对应 UI 的爬虫引擎下拉框（playwright / beautifulsoup）及设备伪装选项。
    """

    crawler_type: Literal["playwright", "beautifulsoup"] = "playwright"
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    link_strategy: str = "same-domain"
    wait_until: str = "domcontentloaded"


class HumanTimingConfig(BaseModel):
    """
    人类化请求时序配置子模型。

    启用后在每次请求前注入服从对数正态分布的随机延迟，
    使时序统计特征与真实用户访问模式对齐，对抗 WAF 行为分析引擎的时序方差检测。

    背景：均匀限速（max_tasks_per_minute）产生低方差请求间隔，
          与人类访问的对数正态分布特征不符，WAF 行为分析引擎对时序方差有专项检测。

    延迟采样算法（由 _sample_human_delay() 实现）：
        1. 以 burst_pause_probability 概率触发"阅读停顿"，
           返回 [15.0, 40.0] 区间的长停顿（模拟用户阅读页面行为）。
        2. 否则从对数正态分布采样：
               raw = random.lognormvariate(log(mean_delay_secs), sigma)
               delay = clamp(raw, min_delay_secs, max_delay_secs)
        对数正态分布的特点是右偏尾，与真实用户访问间隔分布高度吻合。

    Attributes:
        enable                 : 是否启用人类化时序（默认关闭，不影响现有性能）。
        mean_delay_secs        : 对数正态分布的均值（秒），即最常见的请求间隔。
        sigma                  : 标准差，越大时序越随机，建议范围 0.5–1.2。
        min_delay_secs         : 硬下限（秒），防止过于激进的采样值。
        max_delay_secs         : 硬上限（秒），防止过长停顿影响爬取效率。
        burst_pause_probability: 触发"阅读停顿"（15–40s 长停顿）的概率，
                                 模拟用户阅读页面的自然行为节律。
    """

    enable: bool = False
    mean_delay_secs: float = 2.5
    sigma: float = 0.8
    min_delay_secs: float = 0.5
    max_delay_secs: float = 15.0
    burst_pause_probability: float = 0.04


def _sample_human_delay(cfg: "HumanTimingConfig") -> float:
    """
    根据 HumanTimingConfig 采样一次人类化请求延迟值（秒）。

    算法：
        1. 以 cfg.burst_pause_probability 概率直接返回 [15.0, 40.0] 区间均匀随机值
           （模拟用户阅读停顿，长停顿打断周期性节律）。
        2. 否则从对数正态分布采样：
               mu  = math.log(cfg.mean_delay_secs)
               raw = random.lognormvariate(mu, cfg.sigma)
           将 raw 截断到 [cfg.min_delay_secs, cfg.max_delay_secs] 区间后返回。

    调用位置：请求 handler 前置钩子（pre-request hook），
              在 cfg.enable=True 时 await asyncio.sleep(_sample_human_delay(cfg))。

    Args:
        cfg: HumanTimingConfig 实例，提供分布参数与边界约束。
    Returns:
        本次请求应等待的秒数（float）。
    """
    # 步骤 1：以 burst_pause_probability 概率触发"阅读停顿"长停顿
    if random.random() < cfg.burst_pause_probability:
        return random.uniform(15.0, 40.0)

    # 步骤 2：对数正态分布采样，mu = ln(mean)，sigma 控制右偏尾宽度
    mu = math.log(cfg.mean_delay_secs)
    raw = random.lognormvariate(mu, cfg.sigma)

    # 截断到硬边界 [min_delay_secs, max_delay_secs]
    return max(cfg.min_delay_secs, min(raw, cfg.max_delay_secs))


class PerformanceConfig(BaseModel):
    """
    performance 配置块：并发与限速参数。
    max_concurrency 允许 'auto' 或具体整数（来自前端下拉框）。
    human_timing 子模型控制人类化时序注入（默认禁用，不影响现有性能基线）。
    """

    max_concurrency: Union[int, Literal["auto"]] = "auto"
    min_concurrency: int = 1
    max_requests_per_crawl: int = 9999
    limit_rate: bool = False
    max_tasks_per_minute: int = 120
    human_timing: HumanTimingConfig = Field(default_factory=HumanTimingConfig)
    # 人类化时序子配置；默认 enable=False，对现有性能指标零影响
    # 启用后在每次请求前注入对数正态分布随机延迟，对抗 WAF 时序方差检测


class TimeoutsConfig(BaseModel):
    """
    timeouts_and_retries 配置块：超时与重试参数。
    所有时间单位均为秒（由引擎工厂转换为 timedelta）。
    """

    request_handler_timeout_secs: int = 60
    navigation_timeout_secs: int = 30
    max_request_retries: int = 3


class StealthConfig(BaseModel):
    """
    stealth 配置块：反检测与隐身参数。
    控制无头模式、指纹伪造开关、SSL 证书忽略等浏览器安全策略，
    以及浏览器启动后端（stealth_engine）与鼠标行为模拟方式（behavior_mode）的选择。

    stealth_engine 字段说明：
        "chromium"    : 标准 Playwright Chromium（默认）。
                        新一代无头模式下 WAF 检测通过率中，适合无专项 WAF 的普通站点。
                        并发上限建议 8–16，无额外依赖。
                        （原键名 "playwright" 已于 V10.2 废弃，统一为 "chromium"）
        "camoufox"    : Camoufox 补丁版 Firefox，在二进制层消除 WebGL / Canvas 指纹泄露。
                        通过率最高，适合高价值、高防护目标。并发上限建议 2–4。
        "rebrowser"   : rebrowser-patches 补丁版 Chromium，移除 CDP 自动化特征。
                        通过率高，适合中高防护商业站点。并发上限建议 4–8。

    behavior_mode 字段说明：
        "playwright_api"    : 使用 Playwright page.click() / locator 虚拟输入（默认）。
        "pyautogui_bezier"  : 将 DOM 元素坐标映射为屏幕坐标后，
                              通过 PyAutoGUI + 贝塞尔曲线执行 OS 级真实鼠标事件。

    session_mode 字段说明：
        "pool"       : 多身份轮换模式（默认）。SessionPool 维护多个 session，
                       每个 session 有独立 Cookie/身份，适合高并发快速抓取。
                       缺点：每个 session 的"数字包浆"积累量极浅，信誉深度有限。
        "persistent" : 单一固定身份模式。SessionPool(max_pool_size=1)，
                       整个任务过程使用同一 session 持续积累 Cookie 与行为历史，
                       适合需要建立访问信誉的高防护目标（如学术数据库、金融类站点）。
        ⚠️ pool 模式下多身份轮换与 user_data_dir 持久化策略方向相反：
           pool 追求多样性，persistent 追求深度信誉，应根据目标站点防护级别选择。

    window_mode 字段说明：
        "headless"  : 新一代无头模式（默认）。后端传入 headless=True，
                      Playwright 1.40+ 自动使用 headless=new（非 CDP hack）。
        "minimized" : 有头最小化模式。后端传入 headless=False 并附加
                      --start-minimized（任务栏可见但窗口最小化），
                      启用真实 GPU 渲染，适合需要 GPU 直通的指纹场景。
                      兜底方案：--window-position=-32000,-32000（部分系统下
                      --start-minimized 不生效时将窗口移出可视区域）。
        "normal"    : 有头正常窗口，供开发调试使用。
        ⚠️ minimized 模式下 PyAutoGUIBezierSimulator._resolve_window_offset()
           需要处理窗口不在可视区域时的坐标映射边界情况。

    user_data_dir 字段说明：
        留空时每次任务使用全新临时 BrowserContext，无 Cookie / localStorage 积累。
        非空时各后端将此路径注入 _build_launch_options() 的 user_data_dir 参数
        （Chromium 系）或 _build_camoufox_config() 的 profile_path 参数（Camoufox），
        实现"数字包浆"持续积累。此方案替代与 Crawlee 架构不兼容的 launch_persistent_context。

    扩展指引：
        - 新增启动后端：在 engine/anti_bot/stealth/ 下新建文件并继承 AbstractBrowserBackend，
          同时在此字段的 Literal 中追加新值即可；无需修改其他策略代码。
        - 新增行为模拟器：在 engine/anti_bot/behavior/ 下新建文件并继承 AbstractBehaviorSimulator，
          同时在此字段的 Literal 中追加新值即可；无需修改 Interactor 的任何业务逻辑。
    """

    headless: bool = True
    use_fingerprint: bool = False
    ignore_ssl_error: bool = True
    stealth_engine: Literal["chromium", "camoufox", "rebrowser"] = Field(
        default="chromium",
        description="浏览器伪装/启动后端，与 UI「设备伪装」下拉框 stealth_engine 一致。",
    )
    # "chromium"  → PlaywrightBackend（标准 Playwright Chromium，默认）
    # "camoufox"  → CamoufoxBackend（补丁版 Firefox，最高防护）
    # "rebrowser" → RebrowserBackend（CDP 补丁版 Chromium，高防护）
    # ⚠️ 旧值 "playwright" 已废弃，Dispatcher 后端映射表需同步更新为 "chromium"
    behavior_mode: Literal["playwright_api", "pyautogui_bezier"] = "playwright_api"

    window_mode: Literal["headless", "minimized", "normal"] = "headless"
    # headless  : 新一代无头模式（默认，速度最优，Playwright 1.40+ 自动启用 headless=new）
    # minimized : 有头最小化模式（GPU 直通，任务栏静默；--start-minimized + 坐标偏移兜底）
    # normal    : 有头正常窗口（调试场景）
    # 注：window_mode 优先级高于 headless 字段；后端 _build_launch_options() 以
    #     window_mode 为准，headless 字段仅作向后兼容保留。

    session_mode: Literal["pool", "persistent"] = "pool"
    # pool       : 多身份轮换（默认），SessionPool 维护多个 session 身份，适合高并发快速抓取
    # persistent : 单一固定身份积累，SessionPool(max_pool_size=1)，适合需要建立访问信誉的目标
    # 注：两种模式与 user_data_dir 协同使用时效果最佳——persistent + user_data_dir 非空
    #     实现"固定身份 + 持久化浏览器档案"双重信誉积累，形成完整的 D4 状态层防护

    user_data_dir: str = ""
    # 留空：不启用持久化（每次任务使用临时 BrowserContext，无状态积累）
    # 非空：持久化目录绝对路径，每次启动复用同一浏览器档案，实现"数字包浆"积累
    # 路径规范建议：{save_directory}/_browser_profile/{stealth_engine}/
    # 替代旧方案 launch_persistent_context（该 API 与 Crawlee PlaywrightCrawler 架构不兼容）


class UiFiltersConfig(BaseModel):
    """
    ui_filters 配置块：本地文件过滤条件。
    仅在下载后的结果筛选阶段使用，不影响爬取行为本身。
    """

    save_excel: bool = False
    save_log: bool = False
    min_file_size_mb: float = 0.0
    min_page_count: int = 0
    min_img_size_mb: float = 0.0
    min_px: int = 0


# ---------------------------------------------------------------------------
# 第一部分半：浏览器内核本地化隔离配置（须在任何 Playwright / Camoufox import 前生效）
# ---------------------------------------------------------------------------

import os
from pathlib import Path


class BrowserCoresConfig:
    """
    浏览器内核本地化隔离配置。

    负责在进程最早时刻将所有浏览器二进制与缓存文件的存放路径强制锁定到项目
    根目录的 ./browser_cores 文件夹，实现"绿色免安装"——不污染系统的 AppData、
    Cache、~/.cache 等目录。

    ━━ 三种浏览器的路径隔离机制 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ① 标准 Playwright Chromium（PlaywrightBackend / CrawleeEngineFactory 主路径）：
        PLAYWRIGHT_BROWSERS_PATH = ./browser_cores/playwright
        作用：
          - 控制 playwright.sync_async_api.playwright().start() 内部调用
            _lazy_download_manager.download() 时的目标目录。
          - 控制 `python -m playwright install` 的安装目录。
          - 若 executable_path 显式传入，则忽略此变量；此变量仅控制"未指定
            executable_path 时 Playwright 自动下载或查找浏览器"的查找路径。
        优先级：executable_path（最高） > PLAYWRIGHT_BROWSERS_PATH > 系统默认。

    ② rebrowser-patches（RebrowserBackend）：
        REBROWSER_BROWSERS_PATH = ./browser_cores/rebrowser
        作用：
          - rebrowser-patches 的 Python monkey-patch 会在其内部调用
            playwright.async_api.sync_playwright() 或 async 版本，
            与标准 Playwright 共享底层浏览器查找逻辑。
          - 将 rebrowser 专属 Chromium 二进制（独立于标准 Playwright 安装目录）
            存放在此目录，确保与系统 Playwright 物理隔离。
        注意：rebrowser 补丁以 Python 包形式注入时，底层 Chromium 二进制
              仍走标准 Playwright 下载逻辑，此时 REBROWSER_BROWSERS_PATH
              与 PLAYWRIGHT_BROWSERS_PATH 指向同一目录也无妨——
              rebrowser 的优势在于 Python 层的 CDP monkey-patch，不依赖独立二进制。

    ③ Camoufox（CamoufoxBackend）：
        CAMOUFOX_CACHE_DIR = ./browser_cores/camoufox（与其它引擎并列设置，便于运维统一认知）。
        注意：当前 camoufox pip 包的 pkgman.INSTALL_DIR 默认走 platformdirs 用户缓存目录，
        并不读取 CAMOUFOX_CACHE_DIR；_init_browser_env() 会在 import camoufox.pkgman 时
        将 INSTALL_DIR 强制改为上述 browser_cores/camoufox 绝对路径。
        若 AppConfig.camoufox_path 非空且文件存在，后端启动时以该可执行文件为准（最高优先级）。

    ━━ 路径隔离的核心原则 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    1. 所有路径均基于 project_root/browser_cores/（项目根目录的相对路径或绝对路径）。
       严禁硬编码 Windows C:\\Users\\... 等系统路径，确保项目可迁移。
    2. 必须在任何 Playwright / Camoufox 模块 import 之前设置环境变量——
       环境变量一旦设定便不可更改，且部分库在首次 import 时已缓存了默认路径。
    3. 调用时机：main.py 最早期（任何业务模块 import 之前）调用
       _init_browser_env() 完成隔离。
    4. 三种浏览器的路径变量相互独立，不存在冲突——它们控制的是不同库的不同目录，
       但可以统一存放�� ./browser_cores/{engine}/ 子目录下，便于管理。

    Attributes:
        root_dir      : 浏览器内核根目录（默认 ./browser_cores）。
        playwright_dir: Playwright Chromium 专属子目录（默认 {root}/playwright）。
        rebrowser_dir : rebrowser-patches 专属子目录（默认 {root}/rebrowser）。
        camoufox_dir  : Camoufox Firefox 专属子目录（默认 {root}/camoufox）。
    """

    def __init__(
        self,
        root_dir: str = "./browser_cores",
    ) -> None:
        self.root_dir      = root_dir
        self.playwright_dir = os.path.join(root_dir, "playwright")
        self.rebrowser_dir  = os.path.join(root_dir, "rebrowser")
        self.camoufox_dir   = os.path.join(root_dir, "camoufox")

    def resolve_path(self, subdir: str) -> str:
        """返回绝对路径，确保 os.path.isfile() 等 API 正常工作。"""
        return str(Path(subdir).resolve())

    @property
    def playwright_path(self) -> str:
        return self.resolve_path(self.playwright_dir)

    @property
    def rebrowser_path(self) -> str:
        return self.resolve_path(self.rebrowser_dir)

    @property
    def camoufox_path(self) -> str:
        return self.resolve_path(self.camoufox_dir)


# ---------------------------------------------------------------------------
# 浏览器环境隔离状态（模块级，进程唯一）
# ---------------------------------------------------------------------------

_browser_env_initialized: bool = False


def _init_browser_env(
    root_dir: str = "./browser_cores",
    force: bool = False,
) -> BrowserCoresConfig:
    """
    在进程最早时刻设置所有浏览器内核的本地化路径环境变量。

    调用时机要求：
        ★ 必须在任何 playwright、camoufox、rebrowser_patches 模块 import 之前执行。
        ★ 建议调用位置：main.py 的 if __name__ == "__main__" 块最顶部（任何 import 之前）。
        ★ 次优位置：src/config/settings.py 模块级（settings.py 是最早被业务层 import 的模块之一）。

    调用约定：
        - 同一进程只执行一次（_browser_env_initialized 保护）。
        - 接受 force=True 可在已初始化状态下强制重设（用于测试或重配置场景）。
        - 返回 BrowserCoresConfig 实例，供调用方验证路径或进行后续配置。

    路径说明：
        - 所有路径基于传入的 root_dir（默认 "./browser_cores"，相对于当前工作目录）。
        - Windows 下路径会被转换为绝对路径（如 C:\\Users\\...\\browser_cores\\playwright）。
        - 若需要项目根目录的绝对路径，请在外层先 resolve project_root 再传入。

    效果：
        os.environ["PLAYWRIGHT_BROWSERS_PATH"]  = ./browser_cores/playwright  → 绝对路径
        os.environ["REBROWSER_BROWSERS_PATH"]   = ./browser_cores/rebrowser  → 绝对路径
        os.environ["CAMOUFOX_CACHE_DIR"]         = ./browser_cores/camoufox   → 绝对路径

    示例（main.py 入口）：
        from src.config.settings import _init_browser_env
        _init_browser_env()          # ← 在所有其他 import 之前调用
        # 之后正常 import webview、loguru 等
    """
    global _browser_env_initialized

    if _browser_env_initialized and not force:
        # 已初始化，直接返回（避免重复设置和日志噪音）
        return BrowserCoresConfig(root_dir)

    cfg = BrowserCoresConfig(root_dir)

    # ① 标准 Playwright Chromium
    #    Playwright 在内部 _fetch_api_client() / _download_manager 中读取此变量
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = cfg.playwright_path

    # ② rebrowser-patches
    #    rebrowser_patches 的 monkey-patch 会间接使用 playwright 的浏览器查找逻辑，
    #    走 PLAYWRIGHT_BROWSERS_PATH。为了语义清晰和未来可能的独立二进制需求，
    #    同时设置 REBROWSER_BROWSERS_PATH（供 rebrowser 独立二进制路径参考）。
    os.environ["REBROWSER_BROWSERS_PATH"] = cfg.rebrowser_path

    # ③ Camoufox（独立环境变量，不与 Playwright 共享）
    os.environ["CAMOUFOX_CACHE_DIR"] = cfg.camoufox_path

    # Camoufox 的 pkgman.INSTALL_DIR 在 import 时固定为 platformdirs.user_cache_dir("camoufox")，
    # 不会读取 CAMOUFOX_CACHE_DIR；在首次 import camoufox.pkgman 之前覆盖 INSTALL_DIR，
    # 才能把二进制与 addons 落到 browser_cores/camoufox（与 GUI 入口 main._init_browser_env 一致）。
    try:
        import camoufox.pkgman as _camoufox_pkgman

        _camoufox_pkgman.INSTALL_DIR = Path(cfg.camoufox_path).resolve()
    except ImportError:
        pass

    _browser_env_initialized = True

    return cfg


# ---------------------------------------------------------------------------
# 第二部分：应用级固定配置（由环境变量 / .env 驱动，不受 UI 控制）
# ---------------------------------------------------------------------------

class AppConfig(BaseSettings):
    """
    应用级固定配置单例。
    负责管理与运行时任务无关的全局常量：日志目录、数据库路径、
    各浏览器引擎可执行文件路径、会话池大小等。
    通过 PRISM_ 前缀的环境变量或项目根目录 .env 文件注入。
    Pattern: BaseSettings 自动映射环境变量（Pydantic-Settings）。

    浏览器可执行路径字段说明：
        chromium_path    : 标准 Playwright Chromium 二进制（或系统安装版，留空则自动查找）。
        rebrowser_path   : rebrowser-patches 打补丁后的 Chromium 二进制路径；
                           留空时回退到 chromium_path（补丁以 Python 包形式注入时不需要独立二进制）。
        camoufox_path    : Camoufox Firefox 二进制路径；
                           留空时由 camoufox Python 包自动管理（推荐）。

    扩展指引：
        新增引擎类型时，在此处追加对应的 xxx_path 字段并在 .env 中配置即可，
        无需修改任何业务层代码。
    """

    log_dir: str = "./logs"
    log_level: str = "INFO"
    # 浏览器可执行路径：指向 ./browser_cores/{engine} 下的实际二进制文件。
    # Playwright 会在 PLAYWRIGHT_BROWSERS_PATH 目录下自动维护版本子目录
    # （如 ./browser_cores/playwright/chrome-<version>/chrome.exe），
    # 优先使用此字段指定的绝对路径；若留空则回退到系统 Playwright 默认路径。
    chromium_path: str = ""
    # rebrowser-patches 补丁版 Chromium 专属二进制路径。
    # 若留空，回退到 chromium_path（rebrowser 的优势在于 Python 层 CDP monkey-patch，
    # 不依赖独立打补丁的 Chromium 二进制）。
    rebrowser_path: str = ""
    # Camoufox Firefox 二进制路径。若留空且未设置 CAMOUFOX_CACHE_DIR，
    # Camoufox 默认使用 ~/.cache/camoufox（污染系统目录）。
    # Camoufox 通过 `python -m camoufox fetch` 下载时会读取 CAMOUFOX_CACHE_DIR
    # 环境变量（在 _init_browser_env() 中已强制设置为 ./browser_cores/camoufox）。
    camoufox_path: str = ""
    db_path: str = "./data/prism.db"
    max_session_pool_size: int = 10
    download_temp_subdir: str = "_temp_playwright"

    model_config = {
        "env_prefix": "PRISM_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


# ---------------------------------------------------------------------------
# 第三部分：顶层 SSOT 模型（PrismSettings）
# ---------------------------------------------------------------------------

class PrismSettings(BaseModel):
    """
    顶层运行时参数聚合模型（SSOT 核心）。
    将 UI 传来的完整 JSON 字典解析并校验为强类型嵌套子模型，
    是整个爬虫进程中所有模块参数的唯一可信来源。
    Pattern: SSOT —— 每次任务启动时由 update_settings() 刷新此单例。
    """

    task_info: TaskInfoConfig = Field(default_factory=TaskInfoConfig)
    strategy_settings: StrategyConfig = Field(default_factory=StrategyConfig)
    engine_settings: EngineConfig = Field(default_factory=EngineConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    timeouts_and_retries: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    stealth: StealthConfig = Field(default_factory=StealthConfig)
    ui_filters: UiFiltersConfig = Field(default_factory=UiFiltersConfig)

    @model_validator(mode="after")
    def _normalize_stealth_engine_for_crawler(self) -> PrismSettings:
        """
        静态解析（beautifulsoup）不走 Playwright 浏览器栈，伪装后端强制回退为 chromium，
        与前端在 BS4 模式下禁用「设备伪装」选择的行为一致。
        """
        if self.engine_settings.crawler_type == "beautifulsoup":
            if self.stealth.stealth_engine != "chromium":
                self.stealth = self.stealth.model_copy(
                    update={"stealth_engine": "chromium"}
                )
        return self

    @classmethod
    def from_ui_dict(cls, config_dict: dict) -> "PrismSettings":
        """
        工厂方法：从 UI 传来的原始 JSON 字典构建并返回 PrismSettings 实例。
        负责将扁平嵌套字典映射到对应子模型，屏蔽 Pydantic 解析细节。
        """
        # model_validate 直接处理嵌套 dict → 嵌套 BaseModel 的递归解析与校验
        return cls.model_validate(config_dict)

    def to_flat_dict(self) -> dict:
        """
        将嵌套配置模型展平为单层字典。
        主要用于与旧引擎工厂接口（site_engines.py 风格）保持兼容。
        """
        # 将各 section 的字段逐层合并到同一平层；
        # 深层子模型（如 human_timing）以嵌套 dict 形式保留，
        # 由引擎工厂按需取用，不强制二次展开
        result: Dict[str, object] = {}
        for section_value in self.model_dump().values():
            if isinstance(section_value, dict):
                result.update(section_value)
        return result

    def get_effective_max_concurrency(self) -> int:
        """
        解析 max_concurrency 字段：若为 'auto' 则返回平台默认值（16），
        否则返回 int 并截断至物理上限，保证引擎工厂拿到的永远是 int 类型。
        """
        _AUTO_DEFAULT = 16
        # 物理上限：I/O 密集型爬虫取 CPU 线程数 × 4，最低不低于 _AUTO_DEFAULT
        _PHYSICAL_MAX = max(_AUTO_DEFAULT, (os.cpu_count() or 4) * 4)

        val = self.performance.max_concurrency
        if val == "auto":
            return _AUTO_DEFAULT

        # 截断到 [min_concurrency, _PHYSICAL_MAX]
        return max(self.performance.min_concurrency, min(int(val), _PHYSICAL_MAX))


# ---------------------------------------------------------------------------
# 第四部分：模块级单例管理（线程安全）
# ---------------------------------------------------------------------------

_app_config: Optional[AppConfig] = None
_app_config_lock = threading.Lock()

_runtime_settings: Optional[PrismSettings] = None
_settings_lock = threading.Lock()


def get_app_config() -> AppConfig:
    """
    获取 AppConfig 单例（双重检查锁，懒加载）。
    整个进程生命周期内只实例化一次，线程安全。
    """
    global _app_config
    if _app_config is None:
        with _app_config_lock:
            # 持锁后二次判断，防止两个线程同时通过外层 None 检查
            if _app_config is None:
                _app_config = AppConfig()
    return _app_config


def get_settings() -> PrismSettings:
    """
    获取当前任务的运行时 PrismSettings 单例。
    若尚未通过 update_settings() 初始化，则返回携带全部默认值的实例。
    """
    global _runtime_settings
    if _runtime_settings is None:
        with _settings_lock:
            if _runtime_settings is None:
                # 首次访问前尚未 update_settings()，构造全默认值实例
                _runtime_settings = PrismSettings()
    return _runtime_settings


def update_settings(config_dict: dict) -> PrismSettings:
    """
    从 UI 传来的 JSON 字典更新并替换全局运行时 PrismSettings 单例。
    每次任务启动前由 MasterDispatcher 调用，线程安全。
    """
    global _runtime_settings
    # 在锁外完成 Pydantic 解析（避免持锁期间做 I/O 或耗时校验）
    new_settings = PrismSettings.from_ui_dict(config_dict)
    # 持锁原子替换引用，确保 get_settings() 并发读取时不会看到中间态
    with _settings_lock:
        _runtime_settings = new_settings
    return new_settings


def apply_stealth_engine_patch(engine: str) -> bool:
    """
    仅更新运行时 StealthConfig.stealth_engine（供金丝雀窗口等与主界面同步，无需完整 UI JSON）。
    合法值：chromium / rebrowser / camoufox。
    """
    if engine not in ("chromium", "rebrowser", "camoufox"):
        return False
    global _runtime_settings
    with _settings_lock:
        base = _runtime_settings if _runtime_settings is not None else PrismSettings()
        _runtime_settings = base.model_copy(
            update={
                "stealth": base.stealth.model_copy(update={"stealth_engine": engine}),
            },
        )
    return True
