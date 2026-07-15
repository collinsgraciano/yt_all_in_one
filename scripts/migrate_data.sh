#!/usr/bin/env bash
# ============================================================================
# 数据迁移脚本：旧 PostgreSQL → Docker PostgreSQL（方案 A）
#
# 用法：
#   chmod +x scripts/migrate_data.sh
#   ./scripts/migrate_data.sh
#
# 或通过环境变量指定旧库连接信息：
#   OLD_PG_HOST=127.0.0.1 OLD_PG_PORT=5432 OLD_PG_USER=postgres \
#   OLD_PG_DB=audiobook ./scripts/migrate_data.sh
# ============================================================================

set -euo pipefail

# ── 颜色输出 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 旧库连接参数（可通过环境变量覆盖）──
OLD_PG_HOST="${OLD_PG_HOST:-127.0.0.1}"
OLD_PG_PORT="${OLD_PG_PORT:-5432}"
OLD_PG_USER="${OLD_PG_USER:-postgres}"
OLD_PG_DB="${OLD_PG_DB:-audiobook}"
OLD_PG_PASSWORD="${OLD_PG_PASSWORD:-}"

# ── 新库连接参数（与 docker-compose.yml 一致）──
NEW_PG_USER="audiobook_app"
NEW_PG_DB="audiobook"
NEW_PG_CONTAINER="audiobook_postgres"

# ── 需要迁移的 6 张核心表 ──
CORE_TABLES=(
    "books"
    "book_processing_states"
    "youtube_credentials"
    "modelscope_tokens"
    "channel_runtime_settings"
    "task_queue"
)

# ── 项目根目录 ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_FILE="/tmp/audiobook_migration_$(date +%Y%m%d_%H%M%S).sql"

# ============================================================================
# 前置检查
# ============================================================================
info "========================================"
info "  数据迁移脚本（方案 A：同一 docker-compose）"
info "========================================"
info ""
info "旧库: ${OLD_PG_HOST}:${OLD_PG_PORT}/${OLD_PG_DB} (用户: ${OLD_PG_USER})"
info "新库: Docker 容器 ${NEW_PG_CONTAINER} → ${NEW_PG_DB} (用户: ${NEW_PG_USER})"
info ""

# 检查是否在项目根目录
if [ ! -f "$PROJECT_ROOT/docker-compose.yml" ]; then
    error "未找到 docker-compose.yml，请在项目根目录下运行此脚本。"
    exit 1
fi

cd "$PROJECT_ROOT"

# 检查 .env 文件
if [ ! -f ".env" ]; then
    warn "未找到 .env 文件，从 .env.example 复制..."
    cp .env.example .env
    warn "请编辑 .env 设置 POSTGRES_PASSWORD 和 SECRET_KEY，然后重新运行此脚本。"
    exit 1
fi

# 读取 POSTGRES_PASSWORD
NEW_PG_PASSWORD="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d'=' -f2- | tr -d '"' | tr -d "'")"
if [ -z "$NEW_PG_PASSWORD" ]; then
    error ".env 中未设置 POSTGRES_PASSWORD，请先配置。"
    exit 1
fi

info "新库密码: ${NEW_PG_PASSWORD:0:2}****（已从 .env 读取）"
info ""

# ============================================================================
# 第 1 步：备份旧库数据
# ============================================================================
info "──── 第 1 步：备份旧库数据 ────"

# 设置旧库密码环境变量
if [ -n "$OLD_PG_PASSWORD" ]; then
    export PGPASSWORD="$OLD_PG_PASSWORD"
fi

# 检查旧库是否可连接
info "测试旧库连接 ${OLD_PG_HOST}:${OLD_PG_PORT}/${OLD_PG_DB} ..."
if ! psql -h "$OLD_PG_HOST" -p "$OLD_PG_PORT" -U "$OLD_PG_USER" -d "$OLD_PG_DB" -c "SELECT 1;" >/dev/null 2>&1; then
    error "无法连接旧库，请检查连接参数。"
    error "可尝试设置 OLD_PG_PASSWORD 环境变量："
    error "  OLD_PG_PASSWORD=你的密码 $0"
    exit 1
fi
ok "旧库连接成功"

# 逐表检查数据量
info "旧库各表数据量："
for table in "${CORE_TABLES[@]}"; do
    count=$(psql -h "$OLD_PG_HOST" -p "$OLD_PG_PORT" -U "$OLD_PG_USER" -d "$OLD_PG_DB" \
        -t -c "SELECT count(*) FROM public.${table};" 2>/dev/null || echo "N/A")
    printf "  %-30s %s 行\n" "$table" "$count"
