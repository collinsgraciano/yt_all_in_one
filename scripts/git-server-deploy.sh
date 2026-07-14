#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  服务器端部署脚本
#  在服务器上手动运行：git pull → 智能构建 → 重启 → 健康检查
#
#  用法（SSH 登录服务器后）：
#    cd /root/audiobook
#    bash scripts/git-server-deploy.sh
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SERVER_PATH="$(cd "$(dirname "$0")/.." && pwd)"
cd "${SERVER_PATH}"

echo "═══════════════════════════════════════════════════════════"
echo "  部署开始 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  路径: ${SERVER_PATH}"
echo "═══════════════════════════════════════════════════════════"

# ─── 1. 拉取最新代码 ───
echo "[1/4] git pull..."
git pull
echo "  当前版本: $(git rev-parse --short HEAD)"
echo ""

# ─── 2. 检查 .env ───
echo "[2/4] 检查 .env..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "  [!] 已从 .env.example 创建 .env，请编辑后重新运行"
        echo "      nano .env"
        exit 1
    else
        echo "  [x] .env 不存在，请手动创建"
        exit 1
    fi
else
    echo "  .env OK"
fi
echo ""

# ─── 3. 智能构建与重启 ───
echo "[3/4] Docker 构建..."

# 读数据库模式
DB_MODE="$(grep -E '^DB_MODE=' .env 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo 'self')"
COMPOSE="-f docker-compose.yml"
if [ "$DB_MODE" = "external" ]; then
    COMPOSE="$COMPOSE -f docker-compose.external-db.yml"
else
    COMPOSE="$COMPOSE -f docker-compose.self-db.yml"
fi
echo "  数据库模式: ${DB_MODE}"

# 判断是否需要重建镜像
NEED_BUILD=false

REQ_HASH_FILE=".cache_req_hash"
CUR_REQ_HASH=$(md5sum requirements.txt 2>/dev/null | awk '{print $1}' || echo "none")
LAST_REQ_HASH=$(cat "$REQ_HASH_FILE" 2>/dev/null || echo "")

if [ "$CUR_REQ_HASH" != "$LAST_REQ_HASH" ]; then
    NEED_BUILD=true
    echo "  > requirements.txt 有变更"
fi

DOCKER_HASH_FILE=".cache_docker_hash"
CUR_DOCKER_HASH=$(cat docker/Dockerfile.web 2>/dev/null | md5sum | awk '{print $1}' || echo "none")
LAST_DOCKER_HASH=$(cat "$DOCKER_HASH_FILE" 2>/dev/null || echo "")

if [ "$CUR_DOCKER_HASH" != "$LAST_DOCKER_HASH" ]; then
    NEED_BUILD=true
    echo "  > Dockerfile 有变更"
fi

if [ "$NEED_BUILD" = true ]; then
    echo "  正在构建镜像..."
    docker-compose $COMPOSE build
    echo "$CUR_REQ_HASH" > "$REQ_HASH_FILE"
    echo "$CUR_DOCKER_HASH" > "$DOCKER_HASH_FILE"
else
    echo "  依赖未变更，跳过构建"
fi

echo "  重启服务..."
docker-compose $COMPOSE up -d
echo ""

# ─── 4. 健康检查 ───
echo "[4/4] 等待服务就绪..."
sleep 3
for i in $(seq 1 15); do
    if curl -sf http://localhost:8080/ >/dev/null 2>&1; then
        echo "  ✓ 服务就绪"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "  [!] 服务未就绪，查看日志:"
        echo "      docker-compose $COMPOSE logs --tail 20"
    fi
    sleep 2
done

echo ""
echo "─── 服务状态 ───"
docker-compose $COMPOSE ps

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  部署完成 — $(date '+%H:%M:%S')"
echo "  访问: http://$(hostname -I | awk '{print $1}'):8080"
echo "═══════════════════════════════════════════════════════════"
