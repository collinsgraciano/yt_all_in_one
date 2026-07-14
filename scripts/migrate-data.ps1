# ============================================================
#  Data Migration Script: Old PostgreSQL -> Docker PostgreSQL
#  Windows PowerShell version of migrate_data.sh
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File scripts\migrate-data.ps1
#
#  Or with environment variables:
#    $env:OLD_PG_HOST="127.0.0.1"; $env:OLD_PG_PORT="5432"; $env:OLD_PG_USER="postgres"; $env:OLD_PG_DB="audiobook"; `
#    powershell -ExecutionPolicy Bypass -File scripts\migrate-data.ps1
#
#  Prerequisites:
#    - PostgreSQL client tools (psql, pg_dump) installed on host
#    - Docker running with audiobook_postgres container
# ============================================================

# Use "Continue" so native command stderr (psql/docker) doesn't throw.
# Critical errors are handled via explicit $LASTEXITCODE checks below.
$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# --- Output helpers ---
function Write-Info  { Write-Host "[INFO]  $args" -ForegroundColor Cyan }
function Write-Ok    { Write-Host "[OK]    $args" -ForegroundColor Green }
function Write-Warn  { Write-Host "[WARN]  $args" -ForegroundColor Yellow }
function Write-Err   { Write-Host "[ERROR] $args" -ForegroundColor Red }

# --- Old DB connection params (overridable via env vars) ---
$oldPgHost = if ($env:OLD_PG_HOST) { $env:OLD_PG_HOST } else { "127.0.0.1" }
$oldPgPort = if ($env:OLD_PG_PORT) { $env:OLD_PG_PORT } else { "5432" }
$oldPgUser = if ($env:OLD_PG_USER) { $env:OLD_PG_USER } else { "postgres" }
$oldPgDb = if ($env:OLD_PG_DB) { $env:OLD_PG_DB } else { "audiobook" }
$oldPgPassword = $env:OLD_PG_PASSWORD

# --- New DB connection params (match docker-compose.yml) ---
$newPgUser = "audiobook_app"
$newPgDb = "audiobook"
$newPgContainer = "audiobook_postgres"

# --- Core tables to migrate ---
$coreTables = @(
    "books"
    "book_processing_states"
    "youtube_credentials"
    "modelscope_tokens"
    "channel_runtime_settings"
    "task_queue"
)

# --- Backup file path ---
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupFile = Join-Path $env:TEMP "audiobook_migration_$timestamp.sql"

# ============================================================================
# Pre-checks
# ============================================================================
Write-Info "========================================"
Write-Info "  Data Migration Script (Docker PostgreSQL)"
Write-Info "========================================"
Write-Info ""
Write-Info "Old DB: ${oldPgHost}:${oldPgPort}/${oldPgDb} (user: ${oldPgUser})"
Write-Info "New DB: Docker container ${newPgContainer} -> ${newPgDb} (user: ${newPgUser})"
Write-Info ""

# Check docker-compose.yml exists
if (-not (Test-Path (Join-Path $ProjectRoot "docker-compose.yml"))) {
    Write-Err "docker-compose.yml not found. Please run from project root."
    Read-Host "Press Enter to exit"
    exit 1
}

# Check .env file
$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Warn ".env not found, copying from .env.example..."
    $envExample = Join-Path $ProjectRoot ".env.example"
    if (Test-Path $envExample) {
        Copy-Item $envExample $envFile
        Write-Warn "Please edit .env to set POSTGRES_PASSWORD and SECRET_KEY, then re-run."
    } else {
        Write-Err ".env.example not found either. Please create .env manually."
    }
    Read-Host "Press Enter to exit"
    exit 1
}

# Read POSTGRES_PASSWORD from .env
$newPgPassword = $null
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $parts = $line -split '=', 2
        if ($parts.Length -eq 2 -and $parts[0].Trim() -eq "POSTGRES_PASSWORD") {
            $newPgPassword = $parts[1].Trim().Trim('"').Trim("'")
        }
    }
}
if ([string]::IsNullOrWhiteSpace($newPgPassword)) {
    Write-Err "POSTGRES_PASSWORD not set in .env. Please configure first."
    Read-Host "Press Enter to exit"
    exit 1
}

# Show masked password
$maskedPass = $newPgPassword.Substring(0, [Math]::Min(2, $newPgPassword.Length)) + "****"
Write-Info "New DB password: $maskedPass (read from .env)"
Write-Info ""

# ============================================================================
# Step 1: Backup old DB data
# ============================================================================
Write-Info "---- Step 1: Backup old DB data ----"

# Set PGPASSWORD for old DB
if ($oldPgPassword) {
    $env:PGPASSWORD = $oldPgPassword
}

