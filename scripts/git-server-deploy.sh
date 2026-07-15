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
echo "  部署开始 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  路径: ${SERVER_PATH}"
echo "═══════════════════════════════════════════════════════════"

# ─── 1. 拉取最新代码 ───
echo "[1/4] git pull..."
# 记录 pull 前脚本自身的 hash
OLD_SCRIPT_HASH=$(md5sum "$0" 2>/dev/null | awk '{print $1}' || echo "")
git pull
NEW_SCRIPT_HASH=$(md5sum "$0" 2>/dev/null | awk '{print $1}' || echo "")
echo "  当前版本: $(git rev-parse --short HEAD)"
# 如果脚本自身被 git pull 更新了，用新版本重新执行
if [ "$OLD_SCRIPT_HASH" != "$NEW_SCRIPT_HASH" ]; then
    echo "  > 部署脚本自身有更新，自动重新执行新版本..."
    exec bash "$0" "$@"
fi
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

# ─── 2.5 预下载 DeepFilter 二进制（持久化到宿主机）───
echo "[2.5/4] 检查 DeepFilter 二进制..."
DEEPFILTER_DIR="${SERVER_PATH}/data/deepfilter"
DEEPFILTER_BIN="deep-filter-0.5.6-x86_64-unknown-linux-musl"
DEEPFILTER_URL="https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl"

mkdir -p "$DEEPFILTER_DIR"
if [ -f "$DEEPFILTER_DIR/$DEEPFILTER_BIN" ]; then
    echo "  ✓ DeepFilter 已在宿主机缓存中"
else
    echo "  > 下载 DeepFilter 到 $DEEPFILTER_DIR ..."
    if wget --tries=5 --timeout=30 --retry-connrefused \
        "$DEEPFILTER_URL" -O "$DEEPFILTER_DIR/$DEEPFILTER_BIN"; then
        chmod +x "$DEEPFILTER_DIR/$DEEPFILTER_BIN"
        echo "  ✓ DeepFilter 下载完成（后续重建镜像不再重复下载）"
    else
        echo "  [!] DeepFilter 下载失败，容器启动时会自动重试"
    fi
fi
echo ""

# ─── 2.6 预下载 BGM 音乐库（持久化到宿主机）───
echo "[2.6/4] 检查 BGM 音乐库..."
MUSIC_DIR="${SERVER_PATH}/data/music"
MUSIC_ZIP_URL="https://huggingface.co/datasets/oooooo1323/cm/resolve/main/Parisian%20Breeze.zip"
MUSIC_ARCHIVE="${MUSIC_DIR}/Parisian_Breeze.zip"

mkdir -p "$MUSIC_DIR"
EXISTING_MUSIC=$(find "$MUSIC_DIR" -type f \( -iname "*.mp3" -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.m4a" -o -iname "*.ogg" -o -iname "*.aac" -o -iname "*.wma" \) 2>/dev/null | wc -l)
if [ "$EXISTING_MUSIC" -gt 0 ]; then
    echo "  ✓ BGM 音乐库已有 ${EXISTING_MUSIC} 个音频文件"
else
    echo "  > 下载 BGM 音乐包到 $MUSIC_DIR ..."
    if wget --tries=5 --timeout=60 --retry-connrefused \
        "$MUSIC_ZIP_URL" -O "$MUSIC_ARCHIVE"; then
        echo "  > 解压音乐包..."
        unzip -o -q "$MUSIC_ARCHIVE" -d "$MUSIC_DIR" 2>/dev/null || python3 -c "
import zipfile, sys
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])
" "$MUSIC_ARCHIVE" "$MUSIC_DIR"
        rm -f "$MUSIC_ARCHIVE"
        MUSIC_COUNT=$(find "$MUSIC_DIR" -type f \( -iname "*.mp3" -o -iname "*.wav" -o -iname "*.flac" -o -iname "*.m4a" -o -iname "*.ogg" -o -iname "*.aac" -o -iname "*.wma" \) 2>/dev/null | wc -l)
        echo "  ✓ BGM 音乐库下载完成，共 ${MUSIC_COUNT} 个音频文件"
    else
        echo "  [!] BGM 音乐下载失败，容器启动时会自动重试"
    fi
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
CUR_DOCKER_HASH=$(cat docker/Dockerfile.web 2>/dev/null | md5sum | awk '{print $1}' || echo "none")
LAST_DOCKER_HASH=$(cat "$DOCKER_HASH_FILE" 2>/dev/null || echo "")
if [ "$CUR_DOCKER_HASH" != "$LAST_DOCKER_HASH" ]; then
    NEED_BUILD=true
    echo "  > Dockerfile 有变更"
fi

# 检查 3: backend/ 和 pipeline/ 源码变更（.py, .html, .sql, .j2）
SRC_HASH_FILE=".cache_src_hash"
CUR_SRC_HASH=$(find backend/ pipeline/ docker/ -type f \( -name '*.py' -o -name '*.html' -o -name '*.sql' -o -name '*.j2' -o -name '*.txt' -o -name '*.sh' \) 2>/dev/null | sort | xargs cat 2>/dev/null | md5sum | awk '{print $1}' || echo "none")
LAST_SRC_HASH=$(cat "$SRC_HASH_FILE" 2>/dev/null || echo "")
if [ "$CUR_SRC_HASH" != "$LAST_SRC_HASH" ]; then
    NEED_BUILD=true
    echo "  > 源代码有变更"
fi

if [ "$NEED_BUILD" = true ]; then
    echo "  正在构建镜像..."
    $DC $COMPOSE build
    echo "$CUR_REQ_HASH" > "$REQ_HASH_FILE"
    echo "$CUR_DOCKER_HASH" > "$DOCKER_HASH_FILE"
    echo "$CUR_SRC_HASH" > "$SRC_HASH_FILE"
else
    echo "  依赖与源码均未变更，跳过构建"
fi

echo "  重启服务..."
$DC $COMPOSE up -d
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
        echo "      $DC $COMPOSE logs --tail 20"
    fi
    sleep 2
done

echo ""
echo "─── 服务状态 ───"
$DC $COMPOSE ps

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  部署完成 — $(date '+%H:%M:%S')"
echo "  访问: http://$(hostname -I | awk '{print $1}'):8080"
echo "═══════════════════════════════════════════════════════════"
