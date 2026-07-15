#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# TG 章节缓存迁移 — Docker 运行脚本
#
# 用法:
#   bash run.sh                            # 迁移所有记录
#   bash run.sh --only-uploaded            # 仅迁移已上传的章节
#   bash run.sh --only-complete-books      # 仅迁移整本完整的书
#   bash run.sh --dry-run                  # 试运行
#   bash run.sh --help                     # 查看帮助
#
#   bash run.sh --bg --only-complete-books  # 后台运行（断开 SSH 不中断）
#   tail -f logs/migrate_*.log             # 查看后台日志
#
# 环境变量:
#   SOURCE_DATABASE_URL  旧项目数据库连接串（默认: host.docker.internal:5432）
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

# ── 解析 --bg 参数 ──
BG_MODE=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--bg" ]; then
        BG_MODE=true
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]}"

# ── 源数据库 (旧项目) ──
# 默认通过 host.docker.internal 访问宿主机上的旧库
if [ -z "${SOURCE_DATABASE_URL:-}" ]; then
    warn "SOURCE_DATABASE_URL 未设置，使用默认值 (host.docker.internal:5432)"
    export SOURCE_DATABASE_URL="postgresql://audiobook_app:inriynisse1991@host.docker.internal:5432/audiobook"
else
    # 替换 127.0.0.1 / localhost 为 host.docker.internal
    SOURCE_DSN="${SOURCE_DATABASE_URL//127.0.0.1/host.docker.internal}"
    SOURCE_DSN="${SOURCE_DSN//localhost/host.docker.internal}"
    export SOURCE_DATABASE_URL="$SOURCE_DSN"
fi

# ── 目标数据库 (新项目) ──
if [ -z "${DATABASE_URL:-}" ]; then
    error "DATABASE_URL 环境变量未设置"
    info "请设置 DATABASE_URL 指向新项目数据库，例如："
    info '  export DATABASE_URL="postgresql://audiobook_app:your_password@host.docker.internal:5432/audiobook"'
    exit 1
fi
# 替换 127.0.0.1 / localhost 为 host.docker.internal
TARGET_DSN="${DATABASE_URL//127.0.0.1/host.docker.internal}"
TARGET_DSN="${TARGET_DSN//localhost/host.docker.internal}"
export DATABASE_URL="$TARGET_DSN"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  TG 章节缓存迁移 — Docker 模式"
echo "═══════════════════════════════════════════════════"
info "源数据库 (旧): ${SOURCE_DATABASE_URL#*@}"
info "目标库 (新):   ${DATABASE_URL#*@}"
info "后台运行:      ${BG_MODE}"
if [ $# -gt 0 ]; then
    info "迁移参数:     $*"
else
    info "迁移参数:     (无 — 迁移所有记录)"
fi
echo "═══════════════════════════════════════════════════"
echo ""

# ── 构建镜像 ──
info "构建镜像 (首次运行需下载基础镜像)..."
$DC build 2>&1 | tail -3
ok "镜像就绪"
echo ""

# ═══════════════════════════════════════════════════════════════
# 后台模式：断开 SSH 也不中断，日志自动保存到文件
# ═══════════════════════════════════════════════════════════════
if [ "$BG_MODE" = true ]; then
    mkdir -p logs
    LOG_FILE="logs/migrate_$(date +%Y%m%d_%H%M%S).log"

    info "后台运行模式"
    info "日志文件: $(pwd)/$LOG_FILE"
    info ""

    # 先写入头部信息到日志文件
    {
        echo "═══════════════════════════════════════════════════"
        echo "  TG 章节缓存迁移 — 后台运行"
        echo "  启动时间: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "  源数据库: ${SOURCE_DATABASE_URL#*@}"
        echo "  目标库:   ${DATABASE_URL#*@}"
        echo "  参数:     $*"
        echo "  PID:      $$"
        echo "═══════════════════════════════════════════════════"
        echo ""
    } > "$LOG_FILE"

    # 后台启动容器，日志同时写入文件
    # 用 nohup + docker logs 的方式确保断开 SSH 不中断
    info "启动后台容器..."

    # 先用 docker compose run -d 启动（detached 模式）
    CONTAINER_ID=$($DC run -d --rm migrate-tg "$@" 2>&1)
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
    info ""
    info "检查容器状态:"
    info "  docker ps -a --filter id=$CONTAINER_ID"
    info "═══════════════════════════════════════════════════"

    # 在后台跟随容器日志写入文件
    # docker logs -f 会阻塞直到容器退出
    nohup docker logs -f "$CONTAINER_ID" >> "$LOG_FILE" 2>&1 &
    LOG_PID=$!

    # 等待容器结束
    EXIT_CODE=$(docker wait "$CONTAINER_ID" 2>/dev/null || echo "1")

    # 停止日志跟随
    kill "$LOG_PID" 2>/dev/null || true
    wait "$LOG_PID" 2>/dev/null || true

    # 写入尾部信息
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
# 前台模式：直接运行，输出到终端
# ═══════════════════════════════════════════════════════════════
else
    info "前台运行模式"
    info "启动迁移容器..."
    $DC run --rm migrate-tg "$@"
fi
