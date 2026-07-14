@echo off
REM ============================================================
REM  Launcher: runs the PowerShell sync script
REM  All logic is in sync-to-server.ps1 to avoid CMD parsing issues
REM ============================================================
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0sync-to-server.ps1"
