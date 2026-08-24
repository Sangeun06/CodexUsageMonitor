@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
if errorlevel 1 (
  echo.
  echo Uninstall failed. Review the message above.
  pause
  exit /b 1
)
echo.
echo Uninstall completed.
pause
