#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  VPS 中继调度器 — 快速部署脚本
#  在服务器上运行：构建 → 重启 → 健康检查
#
#  用法（SSH 登录服务器后）：
#    cd /root/audiobook/hf_workers/vps_relay
#    bash deploy.sh
#
#  首次部署前，请先编辑 docker-compose.yml 中的环境变量：
#    - POSTGRES_DSN
#    - WORKER_URLS
#    - WEB_PASSWORD（可选）
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

# ─── 检测 docker compose 命令（优先 v2，避免 snap 沙箱问题）───
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif docker-compose version >/dev/null 2>&1; then
    DC="docker-compose"
else
    echo "  [x] 未找到 docker compose 命令，请安装 Docker"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════"
echo "  VPS 中继调度器部署 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  路径: ${SCRIPT_DIR}"
echo "═══════════════════════════════════════════════════════════"

# ─── 1. 检查 docker-compose.yml ───
echo "[1/4] 检查配置文件..."
if [ ! -f docker-compose.yml ]; then
    echo "  [x] docker-compose.yml 不存在"
    exit 1
fi
echo "  docker-compose.yml OK"

# 检查是否还是默认配置
if grep -q "your_password" docker-compose.yml 2>/dev/null; then
    echo "  [!] docker-compose.yml 中仍包含默认密码，请先编辑："
    echo "      nano docker-compose.yml"
    echo "      修改 POSTGRES_DSN / WORKER_URLS 等变量后重新运行"
    exit 1
fi
echo ""

# ─── 2. 智能构建 ───
echo "[2/4] Docker 构建..."

# 判断是否需要重建镜像
NEED_BUILD=false

# 检查 1: requirements.txt
REQ_HASH_FILE=".cache_req_hash"
CUR_REQ_HASH=$(md5sum requirements.txt 2>/dev/null | awk '{print $1}' || echo "none")
LAST_REQ_HASH=$(cat "$REQ_HASH_FILE" 2>/dev/null || echo "")
if [ "$CUR_REQ_HASH" != "$LAST_REQ_HASH" ]; then
    NEED_BUILD=true
    echo "  > requirements.txt 有变更"
fi

# 检查 2: Dockerfile
DOCKER_HASH_FILE=".cache_docker_hash"
CUR_DOCKER_HASH=$(md5sum Dockerfile 2>/dev/null | awk '{print $1}' || echo "none")
LAST_DOCKER_HASH=$(cat "$DOCKER_HASH_FILE" 2>/dev/null || echo "")
if [ "$CUR_DOCKER_HASH" != "$LAST_DOCKER_HASH" ]; then
    NEED_BUILD=true
    echo "  > Dockerfile 有变更"
fi

# 检查 3: app.py 源码变更
SRC_HASH_FILE=".cache_src_hash"
CUR_SRC_HASH=$(md5sum app.py 2>/dev/null | awk '{print $1}' || echo "none")
LAST_SRC_HASH=$(cat "$SRC_HASH_FILE" 2>/dev/null || echo "")
if [ "$CUR_SRC_HASH" != "$LAST_SRC_HASH" ]; then
    NEED_BUILD=true
    echo "  > app.py 有变更"
fi

if [ "$NEED_BUILD" = true ]; then
    echo "  正在构建镜像..."
    $DC build
    echo "$CUR_REQ_HASH" > "$REQ_HASH_FILE"
    echo "$CUR_DOCKER_HASH" > "$DOCKER_HASH_FILE"
    echo "$CUR_SRC_HASH" > "$SRC_HASH_FILE"
else
    echo "  依赖与源码均未变更，跳过构建"
fi
echo ""

# ─── 3. 重启服务 ───
echo "[3/4] 重启服务..."
$DC up -d
echo ""

# ─── 4. 健康检查 ───
echo "[4/4] 等待服务就绪..."
WEB_PORT=$(grep -E 'WEB_PORT' docker-compose.yml 2>/dev/null | head -1 | cut -d'=' -f2 | tr -d ' ' || echo "38080")
if [ -z "$WEB_PORT" ]; then
    WEB_PORT="38080"
fi

sleep 3
for i in $(seq 1 15); do
    if curl -sf "http://localhost:${WEB_PORT}/api/status" >/dev/null 2>&1; then
        echo "  ✓ 服务就绪"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "  [!] 服务未就绪，查看日志:"
        echo "      $DC logs --tail 30"
    fi
    sleep 2
done

echo ""
echo "─── 服务状态 ───"
$DC ps

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  部署完成 — $(date '+%H:%M:%S')"
echo "  访问: http://$(hostname -I | awk '{print $1}'):${WEB_PORT}"
echo "═══════════════════════════════════════════════════════════"
