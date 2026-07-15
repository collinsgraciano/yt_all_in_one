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
REM    run.bat --bg --only-complete-books （后台运行）
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

REM ── 解析 --bg 参数 ──
set BG_MODE=false
set ARGS=
:parse_args
if "%~1"=="" goto parse_done
if "%~1"=="--bg" (
    set BG_MODE=true
    shift
    goto parse_args
)
set ARGS=!ARGS! %~1
shift
goto parse_args
:parse_done

REM ── 源数据库 ──
if "!SOURCE_DATABASE_URL!"=="" (
    echo [WARN] SOURCE_DATABASE_URL 未设置，使用默认值 ^(host.docker.internal:5432^)
    set SOURCE_DATABASE_URL=postgresql://audiobook_app:inriynisse1991@host.docker.internal:5432/audiobook
)

REM ── 目标数据库 ──
if "!DATABASE_URL!"=="" (
    echo [ERROR] DATABASE_URL 环境变量未设置
    echo [INFO]  请设置 DATABASE_URL 指向新项目数据库，例如：
    echo         set DATABASE_URL=postgresql://audiobook_app:password@host.docker.internal:5432/audiobook
    exit /b 1
)

echo.
echo ==================================================
echo   TG 章节缓存迁移 - Docker 模式
echo ==================================================
echo   源数据库 (旧): !SOURCE_DATABASE_URL:*@=!
echo   目标库   (新): !DATABASE_URL:*@=!
echo   后台运行:       !BG_MODE!
if "!ARGS!"=="" (
    echo   迁移参数:   无（迁移所有记录）
) else (
    echo   迁移参数:  !ARGS!
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

REM ── 后台模式 ──
if "!BG_MODE!"=="true" (
    if not exist logs mkdir logs

    for /f "tokens=2 delims==" %%a in ('wmic os get localdatetime /value 2^>nul ^| find "="') do set _dt=%%a
    set LOG_FILE=logs\migrate_!_dt:~0,8 !__dt:~8,4!.log

    echo [INFO] 后台运行模式
    echo [INFO] 日志文件: %cd%\!LOG_FILE!
    echo.

    REM 启动 detached 容器
    for /f "delims=" %%i in ('!DC! run -d --rm migrate-tg !ARGS! 2^>^&1') do set CONTAINER_ID=%%i

    echo [OK]   容器已启动: !CONTAINER_ID!
    echo.
    echo ==================================================
    echo   迁移正在后台运行
    echo ==================================================
    echo.
    echo   查看实时日志:
    echo     docker logs -f !CONTAINER_ID!
    echo.
    echo   查看日志文件:
    echo     type %cd%\!LOG_FILE!
    echo ==================================================

    REM 跟随日志写入文件
    start /b docker logs -f !CONTAINER_ID! > !LOG_FILE! 2>&1

    REM 等待容器结束
    :wait_loop
    docker inspect --format "{{.State.Status}}" !CONTAINER_ID! 2>nul | find "exited" >nul
    if !errorlevel! equ 0 (
        goto container_done
    )
    timeout /t 2 /nobreak >nul
    goto wait_loop

    :container_done
    for /f "delims=" %%i in ('docker inspect --format "{{.State.ExitCode}}" !CONTAINER_ID! 2^>nul') do set EXIT_CODE=%%i
    if "!EXIT_CODE!"=="" set EXIT_CODE=1

    echo.
    if "!EXIT_CODE!"=="0" (
        echo [OK]   迁移完成！日志: %cd%\!LOG_FILE!
    ) else (
        echo [ERROR] 迁移失败 (退出码 !EXIT_CODE!)，日志: %cd%\!LOG_FILE!
    )
    exit /b !EXIT_CODE!
)

REM ── 前台模式 ──
echo [INFO] 前台运行模式
echo [INFO] 启动迁移容器...
!DC! run --rm migrate-tg !ARGS!
