@echo off
REM Launches the Budget Tool Analytics Dashboard

REM Updates come from the app itself: an "Update available" button appears at the top when there is a new version.
REM Dependencies (pypdf) install automatically the first time a PDF is imported.
REM python -m pip install --quiet --disable-pip-version-check -r requirements.txt

REM Troubleshooting options go after the command, e.g.:  launch_dashboard.bat --no-app-window   or   --keep-running
REM Closing the page or using Quit stops the server and this window closes. If it stops with an error, the window stays open.
python budget_dashboard.py %*
if errorlevel 1 pause
