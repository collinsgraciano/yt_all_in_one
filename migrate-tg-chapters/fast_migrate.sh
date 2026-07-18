#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 快速数据迁移 — 纯 SQL 管道方式（比 Python 逐行快 10-50 倍）
#
# 原理：
#   在单个 postgres:16-alpine 容器内，用 psql 管道直传：
#     psql 源库 "COPY (SELECT ...) TO STDOUT" | psql 目标库 -c "..." -c "\copy ..."
#   多个 -c 在同一会话执行，临时表共享
#
# 用法:
#   bash fast_migrate.sh --all                             # 迁移两张表
#   bash fast_migrate.sh --books                           # 仅迁移 books
#   bash fast_migrate.sh --chapters                        # 仅迁移 chapters（全部）
#   bash fast_migrate.sh --chapters --only-complete-books   # 仅整本完整的书
#   bash fast_migrate.sh --all --dry-run                   # 试运行
#   bash fast_migrate.sh --bg --all                        # 后台运行
#
# 环境变量:
#   SOURCE_DATABASE_URL  旧项目数据库连接串
#   DATABASE_URL         新项目数据库连接串
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
BG_MODE=false
MIGRATE_BOOKS=false
MIGRATE_CHAPTERS=false
ONLY_COMPLETE=false
DRY_RUN=false
PASSTHROUGH=()

for arg in "$@"; do
    case "$arg" in
        --bg)                   BG_MODE=true ;;
        --books)                MIGRATE_BOOKS=true ;;
        --chapters)             MIGRATE_CHAPTERS=true ;;
        --all)                  MIGRATE_BOOKS=true; MIGRATE_CHAPTERS=true ;;
        --only-complete-books)  ONLY_COMPLETE=true; PASSTHROUGH+=("$arg") ;;
        --dry-run)              DRY_RUN=true; PASSTHROUGH+=("$arg") ;;
        *)                      PASSTHROUGH+=("$arg") ;;
    esac
done

if [ "$MIGRATE_BOOKS" = false ] && [ "$MIGRATE_CHAPTERS" = false ]; then
    MIGRATE_CHAPTERS=true
fi

# ── 数据库连接串 ──
SOURCE_DSN="${SOURCE_DATABASE_URL:-postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook}"
TARGET_DSN="${DATABASE_URL:-postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook}"

# Docker 内用 host.docker.internal 访问宿主机
SOURCE_DSN_DOCKER="${SOURCE_DSN//127.0.0.1/host.docker.internal}"
SOURCE_DSN_DOCKER="${SOURCE_DSN_DOCKER//localhost/host.docker.internal}"
TARGET_DSN_DOCKER="${TARGET_DSN//127.0.0.1/host.docker.internal}"
TARGET_DSN_DOCKER="${TARGET_DSN_DOCKER//localhost/host.docker.internal}"

# ── 后台模式 ──
if [ "$BG_MODE" = true ]; then
    mkdir -p logs
    LOG_FILE="logs/fast_migrate_$(date +%Y%m%d_%H%M%S).log"

    # 从已解析的标志重建参数（不依赖 PASSTHROUGH，避免 --all/--books/--chapters 丢失）
    REEXEC_ARGS=()
    if [ "$MIGRATE_BOOKS" = true ] && [ "$MIGRATE_CHAPTERS" = true ]; then
        REEXEC_ARGS+=("--all")
    elif [ "$MIGRATE_BOOKS" = true ]; then
        REEXEC_ARGS+=("--books")
    elif [ "$MIGRATE_CHAPTERS" = true ]; then
        REEXEC_ARGS+=("--chapters")
    fi
    if [ "$ONLY_COMPLETE" = true ]; then
        REEXEC_ARGS+=("--only-complete-books")
    fi
    if [ "$DRY_RUN" = true ]; then
        REEXEC_ARGS+=("--dry-run")
    fi
    # 附加其他未知参数
    REEXEC_ARGS+=("${PASSTHROUGH[@]}")

    info "后台运行模式"
    info "日志文件: $(pwd)/$LOG_FILE"
    info ""
    nohup bash "$0" "${REEXEC_ARGS[@]}" > "$LOG_FILE" 2>&1 &
    BG_PID=$!
    ok "后台进程已启动 (PID: $BG_PID)"
    info ""
    info "  查看日志:   tail -f $(pwd)/$LOG_FILE"
    info "  停止迁移:   kill $BG_PID"
    info ""
    info "  你可以断开 SSH 了"
    exit 0
