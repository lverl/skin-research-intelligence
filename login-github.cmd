@echo off
setlocal
cd /d "%~dp0"
"%~dp0downloads\github-cli\bin\gh.exe" auth login --hostname github.com --git-protocol https --web
if errorlevel 1 (
  echo GitHub login failed. See the message above.
) else (
  echo GitHub login completed. Return to Codex.
)
pause
