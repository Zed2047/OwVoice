@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\repair_update_legacy.ps1"
if errorlevel 1 (
  echo.
  echo 修复更新失败，请保留本窗口中的报错信息并联系开发者。
  pause
)
