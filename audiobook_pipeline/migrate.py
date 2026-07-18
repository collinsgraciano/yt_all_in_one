#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
有声书数据迁移脚本: 远程 PostgreSQL → 本地 PostgreSQL (重构版)

设计理念:
  直接将远程 books 表原封不动迁移过来 (保留所有顶层列: book_name, author,
  category, total_chapters, tags, note, status, created_at, updated_at),
  然后从 book_data->'chapters_data' 解析章节插入 audiobook_chapters 表。

  不再需要 _top_level 合并 hack, 不再需要 DuckDB, 简单直接。

特性:
  - 远程 books 表所有列原封不动迁移 (SELECT * → INSERT)
  - 无章节的图书不入库 (chapters_data 为空则跳过)
  - 解析章节插入 audiobook_chapters 表
  - 支持 --force 覆盖已有数据
  - 支持 --skip-chapters 仅迁移 books 不解析章节

使用方法:
    # 基本迁移 (远程 → 本地 Docker)
    python3 migrate.py

    # 指定远程/本地 DSN
    python3 migrate.py \\
        --remote-dsn "postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook" \\
        --local-dsn  "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook"

    # 覆盖已有数据
    python3 migrate.py --force

    # 仅迁移 books, 不解析章节
    python3 migrate.py --skip-chapters

    # 仅解析章节 (books 已迁移)
    python3 migrate.py --chapters-only
