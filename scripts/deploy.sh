#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 智能部署脚本 — 自动识别数据库模式
# ═══════════════════════════════════════════════════════════════
# 读取 .env 中的 DB_MODE 决定使用哪个 docker-compose 覆盖文件：
#   DB_MODE=self      → docker-compose.self-db.yml
#   DB_MODE=external  → docker-compose.external-db.yml
#
# 用法：
#   bash scripts/deploy.sh           # 标准部署
#   bash scripts/deploy.sh --lowmem  # 低配 VPS 部署
#   bash scripts/deploy.sh --rebuild # 强制重建镜像
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

cd "$(dirname "$0")/.."

# ─── 检测 docker compose 命令（优先 v2，避免 snap 沙箱问题）───
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif docker-compose version >/dev/null 2>&1; then
    DC="docker-compose"
else
    echo "❌ 未找到 docker compose 命令，请安装 Docker"
    exit 1
fi

# ─── 解析参数 ───
LOWMEM=false
REBUILD=false
for arg in "$@"; do
    case "$arg" in
        --lowmem) LOWMEM=true ;;
        --rebuild) REBUILD=true ;;
    esac
done

# ─── 加载 .env ───
if [ -f .env ]; then
    # 安全加载 .env（只读取变量，不执行任何代码）
    set -a
    source <(grep -v '^#' .env | grep -v '^$' | sed 's/^/export /')
    set +a
fi

DB_MODE="${DB_MODE:-self}"
SERVER_PATH="${SERVER_PATH:-/opt/audiobook}"

echo "═══════════════════════════════════════════════════"
echo "  有声书管理系统部署"
echo "  数据库模式: ${DB_MODE}"
if [ "$LOWMEM" = true ]; then
    echo "  低配模式:   已启用"
fi
echo "═══════════════════════════════════════════════════"

# ─── 组装 docker-compose 文件列表 ───
COMPOSE_FILES="-f docker-compose.yml"

if [ "$DB_MODE" = "external" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.external-db.yml"

    # 检查 EXTERNAL_DATABASE_URL 是否已设置
    if [ -z "${EXTERNAL_DATABASE_URL:-}" ]; then
        echo ""
        echo "❌ 错误：DB_MODE=external 但未设置 EXTERNAL_DATABASE_URL"
        echo "   请在 .env 文件中配置 EXTERNAL_DATABASE_URL"
        echo "   示例：EXTERNAL_DATABASE_URL=postgresql://user:pass@host:5432/audiobook"
        exit 1
    fi
    echo "  外部数据库: ${EXTERNAL_DATABASE_URL}"
else
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.self-db.yml"
    echo "  自建数据库密码: ${POSTGRES_PASSWORD:-changeme_strong_password}"
fi

if [ "$LOWMEM" = true ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.lowmem.yml"
fi

echo ""
echo "Compose 文件: $COMPOSE_FILES"

# ─── 构建镜像 ───
echo ""
echo "［1/3］构建镜像..."
if [ "$REBUILD" = true ]; then
    $DC $COMPOSE_FILES build --no-cache web
else
    $DC $COMPOSE_FILES build web
fi

# ─── 启动服务 ───
echo ""
echo "［2/3］启动服务..."
$DC $COMPOSE_FILES up -d

# ─── 等待服务就绪 ───
echo ""
echo "［3/3］等待服务启动..."
sleep 3

MAX_RETRIES=15
RETRY=0
while [ $RETRY -lt $MAX_RETRIES ]; do
    RETRY=$((RETRY + 1))
    if curl -sf "http://localhost:8080/" > /dev/null 2>&1; then
        echo "  ✅ Web 服务已就绪"
        break
    fi
    if [ $RETRY -eq $MAX_RETRIES ]; then
        echo "  ⚠️  Web 服务未在预期时间内就绪，请检查日志："
        echo "     $DC $COMPOSE_FILES logs web --tail 20"
    fi
    sleep 2
done

# ─── 显示状态 ───
echo ""
echo "─── 服务状态 ───"
$DC $COMPOSE_FILES ps

echo ""
echo "═══════════════════════════════════════════════════"
echo "  部署完成！"
echo "  访问地址: http://$(hostname -I | awk '{print $1}'):8080"
echo "  默认密码: ${APP_PASSWORD:-inriynisse}"
echo "═══════════════════════════════════════════════════"
