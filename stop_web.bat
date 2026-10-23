@echo off
REM ============================================================
REM  MA120 Web Console - stop the background server (Windows)
REM
REM  Finds whatever is LISTENING on port 8765 and kills it.
REM
REM  IMPORTANT: Keep this file ASCII-only with CRLF endings.
REM  cmd.exe decodes .bat with the system ANSI codepage (GBK on
REM  zh-CN), so non-ASCII comments become bogus commands.
REM  Do NOT add Chinese here.
REM ============================================================

setlocal enabledelayedexpansion

set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"LISTENING.*:8765"') do (
    echo   Stopping PID %%p ...
    taskkill /PID %%p /F >nul 2>&1
    if !errorlevel! equ 0 (
        echo   [OK] PID %%p stopped.
    ) else (
        echo   [FAIL] Could not stop PID %%p
    )
    set FOUND=1
)

if "!FOUND!"=="0" (
    echo   Nothing is listening on port 8765.
    echo   The server is probably not running.
)

ping -n 4 127.0.0.1 >nul
endlocal
