@echo off
REM ============================================================
REM  MA120 Web Console - one-click launcher (Windows)
REM
REM  IMPORTANT: Keep this file ASCII-only (UTF-8 without BOM)
REM  with CRLF line endings.
REM  cmd.exe decodes .bat using the system ANSI codepage (GBK on
REM  zh-CN), so Chinese comments get mangled into bogus commands
REM  (e.g. "'et' is not recognized"). English comments are safe on
REM  any locale. Do NOT add non-ASCII characters to this file.
REM ============================================================

cd /d "%~dp0"

REM Chinese stock names need UTF-8 or they show as mojibake
set PYTHONUTF8=1
REM Keep uv cache inside the project instead of the user profile
set UV_CACHE_DIR=%~dp0.uv-cache

echo.
echo   MA120 Web Console starting...
echo   URL: http://127.0.0.1:8765
echo   Stop: press Ctrl+C in this window
echo.

REM Detect port 8765 already in use to avoid a silent failure.
netstat -ano | findstr /R /C:"LISTENING.*:8765" >nul
if %errorlevel% equ 0 (
    echo   [NOTE] Port 8765 is already listening.
    echo          If the page will not open, an old instance may
    echo          still be holding the port. Close it first.
    echo.
)

uv run python web_app.py --host 127.0.0.1 --port 8765

echo.
echo   Server stopped.
pause