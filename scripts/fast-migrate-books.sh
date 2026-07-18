#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 快速书籍迁移脚本 — 纯 PostgreSQL COPY 导出/导入，无需 Python
#
# 适用场景:
#   - 旧库和新库在不同服务器上，各自 Docker 运行 PostgreSQL 16
#   - 版本/配置完全一致
#   - 允许直接清空目标表后替换
#
# 用法（在目标服务器上运行）:
#   bash scripts/fast-migrate-books.sh
#
# 环境变量:
#   SRC_HOST        旧库服务器 IP（默认 127.0.0.1 = 本机）
#   SRC_USER        旧库 SSH 用户（默认 root）
#   SRC_CONTAINER   旧库 Docker 容器名（默认 audiobook_pg）
#   SRC_PG_USER     旧库 PG 用户（默认 audiobook_app）
#   SRC_PG_DB       旧库 PG 数据库（默认 audiobook）
#   DST_CONTAINER   新库 Docker 容器名（默认 audiobook_postgres）
#   DST_PG_USER     新库 PG 用户（默认 audiobook_app）
#   DST_PG_DB       新库 PG 数据库（默认 audiobook）
#
# 完整示例（旧库在远程 1.2.3.4）:
#   SRC_HOST=1.2.3.4 SRC_USER=root bash scripts/fast-migrate-books.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ── 颜色 ──
G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; R='\033[0;31m'; N='\033[0m'
info()  { echo -e "${C}[INFO]${N}  $*"; }
ok()    { echo -e "${G}[OK]${N}    $*"; }
warn()  { echo -e "${Y}[WARN]${N}  $*"; }
error() { echo -e "${R}[ERROR]${N} $*"; exit 1; }

# ── 配置 ──
SRC_HOST="${SRC_HOST:-127.0.0.1}"
SRC_USER="${SRC_USER:-root}"
SRC_CONTAINER="${SRC_CONTAINER:-audiobook_pg}"
SRC_PG_USER="${SRC_PG_USER:-audiobook_app}"
SRC_PG_DB="${SRC_PG_DB:-audiobook}"

DST_CONTAINER="${DST_CONTAINER:-audiobook_postgres}"
DST_PG_USER="${DST_PG_USER:-audiobook_app}"
DST_PG_DB="${DST_PG_DB:-audiobook}"

CSV_FILE="/tmp/old_books_export.csv"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  快速书籍迁移（纯 PostgreSQL COPY，无需 Python）"
echo "═══════════════════════════════════════════════════════════"
echo "  源库: ${SRC_USER}@${SRC_HOST} → Docker ${SRC_CONTAINER} (${SRC_PG_DB})"
echo "  目标: 本机 Docker ${DST_CONTAINER} (${DST_PG_DB})"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ═════════════════════════════════════════════════════════════
# 步骤 1: 从旧库 COPY 导出（二进制 COPY 格式，最快的导出方式）
# ═════════════════════════════════════════════════════════════
info "步骤 1/4: 从旧库 COPY 导出 books 表..."

EXPORT_CMD="docker exec ${SRC_CONTAINER} psql -U ${SRC_PG_USER} -d ${SRC_PG_DB} -c \"COPY books TO '/tmp/old_books_export.csv' WITH CSV HEADER\""

if [ "$SRC_HOST" = "127.0.0.1" ] || [ "$SRC_HOST" = "localhost" ]; then
    # 旧库在本机
    info "  旧库在本机，直接导出..."
    eval "$EXPORT_CMD"
    docker cp "${SRC_CONTAINER}:/tmp/old_books_export.csv" "$CSV_FILE"
else
    # 旧库在远程服务器，通过 SSH 导出 + scp 传回
    info "  旧库在远程 ${SRC_HOST}，通过 SSH 导出并传回..."
    ssh "${SRC_USER}@${SRC_HOST}" "$EXPORT_CMD"
    scp "${SRC_USER}@${SRC_HOST}:/tmp/old_books_export.csv" "$CSV_FILE"
    # 清理远程临时文件
    ssh "${SRC_USER}@${SRC_HOST}" "rm -f /tmp/old_books_export.csv" 2>/dev/null || true
fi

if [ ! -s "$CSV_FILE" ]; then
    error "导出文件为空！请检查旧库连接和容器名。"
fi

ROW_COUNT=$(wc -l < "$CSV_FILE")
SIZE_KB=$(du -k "$CSV_FILE" | cut -f1)
ok "导出完成: ${ROW_COUNT} 行, ${SIZE_KB}KB"

# ═════════════════════════════════════════════════════════════
# 步骤 2: 将 CSV 复制到目标容器
# ═════════════════════════════════════════════════════════════
info "步骤 2/4: 复制 CSV 到目标容器..."
docker cp "$CSV_FILE" "${DST_CONTAINER}:/tmp/old_books_export.csv"
ok "文件已复制到容器"

