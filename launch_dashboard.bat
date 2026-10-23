@echo off
REM Launches the Budget Tool Analytics Dashboard

REM --- Auto-update from Git: DISABLED while testing. ---
REM To turn on before handing this out, delete the "echo" line below and remove "REM " from the git and pip lines under it.
echo [Git pull is disabled - running the local copy as is]
REM git rev-parse --is-inside-work-tree >nul 2>&1 && for /f %%b in ('git rev-parse --abbrev-ref HEAD') do if "%%b"=="main" git pull --ff-only origin main
REM Dependencies (pypdf) install automatically the first time a PDF is imported.
REM python -m pip install --quiet --disable-pip-version-check -r requirements.txt

python budget_dashboard.py
pause
