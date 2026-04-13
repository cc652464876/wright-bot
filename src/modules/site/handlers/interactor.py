"""
@Layer   : Modules 层（第四层 · 业务逻辑）
@Role    : DOM 页面清洗与元素交互器
@Pattern : Chain of Responsibility（职责链节点） +
           Command Pattern（将按钮封装为 Crawlee Request 命令） +
           Strategy Pattern（行为模拟器可替换）
@Description:
    Interactor 负责与页面 DOM 进行三类交互：
    1. clear_cookie_banners()    : 战前清理，自动点击同意 Cookie 弹窗，
                                   防止遮罩层阻挡后续点击操作。
    2. extract_raw_links()       : 从页面提取所有 a[href] 链接，
                                   兼容 Playwright（动态渲染）和 BeautifulSoup（静态解析）两种模式。
    3. trigger_download_buttons(): 侦察兵模式：扫描疑似下载按钮（button / 伪链接 a），
                                   不直接点击，而是封装为带唯一指纹的 Crawlee Request（NEED_CLICK 标签）
                                   推入队列，交由 ActionHandler 在独立沙箱中执行。
    与 ErrorRegistry 的 error_interceptor 集成，将 DOM 操作异常拦截后上报。

    行为模拟器（AbstractBehaviorSimulator）通过构造函数注入，控制"如何执行点击/悬停"：
        - PlaywrightBehaviorSimulator : 使用 Playwright locator.click()（默认，无额外依赖）。
        - PyAutoGUIBezierSimulator    : 将 DOM 元素坐标转换为屏幕坐标，
                                        通过 PyAutoGUI + 贝塞尔曲线执行 OS 级真实鼠标事件，
                                        可通过 WAF 对 DevTools Protocol 鼠标事件的检测。
    切换模拟器只需在外部替换注入的实例，本类中所有业务逻辑（选择器、Cookie 判断等）零修改。

    Pattern: Chain of Responsibility（请求处理管线第二个节点）
             + Command（Request.from_url 封装点击命令）
             + Strategy（AbstractBehaviorSimulator 行为策略）
"""

from __future__ import annotations

import asyncio
from typing import Callable, List, Optional, TYPE_CHECKING
from urllib.parse import urlparse

from crawlee import Request  # 实例化 Request 对象（非 TYPE_CHECKING，运行时需要）

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page
    from src.modules.site.audit.error_registry import ErrorRegistry
    from src.engine.anti_bot.behavior.base import AbstractBehaviorSimulator

_log = get_logger(__name__)