done

# 导出数据（仅数据，不导表结构，使用 INSERT 格式以兼容列差异）
info "导出数据到 ${BACKUP_FILE} ..."
TABLE_ARGS=""
for table in "${CORE_TABLES[@]}"; do
    TABLE_ARGS="$TABLE_ARGS --table=public.${table}"
done

pg_dump -h "$OLD_PG_HOST" -p "$OLD_PG_PORT" -U "$OLD_PG_USER" -d "$OLD_PG_DB" \
    --data-only --column-inserts --no-owner --no-privileges \
    $TABLE_ARGS \
    > "$BACKUP_FILE"

if [ ! -s "$BACKUP_FILE" ]; then
    error "备份文件为空，迁移中止。"
    exit 1
fi

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
ok "备份完成: ${BACKUP_FILE} (${BACKUP_SIZE})"

# ============================================================================
# 第 2 步：启动 Docker PostgreSQL
# ============================================================================
info ""
info "──── 第 2 步：启动 Docker PostgreSQL ────"

# 检查 Docker 是否运行
if ! docker info >/dev/null 2>&1; then
    error "Docker 未运行，请先启动 Docker。"
    exit 1
fi

# 检查容器是否已存在
CONTAINER_STATUS=$(docker inspect -f '{{.State.Status}}' "$NEW_PG_CONTAINER" 2>/dev/null || echo "not_found")

