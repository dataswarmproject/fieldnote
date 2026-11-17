@echo off
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py --stop
) else (
  python app.py --stop
)
if errorlevel 1 pause
