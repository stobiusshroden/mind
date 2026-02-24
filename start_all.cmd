@echo off
setlocal
REM ==============================
REM Start Hybrid + OpenClaw Bridge
REM ==============================
REM Prefers local venv .venv if present; otherwise falls back to system python.

set HYBRID_HOST=127.0.0.1
set HYBRID_PORT=8000
set BRIDGE_HOST=127.0.0.1
set BRIDGE_PORT=17171

REM Choose python executable
set PY=python
if exist "%~dp0.venv\Scripts\python.exe" (
  set PY=%~dp0.venv\Scripts\python.exe
)

echo [start_all] Starting OpenClaw Bridge on %BRIDGE_HOST%:%BRIDGE_PORT% ...
start "openclaw-bridge" cmd /k "cd /d %~dp0openclaw_bridge && %PY% -m uvicorn bridge:app --host %BRIDGE_HOST% --port %BRIDGE_PORT%"

REM small delay
ping 127.0.0.1 -n 2 >nul

echo [start_all] Starting Hybrid on %HYBRID_HOST%:%HYBRID_PORT% ...
start "hybrid" cmd /k "cd /d %~dp0 && %PY% -m uvicorn hybrid_reservoir_service:app --host %HYBRID_HOST% --port %HYBRID_PORT%"

REM small delay
ping 127.0.0.1 -n 2 >nul

echo [start_all] Open UI:
echo   http://%HYBRID_HOST%:%HYBRID_PORT%/ui/chat.html
start "hybrid-ui" "http://%HYBRID_HOST%:%HYBRID_PORT%/ui/chat.html"

echo [start_all] Done.
endlocal
