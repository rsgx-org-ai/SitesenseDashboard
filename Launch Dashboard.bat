@echo off
setlocal

rem Launches the Sitesense Dashboard and makes sure the local refresh helper is
rem running first, since the "Refresh data" button silently fails to reach it
rem if it isn't. The Startup-folder shortcut normally starts the helper at
rem login already; this check exists for the times it doesn't - after a
rem sleep/resume, if it crashed, or the very first run.

cd /d "%~dp0"

echo Checking refresh helper on port 8799...
powershell -NoProfile -Command ^
  "try { Invoke-RestMethod -Uri 'http://127.0.0.1:8799/status' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"

if %ERRORLEVEL% NEQ 0 (
    echo   Not running - starting it now.
    start "" /min pythonw.exe "%~dp0refresh_server.py"
    timeout /t 2 /nobreak >nul
) else (
    echo   Already running.
)

echo Opening Sitesense_Dashboard.html...
start "" "%~dp0Sitesense_Dashboard.html"

endlocal
