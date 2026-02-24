@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ==============================
REM Stop Hybrid + OpenClaw Bridge
REM ==============================

call :kill_port 8000
call :kill_port 17171

echo [stop_all] Done.
endlocal
exit /b 0

:kill_port
set PORT=%~1
for /f "tokens=5" %%P in ('netstat -aon ^| findstr ":%PORT%" ^| findstr LISTENING') do (
  echo [stop_all] Killing PID %%P on port %PORT% ...
  taskkill /PID %%P /F >nul 2>nul
)
exit /b 0
