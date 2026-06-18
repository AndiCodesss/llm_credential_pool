@echo off
setlocal EnableExtensions
REM Hidden, self-restarting launcher for the quota dashboard (no console, no browser popup).
REM Started by start-dashboard-hidden.vbs via the "CLIProxyAPI-Dashboard" scheduled task.
set "APP=%~dp0"
set "SCRIPT=%APP%quota-dashboard.py"
set "STOP=%APP%DASH_STOP"
set "LOG=%APP%dashboard.log"
cd /d "%APP%"

REM Locate Python robustly: the launcher first, then py / python on PATH.
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo [%DATE% %TIME%] FATAL: no Python found ^(py/python^). Install Python or fix PATH.>>"%LOG%"
  exit /b 9
)

:loop
if exist "%STOP%" (
  echo [%DATE% %TIME%] STOP file present; exiting. Delete "%STOP%" to allow restart.>>"%LOG%"
  exit /b 0
)
netstat -ano | findstr /C:":8788" | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
  echo [%DATE% %TIME%] port 8788 already listening; this launcher exiting.>>"%LOG%"
  exit /b 0
)
echo [%DATE% %TIME%] starting dashboard with %PY% ...>>"%LOG%"
%PY% "%SCRIPT%" >>"%LOG%" 2>&1
echo [%DATE% %TIME%] dashboard exited (code %ERRORLEVEL%); restarting in 5s.>>"%LOG%"
REM Reliable delay that works with no console (timeout needs console input; ping does not).
ping -n 6 127.0.0.1 >nul
goto loop
