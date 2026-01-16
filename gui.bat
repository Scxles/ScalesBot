@echo off
setlocal
cd /d "%~dp0"

REM Ensure deps (quiet) - comment out if you prefer manual installs
py -m pip install -r requirements.txt >nul 2>nul

REM Prefer a no-console launcher if available
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw gui.py
  exit /b
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw gui.py
  exit /b
)

REM Fallback (console may appear)
start "" py gui.py
