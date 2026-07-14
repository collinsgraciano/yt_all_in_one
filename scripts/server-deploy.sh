#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 服务器端部署/更新脚本 — 在远程服务器上执行
# 功能：解压代码 → 智能判断是否需要重建镜像 → 重启服务
# ═══════════════════════════════════════════════════════════════
# 此脚本由 sync-to-server.bat 通过 SSH 自动调用，
# 也可在服务器上手动执行：
#   bash server-deploy.sh
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ─── 配置 ───
SERVER_PATH="${SERVER_PATH:-/opt/audiobook}"
TAR_FILE="/tmp/audiobook_deploy.tar"
DEPLOY_LOG="/tmp/audiobook_deploy.log"

echo "═══════════════════════════════════════════════════════════"
echo "  服务器端部署 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  项目路径: ${SERVER_PATH}"
echo "═══════════════════════════════════════════════════════════"

# ─── Step 1: 准备项目目录 ───
echo "[Step 1/6] 准备项目目录..."
mkdir -p "${SERVER_PATH}"
cd "${SERVER_PATH}"

# 如果是首次部署，初始化目录结构
if [ ! -f "docker-compose.yml" ]; then
    echo "  首次部署，创建初始目录..."
    mkdir -p backend docker pipeline scripts
fi

# ─── Step 2: 备份当前版本 ───
echo "[Step 2/6] 备份当前版本..."
if [ -f "docker-compose.yml" ] && [ -d "backend" ]; then
    BACKUP_DIR="${SERVER_PATH}.backup.$(date '+%Y%m%d_%H%M%S')"
    cp -r backend pipeline docker-compose.yml requirements.txt "${BACKUP_DIR}/" 2>/dev/null || true
    echo "  备份至: ${BACKUP_DIR}"
    # 只保留最近 3 个备份
    ls -d "${SERVER_PATH}.backup."* 2>/dev/null | head -n -3 | xargs rm -rf 2>/dev/null || true
else
    echo "  无需备份（首次部署）"
fi

# ─── Step 3: 解压新代码 ───
echo "[Step 3/6] 解压新代码..."
if [ -f "${TAR_FILE}" ]; then
    # 解压到临时目录，然后同步
    TMP_DIR=$(mktemp -d)
    tar xf "${TAR_FILE}" -C "${TMP_DIR}"

    # 同步文件（保留 .env 等本地文件）
    rsync -av --delete \
        --exclude='.env' \
        --exclude='.env.deploy' \
        --exclude='output_data/' \
        --exclude='music_data/' \
        "${TMP_DIR}/" "${SERVER_PATH}/"

    rm -rf "${TMP_DIR}"
    rm -f "${TAR_FILE}"
    echo "  代码已更新"
else
    echo "  [警告] 未找到上传的代码包，使用现有代码"
fi

# ─── Step 4: 确保 .env 存在 ───
echo "[Step 4/6] 检查环境配置..."
if [ ! -f "${SERVER_PATH}/.env" ]; then
    if [ -f "${SERVER_PATH}/.env.example" ]; then
        cp "${SERVER_PATH}/.env.example" "${SERVER_PATH}/.env"
        echo "  [警告] 已从 .env.example 创建 .env，请编辑配置！"
        echo "         编辑命令: nano ${SERVER_PATH}/.env"
    else
        echo "  [错误] .env 文件不存在，请手动创建"
        exit 1
    fi
else
    echo "  .env 已存在"
fi

# ─── Step 5: 读取数据库模式 ───
echo "[Step 5/6] 检测数据库模式..."
DB_MODE="$(grep -E '^DB_MODE=' "${SERVER_PATH}/.env" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo 'self')"
if [ -z "$DB_MODE" ]; then
    DB_MODE="self"
fi
echo "  数据库模式: ${DB_MODE}"

