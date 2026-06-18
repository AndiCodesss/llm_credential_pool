@echo off
REM Run the cross-model fallback gateway in front of CLIProxyAPI.
setlocal
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% "%~dp0fallback-proxy.py"
