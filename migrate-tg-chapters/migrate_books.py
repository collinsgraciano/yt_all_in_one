#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
书籍数据迁移工具（独立运行，不依赖主项目）

从旧库「下载掌阅有声书到tg」项目的 books 表读取数据，
解析 book_data JSON，提取书名/作者/章节数等字段，
写入新库的 public.books 表。

与 migrate_tg_chapters.py 配合使用：先迁移 books 表，再迁移 chapters 表。
"""

from __future__ import annotations

import os
import sys
import json
import time
import threading
import argparse
from datetime import datetime
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
# 日志与进度工具
# ============================================================

def log(msg: str = ""):
    """带时间戳的输出。"""
    if msg:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")
    else:
        print()


class Heartbeat:
    """后台心跳线程，在长时间操作时打印进度点。"""

    def __init__(self, message: str, interval: int = 5):
        self.message = message
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._elapsed = 0

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()

    def _run(self):
        while not self._stop.wait(self.interval):
            self._elapsed += self.interval
            sys.stdout.write(f"\r{self.message} 已等待 {self._elapsed}s...")
            sys.stdout.flush()

    @property
    def elapsed(self) -> float:
        return self._elapsed


# ============================================================
# 数据解析函数（兼容多种字段名）
# ============================================================

def extract_book_name(book_data: dict, book_id: str) -> str:
    for key in ("bookName", "title", "name"):
        val = book_data.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return f"未知_{book_id}"


def extract_author(book_data: dict) -> Optional[str]:
    for key in ("bookAuthor", "author", "writer"):
        val = book_data.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def extract_chapter_list(book_data: dict) -> list:
    for key in ("tingChapterList", "chapterList", "chapters", "list", "tingChapters", "sectionList"):
        val = book_data.get(key)
        if isinstance(val, list) and val:
            return val

    book_info = book_data.get("bookInfo")
    if isinstance(book_info, dict):
        for key in ("chapters_data", "tingChapterList", "chapterList", "chapters", "list", "tingChapters", "sectionList"):
            val = book_info.get(key)
            if isinstance(val, list) and val:
                return val
    return []


def extract_total_chapters(book_data: dict) -> int:
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
            log("[ERROR] 旧库中不存在 books 表！")
            log("        请确认旧项目数据库是否已正确初始化。")
            return -1

        cur.execute("SELECT COUNT(*) FROM books")
        return cur.fetchone()[0]


def check_target_table(target_conn) -> int:
    """检查新库 books 表的字段结构并返回现有记录数。"""
    with target_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'books'
            ORDER BY ordinal_position
        """)
        columns = [row[0] for row in cur.fetchall()]
        required = {"book_id", "book_name", "author", "total_chapters", "book_data", "status"}
        missing = required - set(columns)
        if missing:
            log(f"[ERROR] 新库 books 表缺少必要字段: {missing}")
            log("        请确认新项目 docker/init-db.sql 已执行。")
            return -1

        cur.execute("SELECT COUNT(*) FROM public.books")
        return cur.fetchone()[0]


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
    log(sep)
    log("  书籍数据迁移：旧项目 → 新项目")
    log(sep)
    log(f"  源数据库:        {source_dsn.split('@')[-1]}")
    log(f"  目标数据库:      {target_dsn.split('@')[-1]}")
    log(f"  批量大小:        {batch_size}")
    log(f"  跳过已存在:      {skip_existing}")
    log(f"  覆盖已有:        {overwrite}")
    log(f"  试运行:          {dry_run}")
    log(sep)
    log()

    # 连接旧库
    log(">>> 连接源数据库...")
    source_conn = psycopg.connect(source_dsn, autocommit=True)
    try:
        source_count = check_source_table(source_conn)
        if source_count < 0:
            return
        log(f"[OK] 源库 books 表: {source_count} 条记录")

        # 连接新库
        log(">>> 连接目标数据库...")
        target_conn = psycopg.connect(target_dsn, autocommit=True)
        try:
            existing_count = check_target_table(target_conn)
            if existing_count < 0:
                return
            log(f"[OK] 目标库 books 表: 现有 {existing_count} 条记录")
            log()

            if dry_run:
                log("[INFO] 试运行模式，不会写入数据。将读取前 5 条记录进行预览...")
                _dry_run_preview(source_conn)
                return

            # 确定冲突策略
            if overwrite:
                conflict_action = """
                    ON CONFLICT (book_id) DO UPDATE SET
                        book_name = EXCLUDED.book_name,
                        author = EXCLUDED.author,
                        total_chapters = EXCLUDED.total_chapters,
                        book_data = EXCLUDED.book_data,
                        status = EXCLUDED.status,
                        updated_at = now()
                """
            else:
                conflict_action = "ON CONFLICT (book_id) DO NOTHING"

            # 分批读取并写入
            processed = 0
            inserted = 0
            skipped = 0
            errors = 0
            start_time = time.time()

            with source_conn.cursor() as src_cur:
                log(">>> 开始读取并写入数据...")
                with Heartbeat("    正在执行查询", interval=5):
                    src_cur.execute("SELECT book_id, book_data, book_status FROM books")
                    first_row = src_cur.fetchone()

                if first_row is None:
                    log("[INFO] 源库 books 表为空，没有数据需要迁移。")
                    return

                log("[OK] 数据开始流入，开始批量写入...")

                batch = []
                row = first_row

                while row is not None:
                    try:
                        book_id, book_data_raw, book_status = row

                        # psycopg3 自动解析 jsonb 为 dict
                        if isinstance(book_data_raw, str):
                            book_data = json.loads(book_data_raw)
                        elif isinstance(book_data_raw, dict):
                            book_data = book_data_raw
                        else:
                            log(f"  [SKIP] book_id={book_id}: book_data 类型异常 ({type(book_data_raw)})")
                            skipped += 1
                            row = src_cur.fetchone()
                            continue

                        book_name = extract_book_name(book_data, str(book_id))
                        author = extract_author(book_data)
                        total_chapters = extract_total_chapters(book_data)
                        status = book_status or "pending"

                        batch.append((
                            str(book_id),
                            book_name,
                            author,
                            None,             # category
                            total_chapters,
                            Jsonb(book_data),
                            [],                # tags
                            None,              # note
                            status,
                        ))

                        if len(batch) >= batch_size:
                            count = _insert_batch(target_conn, batch, conflict_action)
                            inserted += count
                            skipped += len(batch) - count
                            processed += len(batch)
                            batch = []
                            _print_progress(processed, source_count, start_time, inserted, skipped, errors)

                    except Exception as e:
                        log(f"  [ERROR] book_id={row[0]}: {e}")
                        errors += 1

                    row = src_cur.fetchone()

                # 写入最后一批
                if batch:
                    count = _insert_batch(target_conn, batch, conflict_action)
                    inserted += count
                    skipped += len(batch) - count
                    processed += len(batch)

            elapsed = time.time() - start_time

            # 最终统计
            log()
            log(sep)
            log("  迁移完成！")
            log(f"  耗时:          {elapsed:.1f} 秒 ({elapsed/60:.1f} 分钟)")
            log(f"  源库记录:      {source_count}")
            log(f"  已处理:        {processed}")
            log(f"  新增/更新:     {inserted}")
            log(f"  跳过(冲突):    {skipped}")
            log(f"  错误:          {errors}")
            if elapsed > 0:
                log(f"  平均速度:      {processed/elapsed:.0f} 条/秒")
            log()

            # 验证
            with target_conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.books")
                final_count = cur.fetchone()[0]
                log(f"  目标库现有:    {final_count} 条记录")
            log(sep)

        finally:
            target_conn.close()
    finally:
        source_conn.close()


