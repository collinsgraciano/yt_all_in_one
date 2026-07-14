@echo off
REM ============================================================
REM  Audiobook Manager - Smart Deploy (CMD launcher)
REM  All logic is in deploy.ps1
REM
REM  Usage:
REM    scripts\deploy.bat              Standard deploy
REM    scripts\deploy.bat --lowmem      Low-mem deploy
REM    scripts\deploy.bat --rebuild    Force rebuild image
REM    scripts\deploy.bat --lowmem --rebuild
REM ============================================================

set PS_ARGS=

:parse_args
if "%~1"=="" goto run
if /i "%~1"=="--lowmem" set PS_ARGS=%PS_ARGS% -LowMem
if /i "%~1"=="--rebuild" set PS_ARGS=%PS_ARGS% -Rebuild
shift
goto parse_args

:run
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0deploy.ps1"%PS_ARGS%
