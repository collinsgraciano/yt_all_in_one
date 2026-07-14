# ============================================================
#  Rebuild Docker image when dependencies change
#  Run this script when requirements.txt or Dockerfile changes
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$composeFiles = @("-f", "docker-compose.yml", "-f", "docker-compose.self-db.yml", "-f", "docker-compose.dev.yml")

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Rebuild Docker Image - dependency changed" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "[HINT] Run this script when these files change:"
Write-Host "       - requirements.txt - Python dependencies"
Write-Host "       - docker/Dockerfile.web"
Write-Host ""

Write-Host "[1/2] Building Docker image..." -ForegroundColor Yellow
& docker-compose @composeFiles build --no-cache
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Image build failed" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "      [OK]"

Write-Host ""
Write-Host "[2/2] Restarting services..." -ForegroundColor Yellow
& docker-compose @composeFiles up -d
Write-Host "      [OK]"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Rebuild complete! Image updated, services restarted" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
& docker-compose @composeFiles ps
Write-Host ""
Read-Host "Press Enter to exit"
