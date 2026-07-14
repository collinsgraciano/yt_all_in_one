# ============================================================
#  Audiobook Manager - Sync code to remote server
#  Steps: Pack code -> Upload to server -> Remote deploy
# ============================================================
#  Before first use, configure these in .env.deploy:
#    SERVER_HOST     - Server IP or domain
#    SERVER_USER     - SSH username (usually root)
#    SERVER_PATH     - Project path on server (e.g. /opt/audiobook)
#    SERVER_PASSWORD - SSH password
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# --- Read config from .env.deploy ---
$Config = @{
    SERVER_HOST     = "your-server-ip"
    SERVER_USER     = "root"
    SERVER_PATH     = "/opt/audiobook"
    SERVER_PASSWORD = ""
}

$EnvFile = Join-Path $ProjectRoot ".env.deploy"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $parts = $line -split '=', 2
            if ($parts.Length -eq 2) {
                $key = $parts[0].Trim()
                $val = $parts[1].Trim()
                if ($Config.ContainsKey($key)) {
                    $Config[$key] = $val
                }
            }
        }
    }
}

$ServerHost     = $Config.SERVER_HOST
$ServerUser     = $Config.SERVER_USER
$ServerPath     = $Config.SERVER_PATH
$ServerPassword = $Config.SERVER_PASSWORD

# --- Banner ---
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Audiobook Manager - Sync code to remote server" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Server:   $ServerUser@$ServerHost"
Write-Host "  Path:     $ServerPath"
Write-Host ""

# --- Validate config ---
if ($ServerHost -eq "your-server-ip") {
    Write-Host "[ERROR] Please configure server info first!" -ForegroundColor Red
    Write-Host "        Create .env.deploy file or modify the variables in the script"
    Write-Host "        Format:"
    Write-Host "          SERVER_HOST=1.2.3.4"
    Write-Host "          SERVER_USER=root"
    Write-Host "          SERVER_PATH=/opt/audiobook"
    Write-Host "          SERVER_PASSWORD=your_password"
    Read-Host "Press Enter to exit"
    exit 1
}

# --- Detect available SSH tools ---
$UsePutty = $false
$PscpCmd = Get-Command pscp -ErrorAction SilentlyContinue
$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue

if ($PscpCmd -and $PlinkCmd) {
    $UsePutty = $true
    Write-Host "  SSH tool: PuTTY (pscp/plink) - auto password login"
    if (-not $ServerPassword) {
        Write-Host "[ERROR] PuTTY mode requires SERVER_PASSWORD" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
} else {
    Write-Host "  SSH tool: OpenSSH (ssh/scp) - interactive password"
    $SshCmd = Get-Command ssh -ErrorAction SilentlyContinue
    if (-not $SshCmd) {
        Write-Host "[ERROR] ssh command not found, please install OpenSSH client or PuTTY" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    if (-not $ServerPassword) {
        Write-Host "[HINT] SERVER_PASSWORD not set, will prompt for password interactively"
        Write-Host "       For fully automatic deploy, install PuTTY and set SERVER_PASSWORD in .env.deploy"
    }
}
Write-Host ""

# --- Step 1: Pack code ---
Write-Host "[Step 1/3] Packing code..." -ForegroundColor Yellow

$TarFile = Join-Path $env:TEMP "audiobook_deploy.tar"

# Verify all source files/dirs exist before packing
$requiredPaths = @(
    "backend", "pipeline", "docker",
    "docker-compose.yml",
    "docker-compose.self-db.yml",
    "docker-compose.external-db.yml",
    "docker-compose.lowmem.yml",
    "requirements.txt",
    "scripts\server-deploy.sh", "scripts\quick-restart.sh",
    ".env.example"
)
$missingPaths = @()
foreach ($p in $requiredPaths) {
    if (-not (Test-Path $p)) {
        $missingPaths += $p
    }
}
if ($missingPaths.Count -gt 0) {
    Write-Host "[ERROR] Missing files/dirs:" -ForegroundColor Red
    foreach ($m in $missingPaths) { Write-Host "         $m" -ForegroundColor Red }
    Read-Host "Press Enter to exit"
    exit 1
}

# Clean __pycache__ dirs before packing (so we don't need --exclude for them)
Get-ChildItem -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# Write the tar command to a temporary .bat file and execute it via cmd.exe.
# This completely bypasses PowerShell's native-command argument handling,
# which is the root cause of the "Couldn't visit directory" errors on Windows.
$batFile = Join-Path $env:TEMP "audiobook_pack.bat"
$batContent = @"
@echo off
cd /d "$ProjectRoot"
tar cf "$TarFile" --exclude=.git --exclude=__pycache__ --exclude=*.pyc --exclude=.venv --exclude=venv --exclude=.env --exclude=.env.deploy --exclude=*.log --exclude=tmp --exclude=.idea --exclude=.vscode backend pipeline docker docker-compose.yml docker-compose.self-db.yml docker-compose.external-db.yml docker-compose.lowmem.yml requirements.txt scripts\server-deploy.sh scripts\quick-restart.sh .env.example
"@
Set-Content -Path $batFile -Value $batContent -Encoding ASCII

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$packOutput = & cmd /c $batFile 2>&1
$tarExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

if ($tarExit -ne 0 -or -not (Test-Path $TarFile) -or (Get-Item $TarFile).Length -eq 0) {
    Write-Host "[WARN] tar via .bat failed (exit=$tarExit), retrying without --exclude..." -ForegroundColor Yellow
    # Fallback: tar without --exclude (pycache already cleaned above)
    $batContent2 = @"
@echo off
cd /d "$ProjectRoot"
tar cf "$TarFile" backend pipeline docker docker-compose.yml docker-compose.self-db.yml docker-compose.external-db.yml docker-compose.lowmem.yml requirements.txt scripts\server-deploy.sh scripts\quick-restart.sh .env.example
"@
    Set-Content -Path $batFile -Value $batContent2 -Encoding ASCII

    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $packOutput = & cmd /c $batFile 2>&1
    $tarExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
}

Remove-Item $batFile -ErrorAction SilentlyContinue

if ($tarExit -ne 0 -or -not (Test-Path $TarFile) -or (Get-Item $TarFile).Length -eq 0) {
    Write-Host "[ERROR] tar packing failed (exit code: $tarExit)" -ForegroundColor Red
    Write-Host "  Output: $packOutput" -ForegroundColor DarkGray
    Read-Host "Press Enter to exit"
    exit 1
}

$tarSize = (Get-Item $TarFile).Length
Write-Host "        Packed: $tarSize bytes"
Write-Host "        [OK]"
Write-Host ""

# --- Step 2: Upload tar + deploy script to server ---
Write-Host "[Step 2/3] Uploading to server..." -ForegroundColor Yellow

$UploadOk = $false
$deployScript = Join-Path $PSScriptRoot "server-deploy.sh"
$remoteTar = "/tmp/audiobook_deploy.tar"
$remoteScript = "/tmp/audiobook_deploy.sh"

if ($UsePutty) {
    # PuTTY mode: pscp with auto password — upload both files in one call
    & pscp -pw $ServerPassword -batch -no-antispoof $TarFile $deployScript "${ServerUser}@${ServerHost}:/tmp/"
    if ($LASTEXITCODE -eq 0) { $UploadOk = $true }
} else {
    # OpenSSH mode: interactive password — upload both files in one call
    Write-Host "        Please enter server password if prompted:"
    & scp -o StrictHostKeyChecking=no $TarFile $deployScript "${ServerUser}@${ServerHost}:/tmp/"
    if ($LASTEXITCODE -eq 0) { $UploadOk = $true }
}

if (-not $UploadOk) {
    Write-Host "[ERROR] Upload failed, please check SSH connection and password" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "        [OK]"
Write-Host ""

# --- Step 3: Remote deploy ---
# Execute the deploy script on the server via SSH (file was uploaded via scp,
# so encoding is preserved — no stdin piping which corrupts UTF-8/Unicode).
Write-Host "[Step 3/3] Remote deploy..." -ForegroundColor Yellow

$DeployOk = $false
$remoteCmd = "SERVER_PATH='$ServerPath' bash /tmp/server-deploy.sh && rm -f /tmp/server-deploy.sh"

if ($UsePutty) {
    # PuTTY mode: plink with auto password
    & plink -pw $ServerPassword -batch -no-antispoof "${ServerUser}@${ServerHost}" $remoteCmd
    if ($LASTEXITCODE -eq 0) { $DeployOk = $true }
} else {
    # OpenSSH mode: interactive password
    Write-Host "        Please enter server password again if prompted:"
    & ssh -o StrictHostKeyChecking=no "${ServerUser}@${ServerHost}" $remoteCmd
    if ($LASTEXITCODE -eq 0) { $DeployOk = $true }
}

if ($DeployOk) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  Deploy complete!" -ForegroundColor Green
    Write-Host "  URL: http://${ServerHost}:8080" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
} else {
    Write-Host "[ERROR] Remote deploy failed" -ForegroundColor Red
}

# Cleanup temp files
Remove-Item $TarFile -ErrorAction SilentlyContinue

Write-Host ""
Read-Host "Press Enter to exit"