# Test old DB connection
Write-Info "Testing old DB connection ${oldPgHost}:${oldPgPort}/${oldPgDb} ..."
$testResult = & psql -h $oldPgHost -p $oldPgPort -U $oldPgUser -d $oldPgDb -c "SELECT 1;" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err "Cannot connect to old DB. Please check connection params."
    Write-Err "Try setting OLD_PG_PASSWORD env var:"
    Write-Err "  `$env:OLD_PG_PASSWORD='your_password'; scripts\migrate-data.ps1"
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Ok "Old DB connection successful"

# Check row counts per table
Write-Info "Old DB table row counts:"
foreach ($table in $coreTables) {
    $count = & psql -h $oldPgHost -p $oldPgPort -U $oldPgUser -d $oldPgDb -t -c "SELECT count(*) FROM public.${table};" 2>&1 | Where-Object { $_ -is [string] }
    $count = ($count -join "").Trim()
    Write-Host ("  {0,-30} {1} rows" -f $table, $count)
}

# Export data (data only, INSERT format for column compatibility)
Write-Info "Exporting data to $backupFile ..."
$tableArgs = @()
foreach ($table in $coreTables) {
    $tableArgs += "--table=public.${table}"
}

& pg_dump -h $oldPgHost -p $oldPgPort -U $oldPgUser -d $oldPgDb --data-only --column-inserts --no-owner --no-privileges @tableArgs | Out-File -FilePath $backupFile -Encoding UTF8

if (-not (Test-Path $backupFile) -or (Get-Item $backupFile).Length -eq 0) {
    Write-Err "Backup file is empty, migration aborted."
    Read-Host "Press Enter to exit"
    exit 1
}

$backupSize = (Get-Item $backupFile).Length
$backupSizeKB = [Math]::Round($backupSize / 1KB, 1)
Write-Ok "Backup complete: $backupFile ($backupSizeKB KB)"

# ============================================================================
# Step 2: Start Docker PostgreSQL
# ============================================================================
Write-Info ""
Write-Info "---- Step 2: Start Docker PostgreSQL ----"

# Check Docker is running
$dockerInfo = & docker info 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err "Docker is not running. Please start Docker first."
    Read-Host "Press Enter to exit"
    exit 1
}

# Check container status
$containerStatus = & docker inspect -f '{{.State.Status}}' $newPgContainer 2>&1
if ($LASTEXITCODE -ne 0) { $containerStatus = "not_found" }

if ($containerStatus -eq "running") {
    Write-Warn "Container $newPgContainer is already running"
    # Check if already initialized
    $existingRows = & docker exec $newPgContainer psql -U $newPgUser -d $newPgDb -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='channels';" 2>&1 | Where-Object { $_ -is [string] }
    if ($existingRows -and ($existingRows -join "").Trim() -eq "1") {
        Write-Warn "Database already initialized (init-db.sql has been executed)"
    }
} elseif ($containerStatus -eq "not_found") {
    Write-Info "First start, creating and initializing database container..."
    & docker-compose -f docker-compose.yml -f docker-compose.self-db.yml up -d postgres
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to start postgres container"
        Read-Host "Press Enter to exit"
        exit 1
    }

    Write-Info "Waiting for database health check to pass..."
    $maxWait = 60
    $waited = 0
    while ($waited -lt $maxWait) {
        $health = (& docker inspect -f '{{.State.Health.Status}}' $newPgContainer 2>&1 | Where-Object { $_ -is [string] }).Trim()
        if ($health -eq "healthy") { break }
        Start-Sleep -Seconds 2
        $waited += 2
    }
    if ($waited -ge $maxWait) {
        Write-Err "Database health check timeout (60s). Check container logs:"
        Write-Err "  docker logs $newPgContainer"
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Ok "Database container started and healthy"
} else {
    Write-Info "Starting existing container..."
    & docker start $newPgContainer
    Write-Info "Waiting for database ready..."
    Start-Sleep -Seconds 5
}

# Wait for DB to be connectable
Write-Info "Waiting for database to be connectable..."
$dbReady = $false
for ($i = 1; $i -le 30; $i++) {
    $readyCheck = & docker exec $newPgContainer pg_isready -U $newPgUser -d $newPgDb 2>&1 | Where-Object { $_ -is [string] }
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Database is ready"
        $dbReady = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $dbReady) {
    Write-Err "Database ready timeout"
    Read-Host "Press Enter to exit"
    exit 1
}

# ============================================================================
# Step 3: Verify table structure
# ============================================================================
Write-Info ""
Write-Info "---- Step 3: Verify table structure ----"

Write-Info "New DB table list:"
& docker exec $newPgContainer psql -U $newPgUser -d $newPgDb -c "\dt public.*"

# Check all core tables exist
foreach ($table in $coreTables) {
    $exists = & docker exec $newPgContainer psql -U $newPgUser -d $newPgDb -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='${table}';" 2>&1 | Where-Object { $_ -is [string] }
    $exists = ($exists -join "").Trim()
    if ($exists -ne "1") {
        Write-Err "Table public.${table} does not exist! init-db.sql may not have been executed."
        Read-Host "Press Enter to exit"
        exit 1
    }
}
Write-Ok "All 6 core tables exist"

