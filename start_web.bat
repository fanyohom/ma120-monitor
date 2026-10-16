@echo off
REM MA120 Web 工作台一键启动
REM 用法：双击本文件，或在项目根目录执行 start_web.bat
REM 停止：在本窗口按 Ctrl+C

cd /d "%~dp0"

REM Windows 上 uv/python 输出中文需要 UTF-8，否则中文股票名会乱码
set PYTHONUTF8=1
REM 把 uv 缓存放到项目内，避免污染用户全局目录
set UV_CACHE_DIR=%~dp0.uv-cache

echo.
echo   MA120 Web 工作台启动中...
echo   地址：http://127.0.0.1:8765
echo   停止：在此窗口按 Ctrl+C
echo.

REM 先探测端口是否已被占用，避免静默失败（起第二个实例会报 address in use）
netstat -ano | findstr /R /C:"LISTENING.*:8765" >nul
if %errorlevel% equ 0 (
    echo   [提示] 端口 8765 已在监听，服务可能已经在运行了。
    echo          若页面打不开，先检查是否有旧进程占用端口。
    echo.
)

uv run python web_app.py --host 127.0.0.1 --port 8765

echo.
echo   服务已停止。
pause