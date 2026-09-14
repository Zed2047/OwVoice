@echo off
setlocal
set "OWVOICE_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%OWVOICE_ROOT%updater\update_release.ps1" -RecoverOnly -TargetDirectory "%OWVOICE_ROOT%"
if errorlevel 1 (
  echo Update recovery failed. Please keep this window open and check .cache\updates.
  pause
  exit /b 1
)
echo Update recovery completed. You can start OwVoice.exe now.
pause