def _print_progress(processed: int, total: int, start_time: float,
                    inserted: int, skipped: int, errors: int):
    """打印进度信息。"""
    elapsed = time.time() - start_time
    pct = processed * 100 / total if total > 0 else 0
    speed = processed / elapsed if elapsed > 0 else 0
    remaining = (total - processed) / speed if speed > 0 else 0

    if remaining > 60:
        eta_str = f"{remaining/60:.1f}分钟"
    elif remaining > 0:
        eta_str = f"{remaining:.0f}秒"
    else:
        eta_str = "—"

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] 进度: {processed:,}/{total:,} ({pct:.1f}%) | "
          f"速度: {speed:.0f}/秒 | 剩余: {eta_str} | "
          f"新增 {inserted:,} 跳过 {skipped:,} 错误 {errors}")


def _insert_batch(target_conn, batch: list, conflict_action: str) -> int:
    """批量插入数据到新库。"""
    with target_conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO public.books
                (book_id, book_name, author, category, total_chapters, book_data, tags, note, status)
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
                log(f"  [{i}] book_id={book_id} (异常类型)")
                continue

            book_name = extract_book_name(book_data, str(book_id))
            author = extract_author(book_data)
            total_chapters = extract_total_chapters(book_data)

            log(f"  [{i}] book_id={book_id}")
            log(f"      book_name={book_name}")
            log(f"      author={author}")
            log(f"      total_chapters={total_chapters}")
            log(f"      book_status={book_status}")
            log(f"      json_keys={list(book_data.keys())[:10]}")
            log()


# ============================================================
# 主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="书籍数据迁移：从「下载掌阅有声书到tg」项目迁移到当前项目",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 迁移所有书籍（跳过已存在的）
  python migrate_books.py

  # 覆盖已有记录
  python migrate_books.py --overwrite

  # 试运行（不写入数据，仅预览）
  python migrate_books.py --dry-run

  # 指定连接串
  python migrate_books.py \\
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
                        help="跳过已存在的记录（默认行为）")
    parser.add_argument("--overwrite", action="store_true",
                        help="覆盖已有记录（ON CONFLICT DO UPDATE）")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式，只读取预览不写入数据")

    args = parser.parse_args()

    log("书籍数据迁移工具启动")
    log(f"PID: {os.getpid()}")
    log()

    try:
        migrate_books(
            source_dsn=args.source_dsn,
            target_dsn=args.target_dsn,
            batch_size=args.batch_size,
            skip_existing=args.skip_existing,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        log()
        log("[WARNING] 用户中断 (Ctrl+C)，迁移已停止。")
        log("          已写入的数据不会回滚，可重新运行继续迁移。")
        sys.exit(130)
    except Exception as e:
        log()
        log(f"[FATAL] 迁移失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
