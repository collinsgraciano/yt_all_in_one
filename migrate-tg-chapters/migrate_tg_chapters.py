#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG 章节缓存迁移工具（独立运行，不依赖主项目）

从旧库「下载掌阅有声书到tg」项目的 audiobook_chapters 表读取
已上传到 Telegram 的章节记录，写入新库的 public.audiobook_chapters 表。

pipeline 处理章节时会查询此表：
  - 若章节的 audio_url 在缓存表中且有 telegram_file_id，
    则直接从 Telegram 下载已降噪音频，跳过原始下载和 DeepFilter。

Docker 运行（推荐，无需安装 Python）:

  # Linux / macOS / VPS — 前台运行
  bash run.sh --only-complete-books
  bash run.sh --dry-run

  # 后台运行（断开 SSH 也不中断，日志自动保存）
  bash run.sh --bg --only-complete-books

  # Windows CMD
  run.bat --only-complete-books

直接用 Python 运行（需已安装 psycopg）:

  python migrate_tg_chapters.py --only-complete-books
  python migrate_tg_chapters.py \\
      --source-dsn "postgresql://user:pass@old-host:5432/audiobook" \\
      --target-dsn "postgresql://user:pass@new-host:5432/audiobook"
"""

from __future__ import annotations

import os
import sys
import time
import threading
import argparse
from datetime import datetime

try:
    import psycopg
except ImportError:
    print("[ERROR] 需要安装 psycopg (psycopg3): pip install psycopg[binary]")
    sys.exit(1)


# ============================================================
# 默认配置（可通过环境变量覆盖）
# ============================================================

DEFAULT_SOURCE_DSN = os.environ.get(
    "SOURCE_DATABASE_URL",
    "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook",
)

DEFAULT_TARGET_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook",
)

DEFAULT_BATCH_SIZE = 1000


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
    """后台心跳线程，在长时间查询时打印进度点。"""

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
        # 清除心跳行
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
# 迁移逻辑
# ============================================================

def check_source_table(source_conn) -> int:
    """检查旧库 audiobook_chapters 表是否存在并返回记录数。"""
    with source_conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'audiobook_chapters'
        """)
        exists = cur.fetchone()[0]
        if not exists:
            log("[ERROR] 旧库中不存在 audiobook_chapters 表！")
            log("        请确认旧项目数据库是否已正确初始化。")
            return -1

        cur.execute("SELECT COUNT(*) FROM audiobook_chapters")
        return cur.fetchone()[0]


