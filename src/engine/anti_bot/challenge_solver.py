"""
@Layer   : Engine 层（第三层 · 反爬工具箱）
@Role    : 应对 Cloudflare 5s 盾 / Turnstile / hCaptcha 等反爬挑战
@Pattern : Chain of Responsibility（ChallengeDetector 逐类检测） +
           State Pattern（联动 CrawlerStateManager 驱动状态转移）
@Description:
    将反爬挑战检测与应对逻辑从业务代码中彻底解耦，集中封装在此模块。
    ChallengeDetector 通过检查页面标题、URL 特征、DOM 元素识别挑战类型，
    按"职责链"逐类尝试，返回 ChallengeType 枚举值；
    ChallengeSolver 接收检测结果，分发到对应的具体解决策略（私有方法），
    解决成功后通知 StateManager 回归 RUNNING 状态，
    失败则触发 BANNED 转移（驱动 ProxyRotator 切换代理）。
    当前版本仅支持等待型自动通过策略；需人工介入的 CAPTCHA 返回 False 降级处理。
"""

from __future__ import annotations

import asyncio
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

from src.utils.logger import get_logger

_log = get_logger(__name__)

if TYPE_CHECKING:
    from playwright.async_api import Page
    from src.engine.state_manager import CrawlerStateManager


# ---------------------------------------------------------------------------
# 挑战类型枚举
# ---------------------------------------------------------------------------

class ChallengeType(Enum):
    """
    已知的反爬挑战类型枚举。
    Pattern: Chain of Responsibility —— ChallengeDetector 按此顺序逐类检测。

    Attributes:
        CLOUDFLARE_5S        : Cloudflare JS 挑战（5 秒盾），等待后自动通过。
        CLOUDFLARE_TURNSTILE : Cloudflare Turnstile 交互式验证码。
        HCAPTCHA             : hCaptcha 验证码（需第三方服务或人工介入）。
        RECAPTCHA_V2         : Google reCAPTCHA v2（需第三方服务或人工介入）。
        SIMPLE_REDIRECT      : 简单 JS 跳转反爬（延迟后自动跳转）。
        UNKNOWN              : 检测到异常但无法归类的挑战。
        NONE                 : 无挑战，页面正常。
    """

    CLOUDFLARE_5S = auto()
    CLOUDFLARE_TURNSTILE = auto()
    HCAPTCHA = auto()
    RECAPTCHA_V2 = auto()
    SIMPLE_REDIRECT = auto()
    UNKNOWN = auto()
    NONE = auto()


# ---------------------------------------------------------------------------
# 挑战检测器（职责链）
# ---------------------------------------------------------------------------

