# ============================================================
#  Audiobook Manager - Smart Deploy (Windows PowerShell)
#  Auto-detects DB_MODE from .env and selects compose overlays.
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
#    powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -LowMem
#    powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Rebuild
# ============================================================

param(
    [switch]$LowMem,
    [switch]$Rebuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# --- Functions ---
function Write-Info  { Write-Host "[INFO]  $args" -ForegroundColor Cyan }
function Write-Ok     { Write-Host "[OK]    $args" -ForegroundColor Green }
function Write-Warn   { Write-Host "[WARN]  $args" -ForegroundColor Yellow }
function Write-Err    { Write-Host "[ERROR] $args" -ForegroundColor Red }

# --- Load .env ---
$envFile = Join-Path $ProjectRoot ".env"
$envVars = @{}
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $parts = $line -split '=', 2
            if ($parts.Length -eq 2) {
                $key = $parts[0].Trim()
                $val = $parts[1].Trim()
                $envVars[$key] = $val
            }
        }
    }
}

# Resolve DB_MODE
$dbMode = if ($envVars.ContainsKey("DB_MODE")) { $envVars["DB_MODE"] } else { "self" }
if ([string]::IsNullOrWhiteSpace($dbMode)) { $dbMode = "self" }
$dbMode = $dbMode.Trim().ToLower()

# --- Banner ---
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Audiobook Manager - Smart Deploy" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  DB Mode:   $dbMode"
if ($LowMem) { Write-Host "  LowMem:    enabled" }
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# --- Check Docker ---
$dockerInfo = & docker info 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err "Docker is not running! Please start Docker Desktop first."
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Ok "Docker is running"

# --- Assemble compose files ---
$composeArgs = @("-f", "docker-compose.yml")

if ($dbMode -eq "external") {
    $composeArgs += @("-f", "docker-compose.external-db.yml")

    $extUrl = if ($envVars.ContainsKey("EXTERNAL_DATABASE_URL")) { $envVars["EXTERNAL_DATABASE_URL"] } else { "" }
    if ([string]::IsNullOrWhiteSpace($extUrl)) {
        Write-Err "DB_MODE=external but EXTERNAL_DATABASE_URL is not set."
        Write-Host "  Please configure EXTERNAL_DATABASE_URL in .env"
        Write-Host "  Example: EXTERNAL_DATABASE_URL=postgresql://user:pass@host:5432/audiobook"
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Info "External DB: $extUrl"
} else {
    $composeArgs += @("-f", "docker-compose.self-db.yml")
    $pgPass = if ($envVars.ContainsKey("POSTGRES_PASSWORD")) { $envVars["POSTGRES_PASSWORD"] } else { "changeme_strong_password" }
    # Show only first 3 chars for security
    $maskedPass = $pgPass.Substring(0, [Math]::Min(3, $pgPass.Length)) + "****"
    Write-Info "Self-hosted DB password: $maskedPass (read from .env)"
}

if ($LowMem) {
    $composeArgs += @("-f", "docker-compose.lowmem.yml")
}

Write-Host ""
Write-Info "Compose files: $($composeArgs -join ' ')"

# --- Step 1: Build image ---
Write-Host ""
Write-Host "[1/3] Building image..." -ForegroundColor Yellow
if ($Rebuild) {
    & docker-compose @composeArgs build --no-cache web
} else {
    & docker-compose @composeArgs build web
}
if ($LASTEXITCODE -ne 0) {
    Write-Err "Image build failed"
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "      [OK]" -ForegroundColor Green

# --- Step 2: Start services ---
Write-Host ""
Write-Host "[2/3] Starting services..." -ForegroundColor Yellow
& docker-compose @composeArgs up -d
if ($LASTEXITCODE -ne 0) {
    Write-Err "Failed to start services"
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "      [OK]" -ForegroundColor Green

# --- Step 3: Wait for service ready ---
Write-Host ""
Write-Host "[3/3] Waiting for service to start..." -ForegroundColor Yellow
Start-Sleep -Seconds 3

$maxRetries = 15
$ready = $false
for ($i = 1; $i -le $maxRetries; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:8080/" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        # 200 or 302 both mean service is up
        Write-Ok "Web service is ready"
        $ready = $true
        break
    } catch {
        if ($i -eq $maxRetries) {
            Write-Warn "Web service not ready within expected time. Check logs:"
            Write-Host "  docker-compose $($composeArgs -join ' ') logs web --tail 20"
        }
        Start-Sleep -Seconds 2
    }
}

# --- Show status ---
Write-Host ""
Write-Host "--- Service Status ---" -ForegroundColor Cyan
& docker-compose @composeArgs ps

# --- Get local IP ---
$localIP = "localhost"
try {
    $ipInfo = Get-NetIPConfiguration -ErrorAction Stop | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1
    if ($ipInfo -and $ipInfo.IPv4Address) {
        $localIP = $ipInfo.IPv4Address.IPAddress
    }
} catch {
    # Fallback for older Windows or non-standard setups
    try {
        $localIP = (Get-WmiObject Win32_NetworkAdapterConfiguration | Where-Object { $_.DefaultIPGateway -ne $null } | Select-Object -First 1).IPAddress[0]
    } catch {
        $localIP = "localhost"
    }
}

$appPassword = if ($envVars.ContainsKey("APP_PASSWORD")) { $envVars["APP_PASSWORD"] } else { "inriynisse" }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Deploy complete!" -ForegroundColor Green
Write-Host "  URL:      http://${localIP}:8080" -ForegroundColor Green
Write-Host "  Password: $appPassword" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
