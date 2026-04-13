@echo off
:: 切换代码页为 UTF-8 (65001)，解决中文乱码问题
chcp 65001 >nul

:: 切换到当前脚本所在的目录
cd /d "%~dp0"

echo ==========================================
echo       正在启动 PrismPDF 爬虫控制台...
echo ==========================================

:: 启动主程序
python main.py

if %errorlevel% neq 0 (
    echo.
    echo [!] 程序发生错误，请检查上方报错信息。
    pause
)