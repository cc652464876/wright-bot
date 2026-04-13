"""
@Layer   : Engine 层（第三层 · 反爬工具箱）
@Role    : 本地硬件感知的浏览器指纹生成器
@Pattern : Strategy Pattern（按 DeviceProfile 选择生成策略） + Facade（FingerprintInjector）
@Description:
    新增 DeviceProfile.LOCAL_PC 档案：
    程序启动时通过 _read_hardware() 一次性读取本机真实硬件信息
    （屏幕分辨率、GPU 型号、CPU 逻辑核数、系统时区、系统语言），
    生成与当前机器完全吻合的 FingerprintProfile。

    本地桌面应用的核心优势：真实 GPU、真实屏幕、真实字体——
    所有 WAF 硬件一致性检测项与实际硬件完全匹配，无需任何伪造，
    是比静态预设或 JS 注入更彻底的防检测方案。

    硬件读取策略（均为 stdlib / 零额外依赖，Windows 优先）：
    - 屏幕分辨率 : ctypes.windll.user32（SetProcessDPIAware 获取物理像素）
    - GPU 型号   : subprocess + PowerShell CIM（优先）→ wmic（降级）→ Intel UHD fallback
                   Windows 11 24H2 已移除 wmic，PowerShell Get-CimInstance 为新主路径
    - CPU 核数   : os.cpu_count()
    - 时区       : tzlocal.get_localzone()，降级到 Windows 时区名映射表
    - 系统语言   : ctypes.GetUserDefaultLocaleName，降级到 locale.getlocale()

    其余 DeviceProfile 使用经过校验的真实世界常见值预设，
    供需要伪装成其他设备/平台的场景使用。

    FingerprintInjector：
    注入策略已迁移至 BrowserContext.new_context() 参数层（user_agent /
    viewport / locale / timezone_id），不再使用 page.evaluate() JS 覆盖
    （JS 覆盖会留下 Object.defineProperty 调用链，被现代 WAF 识别）。
    此类方法保留骨架，供未来扩展或特殊场景覆盖使用。
"""

from __future__ import annotations

import locale
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


# ---------------------------------------------------------------------------
# 内部：本机硬件信息快照（进程内缓存，只读取一次）
# ---------------------------------------------------------------------------

@dataclass
class _HardwareInfo:
    """
    本机硬件信息快照，由 _read_hardware() 在首次调用时填充并缓存。

    Attributes:
        screen_width    : 主显示器物理宽度（逻辑像素 × DPI 缩放后的真实值）。
        screen_height   : 主显示器物理高度。
        webgl_vendor    : Chrome/ANGLE 上报的 UNMASKED_VENDOR_WEBGL 字符串。
        webgl_renderer  : Chrome/ANGLE 上报的 UNMASKED_RENDERER_WEBGL 字符串。
        cpu_count       : 逻辑 CPU 核数（对应 navigator.hardwareConcurrency）。
        timezone        : IANA 时区字符串（如 'Asia/Shanghai'）。
        languages       : navigator.languages 格式的语言列表（如 ['zh-CN', 'zh', 'en']）。
    """
    screen_width: int
    screen_height: int
    webgl_vendor: str
    webgl_renderer: str
    cpu_count: int
    timezone: str
    languages: List[str]


# 模块级缓存：_read_hardware() 只执行一次 I/O，后续直接返回此对象
_CACHED_HW: Optional[_HardwareInfo] = None


# ---------------------------------------------------------------------------
# 内部：硬件读取工具函数
# ---------------------------------------------------------------------------

