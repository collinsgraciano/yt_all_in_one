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
    info '  export DATABASE_URL="postgresql://audiobook_app:your_password@host.docker.internal:59386/audiobook"'
    info "  或在旧项目的 .env 中设置后运行"
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

# ── 运行迁移 ──
info "启动迁移容器..."
$DC run --rm migrate-tg "$@"
