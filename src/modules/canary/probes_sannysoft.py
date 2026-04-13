"""
bot.sannysoft.com 身份、视口与渲染栈探针。

DOM 结构可能随站点改版变化；抽取在 evaluate + 超时内完成。
判定为纯函数：Baseline + 微扰动策略下不做哈希/字体表严格比对，只做健康度与自洽性。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from src.config.settings import PrismSettings
from src.engine.anti_bot.fingerprint import FingerprintGenerator, FingerprintProfile
from src.utils.logger import get_logger

_log = get_logger(__name__)

DOM_PROBE_FAIL_DESC = "DOM 解析失败或超时"
_EVAL_TIMEOUT_SECS = 25.0

# identity / hardware 象限 item_id（与 dashboard 契约一致）
_IDENTITY_ITEM_IDS: FrozenSet[str] = frozenset({"identity_locale", "viewport_fit"})
_HARDWARE_ITEM_IDS: FrozenSet[str] = frozenset({"webgl_vendor", "canvas_audio"})

# 字体列表健康度（无期望字体表比对）
_FONTS_COUNT_PASS_MIN = 30
_FONTS_COUNT_PASS_MAX = 200
_FONTS_COUNT_WARN_MIN = 8

_SANNYSOFT_EXTRACT_JS = r"""
async () => {
  const out = {
    error: null,
    rows: {},
    navigator: {},
    screen: {},
    viewport: {},
    fonts: { count: 0, hooked: false, err: null },
    rendering: {
      canvas2dNative: false,
      webglNative: false,
      canvasDataOk: false,
      webglDataOk: false,
      webglVendor: "",
      webglRenderer: "",
      canvasDataLen: 0,
      audioNative: false,
      audioOk: false,
    },
    screenLogicOk: true,
  };

  const TEST_STR = "mmmmmmmmmmlli";
  const BASE_FONTS = ["monospace", "sans-serif", "serif"];
  const TEST_FAMILIES = [
    "Arial", "Verdana", "Times New Roman", "Courier New", "Georgia", "Tahoma",
    "Trebuchet MS", "Comic Sans MS", "Impact", "Palatino Linotype", "Lucida Console",
    "Calibri", "Cambria", "Consolas", "Segoe UI", "Helvetica", "Geneva", "Optima",
    "Gill Sans", "Franklin Gothic", "Arial Black", "Book Antiqua", "Century Gothic",
    "Copperplate", "Futura", "Garamond", "Rockwell", "Symbol", "Wingdings", "SimSun",
    "Microsoft YaHei", "SimHei", "KaiTi", "FangSong", "NSimSun", "PMingLiU", "MingLiU",
    "Apple Color Emoji", "Segoe UI Emoji", "Roboto", "Ubuntu", "DejaVu Sans",
    "Liberation Sans", "Droid Sans", "Open Sans", "Noto Sans"
  ];

  function nativeFn(fn) {
    if (typeof fn !== "function") return false;
    try {
      return Function.prototype.toString.call(fn).includes("[native code]");
    } catch (e) {
      return false;
    }
  }

  function offsetWidthHooked() {
    try {
      const p = HTMLElement.prototype;
      const d = Object.getOwnPropertyDescriptor(p, "offsetWidth");
      if (d && typeof d.get === "function" && !nativeFn(d.get)) return true;
    } catch (e) {
      return true;
    }
    return false;
  }

  try {
    out.navigator.ua = navigator.userAgent || "";
    out.navigator.language = navigator.language || "";
    out.navigator.languages = navigator.languages ? Array.from(navigator.languages) : [];
    try {
      out.navigator.timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (e) {
      out.navigator.timeZone = "";
    }

    out.screen.width = screen.width;
    out.screen.height = screen.height;
    out.viewport.innerWidth = window.innerWidth;
    out.viewport.innerHeight = window.innerHeight;
    out.screenLogicOk =
      screen.width >= window.innerWidth && screen.height >= window.innerHeight;

    function findRow(substr) {
      const tables = document.querySelectorAll("table");
      for (const t of tables) {
        for (const tr of t.querySelectorAll("tr")) {
          const tds = tr.querySelectorAll("td");
          if (tds.length < 2) continue;
          const name = (tds[0].innerText || "").trim();
          if (name.includes(substr)) {
            const last = tds[tds.length - 1];
            return {
              name: name,
              text: (last.innerText || "").trim(),
              className: last.className || "",
            };
          }
        }
      }
      return null;
    }

    out.rows.webDriverNew = findRow("WebDriver (New)");
    if (!out.rows.webDriverNew) out.rows.webDriverNew = findRow("WebDriver");
    out.rows.userAgentOld = findRow("User Agent (Old)");

    // ── 字体：可检测数量 + offsetWidth 类 Hook ─────────────────────
    out.fonts.hooked = offsetWidthHooked();
    try {
      if (document.fonts && document.fonts.ready) {
        await document.fonts.ready;
      }
    } catch (e) { /* ignore */ }

    try {
      const canvas = document.createElement("canvas");
      canvas.width = 300;
      canvas.height = 60;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        out.fonts.err = "no_canvas_context";
      } else {
        let detected = 0;
        ctx.textBaseline = "top";
        ctx.font = "72px sans-serif";
        const baseW = ctx.measureText(TEST_STR).width;
        for (const fam of TEST_FAMILIES) {
          ctx.font = "72px '" + fam.replace(/'/g, "") + "', sans-serif";
          const w = ctx.measureText(TEST_STR).width;
          if (Math.abs(w - baseW) > 0.01) detected++;
        }
        out.fonts.count = detected;
      }
    } catch (e) {
      out.fonts.err = String(e);
      out.fonts.count = 0;
    }

    // ── Canvas / WebGL：native 检测 + 数据有效性（非哈希比对）────
    try {
      const proto2d = CanvasRenderingContext2D.prototype;
      const c2 = document.createElement("canvas");
      c2.width = 16;
      c2.height = 16;
      const x = c2.getContext("2d");
      if (x) {
        out.rendering.canvas2dNative = nativeFn(proto2d.fillText);
        x.fillStyle = "#f60";
        x.fillRect(0, 0, 8, 8);
        x.fillStyle = "#069";
        x.fillText(TEST_STR, 2, 2);
        const dataUrl = c2.toDataURL("image/png");
        out.rendering.canvasDataLen = dataUrl ? dataUrl.length : 0;
        out.rendering.canvasDataOk = !!(dataUrl && dataUrl.length > 80);
      }
    } catch (e) {
      out.rendering.canvasDataOk = false;
    }

    try {
      const glProto = WebGLRenderingContext.prototype;
      const cg = document.createElement("canvas");
      cg.width = 1;
      cg.height = 1;
      const gl = cg.getContext("webgl") || cg.getContext("experimental-webgl");
      if (gl) {
        out.rendering.webglNative = nativeFn(glProto.getParameter);
        const vend = gl.getParameter(gl.VENDOR) || "";
        const rend = gl.getParameter(gl.RENDERER) || "";
        out.rendering.webglVendor = String(vend);
        out.rendering.webglRenderer = String(rend);
        out.rendering.webglDataOk = vend.length > 1 && rend.length > 1;
      }
    } catch (e) {
      out.rendering.webglDataOk = false;
    }

    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) {
        out.rendering.audioNative = nativeFn(AC.prototype.createOscillator);
        const ac = new AC();
        const osc = ac.createOscillator();
        out.rendering.audioOk = !!osc;
        ac.close();
      }
    } catch (e) {
      out.rendering.audioOk = false;
    }
  } catch (e) {
    out.error = String(e);
  }
  return out;
}
"""


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _webdriver_row_passes(row: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not row or not isinstance(row, dict):
        return None
    text = _norm_ws(str(row.get("text") or ""))
    cls = _norm_ws(str(row.get("className") or ""))
    if "passed" in cls and "fail" not in cls:
        return True
    if "missing" in text and "passed" in text:
        return True
    if "ok" in text and "fail" not in text:
        return True
    if "present" in text and "fail" in text:
        return False
    if "failed" in text or "fail" in text.split():
        return False
    return None


def _expected_profile(settings: PrismSettings) -> Optional[FingerprintProfile]:
    if not settings.stealth.use_fingerprint:
        return None
    return FingerprintGenerator().generate()


def _ua_strict_match(expected: Optional[FingerprintProfile], page_ua: str) -> bool:
    if expected is None:
        return False
    exp = (expected.user_agent or "").strip()
    if not exp:
        return False
    return exp == (page_ua or "").strip()


def _ua_present(page_ua: str, *, min_len: int = 20) -> bool:
    u = (page_ua or "").strip()
    return len(u) >= min_len


def _screen_compatible(
    expected: Optional[FingerprintProfile],
    sw: int,
    sh: int,
) -> Tuple[bool, str]:
    if not expected:
        if sw >= 320 and sh >= 240:
            return True, f"screen={sw}x{sh}（未比对：未启用自研指纹）"
        return False, f"screen={sw}x{sh} 异常"
    tol = max(8, int(expected.screen_width * 0.03))
    w_ok = abs(sw - int(expected.screen_width)) <= tol
    h_ok = abs(sh - int(expected.screen_height)) <= tol
    ok = w_ok and h_ok
    detail = (
        f"页面 screen={sw}x{sh}, 期望≈{expected.screen_width}x{expected.screen_height}"
    )
    return ok, detail


def _compute_wd_ok(rows: Dict[str, Any]) -> Tuple[bool, str]:
    wd_row = rows.get("webDriverNew")
    wd_verdict = _webdriver_row_passes(wd_row if isinstance(wd_row, dict) else None)
    wd_text = (wd_row or {}).get("text") if isinstance(wd_row, dict) else ""
    wd_part = f"WebDriver: {wd_text or '?'}" if wd_row else "WebDriver: 未找到表格行"

    if wd_verdict is None:
        return False, f"{wd_part} (无法解析状态→按失败处理)"
    return bool(wd_verdict), wd_part


def _synthesize_identity_locale(
    *,
    wd_ok: bool,
    ua_present: bool,
    ua_strict_match: bool,
    has_strict_expected: bool,
    wd_part: str,
    page_ua: str,
    page_lang: str,
    page_tz: str,
    expected: Optional[FingerprintProfile],
) -> Tuple[str, str]:
    if not wd_ok:
        desc = (
            f"WebDriver 暴露，指纹已污染。{wd_part}; "
            f"UA[:80]={page_ua[:80]!r}"
        )
        return "fail", desc[:900]

    if not has_strict_expected:
        if ua_present:
            desc = (
                "底层安全通过。未启用严格指纹校验，仅确认 UA 存在且有效。"
                f" {wd_part}; lang={page_lang!r}; UA[:80]={page_ua[:80]!r}"
            )
            return "warn", desc[:900]
        desc = (
            "底层安全通过但 UA 缺失或过短，无法确认有效。"
            f" {wd_part}; lang={page_lang!r}"
        )
        return "fail", desc[:900]

    if not (expected and (expected.user_agent or "").strip()):
        desc = (
            "已启用指纹注入但期望 UA 为空，无法做严格比对。"
            f" {wd_part}; 页内 UA[:80]={page_ua[:80]!r}"
        )
        return "fail", desc[:900]

    if ua_strict_match:
        desc = (
            f"WebDriver 已隐藏且 UA 与指纹配置完全一致。{wd_part}; "
            f"lang={page_lang!r}; tz={page_tz!r}（仅观测，不参与 pass 条件）; "
            f"UA[:80]={page_ua[:80]!r}"
        )
        return "pass", desc[:900]

    desc = (
        "WebDriver 已隐藏但 UA 与指纹配置不一致（严格匹配失败）。"
        f" {wd_part}; 页[:80]={page_ua[:80]!r}; "
        f"期望[:80]={(expected.user_agent or '')[:80]!r}"
    )
    return "fail", desc[:900]


def _compute_screen_logic_ok(bundle: Dict[str, Any]) -> Tuple[bool, str]:
    sl = bundle.get("screenLogicOk")
    if isinstance(sl, bool) and not sl:
        return False, "screen 小于 inner（逻辑互斥，疑似伪造或异常布局）"
    return True, "screen≥inner 自洽"


def _synthesize_fonts_row(fonts: Dict[str, Any]) -> Tuple[str, str]:
    """
    字体健康度：不做期望列表比对；Hook/空列表 fail；数量区间 pass/warn。
    """
    hooked = bool(fonts.get("hooked"))
    err = fonts.get("err")
    count = int(fonts.get("count") or 0)

    if hooked:
        return "fail", "字体探测：检测到 offsetWidth/getter 非 native 等 Hook 痕迹"
    if err and not count:
        return "fail", f"字体探测异常: {err}"
    if count <= 0:
        return "fail", "字体探测：可检测字体数为 0（异常或被拦截）"

    if _FONTS_COUNT_PASS_MIN <= count <= _FONTS_COUNT_PASS_MAX:
        return (
            "pass",
            f"字体健康度正常（可检测≈{count}，允许与同机型撞衫；未做列表严格比对）",
        )
    if count >= _FONTS_COUNT_WARN_MIN:
        return (
            "warn",
            f"字体数量={count} 超出常规区间[{_FONTS_COUNT_PASS_MIN},{_FONTS_COUNT_PASS_MAX}]，"
            f"建议人工复核环境/虚拟机",
        )
    return "fail", f"字体数量={count} 过低，疑似异常环境"


def _synthesize_webgl_row(rend: Dict[str, Any]) -> Tuple[str, str]:
    """
    WebGL：api_clean + 数据有效；非 native 但 vendor/renderer 正常 → warn（Rebrowser/Camoufox）。
    """
    native_ok = bool(rend.get("webglNative"))
    data_ok = bool(rend.get("webglDataOk"))
    vend = str(rend.get("webglVendor") or "")
    rends = str(rend.get("webglRenderer") or "")

    if not data_ok:
        if not native_ok:
            return "fail", "WebGL：API 非 native 且无有效 VENDOR/RENDERER"
        return "fail", "WebGL：API 看似 native 但未拿到有效渲染器字符串"

    if native_ok:
        return (
            "pass",
            "API 未受污染，WebGL 特征提取正常（扰动已在底层处理）；"
            f" vendor={vend[:60]!r}, renderer={rends[:80]!r}",
        )

    return (
        "warn",
        "WebGL getParameter 呈现非 native（常见于 CDP/内核修补），但 VENDOR/RENDERER 输出有效；"
        f" vendor={vend[:60]!r}, renderer={rends[:80]!r}",
    )


def _synthesize_canvas_audio_row(rend: Dict[str, Any]) -> Tuple[str, str]:
    """Canvas 2D + WebAudio：native + 数据有效；不做音频哈希比对。"""
    c_nat = bool(rend.get("canvas2dNative"))
    c_data = bool(rend.get("canvasDataOk"))
    c_len = int(rend.get("canvasDataLen") or 0)
    a_nat = bool(rend.get("audioNative"))
    a_ok = bool(rend.get("audioOk"))

    audio_note = (
        f"Audio: createOscillator native={a_nat}, 可创建={a_ok}"
    )

    if not c_data:
        if not c_nat:
            return "fail", f"Canvas2D：fillText 非 native 且 toDataURL 无效；{audio_note}"
        return "fail", f"Canvas2D：toDataURL 过短或无效；{audio_note}"

    if not a_ok:
        if c_nat and c_data:
            return (
                "warn",
                f"WebAudio 不可用或受限（常见于策略/无用户手势），Canvas 正常 len≈{c_len}；{audio_note}",
            )
        return (
            "fail",
            f"WebAudio 不可用且 Canvas 异常；{audio_note}",
        )

    if c_nat and a_nat:
        return (
            "pass",
            f"Canvas2D/WebAudio API 未受污染，导出正常（canvas len≈{c_len}）；"
            f" 未比对画布/音频哈希；{audio_note}",
        )

    if c_nat:
        return (
            "warn",
            f"Canvas2D native、toDataURL 正常；WebAudio 非 native 或受限；{audio_note}",
        )

    return (
        "warn",
        f"Canvas2D fillText 非 native（可能底层修补），toDataURL len≈{c_len}；{audio_note}",
    )


def _merge_viewport_fit_state(
    *,
    scr_ok: bool,
    vp_ok: bool,
    scr_detail: str,
    iw: int,
    ih: int,
    screen_logic_ok: bool,
    screen_logic_desc: str,
) -> Tuple[str, str]:
    """视口行：物理/期望屏幕 + inner + screen≥inner 逻辑互斥（不含字体）。"""
    parts = [scr_detail, f"inner={iw}x{ih}", screen_logic_desc]
    desc = "; ".join(parts)[:900]

    if not screen_logic_ok:
        return "fail", desc
    if not scr_ok or not vp_ok:
        return "fail", desc
    return "pass", desc


def _merge_canvas_audio_row(
    fonts_state: str,
    fonts_desc: str,
    canvas_state: str,
    canvas_desc: str,
) -> Tuple[str, str]:
    """硬件「画布与音频」行：合并字体健康度 + Canvas2D（不做哈希比对）。"""
    if fonts_state == "fail" or canvas_state == "fail":
        st = "fail"
    elif fonts_state == "warn" or canvas_state == "warn":
        st = "warn"
    else:
        st = "pass"
    desc = (
        f"Fonts[{fonts_state}]: {fonts_desc[:320]} | "
        f"Canvas[{canvas_state}]: {canvas_desc[:320]}"
    )
    return st, desc[:900]


def build_sannysoft_probe_updates(
    bundle: Dict[str, Any],
    expected: Optional[FingerprintProfile],
) -> List[Tuple[str, str, str]]:
    """
    纯函数：sannysoft 单页 bundle → identity 2 行 + hardware 2 行。
    """
    if bundle.get("error"):
        fd = DOM_PROBE_FAIL_DESC
        return [
            ("identity_locale", "fail", fd),
            ("viewport_fit", "fail", fd),
            ("webgl_vendor", "fail", fd),
            ("canvas_audio", "fail", fd),
        ]

    nav = bundle.get("navigator") or {}
    scr = bundle.get("screen") or {}
    vp = bundle.get("viewport") or {}
    row_map = bundle.get("rows") or {}
    if not isinstance(row_map, dict):
        row_map = {}
    fonts = bundle.get("fonts") or {}
    if not isinstance(fonts, dict):
        fonts = {}
    rend = bundle.get("rendering") or {}
    if not isinstance(rend, dict):
        rend = {}

    page_ua = str(nav.get("ua") or "")
    page_lang = str(nav.get("language") or "")
    page_tz = str(nav.get("timeZone") or "")
    sw = int(scr.get("width") or 0)
    sh = int(scr.get("height") or 0)
    iw = int(vp.get("innerWidth") or 0)
    ih = int(vp.get("innerHeight") or 0)

    wd_ok, wd_part = _compute_wd_ok(row_map)
    ua_pres = _ua_present(page_ua)
    has_strict_expected = expected is not None
    ua_strict = _ua_strict_match(expected, page_ua)

    id_state, id_desc = _synthesize_identity_locale(
        wd_ok=wd_ok,
        ua_present=ua_pres,
        ua_strict_match=ua_strict,
        has_strict_expected=has_strict_expected,
        wd_part=wd_part,
        page_ua=page_ua,
        page_lang=page_lang,
        page_tz=page_tz,
        expected=expected,
    )

    sl_ok, sl_desc = _compute_screen_logic_ok(bundle)
    scr_ok, scr_detail = _screen_compatible(expected, sw, sh)
    vp_ok = iw >= 200 and ih >= 200

    fonts_state, fonts_desc = _synthesize_fonts_row(fonts)
    vp_state, vp_desc = _merge_viewport_fit_state(
        scr_ok=scr_ok,
        vp_ok=vp_ok,
        scr_detail=scr_detail,
        iw=iw,
        ih=ih,
        screen_logic_ok=sl_ok,
        screen_logic_desc=sl_desc,
    )

    gl_state, gl_desc = _synthesize_webgl_row(rend)
    c2_state, c2_desc = _synthesize_canvas_audio_row(rend)
    ca_state, ca_desc = _merge_canvas_audio_row(
        fonts_state, fonts_desc, c2_state, c2_desc
    )

    return [
        ("identity_locale", id_state, id_desc[:900]),
        ("viewport_fit", vp_state, vp_desc[:900]),
        ("webgl_vendor", gl_state, gl_desc[:900]),
        ("canvas_audio", ca_state, ca_desc[:900]),
    ]


# 向后兼容旧名称
build_identity_updates = build_sannysoft_probe_updates


def apply_sannysoft_probe_updates(updates: List[Tuple[str, str, str]]) -> None:
    """按 item_id 分流到 identity / hardware 象限（需由 strategy 调用）。"""
    from src.modules.canary.dashboard import set_quadrant_group

    id_u = [u for u in updates if u[0] in _IDENTITY_ITEM_IDS]
    hw_u = [u for u in updates if u[0] in _HARDWARE_ITEM_IDS]
    if id_u:
        set_quadrant_group("identity", id_u)
    if hw_u:
        set_quadrant_group("hardware", hw_u)


async def _extract_page_bundle(page: Any) -> Dict[str, Any]:
    try:
        raw = await asyncio.wait_for(
            page.evaluate(_SANNYSOFT_EXTRACT_JS),
            timeout=_EVAL_TIMEOUT_SECS,
        )
        if not isinstance(raw, dict):
            return {"error": "invalid_eval_result"}
        return raw
    except asyncio.TimeoutError:
        return {"error": "timeout"}
    except Exception as exc:
        _log.debug("[sannysoft] evaluate 异常: {}", exc)
        return {"error": str(exc)}


async def run_sannysoft_identity_probe(
    page: Any,
    settings: PrismSettings,
) -> List[Tuple[str, str, str]]:
    """
    对当前页执行 sannysoft 探针，返回 identity + hardware 共 4 行更新。
    调用方应使用 apply_sannysoft_probe_updates(updates) 写入看板。
    """
    try:
        expected = _expected_profile(settings)
        bundle = await _extract_page_bundle(page)
        return build_sannysoft_probe_updates(bundle, expected)
    except Exception as exc:
        _log.warning("[sannysoft] run_sannysoft_identity_probe 兜底: {}", exc)
        fd = f"{DOM_PROBE_FAIL_DESC}: {str(exc)[:400]}"
        fd = fd[:500]
        return [
            ("identity_locale", "fail", fd),
            ("viewport_fit", "fail", fd),
            ("webgl_vendor", "fail", fd),
            ("canvas_audio", "fail", fd),
        ]
