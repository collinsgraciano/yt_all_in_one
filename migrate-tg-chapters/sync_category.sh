#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 分类数据同步 — 从远程数据库同步分类到本地（Docker psql 方式）
#
# 用法:
#   bash sync_category.sh --diagnose          # 诊断远程分类信息
#   bash sync_category.sh --sync              # 按 book_id 匹配同步
#   bash sync_category.sh --sync --by-name    # 按 book_name 匹配同步
#
# 环境变量:
#   REMOTE_DSN  远程数据库连接串（有分类信息的库）
#   LOCAL_DSN   本地数据库连接串（需要同步的库）
# ═══════════════════════════════════════════════════════════════

set -euo pipefail
cd "$(dirname "$0")"

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 解析参数 ──
MODE=""
BY_NAME=false

for arg in "$@"; do
    case "$arg" in
        --diagnose)  MODE="diagnose" ;;
        --sync)      MODE="sync" ;;
        --by-name)   BY_NAME=true ;;
        *)           ;;
    esac
done

if [ -z "$MODE" ]; then
    echo "用法: bash sync_category.sh --diagnose | --sync [--by-name]"
    exit 1
fi

# ── 数据库连接串 ──
REMOTE_DSN="${REMOTE_DSN:-postgresql://audiobook_app:inriynisse1991@85.121.241.158:5432/audiobook}"
LOCAL_DSN="${LOCAL_DSN:-${DATABASE_URL:-postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook}}"

# Docker 内用 host.docker.internal 访问宿主机
REMOTE_DSN_DOCKER="${REMOTE_DSN//127.0.0.1/host.docker.internal}"
REMOTE_DSN_DOCKER="${REMOTE_DSN_DOCKER//localhost/host.docker.internal}"
LOCAL_DSN_DOCKER="${LOCAL_DSN//127.0.0.1/host.docker.internal}"
LOCAL_DSN_DOCKER="${LOCAL_DSN_DOCKER//localhost/host.docker.internal}"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  分类数据同步"
echo "═══════════════════════════════════════════════════"
info "远程数据库: ${REMOTE_DSN#*@}"
info "本地数据库: ${LOCAL_DSN#*@}"
info "模式:       ${MODE}"
echo "═══════════════════════════════════════════════════"
echo ""

# ═══════════════════════════════════════════════════════════════
# 诊断模式
# ═══════════════════════════════════════════════════════════════

if [ "$MODE" = "diagnose" ]; then
    info "诊断远程数据库分类信息..."
    echo ""

    docker run --rm \
        --add-host=host.docker.internal:host-gateway \
        postgres:16-alpine \
        bash -c "
            set -e
            echo '>>> 检查 books 表是否有 category 列...'
            HAS_CAT=\$(psql '$REMOTE_DSN_DOCKER' -t -A -c \"
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='books'
                AND column_name = 'category'
            \" 2>/dev/null || echo '')

            if [ -n \"\$HAS_CAT\" ]; then
                echo '[OK] books 表有 category 列！'
                echo ''
                echo '>>> 分类统计:'
                psql '$REMOTE_DSN_DOCKER' -c \"
                    SELECT category, COUNT(*) as cnt
                    FROM books
                    WHERE category IS NOT NULL AND category != ''
                    GROUP BY category
                    ORDER BY cnt DESC
                    LIMIT 20
                \"
                echo ''
                TOTAL_WITH=\$(psql '$REMOTE_DSN_DOCKER' -t -A -c \"
                    SELECT COUNT(*) FROM books WHERE category IS NOT NULL AND category != ''
                \")
                TOTAL=\$(psql '$REMOTE_DSN_DOCKER' -t -A -c 'SELECT COUNT(*) FROM books')
                echo \"有分类: \$TOTAL_WITH / \$TOTAL 本书\"
            else
                echo '[INFO] books 表没有 category 列，检查 book_data JSON...'
                echo ''
                echo '>>> book_data 顶层键名（取样）:'
                psql '$REMOTE_DSN_DOCKER' -t -A -c \"
                    SELECT DISTINCT jsonb_object_keys(book_data)
                    FROM books
                    LIMIT 30
                \"
                echo ''
                echo '>>> 检查可能的分类字段:'
                for key in category bookCategory tingCategory categoryId firstCid sort categoryName tagName bookType tingType type label tags; do
                    CNT=\$(psql '$REMOTE_DSN_DOCKER' -t -A -c \"
                        SELECT COUNT(*) FROM books
                        WHERE book_data->>'\$key' IS NOT NULL
                        AND book_data->>'\$key' != ''
                    \" 2>/dev/null || echo 0)
                    if [ \"\$CNT\" -gt 0 ] 2>/dev/null; then
                        echo \"  [\$key]: \$CNT 本有值\"
                        echo '    示例值:'
                        psql '$REMOTE_DSN_DOCKER' -t -A -c \"
                            SELECT DISTINCT book_data->>'\$key'
                            FROM books
                            WHERE book_data->>'\$key' IS NOT NULL
                            AND book_data->>'\$key' != ''
                            LIMIT 5
                        \" | sed 's/^/      /'
                    fi
                done
                echo ''
                echo '>>> 检查 bookInfo 嵌套字段:'
                for key in category bookCategory tingCategory categoryId firstCid sort; do
                    CNT=\$(psql '$REMOTE_DSN_DOCKER' -t -A -c \"
                        SELECT COUNT(*) FROM books
                        WHERE book_data#>'{bookInfo,\$key}' IS NOT NULL
                    \" 2>/dev/null || echo 0)
                    if [ \"\$CNT\" -gt 0 ] 2>/dev/null; then
                        echo \"  [bookInfo.\$key]: \$CNT 本有值\"
                        echo '    示例值:'
                        psql '$REMOTE_DSN_DOCKER' -t -A -c \"
                            SELECT DISTINCT book_data#>>'{bookInfo,\$key}'
                            FROM books
                            WHERE book_data#>>'{bookInfo,\$key}' IS NOT NULL
                            AND book_data#>>'{bookInfo,\$key}' != ''
                            LIMIT 5
                        \" | sed 's/^/      /'
                    fi
                done
            fi
        "
    echo ""
    ok "诊断完成"
    exit 0
fi

# ═══════════════════════════════════════════════════════════════
# 同步模式
# ═══════════════════════════════════════════════════════════════

if [ "$MODE" = "sync" ]; then
    info "开始同步分类数据..."
    echo ""

    MATCH_FIELD="book_id"
    if [ "$BY_NAME" = true ]; then
        MATCH_FIELD="book_name"
        info "匹配方式: book_name"
    else
        info "匹配方式: book_id"
    fi
    echo ""

    # 使用 Python 脚本在 Docker 内执行（更灵活）
    docker run --rm \
        -e REMOTE_DSN="$REMOTE_DSN_DOCKER" \
        -e LOCAL_DSN="$LOCAL_DSN_DOCKER" \
        -e MATCH_BY_NAME="$BY_NAME" \
        --add-host=host.docker.internal:host-gateway \
        -v "$(pwd)/sync_category.py:/app/sync_category.py:ro" \
        -w /app \
        python:3.12-slim \
        bash -c '
            set -e
            pip install -q "psycopg[binary]" 2>/dev/null
            if [ "$MATCH_BY_NAME" = "true" ]; then
                python sync_category.py --sync --match-by-name
            else
                python sync_category.py --sync
            fi
        '

    echo ""
    ok "同步完成！"
    exit 0
fi