class ChallengeDetector:
    """
    反爬挑战类型探测器。

    通过检查页面标题、URL 路径、特定 DOM 选择器等特征，
    按"职责链"顺序逐类检测当前页面所遭遇的挑战类型。
    各检测方法相互独立，新增挑战类型只需添加对应私有检测方法。

    Pattern: Chain of Responsibility —— detect() 按优先级依次调用各 _check_* 方法。
    """

    async def detect(self, page: "Page") -> ChallengeType:
        """
        检测当前页面的挑战类型（职责链入口）。

        按以下优先级顺序检测：
        Cloudflare 5s → Turnstile → hCaptcha → reCAPTCHA → 简单跳转 → 未知

        Args:
            page: 当前 Playwright Page 实例（已导航到目标 URL）。
        Returns:
            识别到的 ChallengeType 枚举值；无挑战时返回 ChallengeType.NONE。
        """
        try:
            if await self._check_cloudflare_5s(page):
                return ChallengeType.CLOUDFLARE_5S
            if await self._check_cloudflare_turnstile(page):
                return ChallengeType.CLOUDFLARE_TURNSTILE
            if await self._check_hcaptcha(page):
                return ChallengeType.HCAPTCHA
            if await self._check_recaptcha_v2(page):
                return ChallengeType.RECAPTCHA_V2
            if await self._check_simple_redirect(page):
                return ChallengeType.SIMPLE_REDIRECT
        except Exception:
            return ChallengeType.UNKNOWN
        return ChallengeType.NONE

    async def _check_cloudflare_5s(self, page: "Page") -> bool:
        """
        检测 Cloudflare 5s JS 挑战特征。
        特征：页面标题包含 'Just a moment' 或特定 meta 标签 / cf_chl_* 元素存在。

        Args:
            page: 目标 Page。
        Returns:
            True 表示检测到此类挑战。
        """
        try:
            title = (await page.title()).lower()
            if "just a moment" in title:
                return True
            # Cloudflare challenge DOM 标志性元素
            if await page.locator("#challenge-form, #cf-challenge-running, #cf-spinner").count() > 0:
                return True
            # cf_chl_opt 变量存在于页面 JS 环境中
            has_cf_opt = await page.evaluate(
                "() => typeof window.cf_chl_opt !== 'undefined' || typeof window._cf_chl_opt !== 'undefined'"
            )
            if has_cf_opt:
                return True
        except Exception:
            pass
        return False

    async def _check_cloudflare_turnstile(self, page: "Page") -> bool:
        """
        检测 Cloudflare Turnstile 验证码特征。
        特征：页面中存在 cf-turnstile 容器 div 或相关 iframe。

        Args:
            page: 目标 Page。
        Returns:
            True 表示检测到此类挑战。
        """
        try:
            if await page.locator(".cf-turnstile, [data-turnstile-sitekey]").count() > 0:
                return True
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    return True
        except Exception:
            pass
        return False

    async def _check_hcaptcha(self, page: "Page") -> bool:
        """
        检测 hCaptcha iframe 挂载特征。
        特征：页面中存在 h-captcha 容器或来自 hcaptcha.com 的 iframe。

        Args:
            page: 目标 Page。
        Returns:
            True 表示检测到此类挑战。
        """
        try:
            if await page.locator(".h-captcha, [data-hcaptcha-sitekey]").count() > 0:
                return True
            for frame in page.frames:
                if "hcaptcha.com" in frame.url:
                    return True
        except Exception:
            pass
        return False

    async def _check_recaptcha_v2(self, page: "Page") -> bool:
        """
        检测 Google reCAPTCHA v2 特征。
        特征：页面中存在来自 google.com/recaptcha 的 iframe 或 g-recaptcha 容器。

        Args:
            page: 目标 Page。
        Returns:
            True 表示检测到此类挑战。
        """
        try:
            if await page.locator(".g-recaptcha, [data-sitekey]").count() > 0:
                return True
            for frame in page.frames:
                if "google.com/recaptcha" in frame.url:
                    return True
        except Exception:
            pass
        return False

    async def _check_simple_redirect(self, page: "Page") -> bool:
        """
        检测简单 JS 跳转反爬特征。
        特征：页面内容极短（< 500 字符）且包含 location.href 或 meta refresh 标签。

        Args:
            page: 目标 Page。
        Returns:
            True 表示检测到此类挑战。
        """
        try:
            content = await page.content()
            if len(content) < 500:
                lower = content.lower()
                if "location.href" in lower or 'http-equiv="refresh"' in lower or "meta http-equiv" in lower:
                    return True
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# 挑战应对器（总指挥）
# ---------------------------------------------------------------------------

