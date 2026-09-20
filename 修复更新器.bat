@echo off
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\repair_update_legacy.ps1"
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)
