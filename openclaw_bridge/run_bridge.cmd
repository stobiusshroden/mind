@echo off
setlocal
cd /d %~dp0

REM Use Hybrid's venv if present one level up at C:\Users\shrod\Desktop\mind\.venv
set VENV_DIR=%~dp0..\.venv
if exist "%VENV_DIR%\Scripts\python.exe" (
  "%VENV_DIR%\Scripts\python.exe" -m uvicorn bridge:app --host 127.0.0.1 --port 17171
) else (
  python -m uvicorn bridge:app --host 127.0.0.1 --port 17171
)
