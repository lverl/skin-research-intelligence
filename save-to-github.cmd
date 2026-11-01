@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0save-to-github.ps1"
if errorlevel 1 (
  echo Upload did not complete. See the error above.
)
pause