# 根据数据库模式选择 compose 文件
COMPOSE_FILES="-f docker-compose.yml"
if [ "$DB_MODE" = "external" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.external-db.yml"
    # 检查 EXTERNAL_DATABASE_URL
    EXT_URL="$(grep -E '^EXTERNAL_DATABASE_URL=' "${SERVER_PATH}/.env" 2>/dev/null | cut -d'=' -f2- || echo '')"
    if [ -z "$EXT_URL" ]; then
        echo "  [错误] DB_MODE=external 但未设置 EXTERNAL_DATABASE_URL"
        echo "         请编辑 ${SERVER_PATH}/.env 添加外部数据库连接串"
        exit 1
    fi
    echo "  外部数据库: ${EXT_URL}"
else
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.self-db.yml"
fi

# ─── Step 6: 智能构建与重启 ───
echo "[Step 6/6] 构建与重启服务..."
echo "  Compose: $COMPOSE_FILES"

# 检查是否需要重建镜像（依赖文件变更时）
NEED_REBUILD=false

# 对比 requirements.txt 的哈希
if [ -f "${SERVER_PATH}/.last_requirements_hash" ]; then
    CURRENT_HASH=$(md5sum "${SERVER_PATH}/requirements.txt" | awk '{print $1}')
    LAST_HASH=$(cat "${SERVER_PATH}/.last_requirements_hash")
    if [ "${CURRENT_HASH}" != "${LAST_HASH}" ]; then
        NEED_REBUILD=true
        echo "  检测到 requirements.txt 变更，需要重建镜像"
    fi
else
    NEED_REBUILD=true
    echo "  首次部署，需要构建镜像"
fi

# 对比 Dockerfile 的哈希
DOCKERFILE_HASH=$(cat "${SERVER_PATH}/docker/Dockerfile.web" 2>/dev/null | md5sum | awk '{print $1}')
if [ -f "${SERVER_PATH}/.last_dockerfile_hash" ]; then
    LAST_DOCKERFILE_HASH=$(cat "${SERVER_PATH}/.last_dockerfile_hash")
    if [ "${DOCKERFILE_HASH}" != "${LAST_DOCKERFILE_HASH}" ]; then
        NEED_REBUILD=true
        echo "  检测到 Dockerfile 变更，需要重建镜像"
    fi
fi

cd "${SERVER_PATH}"

if [ "$NEED_REBUILD" = true ]; then
    echo "  正在构建镜像（可能需要几分钟）..."
    docker-compose $COMPOSE_FILES build 2>&1 | tail -5

    # 记录哈希
    md5sum requirements.txt | awk '{print $1}' > .last_requirements_hash
    echo "${DOCKERFILE_HASH}" > .last_dockerfile_hash
else
    echo "  依赖未变更，跳过镜像构建（秒级更新）"
fi

# 重启服务
echo "  正在重启服务..."
docker-compose $COMPOSE_FILES up -d

# 等待服务就绪
echo "  等待服务启动..."
sleep 3

# 健康检查
MAX_RETRIES=15
RETRY=0
while [ $RETRY -lt $MAX_RETRIES ]; do
    RETRY=$((RETRY + 1))
    if curl -sf "http://localhost:8080/" > /dev/null 2>&1; then
        echo "  ✓ Web 服务已就绪"
        break
    fi
    if [ $RETRY -eq $MAX_RETRIES ]; then
        echo "  [警告] Web 服务未在预期时间内就绪，请检查日志："
        echo "         docker-compose $COMPOSE_FILES logs web --tail 20"
    fi
    sleep 2
done

# 显示服务状态
echo ""
echo "─── 服务状态 ───"
docker-compose $COMPOSE_FILES ps

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  部署完成！"
echo "  数据库模式: ${DB_MODE}"
echo "  访问地址: http://$(hostname -I | awk '{print $1}'):8080"
echo "  API 文档: http://$(hostname -I | awk '{print $1}'):8080/api/docs"
echo "═══════════════════════════════════════════════════════════"
