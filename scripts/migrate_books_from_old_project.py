#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
书籍数据迁移脚本：从「下载掌阅有声书到tg」项目迁移到当前项目。

从旧库的 books 表（3 列：book_id, book_data, book_status）读取数据，
解析 book_data JSON，提取书名/作者/章节数等字段，
写入新库的 public.books 表（11 列）。

使用方法：
    python scripts/migrate_books_from_old_project.py
    python scripts/migrate_books_from_old_project.py --skip-existing
    python scripts/migrate_books_from_old_project.py --overwrite
    python scripts/migrate_books_from_old_project.py --dry-run
    python scripts/migrate_books_from_old_project.py \
        --source-dsn "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook" \
        --target-dsn "postgresql://audiobook_app:your_password@127.0.0.1:5432/audiobook"
"""

from __future__ import annotations

import os
import sys
import json
import argparse
from typing import Optional

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:
    print("[ERROR] 需要安装 psycopg (psycopg3): pip install psycopg[binary]")
    sys.exit(1)


# ============================================================
# 默认配置
# ============================================================

DEFAULT_SOURCE_DSN = os.environ.get(
    "SOURCE_DATABASE_URL",
    "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook",
)

DEFAULT_TARGET_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook",
)

DEFAULT_BATCH_SIZE = 500


# ============================================================
# 数据解析函数（与旧项目 migrate.py 逻辑一致，兼容多种字段名）
# ============================================================

def extract_book_name(book_data: dict, book_id: str) -> str:
    """从 book_data 中提取书名。"""
    for key in ("bookName", "title", "name"):
        val = book_data.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return f"未知_{book_id}"


def extract_author(book_data: dict) -> Optional[str]:
    """从 book_data 中提取作者。"""
    for key in ("bookAuthor", "author", "writer"):
        val = book_data.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


# ── 分类提取可能的键名 ──
_CATEGORY_KEYS = ("category", "bookCategory", "tingCategory", "categoryId", "firstCid", "sort")


def extract_category(book_data: dict) -> Optional[str]:
    """从 book_data JSON 中尝试提取分类，兼容多种字段名。"""
    for key in _CATEGORY_KEYS:
        val = book_data.get(key)
        if val and str(val).strip():
            return str(val).strip()
    book_info = book_data.get("bookInfo")
    if isinstance(book_info, dict):
        for key in _CATEGORY_KEYS:
            val = book_info.get(key)
            if val and str(val).strip():
                return str(val).strip()
    return None


def extract_chapter_list(book_data: dict) -> list:
    """从 book_data 中提取章节列表，兼容多种字段名和嵌套结构。"""
    # 1. 查顶层字段
    for key in ("tingChapterList", "chapterList", "chapters", "list", "tingChapters", "sectionList"):
        val = book_data.get(key)
        if isinstance(val, list) and val:
            return val

    # 2. 查嵌套在 bookInfo 中的字段
    book_info = book_data.get("bookInfo")
    if isinstance(book_info, dict):
        for key in ("chapters_data", "tingChapterList", "chapterList", "chapters", "list", "tingChapters", "sectionList"):
            val = book_info.get(key)
            if isinstance(val, list) and val:
                return val

    return []


def extract_total_chapters(book_data: dict) -> int:
    """从 book_data 中提取章节总数。"""
    chapters = extract_chapter_list(book_data)
    return len(chapters) if chapters else 0


# ============================================================
# 迁移逻辑
# ============================================================

def check_source_table(source_conn) -> int:
    """检查旧库 books 表是否存在并返回记录数。"""
    with source_conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'books'
        """)
        exists = cur.fetchone()[0]
        if not exists:
            print("[ERROR] 旧库中不存在 books 表！")
            print("        请确认旧项目数据库是否已正确初始化（运行过 migrate.py 或 init.sql）。")
            return -1

        cur.execute("SELECT COUNT(*) FROM books")
        total = cur.fetchone()[0]
        return total