fi

# ═══════════════════════════════════════════════════════════════
# 前台执行
# ═══════════════════════════════════════════════════════════════

echo ""
echo "═══════════════════════════════════════════════════"
echo "  快速数据迁移 — 纯 SQL 管道模式"
echo "═══════════════════════════════════════════════════"
info "源数据库 (旧): ${SOURCE_DSN#*@}"
info "目标库   (新): ${TARGET_DSN#*@}"
info "迁移 books:    ${MIGRATE_BOOKS}"
info "迁移 chapters: ${MIGRATE_CHAPTERS}"
info "仅完整书:      ${ONLY_COMPLETE}"
info "试运行:        ${DRY_RUN}"
echo "═══════════════════════════════════════════════════"
echo ""

# 构建 chapters SELECT（源库执行的查询）— 包含 telegram_bot_id / telegram_bot_user_id / worker_id / claimed_at / error_message
if [ "$ONLY_COMPLETE" = true ]; then
    CHAPTERS_SELECT="SELECT book_id, chapter_id, book_name, chapter_name, audio_url, telegram_file_id, telegram_message_id, telegram_bot_id, telegram_bot_user_id, upload_status, uploaded_at, worker_id, claimed_at, error_message FROM audiobook_chapters WHERE book_id IN (SELECT book_id FROM audiobook_chapters GROUP BY book_id HAVING COUNT(*) = COUNT(CASE WHEN upload_status = 'uploaded' AND telegram_file_id IS NOT NULL THEN 1 END))"
else
    CHAPTERS_SELECT="SELECT book_id, chapter_id, book_name, chapter_name, audio_url, telegram_file_id, telegram_message_id, telegram_bot_id, telegram_bot_user_id, upload_status, uploaded_at, worker_id, claimed_at, error_message FROM audiobook_chapters"
fi

# ── 试运行 ──
if [ "$DRY_RUN" = true ]; then
    info "[DRY-RUN] 只统计，不写入"
    echo ""
    docker run --rm \
        -e SOURCE_DSN="$SOURCE_DSN_DOCKER" \
        -e TARGET_DSN="$TARGET_DSN_DOCKER" \
        -e MIGRATE_BOOKS="$MIGRATE_BOOKS" \
        -e MIGRATE_CHAPTERS="$MIGRATE_CHAPTERS" \
        -e ONLY_COMPLETE="$ONLY_COMPLETE" \
        -e CHAPTERS_SELECT="$CHAPTERS_SELECT" \
        --add-host=host.docker.internal:host-gateway \
        postgres:16-alpine \
        bash -c '
            set -e
            echo "  === 源库统计 ==="
            if [ "$MIGRATE_BOOKS" = "true" ]; then
                printf "  books 总数:          %s\n" "$(psql "$SOURCE_DSN" -t -A -c "SELECT count(*) FROM books;")"
            fi
            if [ "$MIGRATE_CHAPTERS" = "true" ]; then
                printf "  chapters 总数:       %s\n" "$(psql "$SOURCE_DSN" -t -A -c "SELECT count(*) FROM audiobook_chapters;")"
                if [ "$ONLY_COMPLETE" = "true" ]; then
                    printf "  完整书数量:          %s\n" "$(psql "$SOURCE_DSN" -t -A -c "SELECT count(*) FROM (SELECT book_id FROM audiobook_chapters GROUP BY book_id HAVING COUNT(*) = COUNT(CASE WHEN upload_status = '"'"'uploaded'"'"' AND telegram_file_id IS NOT NULL THEN 1 END)) t;")"
                    printf "  完整书 chapters 数:  %s\n" "$(psql "$SOURCE_DSN" -t -A -c "SELECT count(*) FROM ($CHAPTERS_SELECT) t;")"
                fi
            fi
            echo ""
            echo "  === 目标库统计 ==="
            if [ "$MIGRATE_BOOKS" = "true" ]; then
                printf "  books 现有:          %s\n" "$(psql "$TARGET_DSN" -t -A -c "SELECT count(*) FROM public.books;")"
            fi
            if [ "$MIGRATE_CHAPTERS" = "true" ]; then
                printf "  chapters 现有:       %s\n" "$(psql "$TARGET_DSN" -t -A -c "SELECT count(*) FROM public.audiobook_chapters;")"
            fi
        '
    echo ""
    ok "[DRY-RUN] 统计完成"
    exit 0
