$ErrorActionPreference = "Continue"
Set-Location "h:\2026_main_project\yt_aduio_book_one_to_all"
$out = "h:\2026_main_project\yt_aduio_book_one_to_all\scripts\_tar_diag.txt"

# 1. tar version
"=== tar version ===" | Out-File $out -Encoding UTF8
tar --version 2>&1 | Out-File $out -Append -Encoding UTF8
"" | Out-File $out -Append -Encoding UTF8

# 2. Test simple tar
"=== Test: tar cf test1.tar backend\__init__.py ===" | Out-File $out -Append -Encoding UTF8
tar cf "$env:TEMP\test1.tar" "backend/__init__.py" 2>&1 | Out-File $out -Append -Encoding UTF8
"exit code: $LASTEXITCODE" | Out-File $out -Append -Encoding UTF8
"" | Out-File $out -Append -Encoding UTF8

# 3. Test with directory
"=== Test: tar cf test2.tar backend/ ===" | Out-File $out -Append -Encoding UTF8
tar cf "$env:TEMP\test2.tar" "backend/" 2>&1 | Out-File $out -Append -Encoding UTF8
"exit code: $LASTEXITCODE" | Out-File $out -Append -Encoding UTF8
"" | Out-File $out -Append -Encoding UTF8

# 4. Test with --exclude
"=== Test: tar cf test3.tar --exclude=.git backend/ ===" | Out-File $out -Append -Encoding UTF8
tar cf "$env:TEMP\test3.tar" "--exclude=.git" "backend/" 2>&1 | Out-File $out -Append -Encoding UTF8
"exit code: $LASTEXITCODE" | Out-File $out -Append -Encoding UTF8
"" | Out-File $out -Append -Encoding UTF8

# 5. Test with multiple dirs and exclude
"=== Test: tar cf test4.tar --exclude=.git --exclude=__pycache__ backend/ pipeline/ docker/ ===" | Out-File $out -Append -Encoding UTF8
tar cf "$env:TEMP\test4.tar" "--exclude=.git" "--exclude=__pycache__" "backend/" "pipeline/" "docker/" 2>&1 | Out-File $out -Append -Encoding UTF8
"exit code: $LASTEXITCODE" | Out-File $out -Append -Encoding UTF8
"" | Out-File $out -Append -Encoding UTF8

# 6. Full command
"=== Full test: same as sync-to-server ===" | Out-File $out -Append -Encoding UTF8
$TarFile = Join-Path $env:TEMP "audiobook_deploy.tar"
tar cf $TarFile "--exclude=.git" "--exclude=__pycache__" "--exclude=*.pyc" "--exclude=.venv" "--exclude=venv" "--exclude=.env" "--exclude=.env.deploy" "--exclude=*.log" "--exclude=tmp" "--exclude=.idea" "--exclude=.vscode" "backend/" "pipeline/" "docker/" "docker-compose.yml" "requirements.txt" "scripts/server-deploy.sh" "scripts/quick-restart.sh" ".env.example" 2>&1 | Out-File $out -Append -Encoding UTF8
"exit code: $LASTEXITCODE" | Out-File $out -Append -Encoding UTF8

# 7. Test without exclude flags
"=== Test without excludes ===" | Out-File $out -Append -Encoding UTF8
tar cf "$env:TEMP\test5.tar" "backend/" "pipeline/" "docker/" "docker-compose.yml" "requirements.txt" 2>&1 | Out-File $out -Append -Encoding UTF8
"exit code: $LASTEXITCODE" | Out-File $out -Append -Encoding UTF8
"" | Out-File $out -Append -Encoding UTF8

# 8. List current dir
"=== Current dir listing ===" | Out-File $out -Append -Encoding UTF8
Get-ChildItem -Name | Out-File $out -Append -Encoding UTF8
