#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 数据迁移 — Docker 运行脚本（books + audiobook_chapters 两表）
#
# 用法:
#   bash run.sh --all                           # 迁移两张表（推荐）
#   bash run.sh --books                         # 仅迁移 books 表
#   bash run.sh --chapters --only-complete-books  # 仅迁移 chapters 表
#   bash run.sh --all --dry-run                 # 试运行
#   bash run.sh --bg --all                      # 后台运行（断开 SSH 不中断）
#   bash run.sh --help                          # 查看帮助
#
# 环境变量:
#   SOURCE_DATABASE_URL  旧项目数据库连接串
#   DATABASE_URL         新项目数据库连接串
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

cd "$(dirname "$0")"

# ── 颜色输出 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 检测 docker compose ──
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif docker-compose version >/dev/null 2>&1; then
    DC="docker-compose"
else
    error "未找到 docker compose 命令，请先安装 Docker"
    exit 1
fi

# ── 解析参数 ──
BG_MODE=false
MIGRATE_BOOKS=false
MIGRATE_CHAPTERS=false
ARGS=()

for arg in "$@"; do
    case "$arg" in
        --bg)        BG_MODE=true ;;
        --books)     MIGRATE_BOOKS=true ;;
        --chapters)  MIGRATE_CHAPTERS=true ;;
        --all)       MIGRATE_BOOKS=true; MIGRATE_CHAPTERS=true ;;
        *)           ARGS+=("$arg") ;;
    esac
done

# 默认：如果没指定任何表，迁移 chapters（兼容旧用法）
if [ "$MIGRATE_BOOKS" = false ] && [ "$MIGRATE_CHAPTERS" = false ]; then
    MIGRATE_CHAPTERS=true
fi

set -- "${ARGS[@]}"

# ── 源数据库 ──
if [ -z "${SOURCE_DATABASE_URL:-}" ]; then
    warn "SOURCE_DATABASE_URL 未设置，使用默认值 (host.docker.internal:5432)"
    export SOURCE_DATABASE_URL="postgresql://audiobook_app:inriynisse1991@host.docker.internal:5432/audiobook"
else
    SOURCE_DSN="${SOURCE_DATABASE_URL//127.0.0.1/host.docker.internal}"
    SOURCE_DSN="${SOURCE_DSN//localhost/host.docker.internal}"
    export SOURCE_DATABASE_URL="$SOURCE_DSN"
fi

# ── 目标数据库 ──
if [ -z "${DATABASE_URL:-}" ]; then
    error "DATABASE_URL 环境变量未设置"
    info '  export DATABASE_URL="postgresql://audiobook_app:your_password@host.docker.internal:5432/audiobook"'
    exit 1
