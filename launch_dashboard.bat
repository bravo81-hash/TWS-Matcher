@echo off
rem ============================================================
rem  TWS Matcher  -  one-click launcher
rem  Starts the reconciliation dashboard and opens it in your
rem  browser. Close this window to stop the service.
rem ============================================================
title TWS Matcher
cd /d "%~dp0"

rem open the dashboard in the default browser (it auto-refreshes,
rem so it's fine if it loads a moment before the server is ready)
start "" "http://127.0.0.1:8787/"

rem run the service in this window (Ctrl+C or close window to stop)
python dashboard.py

echo.
echo Dashboard stopped. Press any key to close.
pause >nul
