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
".venv\Scripts\python.exe" telegram_jobs.py %*
set "TG_JOBS_EXIT=%ERRORLEVEL%"
pause
exit /b %TG_JOBS_EXIT%
