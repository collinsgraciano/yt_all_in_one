@echo off
REM ============================================================
REM  开发机一键推送脚本 — git add + commit + push to GitHub
REM
REM  用法：
REM    scripts\git-deploy.bat "提交信息"
REM    scripts\git-deploy.bat               (只 push 已有提交)
REM ============================================================

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0git-deploy.ps1" %*