fi
TARGET_DSN="${DATABASE_URL//127.0.0.1/host.docker.internal}"
TARGET_DSN="${TARGET_DSN//localhost/host.docker.internal}"
export DATABASE_URL="$TARGET_DSN"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  数据迁移 — Docker 模式"
echo "═══════════════════════════════════════════════════"
info "源数据库 (旧): ${SOURCE_DATABASE_URL#*@}"
info "目标库 (新):   ${DATABASE_URL#*@}"
info "后台运行:      ${BG_MODE}"
info "迁移 books:    ${MIGRATE_BOOKS}"
info "迁移 chapters: ${MIGRATE_CHAPTERS}"
if [ $# -gt 0 ]; then
    info "迁移参数:     $*"
else
    info "迁移参数:     (无)"
fi
echo "═══════════════════════════════════════════════════"
echo ""

# ── 构建镜像 ──
info "构建镜像..."
$DC build 2>&1 | tail -3
ok "镜像就绪"
echo ""

# ═══════════════════════════════════════════════════════════════
# 构建运行命令
# ═══════════════════════════════════════════════════════════════

run_books_script="/app/migrate_books.py"
run_chapters_script="/app/migrate_tg_chapters.py"

# 根据模式构建要运行的脚本列表
declare -a SCRIPTS
if [ "$MIGRATE_BOOKS" = true ]; then
    SCRIPTS+=("$run_books_script")
fi
if [ "$MIGRATE_CHAPTERS" = true ]; then
    SCRIPTS+=("$run_chapters_script")
fi

# ═══════════════════════════════════════════════════════════════
# 后台模式
# ═══════════════════════════════════════════════════════════════
if [ "$BG_MODE" = true ]; then
    mkdir -p logs
    LOG_FILE="logs/migrate_$(date +%Y%m%d_%H%M%S).log"

    info "后台运行模式"
    info "日志文件: $(pwd)/$LOG_FILE"
    info ""

    {
        echo "═══════════════════════════════════════════════════"
        echo "  数据迁移 — 后台运行"
        echo "  启动时间: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "  源数据库: ${SOURCE_DATABASE_URL#*@}"
        echo "  目标库:   ${DATABASE_URL#*@}"
        echo "  迁移 books:    ${MIGRATE_BOOKS}"
        echo "  迁移 chapters: ${MIGRATE_CHAPTERS}"
        echo "  参数:     $*"
        echo "  PID:      $$"
        echo "═══════════════════════════════════════════════════"
        echo ""
    } > "$LOG_FILE"

    # 构建完整的运行脚本（多表顺序执行）
    RUN_CMD=""
    for script in "${SCRIPTS[@]}"; do
        if [ -n "$RUN_CMD" ]; then
            RUN_CMD="$RUN_CMD && "
        fi
        RUN_CMD="${RUN_CMD}python $script $*"
    done

    info "执行命令: $RUN_CMD"

    # 用 docker compose run -d 启动 detached 容器
    # 用 bash -c 包裹多脚本顺序执行
    CONTAINER_ID=$($DC run -d --rm migrate-tg bash -c "$RUN_CMD" 2>&1)
    CONTAINER_ID=$(echo "$CONTAINER_ID" | tail -1 | tr -d '[:space:]')

    if [ -z "$CONTAINER_ID" ] || [[ "$CONTAINER_ID" == Error* ]]; then
        error "容器启动失败: $CONTAINER_ID"
        cat "$LOG_FILE"
        exit 1
    fi

    ok "容器已启动: $CONTAINER_ID"
    info ""
    info "═══════════════════════════════════════════════════"
    info "  迁移正在后台运行，你可以断开 SSH 了"
    info "═══════════════════════════════════════════════════"
    info ""
    info "查看实时日志:"
    info "  docker logs -f $CONTAINER_ID"
    info ""
    info "查看日志文件:"
    info "  tail -f $(pwd)/$LOG_FILE"
    info "═══════════════════════════════════════════════════"

    # 后台跟随容器日志写入文件
    nohup docker logs -f "$CONTAINER_ID" >> "$LOG_FILE" 2>&1 &
    LOG_PID=$!

    # 等待容器结束
    EXIT_CODE=$(docker wait "$CONTAINER_ID" 2>/dev/null || echo "1")

    kill "$LOG_PID" 2>/dev/null || true
    wait "$LOG_PID" 2>/dev/null || true

    {
        echo ""
        echo "═══════════════════════════════════════════════════"
        echo "  迁移结束时间: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "  退出码: $EXIT_CODE"
        if [ "$EXIT_CODE" = "0" ]; then
            echo "  状态: 成功 ✓"
        else
            echo "  状态: 失败 (退出码 $EXIT_CODE)"
        fi
        echo "═══════════════════════════════════════════════════"
    } >> "$LOG_FILE"

    echo ""
    if [ "$EXIT_CODE" = "0" ]; then
        ok "迁移完成！日志已保存到: $(pwd)/$LOG_FILE"
        info "查看结果: tail -20 $(pwd)/$LOG_FILE"
    else
        error "迁移失败 (退出码 $EXIT_CODE)，日志: $(pwd)/$LOG_FILE"
        info "查看错误: tail -50 $(pwd)/$LOG_FILE"
    fi
    exit "$EXIT_CODE"

# ═══════════════════════════════════════════════════════════════
# 前台模式
# ═══════════════════════════════════════════════════════════════
else
    info "前台运行模式"
    info ""

    for script in "${SCRIPTS[@]}"; do
        info ">>> 运行: python $script $*"
        $DC run --rm migrate-tg python "$script" "$@"
        info ">>> $script 完成"
        info ""
    done

    ok "全部迁移完成！"
fi