def _get_screen_metrics() -> Tuple[int, int]:
    """
    读取主显示器真实物理分辨率（宽, 高）。

    Windows：调用 SetProcessDPIAware() 后通过 GetSystemMetrics 获取，
    确保 DPI 缩放（如 125% / 150%）不影响读取结果，返回真实物理像素。
    非 Windows：返回常见默认值 1920×1080。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception:
            pass
    return 1920, 1080


def _vendor_from_gpu_name(name: str) -> str:
    """
    根据 GPU 型号名推断 Chrome/ANGLE 在 Windows 上报的 UNMASKED_VENDOR_WEBGL 字符串。
    Chrome 通过 ANGLE 渲染层，Vendor 格式为 "Google Inc. (厂商)"。
    """
    n = name.upper()
    if "NVIDIA" in n:
        return "Google Inc. (NVIDIA)"
    if "AMD" in n or "RADEON" in n or "VEGA" in n:
        return "Google Inc. (AMD)"
    if "INTEL" in n:
        return "Google Inc. (Intel)"
    return "Google Inc."


def _renderer_from_gpu_name(name: str) -> str:
    """
    根据 GPU 型号名推断 Chrome/ANGLE 在 Windows 上报的 UNMASKED_RENDERER_WEBGL 字符串。
    格式：ANGLE (厂商简称, 完整型号 Direct3D11 vs_5_0 ps_5_0, D3D11)
    """
    n = name.upper()
    if "NVIDIA" in n:
        prefix = "NVIDIA"
    elif "AMD" in n or "RADEON" in n:
        prefix = "AMD"
    elif "INTEL" in n:
        prefix = "Intel"
    else:
        prefix = "Unknown"
    return f"ANGLE ({prefix}, {name} Direct3D11 vs_5_0 ps_5_0, D3D11)"


def _get_gpu_info() -> Tuple[str, str]:
    """
    读取本机首块 GPU 的型号，转换为 Chrome/ANGLE WebGL 格式的 (vendor, renderer) 元组。

    Windows 三级读取策略（按优先级依次尝试）：

    1. PowerShell CIM（优先路径，Windows 10/11 均支持）：
       命令：powershell -NoProfile -NonInteractive -Command
             "Get-CimInstance Win32_VideoController |
              Select-Object -First 1 -ExpandProperty Name"
       优点：wmic 已在 Windows 11 22H2+ 标记弃用、24H2 部分设备移除；
             CIM/PowerShell 是微软官方推荐的替代方案，全平台稳定。
       参数：creationflags=subprocess.CREATE_NO_WINDOW 避免弹出控制台黑框。
       超时：10 秒（CIM 调用略慢于 wmic，给足裕量）。

    2. wmic（降级路径，兼容 Windows 10 旧版环境）：
       命令：wmic path Win32_VideoController get Name /value
       解析：行格式为 "Name=NVIDIA GeForce RTX 4080"，取 "Name=" 后的值。
       超时：8 秒。

    3. 最终 fallback（非 Windows 或两路均失败）：
       返回 Intel UHD 集显字符串（最常见的安全后备值，保持 WAF 合理性）。

    交叉验证说明：
        WAF 会对比 WebGL UNMASKED_RENDERER 与页面 GPU 渲染行为是否一致。
        若 wmic 因被移除而失败后 fallback 到 Intel UHD，而实际机器为 RTX 3070，
        则 WebGL 渲染结果与上报值不一致，触发 WAF 交叉验证检测点。
        此三级策略确保真实 GPU 型号被正确读取。

    Returns:
        (webgl_vendor, webgl_renderer) 元组，均为 Chrome/ANGLE 格式字符串。
        示例：("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 ...")
    """
    _FALLBACK = "Intel UHD Graphics 630"
    gpu_name: Optional[str] = None

    if sys.platform == "win32":
        # 路径 1：PowerShell CIM（Windows 10/11 主路径，wmic 在 24H2 已部分移除）
        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance Win32_VideoController"
                    " | Select-Object -First 1 -ExpandProperty Name",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            name = result.stdout.strip()
            if name:
                gpu_name = name
        except Exception:
            pass

        if not gpu_name:
            # 路径 2：wmic 降级兼容旧版 Windows 10
            try:
                result = subprocess.run(
                    ["wmic", "path", "Win32_VideoController", "get", "Name", "/value"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for line in result.stdout.splitlines():
                    if line.upper().startswith("NAME="):
                        name = line[5:].strip()
                        if name:
                            gpu_name = name
                            break
            except Exception:
                pass

    # 路径 3：最终 fallback（非 Windows 或两路均失败）
    if not gpu_name:
        gpu_name = _FALLBACK

    return _vendor_from_gpu_name(gpu_name), _renderer_from_gpu_name(gpu_name)


def _get_timezone() -> str:
    """
    读取本机系统时区，返回 IANA 格式字符串（如 'Asia/Shanghai'）。

    优先使用 tzlocal.get_localzone()（跨平台精确）；
    未安装 tzlocal 时降级到 Windows 时区名 → IANA 映射表。
    """
    try:
        from tzlocal import get_localzone
        return str(get_localzone())
    except ImportError:
        pass

    if sys.platform == "win32":
        import time
        _WIN_TO_IANA: Dict[str, str] = {
            "China Standard Time":     "Asia/Shanghai",
            "UTC":                     "UTC",
            "Eastern Standard Time":   "America/New_York",
            "Central Standard Time":   "America/Chicago",
            "Mountain Standard Time":  "America/Denver",
            "Pacific Standard Time":   "America/Los_Angeles",
            "Tokyo Standard Time":     "Asia/Tokyo",
            "Korea Standard Time":     "Asia/Seoul",
            "Singapore Standard Time": "Asia/Singapore",
            "GMT Standard Time":       "Europe/London",
            "Romance Standard Time":   "Europe/Paris",
            "W. Europe Standard Time": "Europe/Berlin",
        }
        return _WIN_TO_IANA.get(time.tzname[0], "Asia/Shanghai")
    return "Asia/Shanghai"


def _get_languages() -> List[str]:
    """
    读取本机系统 UI 语言，返回 navigator.languages 格式的列表。
    例：['zh-CN', 'zh', 'en'] 或 ['en-US', 'en']

    Windows 优先路径：ctypes.GetUserDefaultLocaleName（最准确，返回 IETF 格式如 'zh-CN'）。
    降级路径：locale.getlocale() → 硬编码默认值 ['zh-CN', 'zh', 'en']。
    """
    raw: Optional[str] = None

    # Windows 最可靠方式：直接读取 UI 语言名称（IETF 格式，如 "zh-CN"）
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(85)
            ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85)
            raw = buf.value  # "zh-CN"
            if raw:
                # 转换为 "_" 分隔格式统一处理
                raw = raw.replace("-", "_")  # "zh-CN" → "zh_CN"
        except Exception:
            pass

    # 通用降级：locale 模块
    if not raw:
        try:
            raw = locale.getlocale()[0]  # e.g. "zh_CN"
        except Exception:
            pass

    if not raw:
        return ["zh-CN", "zh", "en"]

    lang_full = raw.replace("_", "-")   # "zh_CN" → "zh-CN"
    lang_short = raw.split("_")[0]       # "zh_CN" → "zh"
    return [lang_full, lang_short, "en"] if lang_short.lower() != "en" else [lang_full, "en"]


def _read_hardware() -> _HardwareInfo:
    """
    一次性读取本机硬件信息并写入模块级缓存 _CACHED_HW。
    后续所有调用直接返回缓存对象，不产生任何 I/O 重复开销。

    读取耗时约 50–200ms（主要来自 wmic 子进程），仅在首次调用时执行。
    """
    global _CACHED_HW
    if _CACHED_HW is not None:
        return _CACHED_HW

    w, h = _get_screen_metrics()
    vendor, renderer = _get_gpu_info()

    _CACHED_HW = _HardwareInfo(
        screen_width=w,
        screen_height=h,
        webgl_vendor=vendor,
        webgl_renderer=renderer,
        cpu_count=os.cpu_count() or 8,
        timezone=_get_timezone(),
        languages=_get_languages(),
    )
    return _CACHED_HW


# ---------------------------------------------------------------------------
# 内部：Chrome Client Hints 请求头构建工具
# ---------------------------------------------------------------------------

def _build_chrome_headers(
    version: str,
    is_mobile: bool,
    platform: str,
    languages: List[str],
) -> Dict[str, str]:
    """
    构建 Chrome 的 Client Hints 与 Accept-Language 请求头字典。

    Args:
        version  : Chrome 主版本号字符串（如 "124"）。
        is_mobile: True 表示移动端，Sec-CH-UA-Mobile 值为 ?1。
        platform : Sec-CH-UA-Platform 值（如 "Windows" / "Android"）。
        languages: navigator.languages 列表，用于生成 Accept-Language。
    """
    accept_lang = ",".join(
        lang if i == 0 else f"{lang};q={round(1.0 - i * 0.1, 1)}"
        for i, lang in enumerate(languages[:4])
    )
    return {
        # 真实浏览器必然携带 Accept 头；缺失此头是高权重 WAF 异常信号
        # 值为 Chrome 127+ 的标准 Accept 字符串，包含 HTML / XML / 图片 / 通配符优先级
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": accept_lang,
        # 字段顺序遵循真实 Chrome 规范：
        #   1. "Not A Brand" 排首位（版本号为 "8" 或 "24"，非固定 "99"）
        #   2. "Chromium" 居中
        #   3. "Google Chrome" 殿后
        # 品牌名 "Not A Brand" 含空格，历史上的 "Not-A.Brand" 写法已被 WAF 标记
        "Sec-CH-UA": (
            f'"Not A Brand";v="8", '
            f'"Chromium";v="{version}", '
            f'"Google Chrome";v="{version}"'
        ),
        "Sec-CH-UA-Mobile": "?1" if is_mobile else "?0",
        "Sec-CH-UA-Platform": f'"{platform}"',
    }


# ---------------------------------------------------------------------------
# 设备档案枚举 & 指纹数据包
# ---------------------------------------------------------------------------

class DeviceProfile(Enum):
    """
    指纹伪造目标设备档案枚举。
    Pattern: Strategy —— FingerprintGenerator 根据此枚举选择生成策略。

    Attributes:
        LOCAL_PC       : 读取本机真实硬件（本地桌面应用首选，最难被检测）。
        WINDOWS_CHROME : Windows 10/11 + Chrome 常见家用主机预设。
        MAC_SAFARI     : macOS Sonoma + Safari 预设。
        LINUX_FIREFOX  : Linux x86_64 + Firefox 预设。
        MOBILE_ANDROID : Android 14 + Chrome Mobile 预设。
        RANDOM         : 从以上非 LOCAL_PC 预设中随机选取。
    """

    LOCAL_PC        = "local_pc"
    WINDOWS_CHROME  = "windows_chrome"
    MAC_SAFARI      = "mac_safari"
    LINUX_FIREFOX   = "linux_firefox"
    MOBILE_ANDROID  = "mobile_android"
    RANDOM          = "random"


@dataclass
class FingerprintProfile:
    """
    一套完整的浏览器指纹数据包。

    由 FingerprintGenerator 生成，由 CrawleeEngineFactory 通过
    BrowserContext.new_context() 参数消费注入（user_agent / viewport /
    locale / timezone_id），extra_headers 通过 context.set_extra_http_headers() 注入。

    Attributes:
        user_agent           : 完整 User-Agent 字符串。
        platform             : navigator.platform（如 'Win32'）。
        languages            : navigator.languages 列表。
        screen_width         : screen.width 像素值。
        screen_height        : screen.height 像素值。
        hardware_concurrency : navigator.hardwareConcurrency（逻辑 CPU 核数）。
        timezone             : Intl.DateTimeFormat 时区 ID（IANA 格式）。
        webgl_vendor         : WebGL UNMASKED_VENDOR_WEBGL。
        webgl_renderer       : WebGL UNMASKED_RENDERER_WEBGL。
        extra_headers        : 附加 HTTP 请求头（Accept-Language / Sec-CH-UA 等）。
    """

    user_agent: str = ""
    platform: str = ""
    languages: List[str] = field(default_factory=list)
    screen_width: int = 1920
    screen_height: int = 1080
    hardware_concurrency: int = 8
    timezone: str = "Asia/Shanghai"
    webgl_vendor: str = ""
    webgl_renderer: str = ""
    extra_headers: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 近期 Chrome 版本列表（供预设随机选取，保持多样性）
# 维护周期：每季度更新一次，保持与 Chrome 稳定版发布节奏同步
# ---------------------------------------------------------------------------

# 更新为当前真实稳定版本区间（2026 Q1–Q2）
_CHROME_VERSIONS: List[str] = ["133", "134", "135", "136"]

# Windows 常见屏幕分辨率（按市场占有率加权，供 WINDOWS_CHROME 预设随机选取）
_WIN_RESOLUTIONS: List[Tuple[int, int]] = [
    (1920, 1080),   # 最常见（FHD）
    (1920, 1080),   # 权重加倍
    (2560, 1440),   # QHD
    (1366, 768),    # 老款笔记本
    (1536, 864),    # Surface 等
    (1440, 900),    # MacBook 接外显
]


# ---------------------------------------------------------------------------
# 指纹生成器
# ---------------------------------------------------------------------------

class FingerprintGenerator:
    """
    浏览器指纹生成器。

    LOCAL_PC 档案（默认）：
        __init__ 时调用 _read_hardware() 读取并缓存本机硬件信息。
        _generate_local_pc() 将真实硬件数据直接填入 FingerprintProfile，
        屏幕分辨率 / WebGL / CPU 核数 / 时区 / 语言均与当前机器完全一致。

    其余预设档案：
        使用经过交叉验证的真实世界常见值，随机化 Chrome 版本与分辨率
        以增加多样性，避免多次请求使用完全相同的指纹。

    Pattern: Strategy Pattern —— generate(device) 按策略委托不同的生成方法。
    """

    def __init__(
        self,
        preferred_device: DeviceProfile = DeviceProfile.LOCAL_PC,
    ) -> None:
        """
        Args:
            preferred_device: 默认档案，调用 generate() 不传参时使用。
                              本地桌面应用建议保持默认 LOCAL_PC。

        初始化时立即触发 _read_hardware()；结果写入模块级缓存，
        多次实例化此类不会重复执行硬件 I/O。
        """
        self._preferred_device = preferred_device
        self._hw: _HardwareInfo = _read_hardware()

    # ------------------------------------------------------------------
    # 公开：生成接口
    # ------------------------------------------------------------------

    def generate(self, device: Optional[DeviceProfile] = None) -> FingerprintProfile:
        """
        按指定设备档案生成完整的 FingerprintProfile。

        Args:
            device: 目标档案；为 None 时使用 preferred_device。
        Returns:
            填充完整的 FingerprintProfile 实例。
        """
        target = device or self._preferred_device
        _dispatch = {
            DeviceProfile.LOCAL_PC:       self._generate_local_pc,
            DeviceProfile.WINDOWS_CHROME: self._generate_windows_chrome,
            DeviceProfile.MAC_SAFARI:     self._generate_mac_safari,
            DeviceProfile.LINUX_FIREFOX:  self._generate_linux_firefox,
            DeviceProfile.MOBILE_ANDROID: self._generate_mobile_android,
            DeviceProfile.RANDOM:         self.generate_random,
        }
        return _dispatch[target]()

    def generate_random(self) -> FingerprintProfile:
        """
        从非 LOCAL_PC 的预设档案中随机选取一套指纹（忽略 preferred_device）。
        通常在代理切换后调用，彻底重置身份特征。

        Returns:
            随机填充的 FingerprintProfile 实例。
        """
        _pool = [
            self._generate_windows_chrome,
            self._generate_mac_safari,
            self._generate_linux_firefox,
            self._generate_mobile_android,
        ]
        return random.choice(_pool)()

    # ------------------------------------------------------------------
    # 私有：各档案生成方法
    # ------------------------------------------------------------------

    def _generate_local_pc(self) -> FingerprintProfile:
        """
        LOCAL_PC 档案：将本机真实硬件数据直接填入指纹包。

        - screen_width / screen_height  : ctypes 读取的真实物理分辨率
        - webgl_vendor / webgl_renderer : wmic 读取的真实 GPU → ANGLE 格式
        - hardware_concurrency          : os.cpu_count() 的真实逻辑核数
        - timezone                      : tzlocal 读取的真实 IANA 时区
        - languages                     : GetUserDefaultLocaleName 读取的真实 UI 语言
        - user_agent                    : Windows + Chrome 近期版本（与 Win32 平台匹配）
        - platform                      : "Win32"（64-bit Windows 浏览器仍报 Win32）
        """
        version = random.choice(_CHROME_VERSIONS)
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version}.0.0.0 Safari/537.36"
        )
        return FingerprintProfile(
            user_agent=ua,
            platform="Win32",
            languages=self._hw.languages,
            screen_width=self._hw.screen_width,
            screen_height=self._hw.screen_height,
            hardware_concurrency=self._hw.cpu_count,
            timezone=self._hw.timezone,
            webgl_vendor=self._hw.webgl_vendor,
            webgl_renderer=self._hw.webgl_renderer,
            extra_headers=_build_chrome_headers(
                version=version,
                is_mobile=False,
                platform="Windows",
                languages=self._hw.languages,
            ),
        )

    def _generate_windows_chrome(self) -> FingerprintProfile:
        """
        Windows 10/11 + Chrome 预设（家用主机典型配置）。
        Chrome 版本与屏幕分辨率随机化，增加指纹多样性。
        WebGL 预设：NVIDIA RTX 3060（主流独立显卡代表）。
        """
        version = random.choice(_CHROME_VERSIONS)
        res = random.choice(_WIN_RESOLUTIONS)
        languages = ["zh-CN", "zh", "en"]
        return FingerprintProfile(
            user_agent=(
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version}.0.0.0 Safari/537.36"
            ),
            platform="Win32",
            languages=languages,
            screen_width=res[0],
            screen_height=res[1],
            hardware_concurrency=random.choice([8, 12, 16]),
            timezone="Asia/Shanghai",
            webgl_vendor="Google Inc. (NVIDIA)",
            webgl_renderer=(
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 "
                "Direct3D11 vs_5_0 ps_5_0, D3D11)"
            ),
            extra_headers=_build_chrome_headers(
                version=version,
                is_mobile=False,
                platform="Windows",
                languages=languages,
            ),
        )

    def _generate_mac_safari(self) -> FingerprintProfile:
        """
        macOS Sonoma 14 + Safari 17 预设（MacBook Pro 14" M3 典型配置）。
        Safari 不发送 Sec-CH-UA Client Hints，extra_headers 仅含 Accept-Language。
        """
        languages = ["zh-CN", "zh", "en"]
        return FingerprintProfile(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4.1 Safari/605.1.15"
            ),
            platform="MacIntel",
            languages=languages,
            screen_width=2560,
            screen_height=1664,
            hardware_concurrency=random.choice([10, 12]),
            timezone="Asia/Shanghai",
            # macOS + Metal 路径，非 ANGLE，Safari 直接上报 Apple Inc.
            webgl_vendor="Apple Inc.",
            webgl_renderer="Apple M3 Pro",
            extra_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    def _generate_linux_firefox(self) -> FingerprintProfile:
        """
        Linux x86_64 + Firefox 124/125 预设（开发者工作站典型配置）。
        Firefox 不支持 Sec-CH-UA，WebGL 使用 Mesa llvmpipe（无 GPU 驱动环境）。
        """
        ff_version = random.choice(["124.0", "125.0"])
        languages = ["zh-CN", "zh", "en"]
        return FingerprintProfile(
            user_agent=(
                f"Mozilla/5.0 (X11; Linux x86_64; rv:{ff_version}) "
                f"Gecko/20100101 Firefox/{ff_version}"
            ),
            platform="Linux x86_64",
            languages=languages,
            screen_width=1920,
            screen_height=1080,
            hardware_concurrency=random.choice([8, 16]),
            timezone="Asia/Shanghai",
            webgl_vendor="Mesa/X.org",
            webgl_renderer="llvmpipe (LLVM 15.0.7, 256 bits)",
            extra_headers={
                "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,en-US;q=0.5,en;q=0.3",
            },
        )

    def _generate_mobile_android(self) -> FingerprintProfile:
        """
        Android 14 + Chrome Mobile 预设（Pixel 8 Pro 典型配置）。
        屏幕为竖屏分辨率 1008×2244，WebGL 使用 Qualcomm Adreno 740。
        """
        version = random.choice(_CHROME_VERSIONS)
        languages = ["zh-CN", "zh", "en"]
        return FingerprintProfile(
            user_agent=(
                f"Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version}.0.6367.82 Mobile Safari/537.36"
            ),
            platform="Linux armv8l",
            languages=languages,
            screen_width=1008,
            screen_height=2244,
            hardware_concurrency=8,
            timezone="Asia/Shanghai",
            webgl_vendor="Qualcomm",
            webgl_renderer="Adreno (TM) 740",
            extra_headers=_build_chrome_headers(
                version=version,
                is_mobile=True,
                platform="Android",
                languages=languages,
            ),
        )


# ---------------------------------------------------------------------------
# 指纹注入器
# ---------------------------------------------------------------------------

# Canvas 噪声注入脚本（仅 chromium / rebrowser 路径使用；camoufox 在底层已处理）
# 每隔 97 字节对像素数据做 XOR 微扰，破坏 Canvas 哈希一致性，同时对视觉无影响
_CANVAS_NOISE_SCRIPT: str = """
(function() {
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const imgData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imgData.data.length; i += 97) {
                imgData.data[i] ^= (Math.random() * 3) | 0;
            }
            ctx.putImageData(imgData, 0, 0);
        }
        return _toDataURL.apply(this, arguments);
    };
})();
"""


class FingerprintInjector:
    """
    指纹注入器（page.add_init_script 路径，供 chromium / rebrowser 引擎使用）。

    注入时机：必须在 page.goto() 之前调用，确保脚本在所有 frame 导航前执行。
    注入 API：page.add_init_script()（而非 page.evaluate()）。
        - add_init_script  : 脚本在每次导航前预注入，对 WAF 无可见调用时序特征。
        - evaluate         : 在导航后执行，留下可被检测的异步调用链，已弃用。

    各路径说明：
        - chromium / rebrowser 引擎：
            需要注入 Canvas 噪声（_CANVAS_NOISE_SCRIPT）和 WebGL 覆盖脚本。
            LOCAL_PC 档案的 webgl_vendor/renderer 来自真实 GPU，无需 WebGL 覆盖。
            非 LOCAL_PC 档案（WINDOWS_CHROME / LINUX_FIREFOX 等）需要 _override_webgl()。
        - camoufox 引擎：
            C++ 层已在二进制内部处理 WebGL / Canvas / AudioContext 随机化，
            调用方应跳过本类所有注入方法（不要重复注入）。

    Pattern: Facade —— 统一封装多条注入路径，调用方只需调用 inject()。
    """

    async def inject(self, page: "Page", profile: FingerprintProfile) -> None:
        """
        将指纹防护脚本注入目标 Page（统一入口）。

        注入顺序（须在 page.goto() 之前完成）：
        1. Navigator 覆盖（_override_navigator）：覆盖 userAgent / platform / languages 等。
        2. Screen 覆盖（_override_screen）：覆盖 screen 尺寸。
        3. Canvas 噪声（_CANVAS_NOISE_SCRIPT）：始终注入（对所有非 camoufox 路径）。
        4. WebGL 覆盖（_override_webgl）：
               仅当 profile.webgl_vendor 非空时注入（LOCAL_PC 档案跳过此步）。

        调用方职责：
            camoufox 引擎的调用方不应调用此方法（底层已处理，避免二次注入冲突）。

        Args:
            page   : 目标 Playwright Page，须在 goto() 之前调用。
            profile: 待注入的 FingerprintProfile 数据包。
        """
        # Navigator 覆盖：userAgent / platform / languages / hardwareConcurrency
        await self._override_navigator(page, profile)

        # Screen 覆盖：screen.width / height / availWidth / availHeight
        await self._override_screen(page, profile)

        # Canvas 噪声：对所有非 camoufox 路径始终注入
        await page.add_init_script(script=_CANVAS_NOISE_SCRIPT)

        # WebGL 覆盖：仅当档案提供了非空的 vendor/renderer（即非 LOCAL_PC 真实 GPU）时注入
        if profile.webgl_vendor and profile.webgl_renderer:
            await self._override_webgl(page, profile)

        # 附加请求头：Accept-Language / Sec-CH-UA 等
        if profile.extra_headers:
            await self._inject_headers(page, profile)

    async def _override_navigator(
        self, page: "Page", profile: FingerprintProfile
    ) -> None:
        """覆盖 navigator.userAgent / platform / languages / hardwareConcurrency。"""
        languages_js = str(profile.languages)
        script = f"""
