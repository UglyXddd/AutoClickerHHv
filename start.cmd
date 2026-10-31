@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Create the Python environment first. See README.md.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py %*
set "HH_BROWSER_EXIT=%ERRORLEVEL%"
pause
exit /b %HH_BROWSER_EXIT%
