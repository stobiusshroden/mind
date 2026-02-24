@echo off
setlocal
cd /d %~dp0

echo [setup] Creating venv .venv (if missing)...
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)

echo [setup] Upgrading pip...
.venv\Scripts\python.exe -m pip install -U pip

echo [setup] Installing requirements...
.venv\Scripts\python.exe -m pip install -r requirements.txt

echo [setup] Done.
echo Next: start_all.cmd
endlocal
