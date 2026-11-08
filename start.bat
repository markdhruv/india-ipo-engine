@echo off
REM One-click start for Windows. Creates the venv on first run.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run - setting up...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
  .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
)
.venv\Scripts\python.exe run.py app %*
if errorlevel 1 pause