# ═════════════════════════════════════════════════════════════
# 步骤 3: 创建临时表 → COPY 导入 → 字段转换 → 替换目标表
# ═════════════════════════════════════════════════════════════
info "步骤 3/4: 导入 + 字段转换 + 替换目标表..."

docker exec -i "$DST_CONTAINER" psql -U "$DST_PG_USER" -d "$DST_PG_DB" <<'PSQL_EOF'
\set ON_ERROR_STOP on
BEGIN;

-- 3.1 创建临时表（与旧表结构完全一致）
CREATE TEMP TABLE old_books_import (
    book_id     text,
    book_data   jsonb,
    book_status text DEFAULT 'pending'
);

-- 3.2 用 COPY 快速导入 CSV 到临时表（比 INSERT 快 10-50 倍）
COPY old_books_import FROM '/tmp/old_books_export.csv' WITH CSV HEADER;

-- 3.3 验证导入数量
SELECT COUNT(*) AS temp_count FROM old_books_import;

-- 3.4 清空目标表（用户确认允许替换）
TRUNCATE public.books;

-- 3.5 从临时表提取字段，转换写入目标表
--     book_data JSONB 中提取: bookName, bookAuthor, tingChapterList
INSERT INTO public.books (
    book_id, book_name, author, category, total_chapters,
    book_data, tags, note, status, book_status, created_at, updated_at
)
SELECT
    ob.book_id,
    -- 书名: 兼容多种 JSON 字段名
    COALESCE(
        ob.book_data->>'bookName',
        ob.book_data->>'title',
        ob.book_data->>'name',
        '未知_' || ob.book_id
    ),
    -- 作者
    ob.book_data->>'bookAuthor',
    -- 分类（旧表无此字段，留空）
    NULL::text,
    -- 章节总数: 安全地从 JSON 数组提取长度（兼容多种字段名）
    COALESCE(
        CASE WHEN jsonb_typeof(ob.book_data->'tingChapterList') = 'array'
             THEN jsonb_array_length(ob.book_data->'tingChapterList') END,
        CASE WHEN jsonb_typeof(ob.book_data->'chapterList') = 'array'
             THEN jsonb_array_length(ob.book_data->'chapterList') END,
        CASE WHEN jsonb_typeof(ob.book_data->'chapters') = 'array'
             THEN jsonb_array_length(ob.book_data->'chapters') END,
        CASE WHEN jsonb_typeof(ob.book_data->'bookInfo'->'tingChapterList') = 'array'
             THEN jsonb_array_length(ob.book_data->'bookInfo'->'tingChapterList') END,
        0
    ),
    -- 完整 JSON 数据原样保留
    ob.book_data,
    -- 标签默认空数组
    ARRAY[]::text[],
    -- 备注
    NULL::text,
    -- 状态: book_status → status
    COALESCE(ob.book_status, 'pending'),
    -- 章节完成标记: book_status → book_status
    COALESCE(ob.book_status, 'pending'),
    now(),
    now()
FROM old_books_import ob;

-- 3.6 验证迁移结果
SELECT
    COUNT(*)                                            AS total,
    COUNT(*) FILTER (WHERE book_name IS NOT NULL)       AS has_name,
    COUNT(*) FILTER (WHERE author IS NOT NULL)          AS has_author,
    COUNT(*) FILTER (WHERE total_chapters > 0)          AS has_chapters,
    COUNT(*) FILTER (WHERE status = 'pending')          AS pending,
    COUNT(*) FILTER (WHERE status = 'success')          AS success,
    COUNT(*) FILTER (WHERE book_status = 'pending')     AS bs_pending,
    COUNT(*) FILTER (WHERE book_status = 'success')     AS bs_success
FROM public.books;

COMMIT;

-- 清理容器内临时文件
\! rm -f /tmp/old_books_export.csv
PSQL_EOF

ok "数据导入并转换完成"

# ═════════════════════════════════════════════════════════════
# 步骤 4: 清理宿主机临时文件
# ═════════════════════════════════════════════════════════════
info "步骤 4/4: 清理临时文件..."
rm -f "$CSV_FILE"
ok "清理完成"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ✅ 迁移完成！"
echo "  目标库 public.books 表已用旧库数据替换"
echo ""
echo "  验证命令:"
echo "    docker exec ${DST_CONTAINER} psql -U ${DST_PG_USER} -d ${DST_PG_DB} -c \\"
echo "      'SELECT COUNT(*), COUNT(*) FILTER (WHERE book_name IS NOT NULL) FROM public.books;'"
echo "═══════════════════════════════════════════════════════════"
