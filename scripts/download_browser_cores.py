# -*- coding: utf-8 -*-
"""
将三种浏览器内核下载到项目 ./browser_cores 下（与 settings._init_browser_env 一致）。

1. chromium   → browser_cores/playwright   （Playwright 官方 Chromium）
2. rebrowser  → browser_cores/rebrowser    （与标准 Chromium 相同二进制，单独目录供隔离；
               rebrowser 后端 Python 补丁不额外下载浏览器）
3. camoufox   → browser_cores/camoufox     （Camoufox 补丁 Firefox）

用法（在项目根目录）:
    python scripts/download_browser_cores.py

依赖:
    pip install playwright camoufox[geoip]
    # rebrowser 仅需 pip install rebrowser-playwright（或 rebrowser-patches），无独立浏览器包

可选参数:
    --skip-rebrowser-copy  不在 rebrowser 目录再装一份 Chromium（rebrowser 将回退共用 playwright 目录）
    --skip-camoufox        不下载 Camoufox（仅 Playwright / rebrowser 目录 Chromium）
    --camoufox-retries N   Camoufox 访问 GitHub 失败时的重试次数（默认 3）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    merged = {**os.environ, **(env or {})}
    print(f"\n$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=str(ROOT), env=merged)


def _print_camoufox_ssl_help(camoufox_dir: str) -> None:
    """Camoufox 需请求 api.github.com；TLS 失败时给出可操作的排查说明。"""
    local = os.environ.get("LOCALAPPDATA", "")
    legacy = os.path.join(local, "camoufox", "camoufox", "Cache") if local else ""
    legacy_hint = legacy or r"%LOCALAPPDATA%\camoufox\camoufox\Cache"
    print(
        "\n[Camoufox] 访问 GitHub API 失败（可能是限流 403、SSL 中断或代理问题）。可依次尝试：\n"
        "  0) 若出现 rate limit / 403：设置环境变量 GITHUB_TOKEN（只读 fine-grained 或 classic token），"
        "或等待约 1 小时后再试；\n"
        "  1) 暂时关闭 VPN / 更换节点，或退出会拦截 HTTPS 的安全软件后再运行本脚本；\n"
        "  2) 若在公司网络，配置系统或环境变量 HTTPS_PROXY / HTTP_PROXY；\n"
        "  3) 企业 MITM 证书：将根证书导出为 PEM，设置环境变量 "
        "REQUESTS_CA_BUNDLE=路径\\corp.pem 后再试；\n"
        "  4) 若曾在本机成功安装过，可把旧目录整夹复制到项目下：\n"
        f"     源（示例）: {legacy_hint}\n"
        f"     目标: {camoufox_dir}\n"
        "  5) 仅需要 Chromium 时，可加参数: --skip-camoufox\n",
        file=sys.stderr,
        flush=True,
    )


def _github_token_patched_requests_get():
    """
    Camoufox 内部 requests.get 不带 Token，易触发 api.github.com 匿名限流（403）。
    若设置了 GITHUB_TOKEN 或 GH_TOKEN，则为发往 github.com 的请求附加 Authorization。
    返回 (restore_fn,)；无需补丁时 restore_fn 为空操作。
    """
    import requests

    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        return lambda: None

    orig = requests.get

    def patched_get(url, *args, **kwargs):
        kw = dict(kwargs)
        url_s = url if isinstance(url, str) else getattr(url, "url", str(url))
        headers = dict(kw.get("headers") or {})
        if "github.com" in url_s and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {token}"
            kw["headers"] = headers
        return orig(url, *args, **kw)

    requests.get = patched_get
    print("已检测到 GITHUB_TOKEN/GH_TOKEN，将用于访问 GitHub API。", flush=True)

    def restore() -> None:
        requests.get = orig

    return restore


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="下载 browser_cores 下的三种浏览器内核")
    parser.add_argument(
        "--skip-rebrowser-copy",
        action="store_true",
        help="跳过在 browser_cores/rebrowser 再安装一份 Chromium",
    )
    parser.add_argument(
        "--skip-camoufox",
        action="store_true",
        help="不下载 Camoufox（避免 GitHub 网络问题时整脚本失败）",
    )
    parser.add_argument(
        "--camoufox-retries",
        type=int,
        default=3,
        metavar="N",
        help="Camoufox 请求 GitHub 失败时的重试次数（默认 3）",
    )
    args = parser.parse_args()

    from src.config.settings import BrowserCoresConfig, _init_browser_env

    cores_root = str((ROOT / "browser_cores").resolve())
    _init_browser_env(cores_root, force=True)
    cfg = BrowserCoresConfig(cores_root)

    print("目标目录:", flush=True)
    print(f"  Playwright: {cfg.playwright_path}", flush=True)
    print(f"  Rebrowser:  {cfg.rebrowser_path}", flush=True)
    print(f"  Camoufox:   {cfg.camoufox_path}", flush=True)

    # ① Playwright Chromium → playwright/
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("错误: 未安装 playwright。请执行: pip install playwright", file=sys.stderr)
        sys.exit(1)

    _run([sys.executable, "-m", "playwright", "install", "chromium"])

    # ② 同一份 Chromium 再装到 rebrowser/（满足 REBROWSER_BROWSERS_PATH 下自动发现）
    if not args.skip_rebrowser_copy:
        _run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            env={"PLAYWRIGHT_BROWSERS_PATH": cfg.rebrowser_path},
        )
        _init_browser_env(cores_root, force=True)

    # ③ Camoufox Firefox → camoufox/
    # 勿用子进程 `python -m camoufox fetch`：子进程不会执行本项目的 _init_browser_env，
    # camoufox 会始终解压到 %LOCALAPPDATA%\\camoufox\\...。此处在同进程内调用 fetch，
    # 且 _init_browser_env 已把 camoufox.pkgman.INSTALL_DIR 指到 browser_cores/camoufox。
    camoufox_network_failed = False
    if args.skip_camoufox:
        print("\n已跳过 Camoufox（--skip-camoufox）", flush=True)
    else:
        try:
            import camoufox  # noqa: F401
        except ImportError:
            print(
                "跳过 Camoufox: 未安装 camoufox。需要时请执行: pip install camoufox[geoip]",
                file=sys.stderr,
            )
        else:
            import requests
            from camoufox.addons import DefaultAddons, maybe_download_addons
            from camoufox.__main__ import CamoufoxUpdate
            from camoufox.locale import ALLOW_GEOIP, download_mmdb

            print("\n$ camoufox fetch（进程内，目标见上方 Camoufox 路径）", flush=True)
            _restore_requests = _github_token_patched_requests_get()
            try:
                retries = max(1, args.camoufox_retries)
                last_exc: BaseException | None = None
                for attempt in range(1, retries + 1):
                    try:
                        CamoufoxUpdate().update()
                        if ALLOW_GEOIP:
                            download_mmdb()
                        maybe_download_addons(list(DefaultAddons))
                        last_exc = None
                        break
                    except requests.exceptions.RequestException as exc:
                        last_exc = exc
                        if attempt < retries:
                            wait = min(30, 5 * attempt)
                            print(
                                f"\nCamoufox 请求失败（{attempt}/{retries}）: {exc}\n"
                                f"{wait} 秒后重试…",
                                flush=True,
                            )
                            time.sleep(wait)
                if last_exc is not None:
                    camoufox_network_failed = True
                    _print_camoufox_ssl_help(cfg.camoufox_path)
                    print(f"[Camoufox] 最终错误: {last_exc}", file=sys.stderr, flush=True)
            finally:
                _restore_requests()

    print("\n完成。若使用 rebrowser 后端，建议: pip install rebrowser-playwright")
    if camoufox_network_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