def check_target_table(target_conn) -> int:
    """检查新库 books 表的字段结构并返回现有记录数。"""
    with target_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'books'
            ORDER BY ordinal_position
        """)
        columns = [row[0] for row in cur.fetchall()]
        required = {"book_id", "book_name", "author", "total_chapters", "book_data", "status", "book_status"}
        missing = required - set(columns)
        if missing:
            print(f"[ERROR] 新库 books 表缺少必要字段: {missing}")
            print("        请确认新项目 docker/init-db.sql 已执行。")
            return -1

        cur.execute("SELECT COUNT(*) FROM public.books")
        existing = cur.fetchone()[0]
        return existing


def migrate_books(
    source_dsn: str,
    target_dsn: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    skip_existing: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
):
    """执行迁移。"""

    sep = "=" * 60
    print(sep)
    print("  书籍数据迁移：旧项目 → 新项目")
    print(sep)
    print(f"  源数据库:    {source_dsn.split('@')[-1]}")
    print(f"  目标数据库:  {target_dsn.split('@')[-1]}")
    print(f"  批量大小:    {batch_size}")
    print(f"  跳过已存在:  {skip_existing}")
    print(f"  覆盖已有:    {overwrite}")
    print(f"  试运行:      {dry_run}")
    print(sep)
    print()

    # 连接旧库
    print(">>> 连接源数据库...")
    source_conn = psycopg.connect(source_dsn, autocommit=True)
    try:
        source_count = check_source_table(source_conn)
        if source_count < 0:
            return
        print(f"[OK] 源库 books 表: {source_count} 条记录")

        # 连接新库
        print(">>> 连接目标数据库...")
        target_conn = psycopg.connect(target_dsn, autocommit=True)
        try:
            existing_count = check_target_table(target_conn)
            if existing_count < 0:
                return
            print(f"[OK] 目标库 books 表: 现有 {existing_count} 条记录")
            print()

            if dry_run:
                print("[INFO] 试运行模式，不会写入数据。将读取前 5 条记录进行预览...")
                _dry_run_preview(source_conn)
                return

            # 确定冲突策略
            if overwrite:
                conflict_action = """
                    ON CONFLICT (book_id) DO UPDATE SET
                        book_name = EXCLUDED.book_name,
                        author = EXCLUDED.author,
                        category = COALESCE(EXCLUDED.category, books.category),
                        total_chapters = EXCLUDED.total_chapters,
                        book_data = EXCLUDED.book_data,
                        status = EXCLUDED.status,
                        book_status = EXCLUDED.book_status,
                        updated_at = now()
                """
            else:
                conflict_action = "ON CONFLICT (book_id) DO NOTHING"

            # 分批读取并写入
            with source_conn.cursor() as src_cur:
                src_cur.execute("SELECT book_id, book_data, book_status FROM books")

                processed = 0
                inserted = 0
                skipped = 0
                errors = 0
                batch = []

                for book_id, book_data_raw, book_status in src_cur:
                    try:
                        # psycopg3 自动解析 jsonb 为 dict
                        if isinstance(book_data_raw, str):
                            book_data = json.loads(book_data_raw)
                        elif isinstance(book_data_raw, dict):
                            book_data = book_data_raw
                        else:
                            print(f"  [SKIP] book_id={book_id}: book_data 类型异常 ({type(book_data_raw)})")
                            skipped += 1
                            continue

                        book_name = extract_book_name(book_data, str(book_id))
                        author = extract_author(book_data)
                        total_chapters = extract_total_chapters(book_data)
                        category = extract_category(book_data)
                        status = book_status or "pending"

                        batch.append((
                            str(book_id),
                            book_name,
                            author,
                            category,        # category (from book_data JSON)
                            total_chapters,
                            Jsonb(book_data),
                            [],                # tags
                            None,              # note
                            status,
                            book_status or "pending",  # book_status (章节完成标记)
                        ))

                        if len(batch) >= batch_size:
                            count = _insert_batch(target_conn, batch, conflict_action)
                            inserted += count
                            skipped += len(batch) - count
                            processed += len(batch)
                            batch = []
                            print(f"  进度: {processed}/{source_count} (新增 {inserted}, 跳过 {skipped})")

                    except Exception as e:
                        print(f"  [ERROR] book_id={book_id}: {e}")
                        errors += 1
                        continue

                # 写入最后一批
                if batch:
                    count = _insert_batch(target_conn, batch, conflict_action)
                    inserted += count
                    skipped += len(batch) - count
                    processed += len(batch)

            # 最终统计
            print()
            print(sep)
            print("  迁移完成！")
            print(f"  源库记录:     {source_count}")
            print(f"  处理:          {processed}")
            print(f"  新增/更新:     {inserted}")
            print(f"  跳过(冲突):    {skipped}")
            print(f"  错误:          {errors}")
            print()

            # 验证
            with target_conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.books")
                final_count = cur.fetchone()[0]
                print(f"  目标库现有:    {final_count} 条记录")
                print(sep)

        finally:
            target_conn.close()
    finally:
        source_conn.close()


def _insert_batch(target_conn, batch: list, conflict_action: str) -> int:
    """批量插入数据到新库。"""
    with target_conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO public.books
                (book_id, book_name, author, category, total_chapters, book_data, tags, note, status, book_status)
            VALUES %s
            {conflict_action}
        """, batch)
        return cur.rowcount


