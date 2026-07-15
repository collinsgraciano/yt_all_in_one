@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  TG 章节缓存迁移 — Docker 运行脚本 (Windows CMD)
REM
REM  用法:
REM    run.bat                            （迁移所有记录）
REM    run.bat --only-uploaded            （仅迁移已上传的）
REM    run.bat --only-complete-books      （仅迁移整本完整的书）
REM    run.bat --dry-run                  （试运行）
REM    run.bat --help                     （查看帮助）
REM
REM  环境变量:
REM    SOURCE_DATABASE_URL  旧项目数据库连接串
REM    DATABASE_URL         新项目数据库连接串
REM ============================================================

cd /d "%~dp0"

REM ── 检测 docker compose ──
docker compose version >nul 2>&1
if !errorlevel! equ 0 (
    set DC=docker compose
) else (
    docker-compose version >nul 2>&1
    if !errorlevel! equ 0 (
        set DC=docker-compose
    ) else (
        echo [ERROR] 未找到 docker compose 命令，请先安装 Docker
        exit /b 1
    )
)

REM ── 源数据库 ──
if "!SOURCE_DATABASE_URL!"=="" (
    echo [WARN] SOURCE_DATABASE_URL 未设置，使用默认值 ^(host.docker.internal:5432^)
    set SOURCE_DATABASE_URL=postgresql://audiobook_app:inriynisse1991@host.docker.internal:5432/audiobook
)

REM ── 目标数据库 ──
if "!DATABASE_URL!"=="" (
    echo [ERROR] DATABASE_URL 环境变量未设置
    echo [INFO]  请设置 DATABASE_URL 指向新项目数据库，例如：
    echo         set DATABASE_URL=postgresql://audiobook_app:password@host.docker.internal:59386/audiobook
    exit /b 1
)

echo.
echo ==================================================
echo   TG 章节缓存迁移 - Docker 模式
echo ==================================================
echo   源数据库 (旧): !SOURCE_DATABASE_URL:*@=!
echo   目标库   (新): !DATABASE_URL:*@=!
if "%~1"=="" (
    echo   迁移参数:   无（迁移所有记录）
) else (
    echo   迁移参数:   %*
)
echo ==================================================
echo.

REM ── 构建镜像 ──
echo [INFO] 构建镜像...
!DC! build
if !errorlevel! neq 0 (
    echo [ERROR] 镜像构建失败
    exit /b 1
)
echo [OK]   镜像就绪
echo.

REM ── 运行迁移 ──
echo [INFO] 启动迁移容器...
!DC! run --rm migrate-tg %*