if [ "$CONTAINER_STATUS" = "running" ]; then
    warn "容器 ${NEW_PG_CONTAINER} 已在运行"
    # 检查是否已有数据（非首次启动）
    EXISTING_ROWS=$(docker exec "$NEW_PG_CONTAINER" psql -U "$NEW_PG_USER" -d "$NEW_PG_DB" \
        -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='channels';" 2>/dev/null || echo "0")
    if [ "$EXISTING_ROWS" = "1" ]; then
        warn "数据库已初始化（init-db.sql 已执行过）"
    fi
elif [ "$CONTAINER_STATUS" = "not_found" ]; then
    info "首次启动，创建并初始化数据库容器..."
    docker-compose up -d postgres

    info "等待数据库健康检查通过..."
    timeout 60 bash -c "until docker inspect -f '{{.State.Health.Status}}' $NEW_PG_CONTAINER 2>/dev/null | grep -q healthy; do sleep 2; done" || {
        error "数据库健康检查超时（60秒），请检查容器日志：docker logs $NEW_PG_CONTAINER"
        exit 1
    }
    ok "数据库容器已启动并健康"
else
    info "启动已存在的容器..."
    docker start "$NEW_PG_CONTAINER"
    info "等待数据库就绪..."
    sleep 5
fi

# 等待数据库可连接
info "等待数据库可连接..."
for i in $(seq 1 30); do
    if docker exec "$NEW_PG_CONTAINER" pg_isready -U "$NEW_PG_USER" -d "$NEW_PG_DB" >/dev/null 2>&1; then
        ok "数据库已就绪"
        break
    fi
    sleep 1
    if [ $i -eq 30 ]; then
        error "数据库就绪超时"
        exit 1
    fi
done

# ============================================================================
# 第 3 步：验证表结构
# ============================================================================
info ""
info "──── 第 3 步：验证表结构 ────"

info "新库表列表："
docker exec "$NEW_PG_CONTAINER" psql -U "$NEW_PG_USER" -d "$NEW_PG_DB" -c "\dt public.*"

# 检查 6 张核心表是否都存在
for table in "${CORE_TABLES[@]}"; do
    EXISTS=$(docker exec "$NEW_PG_CONTAINER" psql -U "$NEW_PG_USER" -d "$NEW_PG_DB" \
        -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='${table}';" 2>/dev/null)
    if [ "$EXISTS" != "1" ]; then
        error "表 public.${table} 不存在！init-db.sql 可能未执行。"
        exit 1
    fi
done
ok "全部 6 张核心表已存在"

# ============================================================================
# 第 4 步：导入旧数据
# ============================================================================
info ""
info "──── 第 4 步：导入旧数据 ────"

# 检查新库是否已有数据（避免重复导入）
HAS_DATA=false
for table in "${CORE_TABLES[@]}"; do
    ROWS=$(docker exec "$NEW_PG_CONTAINER" psql -U "$NEW_PG_USER" -d "$NEW_PG_DB" \
        -t -c "SELECT count(*) FROM public.${table};" 2>/dev/null || echo "0")
    if [ "$ROWS" -gt 0 ] 2>/dev/null; then
        HAS_DATA=true
        warn "表 ${table} 已有 ${ROWS} 行数据"
        break
    fi
done

if [ "$HAS_DATA" = true ]; then
    warn "检测到新库已有数据！"
    echo ""
    read -p "是否清空新库核心表后重新导入？(y/N) " -r
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        info "清空核心表数据..."
        # 按依赖顺序清空（先清有外键引用的表）
        docker exec "$NEW_PG_CONTAINER" psql -U "$NEW_PG_USER" -d "$NEW_PG_DB" <<'EOF'
SET session_replication_role = replica;
TRUNCATE TABLE public.books, public.book_processing_states, public.youtube_credentials,
             public.modelscope_tokens, public.channel_runtime_settings, public.task_queue
CASCADE;
SET session_replication_role = DEFAULT;
EOF
        ok "核心表已清空"
    else
        warn "跳过导入，保留新库现有数据。"
        info "如需手动导入，请执行："
        info "  docker exec -i ${NEW_PG_CONTAINER} psql -U ${NEW_PG_USER} -d ${NEW_PG_DB} < ${BACKUP_FILE}"
        exit 0
    fi
fi

# 导入数据
info "导入数据中..."
docker exec -i "$NEW_PG_CONTAINER" psql -U "$NEW_PG_USER" -d "$NEW_PG_DB" \
    -v ON_ERROR_STOP=off \
    < "$BACKUP_FILE" 2>&1 | while IFS= read -r line; do
    # 过滤掉正常的 NOTICE/INFO 行，只显示错误
    if echo "$line" | grep -qiE "ERROR|FATAL"; then
        warn "$line"
    fi
done

ok "数据导入完成"

# ============================================================================
# 第 5 步：验证数据
# ============================================================================
info ""
info "──── 第 5 步：验证数据 ────"

info "新库各表数据量："
for table in "${CORE_TABLES[@]}"; do
    count=$(docker exec "$NEW_PG_CONTAINER" psql -U "$NEW_PG_USER" -d "$NEW_PG_DB" \
        -t -c "SELECT count(*) FROM public.${table};" 2>/dev/null || echo "ERROR")
    printf "  %-30s %s 行\n" "$table" "$count"
done

# 对比新旧库数据量
info ""
info "数据量对比："
ALL_MATCH=true
for table in "${CORE_TABLES[@]}"; do
    OLD_COUNT=$(psql -h "$OLD_PG_HOST" -p "$OLD_PG_PORT" -U "$OLD_PG_USER" -d "$OLD_PG_DB" \
        -t -c "SELECT count(*) FROM public.${table};" 2>/dev/null || echo "0")
    NEW_COUNT=$(docker exec "$NEW_PG_CONTAINER" psql -U "$NEW_PG_USER" -d "$NEW_PG_DB" \
        -t -c "SELECT count(*) FROM public.${table};" 2>/dev/null || echo "0")

    OLD_COUNT=$(echo "$OLD_COUNT" | xargs)
    NEW_COUNT=$(echo "$NEW_COUNT" | xargs)

    if [ "$OLD_COUNT" = "$NEW_COUNT" ]; then
        printf "  %-30s ${GREEN}%s ✓${NC}\n" "$table" "${OLD_COUNT} → ${NEW_COUNT}"
    else
        printf "  %-30s ${RED}%s ✗${NC}\n" "$table" "${OLD_COUNT} → ${NEW_COUNT}"
        ALL_MATCH=false
    fi
done

# ============================================================================
# 第 6 步：后续操作提示
# ============================================================================
info ""
info "═══════════════════════════════════════════"

if [ "$ALL_MATCH" = true ]; then
    ok "✅ 数据迁移成功！所有表数据量一致。"
else
    warn "⚠️ 部分表数据量不一致，请检查上方输出。"
    warn "   可能原因：主键冲突、数据类型不兼容等。"
    warn "   可手动检查备份文件: ${BACKUP_FILE}"
fi

info ""
info "后续操作："
info ""
info "  1. 启动全部服务："
info "     docker-compose up -d"
info ""
info "  2. 查看服务状态："
info "     docker-compose ps"
info ""
info "  3. 访问 Web 界面："
info "     http://localhost:59386"
info ""
info "  4. 备份文件位置（可安全删除）："
info "     ${BACKUP_FILE}"
info ""
info "  5. 确认无误后，可停止旧库："
info "     sudo systemctl stop postgresql"
info ""
info "═══════════════════════════════════════════"