fi

# ═══════════════════════════════════════════════════════════════
# 正式迁移
# ═══════════════════════════════════════════════════════════════

START_TIME=$(date +%s)

# books 转换 SQL — 直接读取源库顶层列（源库已有完整顶层列）
BOOKS_INSERT_SQL="INSERT INTO public.books (book_id, book_name, author, category, total_chapters, book_data, tags, note, status, book_status) SELECT book_id, COALESCE(NULLIF(book_name, ''), COALESCE(NULLIF(book_data->>'bookName',''), NULLIF(book_data->>'title',''), NULLIF(book_data->>'name',''), '未知_' || book_id)) AS book_name, COALESCE(NULLIF(author, ''), COALESCE(NULLIF(book_data->>'bookAuthor',''), NULLIF(book_data->>'author',''), NULLIF(book_data->>'writer',''))) AS author, COALESCE(NULLIF(category, ''), COALESCE(NULLIF(book_data->>'category',''), NULLIF(book_data->>'bookCategory',''), NULLIF(book_data->>'tingCategory',''), NULLIF(book_data->>'categoryId',''), NULLIF(book_data->>'firstCid',''), NULLIF(book_data->>'sort',''), NULLIF(book_data#>>'{bookInfo,category}',''), NULLIF(book_data#>>'{bookInfo,bookCategory}',''), NULLIF(book_data#>>'{bookInfo,tingCategory}',''))) AS category, COALESCE(total_chapters, CASE WHEN book_data ? 'tingChapterList' THEN jsonb_array_length(book_data->'tingChapterList') WHEN book_data ? 'chapterList' THEN jsonb_array_length(book_data->'chapterList') WHEN book_data ? 'chapters' THEN jsonb_array_length(book_data->'chapters') WHEN book_data ? 'list' THEN jsonb_array_length(book_data->'list') WHEN book_data ? 'tingChapters' THEN jsonb_array_length(book_data->'tingChapters') WHEN book_data ? 'sectionList' THEN jsonb_array_length(book_data->'sectionList') WHEN book_data ? 'chapters_data' THEN jsonb_array_length(book_data->'chapters_data') ELSE 0 END) AS total_chapters, book_data, COALESCE(tags, '{}'::text[]) AS tags, note, COALESCE(status, '') AS status, COALESCE(book_status, 'pending') AS book_status FROM _old_books ON CONFLICT (book_id) DO NOTHING;"

# chapters 写入 SQL — 从临时表写入正式表（含 telegram_bot_id / telegram_bot_user_id / worker_id / claimed_at / error_message）
CHAPTERS_INSERT_SQL="INSERT INTO public.audiobook_chapters (book_id, chapter_id, book_name, chapter_name, audio_url, telegram_file_id, telegram_message_id, telegram_bot_id, telegram_bot_user_id, upload_status, uploaded_at, worker_id, claimed_at, error_message) SELECT book_id, chapter_id, book_name, chapter_name, audio_url, telegram_file_id, telegram_message_id, telegram_bot_id, telegram_bot_user_id, upload_status, uploaded_at, worker_id, claimed_at, error_message FROM _old_chapters ON CONFLICT (book_id, chapter_id) DO UPDATE SET telegram_file_id = COALESCE(EXCLUDED.telegram_file_id, public.audiobook_chapters.telegram_file_id), telegram_message_id = COALESCE(EXCLUDED.telegram_message_id, public.audiobook_chapters.telegram_message_id), telegram_bot_id = COALESCE(EXCLUDED.telegram_bot_id, public.audiobook_chapters.telegram_bot_id), telegram_bot_user_id = COALESCE(EXCLUDED.telegram_bot_user_id, public.audiobook_chapters.telegram_bot_user_id), upload_status = EXCLUDED.upload_status, uploaded_at = COALESCE(EXCLUDED.uploaded_at, public.audiobook_chapters.uploaded_at), book_name = COALESCE(EXCLUDED.book_name, public.audiobook_chapters.book_name), chapter_name = COALESCE(EXCLUDED.chapter_name, public.audiobook_chapters.chapter_name), audio_url = COALESCE(EXCLUDED.audio_url, public.audiobook_chapters.audio_url), worker_id = COALESCE(EXCLUDED.worker_id, public.audiobook_chapters.worker_id), claimed_at = COALESCE(EXCLUDED.claimed_at, public.audiobook_chapters.claimed_at), error_message = COALESCE(EXCLUDED.error_message, public.audiobook_chapters.error_message);"