class Interactor:
    """
    DOM 页面清洗与元素交互器（职责链节点）。

    职责链入口：
    - clear_cookie_banners()    : 在 default_handler 最开始调用。
    - extract_raw_links()       : 链接提取阶段调用。
    - trigger_download_buttons(): 请求处理末尾调用，将发现的按钮裂变为队列任务。
    """

    # Cookie 弹窗自动识别选择器（中英文覆盖）
    COOKIE_SELECTORS: List[str] = [
        'button:has-text("Accept All")',
        'button:has-text("Accept")',
        'button:has-text("Accept cookies")',
        'button:has-text("I Accept")',
        'button:has-text("同意")',
        'button:has-text("接受全部")',
        '#accept-cookies',
        '.cookie-accept',
        '.cookie-banner button',
    ]

    # 疑似下载按钮选择器（宽泛扫描，由 ActionDownloader 后置过滤）
    DOWNLOAD_BUTTON_SELECTORS: List[str] = [
        'button:has-text("PDF"), button:has-text("Download"), button:has-text("下载"), '
        'button:has-text("View"), button:has-text("查看")',
        '[role="button"]:has-text("PDF"), [role="button"]:has-text("Download")',
        'a:not([href]):has-text("PDF"), a:not([href]):has-text("Download")',
        'a[href=""]:has-text("PDF"), a[href=""]:has-text("Download")',
        'a[href="#"]:has-text("PDF"), a[href="#"]:has-text("Download")',
        'a[href^="javascript:" i]:has-text("PDF"), a[href^="javascript:" i]:has-text("Download")',
        'button.pdf-download-btn, button.download-btn, button.pdf-icon',
    ]

    def __init__(
        self,
        is_running: Callable[[], bool],
        record_interaction: Optional[Callable] = None,
        behavior_simulator: Optional["AbstractBehaviorSimulator"] = None,
    ) -> None:
        """
        Args:
            is_running          : 状态检查函数（False 时各方法立即返回）。
            record_interaction  : 可选的审计埋点回调，签名：
                                  async (domain: str, data: dict) -> None
                                  用于将 Cookie 清除、按钮点击等交互行为记录到 audit_center。
            behavior_simulator  : 可选的行为模拟器（AbstractBehaviorSimulator 子类实例）。
                                  为 None 时内部回退到 PlaywrightBehaviorSimulator（默认策略）。
                                  传入 PyAutoGUIBezierSimulator 可将所有点击/悬停替换为
                                  OS 级真实鼠标事件，无需修改任何选择器或交互业务逻辑。

                                  扩展指引：
                                  新增行为策略时，在 engine/anti_bot/behavior/ 下新建文件，
                                  继承 AbstractBehaviorSimulator 并实现接口后注入此参数即可。
                                  ★ 本类（Interactor）代码完全不需要修改。
        """
        # 对外暴露为 self.is_running（trigger_download_buttons 存量代码直接调用此名）
        self.is_running = is_running
        # 内部别名，保持风格统一
        self._is_running = is_running
        self._record_interaction = record_interaction

        if behavior_simulator is not None:
            self._simulator = behavior_simulator
        else:
            # 懒加载默认策略，避免不必要的模块初始化开销
            from src.engine.anti_bot.behavior.playwright_simulator import (
                PlaywrightBehaviorSimulator,
            )
            self._simulator = PlaywrightBehaviorSimulator()

    # ------------------------------------------------------------------
    # 公开接口（职责链节点方法）
    # ------------------------------------------------------------------

    async def clear_cookie_banners(self, page: "Page") -> None:
        """
        战前清理：自动检测并点击全球常见 Cookie 弹窗的同意按钮。
        超时 1.5s 内未发现弹窗则静默跳过，不阻塞主流程。
        成功点击后等待 500ms 让消失动画完成，再触发 record_interaction 埋点。

        Args:
            page: 当前 Playwright Page 实例。
        """
        if not self._is_running():
            return

        for selector in self.COOKIE_SELECTORS:
            try:
                loc = page.locator(selector)
                # 1.5s 超时：如果弹窗不存在则快速放弃，不阻塞主流程
                await loc.wait_for(state="visible", timeout=1500)
                await self._simulator.click(loc, page=page)
                # 等待弹窗消失动画完成，避免后续操作被遮挡
                await asyncio.sleep(0.5)
                _log.debug(f"[Interactor] Cookie 弹窗已关闭: {selector}")
                if self._record_interaction:
                    domain = urlparse(page.url).netloc
                    await self._record_interaction(
                        domain,
                        {
                            "url": page.url,
                            "action": "cookie_banner_dismissed",
                            "description": selector,
                        },
                    )
                return  # 只需关闭第一个成功匹配的弹窗
            except Exception:
                # 弹窗不存在或超时：静默继续尝试下一个选择器
                continue

    async def extract_raw_links(self, context: object) -> List[str]:
        """
        从当前上下文中提取页面内所有 a[href] 原始链接。
        双模式兼容：
        - 若 context.soup 存在（BeautifulSoup 模式）：使用 soup.find_all('a', href=True)。
        - 否则（Playwright 模式）：使用 page.eval_on_selector_all 提取所有 href 属性。
        DOM 操作包裹在 error_interceptor 中，异常不静默吞咽。

        Args:
            context: Crawlee 请求上下文（PlaywrightCrawlingContext 或 BS4Context）。
        Returns:
            原始 href 字符串列表（可能含相对路径、空字符串，由 parser 后续清洗）。
        """
        # BeautifulSoup 模式（静态解析，无需 Playwright DOM 查询）
        soup = getattr(context, "soup", None)
        if soup is not None:
            return [a["href"] for a in soup.find_all("a", href=True)]

        # Playwright 模式（动态渲染，通过 JS evaluate 批量提取）
        page = getattr(context, "page", None)
        if page is None:
            return []

        current_url: str = getattr(
            getattr(context, "request", None), "url", "Unknown"
        )
        from src.modules.site.audit.error_registry import error_interceptor

        async with error_interceptor(page, current_url):
            hrefs: List[str] = await page.eval_on_selector_all(
                "a[href]",
                "elements => elements.map(el => el.getAttribute('href'))",
            )
            return [h for h in hrefs if h]

    async def trigger_download_buttons(self, context: object) -> List[object]:
        """
        侦察兵模式：扫描页面上疑似下载按钮，将其封装为 NEED_CLICK 标签的 Crawlee Request
        并推入请求队列，不直接执行点击（交由 ActionHandler 在独立沙箱处理）。

        防嵌套装甲：通过 JS evaluate 检查按钮是否被真实静态 a[href] 包裹，
        若是则跳过（链接提取阶段已处理）。

        Session 亲和绑定（P0 修复）：
            提取 context.session.id，写入空降任务的 session_id 字段。
            Crawlee 在调度 NEED_CLICK Request 时，将强制复用与本次扫描
            相同的 Session（同一 BrowserContext → 同一 Cookie jar）。
            确保空降敢死队与侦察兵处于同一身份上下文，
            防止 pool 模式下 Session 轮换导致的 Cookie 断层与下载鉴权失败。

        Args:
            context: Crawlee PlaywrightCrawlingContext（须含 .page 和 .add_requests()）。
        Returns:
            实际派发的 Request 对象列表（主要用于日志/测试，通常可忽略返回值）。
        """
        if not self.is_running() or not hasattr(context, 'page') or not context.page:
            return []

        current_url = context.request.url

        # ── Session 亲和绑定核心：在侦察阶段读取当前 Session ID ─────────────
        # context.session 来自 BasicCrawlingContext，类型 Session | None。
        # session_mode="persistent" 时只有一个 Session，此逻辑是 no-op。
        # session_mode="pool" 时此值决定空降任务是否能复用同一 BrowserContext。
        current_session: object = getattr(context, 'session', None)
        current_session_id: Optional[str] = (
            current_session.id if current_session is not None else None
        )

        button_locators = context.page.locator(', '.join(self.DOWNLOAD_BUTTON_SELECTORS))
        button_count = await button_locators.count()

        if button_count == 0:
            return []

        requests_to_add: List[Request] = []

        for i in range(button_count):
            if not self.is_running():
                break

            btn = button_locators.nth(i)

            if not (await btn.is_visible() and await btn.is_enabled()):
                continue

            # 防嵌套装甲：父级有真实静态链接则跳过，链接提取阶段已处理
            if await self._is_wrapped_by_real_link(btn):
                continue

            # unique_key：页面 URL + 按钮索引，保证同一按钮不被重复派发
            unique_key = f"{current_url}#btn{i}"

            requests_to_add.append(
                Request.from_url(
                    url=current_url,
                    label='NEED_CLICK',
                    unique_key=unique_key,
                    # ★ 亲和绑定：Crawlee 调度时复用本次扫描的同一 Session
                    session_id=current_session_id,
                    user_data={'target_index': i},
                )
            )

        if requests_to_add:
            await context.add_requests(requests_to_add)

        return requests_to_add

    # ------------------------------------------------------------------
    # 私有工具
    # ------------------------------------------------------------------

    async def _is_wrapped_by_real_link(self, btn: object) -> bool:
        """
        通过 DOM 树逆向溯源，判断按钮元素是否被真实静态 a[href] 包裹。
        如果被包裹则说明链接提取阶段已能处理，无需交互点击。

        Args:
            btn: Playwright Locator 对象（单个按钮定位器）。
        Returns:
            True 表示被真实链接包裹，应跳过；False 表示是真正的伪链接按钮。
        """
        try:
            return await btn.evaluate(
                """el => {
                    let node = el.parentElement;
                    while (node) {
                        if (node.tagName === 'A') {
                            const href = node.getAttribute('href');
                            if (href
                                && href !== ''
                                && !href.startsWith('#')
                                && !/^javascript:/i.test(href)) {
                                return true;
                            }
                        }
                        node = node.parentElement;
                    }
                    return false;
                }"""
            )
        except Exception:
            # evaluate 失败（如页面已导航）时保守地认为不被包裹，让后续逻辑决定
            return False
