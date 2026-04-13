"""
PrismPDF 爬虫控制台 —— 应用总入口

@Layer   : 根目录（项目入口）
@Role    : 应用启动、全局初始化、pywebview 事件循环启动
@Pattern : Composition Root（依赖组装根）—— 所有顶层组件在此唯一位置完成初始化与组装
@Description:
    main.py 是整个应用的唯一可执行入口（python main.py）。
    职责严格限定为：
    1. 最早初始化浏览器内核路径隔离（_init_browser_env），确保 Playwright/
       rebrowser-patches/Camoufox 的二进制和缓存文件强制存放在 ./browser_cores
       中，不污染系统 AppData、Cache 等目录。
    2. 初始化 loguru 日志系统（确保后续所有模块的日志都能被捕获）。
    3. 禁用 pywebview 调试模式下自动弹出开发者工具（保留 F12 手动打开功能）。
    4. 调用 src/app/bridge.py 的工厂函数 create_app() 组装所有后端组件并创建 Window。
    5. 启动 pywebview GUI 事件循环（webview.start(debug=True)）。
    6. 顶层异常捕获：任何未处理的启动异常打印错误信息，等待用户确认后退出。

    Composition Root 原则：
    - 不在此文件中实例化任何业务对象，全部委托给 create_app()。
    - 不在此文件中编写任何业务逻辑，只做"点火"操作。

    ★ 浏览器内核本地化隔离说明：
    _init_browser_env() 必须在所有其他 import 之前调用，原因：
      - 环境变量在进程内一旦设定不可撤销，且部分库在首次 import 时已缓存了默认路径。
      - PLAYWRIGHT_BROWSERS_PATH / REBROWSER_BROWSERS_PATH / CAMOUFOX_CACHE_DIR
        均需在 Playwright / Camoufox 模块首次 import 之前设置才能生效。
      - browser_cores 根目录默认 ./browser_cores（相对路径，在 pywebview 环境下
        工作目录为项目根目录）；Windows 下自动 resolve 为绝对路径。
"""

from __future__ import annotations

import os
import sys
import traceback


def main() -> None:
    """
    ��用主入口函数。

    执行流程：
    1. 调用 _init_browser_env() 强制浏览器内核本地化隔离（最早，任何 import 之前）。
    2. 调用 _init_logging() 初始化 loguru 日志（最优先，确保后续日志可捕获）。
    3. 打印启动横幅到 stdout（供命令行用户确认程序已启动）。
    4. 调用 src.app.bridge.create_app() 组装所有组件，获取 pywebview Window。
    5. 调用 webview.start(debug=True) 进入 GUI 主循环（阻塞直到窗口关闭）。
    6. 捕获顶层异常：打印错误摘要 + 完整 traceback，等待用户按键后退出。
    """
    try:
        _init_browser_env()

        import webview  # 与 __main__ 块保持同一懒加载入口；二次 import 直接命中缓存

        _init_logging()
        _print_banner()

        from src.app.bridge import create_app

        _window = create_app()  # noqa: F841  — 注册至 pywebview 内部表，start() 自动感知
        webview.start(debug=True)
    except Exception as exc:
        print(f"\n[FATAL] 启动失败：{exc}", file=sys.stderr)
        traceback.print_exc()
        input("\n按 Enter 键退出...")
        sys.exit(1)


def _init_browser_env() -> None:
    """
    最早时刻设置浏览器内核本地化路径（必须在所有业务 import 之前）。

    调用 src.config.settings._init_browser_env() 将 PLAYWRIGHT_BROWSERS_PATH、
    REBROWSER_BROWSERS_PATH、CAMOUFOX_CACHE_DIR 三个环境变量强制锁定到项目根目录
    的 ./browser_cores/{engine}/ 子目录，实现浏览器内核"绿色免安装"。

    关键约束：
        - 环境变量设置必须在 playwright / camoufox / rebrowser_patches 模块
          首次 import 之前执行；main.py 的调用顺序保证了这一约束。
        - 同一进程只执行一次（_init_browser_env 内部有 _browser_env_initialized 保护）。

    路径说明：
        - 默认 ./browser_cores（相对路径，基于 pywebview 当前工作目录）。
        - Windows 下 os.path.join 会使用 Path.resolve() 自动转为绝对路径，
          保证在跨盘符或不同用户目录下行为一致。
    """
    from src.config.settings import _init_browser_env

    _init_browser_env()


def _init_logging() -> None:
    """
    早期日志初始化（在任何业务模块 import 之前调用）。
    从 src.config.settings.get_app_config() 读取 log_dir 和 log_level，
    调用 src.utils.logger.setup_logger() 完成 loguru sink 配置。
    同时拦截标准库 logging（crawlee / playwright 等第三方依赖）转发至 loguru。
    """
    from src.config.settings import get_app_config
    from src.utils.logger import setup_logger

    cfg = get_app_config()
    # intercept_stdlib=True（默认值）确保 crawlee / playwright 日志一并收拢
    setup_logger(log_dir=cfg.log_dir, log_level=cfg.log_level)


def _print_banner() -> None:
    """
    向 stdout 打印 PrismPDF 启动横幅。
    纯 print 调用，不依赖日志系统（确保在日志初始化失败时也能显示）。
    """
    from src import __version__

    bar = "=" * 56
    print(bar)
    print(f"  PrismPDF  v{__version__}  —  PrismPDF 爬虫控制台")
    print("  正在初始化，请稍候...")
    print(bar)


if __name__ == "__main__":
    import webview  # 懒加载：仅在直接运行时导入

    # 禁止调试模式自动弹出 DevTools（保留 F12 手动打开功能）
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False

    main()
