# ============================================================
#  开发机一键推送脚本
#  做三件事：git add → git commit → git push
#
#  用法：
#    scripts\git-deploy.bat "修复了登录bug"
#    scripts\git-deploy.bat "新增批量导入功能"
#    scripts\git-deploy.bat          # 不带消息则只 push 已有提交
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$CommitMsg = if ($args.Count -gt 0) { $args[0] } else { $null }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Push to GitHub" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ─── 检查 git 仓库 ───
if (-not (Test-Path (Join-Path $ProjectRoot ".git"))) {
    Write-Host "Git 仓库未初始化，正在初始化..." -ForegroundColor Yellow
    git init
    git add -A
    git commit -m "initial commit"
    git branch -M main
    Write-Host ""
    Write-Host "请先在 GitHub 创建仓库，然后运行：" -ForegroundColor Yellow
    Write-Host "  git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git" -ForegroundColor White
    Write-Host "  git push -u origin main" -ForegroundColor White
    Write-Host ""
    Read-Host "完成后按回车退出"
    exit 0
}

# ─── 检查远程仓库 ───
$remoteUrl = git remote get-url origin 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "未配置远程仓库，请先添加：" -ForegroundColor Red
    Write-Host "  git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git" -ForegroundColor White
    Read-Host "按回车退出"
    exit 1
}
Write-Host "  Remote: $remoteUrl"
Write-Host ""

# ─── commit ───
if ($CommitMsg) {
    Write-Host "git add -A" -ForegroundColor Gray
    git add -A

    Write-Host "git commit -m `"$CommitMsg`"" -ForegroundColor Gray
    git commit -m $CommitMsg
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {
        Write-Host "commit 失败" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
}

# ─── push ───
$branch = git symbolic-ref --short HEAD 2>&1
if ($LASTEXITCODE -ne 0) { $branch = "main" }

Write-Host "git push origin $branch" -ForegroundColor Gray
git push origin $branch
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "push 失败" -ForegroundColor Red
    Write-Host "如果是认证问题，GitHub 已不支持密码，请用 Token：" -ForegroundColor Yellow
    Write-Host "  https://github.com/settings/tokens -> 生成 token -> 勾选 repo 权限" -ForegroundColor White
    Read-Host "按回车退出"
    exit 1
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Push 完成！" -ForegroundColor Green
Write-Host "  下一步：SSH 登录服务器，运行：" -ForegroundColor Cyan
Write-Host "    cd /root/audiobook && bash scripts/git-server-deploy.sh" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Read-Host "按回车退出"