def _dry_run_preview(source_conn):
    """试运行：读取并预览前 5 条记录。"""
    with source_conn.cursor() as cur:
        cur.execute("SELECT book_id, book_data, book_status FROM books LIMIT 5")
        for i, (book_id, book_data_raw, book_status) in enumerate(cur, 1):
            if isinstance(book_data_raw, str):
                book_data = json.loads(book_data_raw)
            elif isinstance(book_data_raw, dict):
                book_data = book_data_raw
            else:
                print(f"  [{i}] book_id={book_id} (异常类型)")
                continue

            book_name = extract_book_name(book_data, str(book_id))
            author = extract_author(book_data)
            total_chapters = extract_total_chapters(book_data)

            print(f"  [{i}] book_id={book_id}")
            print(f"      book_name={book_name}")
            print(f"      author={author}")
            print(f"      total_chapters={total_chapters}")
            print(f"      book_status={book_status}")
            # 显示 JSON 中的顶层字段名
            print(f"      json_keys={list(book_data.keys())[:10]}")
            print()


# ============================================================
# 主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="书籍数据迁移：从「下载掌阅有声书到tg」项目迁移到当前项目",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用默认配置
  python scripts/migrate_books_from_old_project.py

  # 跳过已存在的记录
  python scripts/migrate_books_from_old_project.py --skip-existing

  # 覆盖已有记录
  python scripts/migrate_books_from_old_project.py --overwrite

  # 试运行（不写入数据，仅预览）
  python scripts/migrate_books_from_old_project.py --dry-run

  # 指定连接串
  python scripts/migrate_books_from_old_project.py \\
      --source-dsn "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook" \\
      --target-dsn "postgresql://audiobook_app:password@127.0.0.1:5432/audiobook"
        """,
    )
    parser.add_argument("--source-dsn", default=DEFAULT_SOURCE_DSN,
                        help=f"源数据库连接串 (默认: {DEFAULT_SOURCE_DSN})")
    parser.add_argument("--target-dsn", default=DEFAULT_TARGET_DSN,
                        help=f"目标数据库连接串 (默认: {DEFAULT_TARGET_DSN})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"批量写入大小 (默认: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--skip-existing", action="store_true",
                        help="跳过已存在的记录（默认行为，等同 ON CONFLICT DO NOTHING）")
    parser.add_argument("--overwrite", action="store_true",
                        help="覆盖已有记录（ON CONFLICT DO UPDATE）")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式，只读取预览不写入数据")

    args = parser.parse_args()

    migrate_books(
        source_dsn=args.source_dsn,
        target_dsn=args.target_dsn,
        batch_size=args.batch_size,
        skip_existing=args.skip_existing,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
