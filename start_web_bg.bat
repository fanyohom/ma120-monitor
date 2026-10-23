@echo off
REM ============================================================
REM  MA120 Web Console - background launcher (Windows)
REM
REM  Starts the server with NO console window and returns at
REM  once. Output goes to web_app.log.
REM
REM  Why pythonw: it has no console, so the service keeps running
REM  after you close this window. python.exe would die with the
REM  window. Because pythonw has no stdout, we pass --log-file so
REM  the app can still write logs.
REM
REM  Why not PowerShell Start-Process: on machines that define
REM  both HTTPS_PROXY and https_proxy, Start-Process throws a
REM  "duplicate key" error before it even launches anything.
REM
REM  IMPORTANT: Keep this file ASCII-only with CRLF endings.
REM  cmd.exe decodes .bat with the system ANSI codepage (GBK on
REM  zh-CN), so non-ASCII comments become bogus commands
REM  (e.g. "'et' is not recognized"). Do NOT add Chinese here.
REM ============================================================

cd /d "%~dp0"

REM Chinese stock names need UTF-8 or they show as mojibake
set PYTHONUTF8=1
REM Keep uv cache inside the project instead of the user profile
set UV_CACHE_DIR=%~dp0.uv-cache

REM ---- already running? ----
netstat -ano | findstr /R /C:"LISTENING.*:8765" >nul
if %errorlevel% equ 0 (
    echo   [SKIP] Port 8765 is already listening.
    echo          Open http://127.0.0.1:8765
    echo          To restart, close the old process first:
    echo            netstat -ano ^| findstr :8765
    echo            taskkill /PID ^<pid^> /F
    ping -n 3 127.0.0.1 >nul
    exit /b 0
)

REM ---- venv must exist ----
if not exist ".venv\Scripts\pythonw.exe" (
    echo   [ERROR] .venv not found. Run "uv sync" first.
    ping -n 4 127.0.0.1 >nul
    exit /b 1
)

echo   Starting MA120 Web Console in background...
start "" ".venv\Scripts\pythonw.exe" web_app.py --host 127.0.0.1 --port 8765 --log-file web_app.log

REM ---- wait for the port to come up (max ~20s) ----
set /a tries=0
:wait
ping -n 3 127.0.0.1 >nul
netstat -ano | findstr /R /C:"LISTENING.*:8765" >nul
if %errorlevel% equ 0 goto ready
set /a tries+=1
if %tries% lss 10 goto wait

echo   [FAILED] Server did not start. Check web_app.log
ping -n 4 127.0.0.1 >nul
exit /b 1

:ready
echo.
echo   OK - running in background: http://127.0.0.1:8765
echo   Log: web_app.log
echo.
echo   To stop it:
echo     netstat -ano ^| findstr :8765      ^(get the PID^)
echo     taskkill /PID ^<pid^> /F
echo.
ping -n 4 127.0.0.1 >nul