def check_target_table(target_conn) -> int:
    """检查新库 audiobook_chapters 表是否存在并返回现有记录数。"""
    with target_conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'audiobook_chapters'
        """)
        exists = cur.fetchone()[0]
        if not exists:
            log("[ERROR] 新库中不存在 audiobook_chapters 表！")
            log("        请确认新项目 docker/init-db.sql 已执行。")
            return -1

        cur.execute("SELECT COUNT(*) FROM public.audiobook_chapters")
        return cur.fetchone()[0]


def fetch_complete_book_ids(source_conn, timeout_seconds: int = 600) -> list[str]:
    """查询旧库中所有章节都已上传到 Telegram 的 book_id 列表。

    判定条件：该书在 audiobook_chapters 中的所有记录
    都满足 upload_status='uploaded' 且 telegram_file_id IS NOT NULL。
    """
    with source_conn.cursor() as cur:
        # 设置查询超时，避免无限等待
        cur.execute(f"SET statement_timeout = {timeout_seconds * 1000}")

        # 先查总数，给用户一个预期
        cur.execute("SELECT COUNT(DISTINCT book_id) FROM audiobook_chapters")
        total_books = cur.fetchone()[0]
        log(f"    源库共 {total_books} 本书，正在筛选全部已上传到 TG 的...")

        with Heartbeat("    正在查询", interval=5) as hb:
            cur.execute("""
                SELECT book_id
                FROM audiobook_chapters
                GROUP BY book_id
                HAVING COUNT(*) = COUNT(
                    CASE WHEN upload_status = 'uploaded'
                          AND telegram_file_id IS NOT NULL
                          AND telegram_file_id != ''
                    THEN 1 END
                )
            """)
            result = [str(row[0]) for row in cur.fetchall()]

        if hb.elapsed > 0:
            log(f"    查询完成，耗时约 {hb.elapsed}s")
        return result


def migrate_chapters(
    source_dsn: str,
    target_dsn: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    only_uploaded: bool = False,
    only_complete_books: bool = False,
    dry_run: bool = False,
):
    """执行迁移。"""

    sep = "=" * 60
    log(sep)
    log("  TG 章节缓存迁移：旧项目 → 新项目")
    log(sep)
    log(f"  源数据库:        {source_dsn.split('@')[-1]}")
    log(f"  目标数据库:      {target_dsn.split('@')[-1]}")
    log(f"  批量大小:        {batch_size}")
    log(f"  仅已上传:        {only_uploaded}")
    log(f"  仅完整书籍:      {only_complete_books}")
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
        log(f"[OK] 源库 audiobook_chapters 表: {source_count} 条记录")

        # 连接新库
        log(">>> 连接目标数据库...")
        target_conn = psycopg.connect(target_dsn, autocommit=True)
        try:
            existing_count = check_target_table(target_conn)
            if existing_count < 0:
                return
            log(f"[OK] 目标库 audiobook_chapters 表: 现有 {existing_count} 条记录")
            log()

            # 查询完整书籍列表（仅当 --only-complete-books 时）
            complete_book_ids: list[str] = []
            if only_complete_books:
                log(f">>> 查询整本全部上传到 Telegram 的书籍 (源库 {source_count} 条记录)...")
                log("    如查询很慢，可在源库创建索引加速:")
                log("    CREATE INDEX ON audiobook_chapters(book_id, upload_status, telegram_file_id);")
                complete_book_ids = fetch_complete_book_ids(source_conn, timeout_seconds=600)
                log(f"[OK] 共找到 {len(complete_book_ids)} 本全部章节已上传到 TG 的书")
                if not complete_book_ids:
                    log("[INFO] 没有符合条件的书籍，迁移结束。")
                    return
                # 显示前几本书的预览
                for bid in complete_book_ids[:5]:
                    log(f"  - book_id={bid}")
                if len(complete_book_ids) > 5:
                    log(f"  ... 共 {len(complete_book_ids)} 本")
                log()

            # 构建查询
            query = "SELECT book_id, chapter_id, book_name, chapter_name, audio_url, telegram_file_id, telegram_message_id, upload_status, uploaded_at FROM audiobook_chapters"
            query_params = []
            where_clauses = []

            if only_complete_books and complete_book_ids:
                # 按完整书籍过滤（优先级高于 only_uploaded）
                where_clauses.append("book_id = ANY(%s)")
                query_params.append(complete_book_ids)
            elif only_uploaded:
                where_clauses.append("upload_status = 'uploaded' AND telegram_file_id IS NOT NULL")

            if where_clauses:
                query += " WHERE " + " AND ".join(where_clauses)

            if dry_run:
                log("[INFO] 试运行模式，不会写入数据。将读取前 10 条记录进行预览...")
                with source_conn.cursor() as cur:
                    cur.execute(f"{query} LIMIT 10", query_params)
                    cols = [desc.name for desc in cur.description]
                    log(f"  字段: {cols}")
                    for i, row in enumerate(cur, 1):
                        log(f"  [{i}] book_id={row[0]} chapter_id={row[1]} status={row[7]} file_id={(row[5] or '')[:30]}...")
                return

            # 先查询符合条件的总数，用于进度计算
            count_query = f"SELECT COUNT(*) FROM ({query}) AS subq"
            with source_conn.cursor() as cur:
                cur.execute(count_query, query_params)
                total_to_migrate = cur.fetchone()[0]
            log(f">>> 待迁移记录: {total_to_migrate} 条")
            log()

            # 分批读取并写入
            processed = 0
            inserted = 0
            skipped = 0
            errors = 0
            tg_cached = 0
            start_time = time.time()

            with source_conn.cursor() as src_cur:
                # 设置较大的 itersize 提高网络效率
                log(">>> 开始读取并写入数据...")
                with Heartbeat("    正在执行查询", interval=5) as hb:
                    src_cur.execute(query, query_params)
                    # 尝试获取第一行，确认查询开始返回数据
                    first_row = src_cur.fetchone()

                if first_row is None:
                    log("[INFO] 没有数据需要迁移。")
                    return

                log("[OK] 数据开始流入，开始批量写入...")

                batch = []
                row = first_row

                while row is not None:
                    try:
                        book_id, chapter_id, book_name, chapter_name, audio_url, \
                            telegram_file_id, telegram_message_id, upload_status, uploaded_at = row

                        batch.append((
                            str(book_id) if book_id else None,
                            str(chapter_id) if chapter_id else None,
                            book_name,
                            chapter_name,
                            audio_url,
                            telegram_file_id,
                            telegram_message_id,
                            upload_status or "pending",
                            uploaded_at,
                        ))

                        if telegram_file_id:
                            tg_cached += 1

                        if len(batch) >= batch_size:
                            count = _insert_batch(target_conn, batch)
                            inserted += count
                            skipped += len(batch) - count
                            processed += len(batch)
                            batch = []
                            _print_progress(processed, total_to_migrate, start_time, inserted, skipped, errors)

                    except Exception as e:
                        log(f"  [ERROR] record book_id={row[0]} chapter_id={row[1]}: {e}")
                        errors += 1

                    row = src_cur.fetchone()

                # 写入最后一批
                if batch:
                    count = _insert_batch(target_conn, batch)
                    inserted += count
                    skipped += len(batch) - count
                    processed += len(batch)

            elapsed = time.time() - start_time

            # 最终统计
            log()
            log(sep)
            log("  迁移完成！")
            log(f"  耗时:          {elapsed:.1f} 秒 ({elapsed/60:.1f} 分钟)")
            log(f"  待迁移记录:    {total_to_migrate}")
            log(f"  已处理:        {processed}")
            log(f"  新增:          {inserted}")
            log(f"  跳过(冲突):    {skipped}")
            log(f"  错误:          {errors}")
            log(f"  有TG缓存:     {tg_cached}")
            if elapsed > 0:
                log(f"  平均速度:      {processed/elapsed:.0f} 条/秒")
            log()

            # 验证
            with target_conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.audiobook_chapters")
                final_count = cur.fetchone()[0]
                log(f"  目标库现有:    {final_count} 条记录")

                cur.execute("SELECT COUNT(*) FROM public.audiobook_chapters WHERE telegram_file_id IS NOT NULL")
                tg_count = cur.fetchone()[0]
                log(f"  有TG缓存的:    {tg_count} 条记录")
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


def _insert_batch(target_conn, batch: list) -> int:
    """批量插入数据到新库。"""
    with target_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.audiobook_chapters
                (book_id, chapter_id, book_name, chapter_name, audio_url,
                 telegram_file_id, telegram_message_id, upload_status, uploaded_at)
            VALUES %s
            ON CONFLICT (book_id, chapter_id) DO UPDATE SET
                telegram_file_id = COALESCE(EXCLUDED.telegram_file_id, public.audiobook_chapters.telegram_file_id),
                telegram_message_id = COALESCE(EXCLUDED.telegram_message_id, public.audiobook_chapters.telegram_message_id),
                upload_status = EXCLUDED.upload_status,
                uploaded_at = COALESCE(EXCLUDED.uploaded_at, public.audiobook_chapters.uploaded_at),
                book_name = COALESCE(EXCLUDED.book_name, public.audiobook_chapters.book_name),
                chapter_name = COALESCE(EXCLUDED.chapter_name, public.audiobook_chapters.chapter_name),
                audio_url = COALESCE(EXCLUDED.audio_url, public.audiobook_chapters.audio_url)
        """, batch)
        return cur.rowcount


# ============================================================
# 主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="TG 章节缓存迁移：从「下载掌阅有声书到tg」项目迁移 audiobook_chapters 数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 仅迁移已上传到 TG 的章节
  python migrate_tg_chapters.py --only-uploaded

  # 仅迁移整本全部上传到 TG 的书
  python migrate_tg_chapters.py --only-complete-books

  # 试运行（不写入数据，仅预览）
  python migrate_tg_chapters.py --dry-run

  # 指定连接串
  python migrate_tg_chapters.py \\
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
    parser.add_argument("--only-uploaded", action="store_true",
                        help="仅迁移已上传到 Telegram 的章节（有 telegram_file_id 的记录）")
    parser.add_argument("--only-complete-books", action="store_true",
                        help="仅迁移整本全部章节已上传到 Telegram 的书（优先级高于 --only-uploaded）")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式，只读取预览不写入数据")

    args = parser.parse_args()

    log("TG 章节缓存迁移工具启动")
    log(f"PID: {os.getpid()}")
    log()

    try:
        migrate_chapters(
            source_dsn=args.source_dsn,
            target_dsn=args.target_dsn,
            batch_size=args.batch_size,
            only_uploaded=args.only_uploaded,
            only_complete_books=args.only_complete_books,
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
