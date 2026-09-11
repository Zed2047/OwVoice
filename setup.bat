@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_v2.ps1"
set "setup_exit=%errorlevel%"
echo.
if not "%setup_exit%"=="0" echo Setup did not finish. See the newest log in the logs folder.
pause

endlocal & exit /b %setup_exit%