docker run --rm \
    -e SOURCE_DSN="$SOURCE_DSN_DOCKER" \
    -e TARGET_DSN="$TARGET_DSN_DOCKER" \
    -e MIGRATE_BOOKS="$MIGRATE_BOOKS" \
    -e MIGRATE_CHAPTERS="$MIGRATE_CHAPTERS" \
    -e CHAPTERS_SELECT="$CHAPTERS_SELECT" \
    -e BOOKS_INSERT_SQL="$BOOKS_INSERT_SQL" \
    -e CHAPTERS_INSERT_SQL="$CHAPTERS_INSERT_SQL" \
    --add-host=host.docker.internal:host-gateway \
    postgres:16-alpine \
    bash -c '
        set -euo pipefail

        if [ "$MIGRATE_BOOKS" = "true" ]; then
            echo "[INFO] ════════════════════════════════════════════"
            echo "[INFO]   [1/2] 迁移 books 表"
            echo "[INFO] ════════════════════════════════════════════"
            echo ""

            echo "[INFO] >>> 管道传输: 源库 COPY → 目标库临时表..."
            # 源库直接读取所有顶层列（含 book_name, author, category, total_chapters, tags, note, status, book_status）
            psql "$SOURCE_DSN" -c "COPY (SELECT book_id, book_name, author, category, total_chapters, book_data, tags, note, status, book_status FROM books) TO STDOUT WITH CSV" | \
            psql "$TARGET_DSN" \
                -c "CREATE TEMP TABLE _old_books (book_id text, book_name text, author text, category text, total_chapters integer, book_data jsonb, tags text[], note text, status text, book_status text);" \
                -c "\copy _old_books FROM STDIN WITH CSV" \
                -c "$BOOKS_INSERT_SQL"

            echo ""
            printf "[OK]   目标库 books 总数: %s\n" "$(psql "$TARGET_DSN" -t -A -c "SELECT count(*) FROM public.books;")"
            echo ""
        fi

        if [ "$MIGRATE_CHAPTERS" = "true" ]; then
            echo "[INFO] ════════════════════════════════════════════"
            echo "[INFO]   [2/2] 迁移 audiobook_chapters 表"
            echo "[INFO] ════════════════════════════════════════════"
            echo ""

            echo "[INFO] >>> 管道传输: 源库 COPY → 目标库临时表..."
            psql "$SOURCE_DSN" -c "COPY ($CHAPTERS_SELECT) TO STDOUT WITH CSV" | \
            psql "$TARGET_DSN" \
                -c "CREATE TEMP TABLE _old_chapters (book_id text, chapter_id text, book_name text, chapter_name text, audio_url text, telegram_file_id text, telegram_message_id bigint, telegram_bot_id int, telegram_bot_user_id bigint, upload_status text, uploaded_at timestamptz, worker_id text, claimed_at timestamptz, error_message text);" \
                -c "\copy _old_chapters FROM STDIN WITH CSV" \
                -c "$CHAPTERS_INSERT_SQL"

            echo ""
            printf "[OK]   目标库 chapters 总数: %s\n" "$(psql "$TARGET_DSN" -t -A -c "SELECT count(*) FROM public.audiobook_chapters;")"
            printf "[OK]   有Bot永久ID: %s\n" "$(psql "$TARGET_DSN" -t -A -c "SELECT count(*) FROM public.audiobook_chapters WHERE telegram_bot_user_id IS NOT NULL;")"
            echo ""
        fi
    '

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo "═══════════════════════════════════════════════════"
ok "全部迁移完成！耗时 ${ELAPSED} 秒"
echo "═══════════════════════════════════════════════════"
