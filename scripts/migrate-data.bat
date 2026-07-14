@echo off
REM ============================================================
REM  Audiobook Manager - Data Migration (CMD launcher)
REM  All logic is in migrate-data.ps1
REM
REM  Usage:
REM    scripts\migrate-data.bat
REM
REM  Or with env vars:
REM    set OLD_PG_HOST=127.0.0.1
REM    set OLD_PG_PORT=5432
REM    set OLD_PG_USER=postgres
REM    set OLD_PG_DB=audiobook
REM    set OLD_PG_PASSWORD=your_password
REM    scripts\migrate-data.bat
REM ============================================================

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0migrate-data.ps1"
