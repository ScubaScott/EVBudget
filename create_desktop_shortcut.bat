@echo off
REM Puts an EVBudget shortcut (with the app icon) on your Desktop. Run it once; rerun it if you move this folder.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0assets\create_shortcut.ps1" -Root "%~dp0"
pause
