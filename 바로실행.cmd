@echo off
setlocal DisableDelayedExpansion
set "BI_EXE=%~dp0BatteryInsight.exe"
if not exist "%BI_EXE%" set "BI_EXE=%~dp0dist\BatteryInsight.exe"
if not exist "%BI_EXE%" (
  echo BatteryInsight.exe was not found. Extract the complete ZIP first.
  pause
  exit /b 1
)
start "" "%BI_EXE%"
exit /b %errorlevel%