# ============================================================================
# Step 4: Import old data
# ============================================================================
Write-Info ""
Write-Info "---- Step 4: Import old data ----"

# Check if new DB already has data (avoid duplicate import)
$hasData = $false
foreach ($table in $coreTables) {
    $rows = & docker exec $newPgContainer psql -U $newPgUser -d $newPgDb -t -c "SELECT count(*) FROM public.${table};" 2>&1 | Where-Object { $_ -is [string] }
    $rows = ($rows -join "").Trim()
    if ($rows -match '^\d+$' -and [int]$rows -gt 0) {
        $hasData = $true
        Write-Warn "Table ${table} already has ${rows} rows"
        break
    }
}

if ($hasData) {
    Write-Warn "Detected existing data in new DB!"
    Write-Host ""
    $reply = Read-Host "Clear new DB core tables and re-import? (y/N)"
    if ($reply -match '^[Yy]$') {
        Write-Info "Clearing core tables..."
        $truncateSql = @"
SET session_replication_role = replica;
TRUNCATE TABLE public.books, public.book_processing_states, public.youtube_credentials,
             public.modelscope_tokens, public.channel_runtime_settings, public.task_queue
CASCADE;
SET session_replication_role = DEFAULT;
"@
        $truncateSql | & docker exec -i $newPgContainer psql -U $newPgUser -d $newPgDb 2>&1 | Out-Null
        Write-Ok "Core tables cleared"
    } else {
        Write-Warn "Skipping import, keeping existing data."
        Write-Info "To import manually:"
        Write-Info "  Get-Content $backupFile | docker exec -i $newPgContainer psql -U $newPgUser -d $newPgDb"
        Read-Host "Press Enter to exit"
        exit 0
    }
}

# Import data
Write-Info "Importing data..."
Get-Content $backupFile -Raw | & docker exec -i $newPgContainer psql -U $newPgUser -d $newPgDb -v ON_ERROR_STOP=off 2>&1 | ForEach-Object {
    if ($_ -match 'ERROR|FATAL') {
        Write-Warn $_
    }
}

Write-Ok "Data import complete"

# ============================================================================
# Step 5: Verify data
# ============================================================================
Write-Info ""
Write-Info "---- Step 5: Verify data ----"

Write-Info "New DB table row counts:"
foreach ($table in $coreTables) {
    $count = & docker exec $newPgContainer psql -U $newPgUser -d $newPgDb -t -c "SELECT count(*) FROM public.${table};" 2>&1 | Where-Object { $_ -is [string] }
    $count = ($count -join "").Trim()
    Write-Host ("  {0,-30} {1} rows" -f $table, $count)
}

# Compare old and new row counts
Write-Info ""
Write-Info "Data comparison:"
$allMatch = $true
foreach ($table in $coreTables) {
    $oldCount = & psql -h $oldPgHost -p $oldPgPort -U $oldPgUser -d $oldPgDb -t -c "SELECT count(*) FROM public.${table};" 2>&1 | Where-Object { $_ -is [string] }
    $newCount = & docker exec $newPgContainer psql -U $newPgUser -d $newPgDb -t -c "SELECT count(*) FROM public.${table};" 2>&1 | Where-Object { $_ -is [string] }

    $oldCount = ($oldCount -join "").Trim()
    $newCount = ($newCount -join "").Trim()

    if ($oldCount -eq $newCount) {
        Write-Host ("  {0,-30} " -f $table) -NoNewline
        Write-Host "${oldCount} -> ${newCount} OK" -ForegroundColor Green
    } else {
        Write-Host ("  {0,-30} " -f $table) -NoNewline
        Write-Host "${oldCount} -> ${newCount} MISMATCH" -ForegroundColor Red
        $allMatch = $false
    }
}

# ============================================================================
# Step 6: Next steps
# ============================================================================
Write-Info ""
Write-Info "========================================"

if ($allMatch) {
    Write-Ok "Data migration successful! All table row counts match."
} else {
    Write-Warn "Some table row counts do not match. Please check output above."
    Write-Warn "  Possible causes: primary key conflicts, data type incompatibility, etc."
    Write-Warn "  Check backup file: $backupFile"
}

Write-Info ""
Write-Info "Next steps:"
Write-Info ""
Write-Info "  1. Start all services:"
Write-Info "     docker-compose -f docker-compose.yml -f docker-compose.self-db.yml up -d"
Write-Info ""
Write-Info "  2. Check service status:"
Write-Info "     docker-compose ps"
Write-Info ""
Write-Info "  3. Open Web UI:"
Write-Info "     http://localhost:8080"
Write-Info ""
Write-Info "  4. Backup file location (safe to delete):"
Write-Info "     $backupFile"
Write-Info ""
Write-Info "========================================"

Write-Host ""
Read-Host "Press Enter to exit"
