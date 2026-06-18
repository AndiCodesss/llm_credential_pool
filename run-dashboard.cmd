@echo off
REM Run the quota dashboard (visible window) and open it in your browser.
setlocal
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
start "" "http://127.0.0.1:8788"
%PY% "%~dp0quota-dashboard.py"
