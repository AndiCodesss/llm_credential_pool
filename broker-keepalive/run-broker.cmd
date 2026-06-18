@echo off
setlocal EnableExtensions
REM Sturdy launcher for the CLIProxyAPI token broker. Started by the "CLIProxyAPI" scheduled task.
REM Layer 1: this wrapper restarts cli-proxy-api.exe if the broker process exits.
REM Layer 2: the CLIProxyAPI-Watchdog scheduled task restarts this task if the wrapper is killed.

set "BROKER_HOME=%USERPROFILE%\.cli-proxy-api"
set "BROKER_EXE=%LOCALAPPDATA%\CLIProxyAPI\app\cli-proxy-api.exe"
set "BROKER_CONFIG=%BROKER_HOME%\config.yaml"
set "OUT_LOG=%BROKER_HOME%\server.out.log"
set "ERR_LOG=%BROKER_HOME%\server.err.log"
set "STOP_FILE=%BROKER_HOME%\STOP"

cd /d "%BROKER_HOME%" || exit /b 1

if not exist "%BROKER_EXE%" (
  echo [%DATE% %TIME%] FATAL: broker executable missing: "%BROKER_EXE%" >> "%ERR_LOG%"
  exit /b 2
)
if not exist "%BROKER_CONFIG%" (
  echo [%DATE% %TIME%] FATAL: broker config missing: "%BROKER_CONFIG%" >> "%ERR_LOG%"
  exit /b 3
)

set /a RESTARTS=0

:loop
if exist "%STOP_FILE%" (
  echo [%DATE% %TIME%] STOP file present; wrapper exiting. Delete "%STOP_FILE%" and run task to restart. >> "%OUT_LOG%"
  exit /b 0
)

REM If another wrapper already has the broker listening, this duplicate wrapper
REM should exit instead of looping forever and creating visible cmd/conhost windows.
netstat -ano | findstr /C:":8317" | findstr /C:"LISTENING" > nul
if not errorlevel 1 (
  echo [%DATE% %TIME%] Broker already listening on 127.0.0.1:8317; duplicate wrapper exiting. >> "%OUT_LOG%"
  exit /b 0
)

set /a RESTARTS+=1
echo [%DATE% %TIME%] Starting CLIProxyAPI restart #%RESTARTS%... >> "%OUT_LOG%"
"%BROKER_EXE%" -config "%BROKER_CONFIG%" >> "%OUT_LOG%" 2>> "%ERR_LOG%"
set "EXIT_CODE=%ERRORLEVEL%"
echo [%DATE% %TIME%] CLIProxyAPI exited with code %EXIT_CODE%. Restarting in 5 seconds... >> "%OUT_LOG%"

REM Avoid a tight crash loop while still recovering quickly.
timeout /t 5 /nobreak > nul
goto loop