(function() {{
    Object.defineProperty(navigator, 'userAgent',           {{ get: () => {repr(profile.user_agent)} }});
    Object.defineProperty(navigator, 'platform',            {{ get: () => {repr(profile.platform)} }});
    Object.defineProperty(navigator, 'languages',           {{ get: () => {languages_js} }});
    Object.defineProperty(navigator, 'language',            {{ get: () => {repr(profile.languages[0] if profile.languages else 'zh-CN')} }});
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {profile.hardware_concurrency} }});
}})();
"""
        await page.add_init_script(script=script)

    async def _override_screen(
        self, page: "Page", profile: FingerprintProfile
    ) -> None:
        """覆盖 screen.width / screen.height / screen.availWidth。"""
        script = f"""
(function() {{
    Object.defineProperty(screen, 'width',      {{ get: () => {profile.screen_width} }});
    Object.defineProperty(screen, 'height',     {{ get: () => {profile.screen_height} }});
    Object.defineProperty(screen, 'availWidth', {{ get: () => {profile.screen_width} }});
    Object.defineProperty(screen, 'availHeight',{{ get: () => {profile.screen_height} }});
}})();
"""
        await page.add_init_script(script=script)

    async def _override_webgl(
        self, page: "Page", profile: FingerprintProfile
    ) -> None:
        """
        通过 page.add_init_script() 注入 WebGL 参数覆盖脚本。

        适用条件（调用方应自行判断，满足全部条件才调用）：
            1. 引擎为 chromium 或 rebrowser（非 camoufox）。
            2. profile.webgl_vendor 和 profile.webgl_renderer 均非空。
            3. 档案类型不为 LOCAL_PC（LOCAL_PC 档案的真实 GPU 无需覆盖）。

        注入逻辑（覆盖 WebGLRenderingContext 和 WebGL2RenderingContext）：
            拦截 getParameter() 调用，对以下两个常量返回 profile 中的预设值：
                UNMASKED_VENDOR_WEBGL   = 0x9245 → profile.webgl_vendor
                UNMASKED_RENDERER_WEBGL = 0x9246 → profile.webgl_renderer
            其余参数透传给原生实现，不影响正常渲染。

        技术选型说明：
            使用 add_init_script 而非 evaluate，是因为前者在所有 frame 导航前
            执行，不产生可被检测的调用时序；evaluate 在导航后执行，会留下
            Object.defineProperty 调用链，被现代 WAF 识别为自动化特征。

        Args:
            page   : 目标 Playwright Page，须在 goto() 之前调用。
            profile: 含 webgl_vendor / webgl_renderer 的 FingerprintProfile。
        """
        vendor = profile.webgl_vendor.replace("\\", "\\\\").replace('"', '\\"')
        renderer = profile.webgl_renderer.replace("\\", "\\\\").replace('"', '\\"')
        script = f"""
(function() {{
    const VENDOR   = 0x9245;
    const RENDERER = 0x9246;
    const _vendor   = "{vendor}";
    const _renderer = "{renderer}";

    function patchCtx(proto) {{
        const _orig = proto.getParameter.bind;
        const orig  = proto.getParameter;
        proto.getParameter = function(pname) {{
            if (pname === VENDOR)   return _vendor;
            if (pname === RENDERER) return _renderer;
            return orig.call(this, pname);
        }};
    }}

    if (typeof WebGLRenderingContext  !== 'undefined') patchCtx(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patchCtx(WebGL2RenderingContext.prototype);
}})();
"""
        await page.add_init_script(script=script)

    async def _inject_headers(
        self, page: "Page", profile: FingerprintProfile
    ) -> None:
        """通过 page.set_extra_http_headers() 注入 Accept-Language / Sec-CH-UA 等头。"""
        if profile.extra_headers:
            await page.set_extra_http_headers(profile.extra_headers)
