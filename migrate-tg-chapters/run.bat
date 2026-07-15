@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  数据迁移 — Docker 运行脚本 (Windows CMD)
REM  支持 books + audiobook_chapters 两表迁移
REM
REM  用法:
REM    run.bat --all                           （迁移两张表）
REM    run.bat --books                         （仅迁移 books 表）
REM    run.bat --chapters --only-complete-books （仅迁移 chapters 表）
REM    run.bat --all --dry-run                 （试运行）
REM    run.bat --bg --all                      （后台运行）
REM    run.bat --help                          （查看帮助）
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

REM ── 解析参数 ──
set BG_MODE=false
set MIGRATE_BOOKS=false
set MIGRATE_CHAPTERS=false
set ARGS=

:parse_args
if "%~1"=="" goto parse_done
if "%~1"=="--bg" (
    set BG_MODE=true
    shift
    goto parse_args
)
if "%~1"=="--books" (
    set MIGRATE_BOOKS=true
    shift
    goto parse_args
)
if "%~1"=="--chapters" (
    set MIGRATE_CHAPTERS=true
    shift
    goto parse_args
)
if "%~1"=="--all" (
    set MIGRATE_BOOKS=true
    set MIGRATE_CHAPTERS=true
    shift
    goto parse_args
)
set ARGS=!ARGS! %~1
shift
goto parse_args
:parse_done

REM 默认：没指定表则迁移 chapters
if "!MIGRATE_BOOKS!"=="false" if "!MIGRATE_CHAPTERS!"=="false" (
    set MIGRATE_CHAPTERS=true
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
    echo         set DATABASE_URL=postgresql://audiobook_app:password@host.docker.internal:5432/audiobook
    exit /b 1
)

echo.
echo ==================================================
echo   数据迁移 - Docker 模式
echo ==================================================
echo   源数据库 (旧): !SOURCE_DATABASE_URL:*@=!
echo   目标库   (新): !DATABASE_URL:*@=!
echo   后台运行:       !BG_MODE!
echo   迁移 books:    !MIGRATE_BOOKS!
echo   迁移 chapters: !MIGRATE_CHAPTERS!
if "!ARGS!"=="" (
    echo   迁移参数:   无
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

REM ── 构建脚本列表 ──
set SCRIPT_LIST=
if "!MIGRATE_BOOKS!"=="true" (
    set SCRIPT_LIST=!SCRIPT_LIST! /app/migrate_books.py
)
if "!MIGRATE_CHAPTERS!"=="true" (
    set SCRIPT_LIST=!SCRIPT_LIST! /app/migrate_tg_chapters.py
)

REM ── 后台模式 ──
if "!BG_MODE!"=="true" (
    if not exist logs mkdir logs

    for /f "tokens=2 delims==" %%a in ('wmic os get localdatetime /value 2^>nul ^| find "="') do set _dt=%%a
    set LOG_FILE=logs\migrate_!_dt:~0,8 !_dt:~8,4!.log

    echo [INFO] 后台运行模式
    echo [INFO] 日志文件: %cd%\!LOG_FILE!
    echo.

    REM 构建运行命令
    set RUN_CMD=
    for %%s in (!SCRIPT_LIST!) do (
        if "!RUN_CMD!"=="" (
            set RUN_CMD=python %%s !ARGS!
        ) else (
            set RUN_CMD=!RUN_CMD! ^&^& python %%s !ARGS!
        )
    )

    echo [INFO] 执行: !RUN_CMD!

    for /f "delims=" %%i in ('!DC! run -d --rm migrate-tg bash -c "!RUN_CMD!" 2^>^&1') do set CONTAINER_ID=%%i

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

    start /b docker logs -f !CONTAINER_ID! > !LOG_FILE! 2>&1

    :wait_loop
    docker inspect --format "{{.State.Status}}" !CONTAINER_ID! 2>nul | find "exited" >nul
    if !errorlevel! equ 0 goto container_done
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

REM ── 前台模式：顺序执行多个脚本 ──
echo [INFO] 前台运行模式
echo.

for %%s in (!SCRIPT_LIST!) do (
    echo [INFO] 运行: python %%s !ARGS!
    !DC! run --rm migrate-tg python %%s !ARGS!
    if !errorlevel! neq 0 (
        echo [ERROR] %%s 执行失败
        exit /b 1
    )
    echo [INFO] %%s 完成
    echo.
)

echo [OK] 全部迁移完成！
