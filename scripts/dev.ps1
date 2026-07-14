# ============================================================
#  Audiobook Manager - Local dev environment quick start
#  Starts Docker dev container with hot reload
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Audiobook Manager - Local Dev Environment" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# --- Check .env file ---
if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) {
    Write-Host "[HINT] .env not found, creating from .env.example..."
    $envExample = Join-Path $ProjectRoot ".env.example"
    if (Test-Path $envExample) {
        Copy-Item $envExample (Join-Path $ProjectRoot ".env")
        Write-Host "[DONE] .env created, please modify passwords and keys as needed"
    } else {
        Write-Host "[ERROR] .env.example not found either, please create .env manually" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# --- Check if Docker is running ---
$dockerInfo = & docker info 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Docker is not running! Please start Docker Desktop first" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "[OK] Docker is running"

# --- Check if Docker image exists ---
# Relax EAP so docker's stderr output doesn't throw under "Stop" policy
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$images = & docker images --format "{{.Repository}}:{{.Tag}}" 2>&1 | Where-Object { $_ -is [string] }
$ErrorActionPreference = $prevEAP
$needBuild = -not ($images -match "audiobook-web")

if ($needBuild) {
    Write-Host "[HINT] First run, building Docker image..."
    & docker-compose -f docker-compose.yml -f docker-compose.self-db.yml -f docker-compose.dev.yml build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Image build failed" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "[DONE] Image build complete"
} else {
    Write-Host "[OK] Docker image already exists"
}

# --- Start dev environment ---
Write-Host ""
Write-Host "--- Starting dev container ---"
Write-Host "[INFO] Web UI:      http://localhost:8080"
Write-Host "[INFO] API docs:    http://localhost:8080/api/docs"
Write-Host "[INFO] PostgreSQL:  localhost:5432"
Write-Host ""
Write-Host "[INFO] Changes to backend/ code will auto-reload"
Write-Host "[INFO] Press Ctrl+C to stop all services"
Write-Host ""

& docker-compose -f docker-compose.yml -f docker-compose.self-db.yml -f docker-compose.dev.yml up

Read-Host "Press Enter to exit"
