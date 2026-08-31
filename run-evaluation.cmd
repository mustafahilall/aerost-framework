@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-evaluation.ps1"
if errorlevel 1 exit /b %errorlevel%
endlocal
