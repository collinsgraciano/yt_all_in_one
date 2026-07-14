@echo off
REM ============================================================
REM  Audiobook Manager - Quick Restart (CMD launcher)
REM  All logic is in quick-restart.ps1
REM
REM  Usage:
REM    scripts\quick-restart.bat          Restart all services
REM    scripts\quick-restart.bat web      Restart only web service
REM ============================================================

set SERVICE=%1
if "%SERVICE%"=="" (
    powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0quick-restart.ps1"
) else (
    powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0quick-restart.ps1" -ServiceName %SERVICE%
)