"""

import os
import sys
import json
import time
import argparse

# ============================================================
# 配置
# ============================================================

DEFAULT_REMOTE_DSN = os.environ.get(
    'REMOTE_POSTGRES_DSN',
    'postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook'
)
DEFAULT_LOCAL_DSN = os.environ.get(
    'POSTGRES_DSN',
    'postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook'
)

BOOKS_TABLE = 'books'
CHAPTERS_TABLE = 'audiobook_chapters'
BATCH_SIZE = 500


# ============================================================
# 数据库工具
# ============================================================

def safe_pg_connect(dsn):
    """连接 PostgreSQL, 失败重试"""
    import psycopg2
    for i in range(3):
        try:
            return psycopg2.connect(dsn)
        except Exception as e:
            print(f'  [连接重试 {i+1}/3] {e}')
            time.sleep(2)
    raise ConnectionError(f'无法连接: {dsn}')


def ensure_tables(dsn):
    """确保本地表结构存在 (init.sql 的 Python 版本, 用于非 Docker 场景)"""
    from psycopg2 import sql
    conn = safe_pg_connect(dsn)
    try:
        with conn.cursor() as cur:
            # books 表 (与远程一致 + book_status)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS books (
                    book_id          text        PRIMARY KEY,
                    book_name        text,
                    author           text,
                    category         text,
                    total_chapters   integer,
                    book_data        jsonb,
                    tags             text[],
                    note             text,
                    status           text        DEFAULT '',
                    created_at       timestamptz DEFAULT now(),
                    updated_at       timestamptz DEFAULT now(),
                    book_status      varchar(50) DEFAULT 'pending'
                )
            """)
            # 兼容旧表: 补充列
            for col_sql in [
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS book_name text",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS author text",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS category text",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS total_chapters integer",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS tags text[]",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS note text",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS status text DEFAULT ''",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now()",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS book_status varchar(50) DEFAULT 'pending'",
            ]:
                cur.execute(col_sql)

            # audiobook_chapters 表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audiobook_chapters (
                    book_id              VARCHAR(255),
                    chapter_id           VARCHAR(255),
                    book_name            TEXT,
                    chapter_name         TEXT,
                    audio_url            TEXT,
                    telegram_file_id     TEXT,
                    telegram_message_id  BIGINT,
                    telegram_bot_id      INT,
                    telegram_bot_user_id BIGINT,
                    upload_status        VARCHAR(50) DEFAULT 'pending',
                    worker_id            VARCHAR(100),
                    claimed_at           TIMESTAMP,
                    uploaded_at          TIMESTAMP,
                    error_message        TEXT,
                    PRIMARY KEY (book_id, chapter_id)
                )
            """)
            cur.execute("ALTER TABLE audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_bot_id INT")
            cur.execute("ALTER TABLE audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_bot_user_id BIGINT")

            # 索引
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_books_category    ON books(category)",
                "CREATE INDEX IF NOT EXISTS idx_books_status      ON books(status)",
                "CREATE INDEX IF NOT EXISTS idx_books_book_status ON books(book_status)",
                "CREATE INDEX IF NOT EXISTS idx_books_tags_gin    ON books USING gin(tags)",
                "CREATE INDEX IF NOT EXISTS idx_books_updated_at  ON books(updated_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_chapters_upload_status ON audiobook_chapters(upload_status)",
                "CREATE INDEX IF NOT EXISTS idx_chapters_book_id       ON audiobook_chapters(book_id)",
                "CREATE INDEX IF NOT EXISTS idx_chapters_book_status   ON audiobook_chapters(book_id, upload_status)",
            ]:
                cur.execute(idx_sql)

        conn.commit()
        print(f'[OK] 表结构已就绪')
    finally:
        conn.close()


# ============================================================
# 章节解析
# ============================================================

def extract_chapters(book_data):
    """从 book_data JSON 中提取章节列表"""
    if not isinstance(book_data, dict):
        return []
    for key in ['chapters_data', 'tingChapterList', 'chapterList', 'chapters', 'list', 'tingChapters', 'sectionList']:
        if key in book_data and book_data[key]:
            return book_data[key]
    book_info = book_data.get('bookInfo')
    if isinstance(book_info, dict):
        for key in ['chapters_data', 'tingChapterList', 'chapterList', 'chapters', 'list', 'tingChapters', 'sectionList']:
            if key in book_info and book_info[key]:
                return book_info[key]
    return []


def extract_chapter_info(chapter):
    """从章节 dict 中提取 (chapter_id, chapter_name, audio_url)"""
    if not isinstance(chapter, dict):
        return None, None, None
    chapter_id = None
    for key in ['tingChapterId', 'chapterId', 'id', 'chapter_id', 'sectionId']:
        if key in chapter and chapter[key]:
            chapter_id = str(chapter[key])
            break
    chapter_name = None
    for key in ['chapterName', 'name', 'title', 'tingChapterName', 'sectionName']:
        if key in chapter and chapter[key]:
            chapter_name = str(chapter[key])
            break
    audio_url = None
    for key in ['playUrl', 'downUrl', 'url', 'filePath', 'mediaUrl', 'audioUrl', 'tingUrl', 'fileUrl', 'mp3Url', 'downloadUrl']:
        if key in chapter and chapter[key]:
            audio_url = str(chapter[key])
            break
    return chapter_id, chapter_name, audio_url


# ============================================================
# 迁移: books 表
# ============================================================

def migrate_books(remote_dsn, local_dsn, force=False):
    """迁移 books 表: 远程 → 本地 (保留所有列)"""
    from psycopg2.extras import execute_values, Json

    print(f'>>> 迁移 books 表...')
    print(f'  远程: {remote_dsn.split("@")[-1] if "@" in remote_dsn else remote_dsn}')
    print(f'  本地: {local_dsn.split("@")[-1] if "@" in local_dsn else local_dsn}')

    remote_conn = safe_pg_connect(remote_dsn)
    remote_conn.set_session(readonly=True)  # 只读模式, 必须在任何查询前调用
    local_conn = safe_pg_connect(local_dsn)

    try:
        # 远程: 统计总数
        with remote_conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM books')
            total = cur.fetchone()[0]
        print(f'  远程 books 总数: {total}')

        # 本地: 迁移前计数
        with local_conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {BOOKS_TABLE}')
            before = cur.fetchone()[0]
        print(f'  本地 books 迁移前: {before}')

        # 远程: 流式读取 (使用命名游标避免内存问题)
        from psycopg2.extras import NamedTupleCursor
        named_cur = remote_conn.cursor('migrate_cursor', withhold=True)
        named_cur.itersize = BATCH_SIZE
        named_cur.execute("""
            SELECT book_id, book_name, author, category, total_chapters,
                   book_data, tags, note, status, created_at, updated_at
            FROM books
            ORDER BY book_id
        """)

        conflict_clause = (
            'ON CONFLICT (book_id) DO UPDATE SET '
            'book_name=EXCLUDED.book_name, author=EXCLUDED.author, '
            'category=EXCLUDED.category, total_chapters=EXCLUDED.total_chapters, '
            'book_data=EXCLUDED.book_data, tags=EXCLUDED.tags, note=EXCLUDED.note, '
            'status=EXCLUDED.status, created_at=EXCLUDED.created_at, '
            'updated_at=EXCLUDED.updated_at'
            if force else 'ON CONFLICT (book_id) DO NOTHING'
        )

        insert_sql = (
            f'INSERT INTO {BOOKS_TABLE} (book_id, book_name, author, category, '
            f'total_chapters, book_data, tags, note, status, created_at, updated_at) '
            f'VALUES %s ' + conflict_clause
        )

        batch = []
        migrated = 0
        skipped = 0

        while True:
            rows = named_cur.fetchmany(BATCH_SIZE)
            if not rows:
                break
            for row in rows:
                batch.append((
                    row[0],   # book_id
                    row[1],   # book_name
                    row[2],   # author
                    row[3],   # category
                    row[4],   # total_chapters
                    Json(row[5]) if isinstance(row[5], (dict, list)) else row[5],  # book_data (jsonb)
                    row[6],   # tags (text[])
                    row[7],   # note
                    row[8],   # status
                    row[9],   # created_at
                    row[10],  # updated_at
                ))

            if batch:
                with local_conn.cursor() as cur:
                    execute_values(cur, insert_sql, batch, page_size=500)
                local_conn.commit()
                migrated += len(batch)
                batch = []
                pct = migrated * 100 // total if total else 100
                print(f'\r  进度: {migrated}/{total} ({pct}%)', end='', flush=True)

        named_cur.close()
        print()

        # 本地: 迁移后计数
        with local_conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {BOOKS_TABLE}')
            after = cur.fetchone()[0]
        inserted = after - before
        print(f'[OK] books 迁移完成: 新增 {inserted} 条 (本地现有 {after} 条)')
        return after

    finally:
        remote_conn.close()
        local_conn.close()


# ============================================================
# 迁移: 解析章节
# ============================================================

def migrate_chapters(local_dsn, force=False):
    """从本地 books.book_data 解析章节, 插入 audiobook_chapters"""
    from psycopg2.extras import execute_values

    print(f'\n>>> 解析章节并插入 {CHAPTERS_TABLE}...')

    conn = safe_pg_connect(local_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {BOOKS_TABLE}')
            total_books = cur.fetchone()[0]
            cur.execute(f'SELECT COUNT(*) FROM {CHAPTERS_TABLE}')
            before = cur.fetchone()[0]

        print(f'  本地 books 总数: {total_books}')
        print(f'  章节表迁移前: {before}')

        # 流式读取 books
        cur = conn.cursor('chapter_cursor', withhold=True)
        cur.itersize = BATCH_SIZE
        cur.execute(f'SELECT book_id, book_name, book_data FROM {BOOKS_TABLE} ORDER BY book_id')

        conflict_clause = (
            'ON CONFLICT (book_id, chapter_id) DO UPDATE SET '
            'book_name=EXCLUDED.book_name, chapter_name=EXCLUDED.chapter_name, '
            'audio_url=EXCLUDED.audio_url'
            if force else 'ON CONFLICT (book_id, chapter_id) DO NOTHING'
        )

        insert_sql = (
            f'INSERT INTO {CHAPTERS_TABLE} '
            f'(book_id, chapter_id, book_name, chapter_name, audio_url, upload_status) '
            f'VALUES %s ' + conflict_clause
        )

        batch = []
        books_with_chapters = 0
        books_no_chapters = 0
        total_chapters = 0

        while True:
            rows = cur.fetchmany(BATCH_SIZE)
            if not rows:
                break
            for row in rows:
                book_id = row[0]
                book_name = row[1] or f'未知_{book_id}'
                book_data = row[2]

                # book_data 可能是 dict (psycopg2 jsonb) 或 str
                if isinstance(book_data, str):
                    try:
                        book_data = json.loads(book_data)
                    except (json.JSONDecodeError, TypeError):
                        books_no_chapters += 1
                        continue

                chapters = extract_chapters(book_data)
                if not chapters:
                    books_no_chapters += 1
                    continue

                books_with_chapters += 1
                for ch in chapters:
                    ch_id, ch_name, audio_url = extract_chapter_info(ch)
                    if not ch_id:
                        continue
                    if not ch_name:
                        ch_name = f'第{ch_id}章'
                    batch.append((str(book_id), ch_id, book_name, ch_name, audio_url, 'pending'))
                    total_chapters += 1

            if batch:
                with conn.cursor() as insert_cur:
                    execute_values(insert_cur, insert_sql, batch, page_size=500)
                conn.commit()
                batch = []

        cur.close()

        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {CHAPTERS_TABLE}')
            after = cur.fetchone()[0]
        inserted = after - before

        print(f'  有章节的书: {books_with_chapters}')
        print(f'  无章节跳过: {books_no_chapters}')
        print(f'  解析出章节: {total_chapters}')
        print(f'[OK] 章节迁移完成: 新增 {inserted} 条 (章节表现有 {after} 条)')
        return after

    finally:
        conn.close()


# ============================================================
# 报告
# ============================================================

def print_report(dsn):
    sep = '=' * 60
    print(f'\n{sep}')
    print('         迁移报告 (PG → PG 重构版)')
    print(sep)

    conn = safe_pg_connect(dsn)
    try:
        with conn.cursor() as cur:
            # books 统计
            cur.execute(f'SELECT COUNT(*) FROM {BOOKS_TABLE}')
            books_total = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM {BOOKS_TABLE} WHERE book_status = 'pending'")
            books_pending = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM {BOOKS_TABLE} WHERE book_status = 'success'")
            books_success = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM {BOOKS_TABLE} WHERE category IS NOT NULL AND category != ''")
            books_has_cat = cur.fetchone()[0]

            print(f'\nbooks 表: {books_total} 条')
            print(f'  pending:          {books_pending}')
            print(f'  success:          {books_success}')
            print(f'  有 category:      {books_has_cat}')

            # category 分布
            cur.execute(f"""
                SELECT category, COUNT(*) as cnt FROM {BOOKS_TABLE}
                WHERE category IS NOT NULL AND category != ''
                GROUP BY category ORDER BY cnt DESC LIMIT 10
            """)
            cats = cur.fetchall()
            if cats:
                print(f'  category 分布 (前10):')
                for c in cats:
                    print(f'    {c[0]}: {c[1]}')

            # chapters 统计
            cur.execute(f'SELECT COUNT(*) FROM {CHAPTERS_TABLE}')
            ch_total = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM {CHAPTERS_TABLE} WHERE upload_status = 'pending'")
            ch_pending = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM {CHAPTERS_TABLE} WHERE audio_url IS NOT NULL AND audio_url != ''")
            ch_has_url = cur.fetchone()[0]

            print(f'\naudiobook_chapters 表: {ch_total} 条')
            print(f'  有音频URL: {ch_has_url}')
            print(f'  无音频URL: {ch_total - ch_has_url}')
            print(f'  pending:   {ch_pending}')

        print(f'\n{sep}')
    finally:
        conn.close()


# ============================================================
# 主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='有声书数据迁移: 远程 PG → 本地 PG (重构版)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--remote-dsn', default=DEFAULT_REMOTE_DSN,
                        help='远程 PostgreSQL 连接串')
    parser.add_argument('--local-dsn', default=DEFAULT_LOCAL_DSN,
                        help='本地 PostgreSQL 连接串')
    parser.add_argument('--force', action='store_true',
                        help='覆盖已有数据 (ON CONFLICT DO UPDATE)')
    parser.add_argument('--skip-chapters', action='store_true',
                        help='仅迁移 books, 不解析章节')
    parser.add_argument('--chapters-only', action='store_true',
                        help='仅解析章节 (books 已迁移)')
    args = parser.parse_args()

    sep = '=' * 60
    print(sep)
    print('  有声书数据迁移: 远程 PG → 本地 PG (重构版)')
    print(sep)
    print(f'  远程: {args.remote_dsn.split("@")[-1] if "@" in args.remote_dsn else args.remote_dsn}')
    print(f'  本地: {args.local_dsn.split("@")[-1] if "@" in args.local_dsn else args.local_dsn}')
    print(f'  覆盖: {args.force}')
    print(sep + '\n')

    # 检查依赖
    try:
        import psycopg2
        from psycopg2.extras import execute_values
        print('[OK] psycopg2 可用')
    except ImportError:
        print('[安装] psycopg2-binary...')
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'psycopg2-binary', '-q'])
        print('[OK] psycopg2 已安装')

    # 确保表结构
    print('\n>>> 确保本地表结构...')
    ensure_tables(args.local_dsn)

    # 迁移 books
    if not args.chapters_only:
        migrate_books(args.remote_dsn, args.local_dsn, force=args.force)

    # 解析章节
    if not args.skip_chapters:
        migrate_chapters(args.local_dsn, force=args.force)

    # 报告
    print_report(args.local_dsn)


if __name__ == '__main__':
    main()
