# ============================================================
#  Audiobook Manager - Quick Restart (Windows PowerShell)
#  Restart Docker containers without rebuilding images (fast).
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File scripts\quick-restart.ps1
#    powershell -ExecutionPolicy Bypass -File scripts\quick-restart.ps1 web
# ============================================================

param(
    [string]$ServiceName = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host ""
Write-Host "--- Quick Restart Services ---" -ForegroundColor Cyan

# --- Check Docker ---
$dockerInfo = & docker info 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Docker is not running! Please start Docker Desktop first." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# --- Determine compose files from .env ---
$envFile = Join-Path $ProjectRoot ".env"
$composeArgs = @("-f", "docker-compose.yml")
$dbMode = "self"

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $parts = $line -split '=', 2
            if ($parts.Length -eq 2 -and $parts[0].Trim() -eq "DB_MODE") {
                $dbMode = $parts[1].Trim().ToLower()
            }
        }
    }
}

if ($dbMode -eq "external") {
    $composeArgs += @("-f", "docker-compose.external-db.yml")
} else {
    $composeArgs += @("-f", "docker-compose.self-db.yml")
}

# --- Restart ---
if ([string]::IsNullOrWhiteSpace($ServiceName)) {
    Write-Host "Restarting all services..."
    & docker-compose @composeArgs restart
} else {
    Write-Host "Restarting service: $ServiceName"
    & docker-compose @composeArgs restart $ServiceName
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Restart failed" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# --- Show status ---
Write-Host ""
Write-Host "--- Service Status ---" -ForegroundColor Cyan
& docker-compose @composeArgs ps

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host ""