class ChallengeSolver:
    """
    反爬挑战应对器（总指挥）。

    接收 ChallengeDetector 识别的 ChallengeType，
    分发到对应的私有解决策略方法；
    解决成功后通知 StateManager 转回 RUNNING 状态；
    解决失败则通知 StateManager 转移到 BANNED，触发代理轮换重试。

    Pattern: State Pattern —— 解决结果直接驱动 CrawlerStateManager 状态转移。
    """

    def __init__(
        self,
        state_manager: Optional["CrawlerStateManager"] = None,
        challenge_timeout_secs: float = 30.0,
    ) -> None:
        """
        Args:
            state_manager          : 可选的状态机实例；提供时自动驱动状态转移。
            challenge_timeout_secs : 单次挑战等待的最长秒数，超时判定为失败。
        """
        self._state_manager = state_manager
        self._timeout       = challenge_timeout_secs
        self._detector      = ChallengeDetector()

    async def solve(self, page: "Page", challenge_type: ChallengeType) -> bool:
        """
        尝试解决指定类型的挑战（统一入口）。

        流程：
        1. 通知 StateManager → CHALLENGE（若已提供）。
        2. 根据 challenge_type 分发到对应的 _solve_* 方法。
        3. 成功：通知 StateManager → RUNNING，返回 True。
        4. 失败：通知 StateManager → BANNED，返回 False。

        Args:
            page          : 当前 Playwright Page 实例。
            challenge_type: 由 ChallengeDetector 识别的挑战类型。
        Returns:
            True 表示挑战已解决，可继续抓取；False 表示失败，需外部介入（代理切换等）。
        """
        from src.engine.state_manager import CrawlerState, StateTransitionError

        # 步骤 1：通知状态机进入 CHALLENGE 状态
        if self._state_manager:
            try:
                await self._state_manager.transition_to(
                    CrawlerState.CHALLENGE,
                    reason=f"检测到挑战类型：{challenge_type.name}",
                )
            except StateTransitionError as exc:
                _log.warning(
                    "[ChallengeSolver] 无法进入 CHALLENGE（当前 FSM 不允许）: {}",
                    exc,
                )

        # 步骤 2：按挑战类型分发解决策略
        _dispatch = {
            ChallengeType.CLOUDFLARE_5S:        self._solve_cloudflare_5s,
            ChallengeType.CLOUDFLARE_TURNSTILE:  self._solve_cloudflare_turnstile,
            ChallengeType.SIMPLE_REDIRECT:       self._solve_simple_redirect,
            ChallengeType.UNKNOWN:               self._handle_unknown_challenge,
        }
        solver = _dispatch.get(challenge_type)
        if solver:
            success = await solver(page)
        else:
            # HCAPTCHA / RECAPTCHA_V2 / NONE 等无法自动处理
            success = False

        # 步骤 3/4：通知状态机结果
        if self._state_manager:
            try:
                if success:
                    await self._state_manager.transition_to(
                        CrawlerState.RUNNING,
                        reason=f"挑战已解决：{challenge_type.name}",
                    )
                else:
                    await self._state_manager.transition_to(
                        CrawlerState.BANNED,
                        reason=f"挑战失败，触发代理轮换：{challenge_type.name}",
                    )
            except StateTransitionError as exc:
                _log.warning(
                    "[ChallengeSolver] 挑战结束后 FSM 转移失败: {}",
                    exc,
                )

        return success

    async def _solve_cloudflare_5s(self, page: "Page") -> bool:
        """
        等待并通过 Cloudflare 5s JS 挑战。
        策略：轮询 page.title()，等待标题从 'Just a moment' 变为正常内容；
        超出 challenge_timeout_secs 则判定失败。

        Args:
            page: 目标 Playwright Page。
        Returns:
            True 表示挑战页面已消失，False 表示等待超时。
        """
        deadline = asyncio.get_event_loop().time() + self._timeout
        poll_interval = 1.0  # 秒

        while asyncio.get_event_loop().time() < deadline:
            try:
                title = (await page.title()).lower()
                if "just a moment" not in title:
                    # 挑战页面消失，再额外等待导航稳定
                    await asyncio.sleep(0.5)
                    return True
            except Exception:
                pass
            await asyncio.sleep(poll_interval)

        return False

    async def _solve_cloudflare_turnstile(self, page: "Page") -> bool:
        """
        尝试通过 Cloudflare Turnstile 验证码。
        当前策略：等待 Turnstile widget 自动完成（部分 non-interactive 配置可自动通过）；
        需要用户交互的版本直接返回 False（降级处理）。

        Args:
            page: 目标 Playwright Page。
        Returns:
            True 表示自动通过；False 表示需要人工介入。
        """
        # 等待 Turnstile 的 cf-turnstile-response 隐藏字段被自动填充（non-interactive 流程）
        wait_secs = min(self._timeout, 15.0)
        deadline  = asyncio.get_event_loop().time() + wait_secs

        while asyncio.get_event_loop().time() < deadline:
            try:
                # Turnstile 完成后 cf-turnstile-response 字段将被写入非空值
                val = await page.evaluate(
                    "() => {"
                    "  const el = document.querySelector('[name=\"cf-turnstile-response\"]');"
                    "  return el ? el.value : '';"
                    "}"
                )
                if val:
                    return True
                # 如果 Turnstile 容器已消失，也认为通过
                if await page.locator(".cf-turnstile").count() == 0:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)

        # 超时：需要人工介入，降级处理
        return False

    async def _solve_simple_redirect(self, page: "Page") -> bool:
        """
        处理简单 JS 跳转反爬。
        策略：等待 page.wait_for_load_state('networkidle')，
        验证最终 URL 与原始 URL 不同（跳转完成）。

        Args:
            page: 目标 Playwright Page。
        Returns:
            True 表示跳转完成且页面可用；False 表示超时或跳转目标异常。
        """
        original_url = page.url
        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=self._timeout * 1000,  # Playwright 使用毫秒
            )
            final_url = page.url
            # 跳转完成且目标 URL 正常（不是 about:blank 或错误页）
            if final_url != original_url and not final_url.startswith("about:"):
                return True
        except Exception:
            pass
        return False

    async def _handle_unknown_challenge(self, page: "Page") -> bool:
        """
        未知挑战兜底策略。
        等待固定时间（challenge_timeout_secs / 2）后重新触发 ChallengeDetector 检测；
        若二次检测返回 NONE 则认为自动通过，否则返回 False。

        Args:
            page: 目标 Playwright Page。
        Returns:
            True 表示挑战自动消失；False 表示持续存在。
        """
        wait_secs = self._timeout / 2.0
        await asyncio.sleep(wait_secs)

        try:
            second_check = await self._detector.detect(page)
            return second_check == ChallengeType.NONE
        except Exception:
            return False
