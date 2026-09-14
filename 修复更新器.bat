@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\repair_update_legacy.ps1"
if errorlevel 1 (
  echo.
  echo Update repair failed. Keep this window open and contact the developer with the error details.
  pause
)
