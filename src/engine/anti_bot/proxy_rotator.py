"""
@Layer   : Engine 层（第三层 · 反爬工具箱）
@Role    : 代理池配置构建器（ProxyConfiguration 集成适配器）
@Pattern : Adapter Pattern（将 ProxyConfig 列表转换为 Crawlee ProxyConfiguration）
@Description:
    维护一个代理节点列表（ProxyConfig），对外提供
    to_crawlee_proxy_configuration() 接口，将代理池转换为
    Crawlee ProxyConfiguration 实例，供 CrawleeEngineFactory 注入爬虫。

    ── 设计原则（精简说明）───────────────────────────────────────────────
    本模块 不 自行实现代理轮换、健康检查、黑名单等逻辑。
    这些职责完全委托给 Crawlee 的原生机制：

      轮换策略：ProxyConfiguration 内置轮询（rotate=True），
                按 Session 轮换，无需手动维护索引。

      健康检查：Crawlee 的 SessionPool 在 Session 失败次数超过
                max_error_score 后自动退役该 Session 并切换到下一个代理，
                与手写 fail_count + blacklist 效果完全等价。

      并发安全：ProxyConfiguration 本身线程/协程安全，无需 asyncio.Lock。

    本模块只需做一件事：持有代理列表 + 构建 ProxyConfiguration 对象。

    ── 协议选型（protocol 默认 socks5）────────────────────────────────────
    HTTP 代理：代理服务器重新发起 TLS 握手，Chromium 的 BoringSSL JA3 指纹
               被代理端 TLS 替换，WAF 可通过 JA3 识别为非真实 Chrome。
    SOCKS5 代理：工作在传输层（TCP），不介入 TLS，Chromium 与目标服务器
               直接端到端 TLS 握手，JA3 指纹保真（完全来自真实 BoringSSL）。
    → 默认使用 socks5，对 TLS 指纹检测最为友好。

    Pattern: Adapter —— 屏蔽 Crawlee ProxyConfiguration API 差异，
             CrawleeEngineFactory 只需调用 build() 即可获得注入就绪的对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from crawlee.proxy_configuration import ProxyConfiguration


# ---------------------------------------------------------------------------
# 代理配置数据类
# ---------------------------------------------------------------------------

@dataclass
class ProxyConfig:
    """
    单条代理节点配置数据类。

    Attributes:
        host    : 代理服务器 IP 或域名。
        port    : 代理端口号。
        protocol: 协议类型（'socks5' / 'http' / 'https'，默认 'socks5'）。
        username: 认证用户名（无需认证时为 None）。
        password: 认证密码（无需认证时为 None）。
    """

    host: str
    port: int
    protocol: str = "socks5"
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def url(self) -> str:
        """
        拼接完整代理 URL（含认证信息）。
        格式：protocol://username:password@host:port 或 protocol://host:port

        Returns:
            标准代理 URL 字符串。
        """
        if self.is_authenticated:
            return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def is_authenticated(self) -> bool:
        """
        判断该代理是否需要用户名/密码认证。

        Returns:
            True 表示需要认证（username 和 password 均非空）。
        """
        return bool(self.username) and bool(self.password)


# ---------------------------------------------------------------------------
# 代理池构建器
# ---------------------------------------------------------------------------

class ProxyRotator:
    """
    代理池配置构建器（Crawlee ProxyConfiguration 适配器）。

    职责：
    1. 持有 ProxyConfig 列表。
    2. 提供 build() 工厂方法，返回配置完毕的 Crawlee ProxyConfiguration 实例。
    3. 支持运行时动态增减代理节点（add_proxy / remove_proxy）。

    不负责：
    - 代理轮换（由 ProxyConfiguration 内置轮询处理）。
    - 健康检查 / 黑名单（由 Crawlee SessionPool 的 Session 退役机制处理）。
    - 并发锁（ProxyConfiguration 本身协程安全，无需额外锁）。

    Pattern: Adapter（ProxyConfig list → Crawlee ProxyConfiguration）
    """

    def __init__(self, proxies: Optional[List[ProxyConfig]] = None) -> None:
        """
        Args:
            proxies: 初始代理列表（可为空，后续通过 add_proxy() 动态添加）。

        初始化说明：
            self._proxies : List[ProxyConfig] —— 当前代理节点列表。
        """
        self._proxies: List[ProxyConfig] = list(proxies) if proxies else []

    # ------------------------------------------------------------------
    # 公开：代理池管理
    # ------------------------------------------------------------------

    def add_proxy(self, proxy: ProxyConfig) -> None:
        """
        向代理池追加新节点。

        Args:
            proxy: 待添加的 ProxyConfig 实例。
        """
        self._proxies.append(proxy)

    def remove_proxy(self, proxy: ProxyConfig) -> None:
        """
        从代理池移除指定节点，若不存在则静默忽略。

        Args:
            proxy: 待移除的 ProxyConfig 实例。
        """
        try:
            self._proxies.remove(proxy)
        except ValueError:
            pass

    @property
    def count(self) -> int:
        """
        返回当前代理池节点数量。

        Returns:
            代理节点数量整数。
        """
        return len(self._proxies)

    # ------------------------------------------------------------------
    # 公开：Crawlee 集成
    # ------------------------------------------------------------------

    async def build(self) -> Optional["ProxyConfiguration"]:
        """
        构建并返回 Crawlee ProxyConfiguration 实例，供 CrawleeEngineFactory 注入。

        实现：
            from crawlee.proxy_configuration import ProxyConfiguration
            return await ProxyConfiguration.create(
                proxy_urls=[p.url for p in self._proxies]
            )
        若代理池为空，返回 None（爬虫退化为直连模式）。

        Returns:
            配置完毕的 ProxyConfiguration 实例；代理池为空时返回 None。
        """
        if not self._proxies:
            return None
        from crawlee.proxy_configuration import ProxyConfiguration
        return await ProxyConfiguration.create(
            proxy_urls=[p.url for p in self._proxies]
        )
