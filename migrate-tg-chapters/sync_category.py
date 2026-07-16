#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分类数据同步脚本：从远程数据库拉取分类信息，同步到本地数据库。

用法:
    # 1. 先诊断远程数据库的分类信息存储位置
    python sync_category.py --diagnose

    # 2. 执行同步（按 book_id 匹配）
    python sync_category.py --sync

    # 3. 按 book_name 匹配（当 book_id 不一致时）
    python sync_category.py --sync --match-by-name

环境变量:
    REMOTE_DSN  远程数据库连接串（有分类信息的库）
    LOCAL_DSN   本地数据库连接串（需要同步的库）
"""

from __future__ import annotations

import os
import sys
import json
import time

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:
    print("[ERROR] 需要安装 psycopg: pip install psycopg[binary]")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

REMOTE_DSN = os.environ.get(
    "REMOTE_DSN",
    "postgresql://audiobook_app:inriynisse1991@85.121.241.158:5432/audiobook",
)

LOCAL_DSN = os.environ.get(
    "LOCAL_DSN",
    os.environ.get(
        "DATABASE_URL",
        "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook",
    ),
)

# book_data JSON 中分类的可能键名
_CATEGORY_KEYS = (
    "category", "bookCategory", "tingCategory", "categoryId",
    "firstCid", "sort", "categoryName", "tagName", "bookType",
    "tingType", "type", "label", "tags",
)


def log(msg: str = ""):
    ts = time.strftime("%H:%M:%S")
    if msg:
        print(f"[{ts}] {msg}")
    else:
        print()


# ═══════════════════════════════════════════════════════════════
# 诊断：查看远程数据库的表结构和分类信息
# ═══════════════════════════════════════════════════════════════

def diagnose(remote_dsn: str):
    """诊断远程数据库，找出分类信息存储在哪里。"""
    sep = "=" * 60
    log(sep)
    log("  诊断远程数据库分类信息")
    log(sep)
    log(f"  远程数据库: {remote_dsn.split('@')[-1]}")
    log(sep)
    log()

    conn = psycopg.connect(remote_dsn)
    try:
        with conn.cursor() as cur:
            # 1. 检查 books 表的列
            log(">>> 1. 检查 books 表结构...")
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'books'
                ORDER BY ordinal_position
            """)
            columns = cur.fetchall()
            log(f"  books 表列:")
            has_category_col = False
            for col_name, col_type in columns:
                marker = " <--- 分类列!" if "categ" in col_name.lower() else ""
                if "categ" in col_name.lower():
                    has_category_col = True
                log(f"    {col_name} ({col_type}){marker}")

            if has_category_col:
                log()
                log(">>> 2. books 表有 category 列！检查数据...")
                cur.execute("""
                    SELECT COUNT(*) FROM books
                    WHERE category IS NOT NULL AND category != ''
                """)
                count = cur.fetchone()[0]
                log(f"  有分类的书籍数: {count}")

                cur.execute("""
                    SELECT category, COUNT(*) as cnt
                    FROM books
                    WHERE category IS NOT NULL AND category != ''
                    GROUP BY category
                    ORDER BY cnt DESC
                    LIMIT 20
                """)
                log(f"  分类分布（前20）:")
                for cat, cnt in cur.fetchall():
                    log(f"    {cat}: {cnt} 本")
                return "column"

            # 2. 没有独立的 category 列，检查 book_data JSON
            log()
            log(">>> 2. books 表没有 category 列，检查 book_data JSON...")
            cur.execute("SELECT book_id, book_data FROM books LIMIT 5")
            rows = cur.fetchall()
            log(f"  取样 {len(rows)} 本书，检查 book_data JSON 中的字段:")

            all_keys = set()
            category_candidates = {}

            for book_id, book_data_raw in rows:
                if isinstance(book_data_raw, str):
                    bd = json.loads(book_data_raw)
                elif isinstance(book_data_raw, dict):
                    bd = book_data_raw
                else:
                    continue

                all_keys.update(bd.keys())

                # 检查每个可能的分类键
                for key in _CATEGORY_KEYS:
                    val = bd.get(key)
                    if val is not None and str(val).strip():
                        if key not in category_candidates:
                            category_candidates[key] = []
                        if len(category_candidates[key]) < 3:
                            category_candidates[key].append((book_id, str(val)[:100]))

                # 检查 bookInfo 嵌套
                book_info = bd.get("bookInfo")
                if isinstance(book_info, dict):
                    for key in _CATEGORY_KEYS:
                        val = book_info.get(key)
                        if val is not None and str(val).strip():
                            nested_key = f"bookInfo.{key}"
                            if nested_key not in category_candidates:
                                category_candidates[nested_key] = []
                            if len(category_candidates[nested_key]) < 3:
                                category_candidates[nested_key].append((book_id, str(val)[:100]))

            log()
            log(f"  book_data 所有顶层键名: {sorted(all_keys)}")
            log()

            if category_candidates:
                log("  发现可能的分类字段:")
                for key, samples in sorted(category_candidates.items()):
                    log(f"    [{key}]:")
                    for bid, val in samples:
                        log(f"      book_id={bid} → {val}")

                # 取第一个候选作为最佳猜测
                best_key = list(category_candidates.keys())[0]
                log()
                log(f"  >>> 推荐使用分类字段: {best_key}")
                return f"json:{best_key}"
            else:
                log("  [WARNING] 未在 book_data 中找到分类字段！")
                log("  完整 book_data 示例:")
                if rows:
                    book_id, book_data_raw = rows[0]
                    if isinstance(book_data_raw, str):
                        bd = json.loads(book_data_raw)
                    else:
                        bd = book_data_raw
                    # 打印所有键和值（截断长值）
                    for k, v in bd.items():
                        if k in ("tingChapterList", "chapterList", "chapters", "list", "tingChapters", "sectionList", "chapters_data"):
                            if isinstance(v, list):
                                log(f"    {k}: [列表，{len(v)} 项]")
                            else:
                                log(f"    {k}: {str(v)[:80]}")
                        elif isinstance(v, str) and len(v) > 200:
                            log(f"    {k}: {v[:200]}...")
                        else:
                            log(f"    {k}: {v}")
                return None

    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 同步：从远程拉取分类，更新到本地
# ═══════════════════════════════════════════════════════════════

def _extract_category(book_data_raw, category_key: str):
    """从 book_data 中提取分类值。"""
    if isinstance(book_data_raw, str):
        bd = json.loads(book_data_raw)
    elif isinstance(book_data_raw, dict):
        bd = book_data_raw
    else:
        return None

    # 如果指定了嵌套键（bookInfo.xxx）
    if "." in category_key:
        parts = category_key.split(".", 1)
        parent = bd.get(parts[0])
        if isinstance(parent, dict):
            val = parent.get(parts[1])
            if val is not None and str(val).strip():
                return str(val).strip()
        return None

    # 顶层键
    val = bd.get(category_key)
    if val is not None and str(val).strip():
        return str(val).strip()
    return None


def sync(remote_dsn: str, local_dsn: str, match_by_name: bool = False):
    """从远程数据库同步分类到本地数据库。"""
    sep = "=" * 60
    log(sep)
    log("  分类数据同步：远程 → 本地")
    log(sep)
    log(f"  远程数据库: {remote_dsn.split('@')[-1]}")
    log(f"  本地数据库: {local_dsn.split('@')[-1]}")
    log(f"  匹配方式:   {'book_name' if match_by_name else 'book_id'}")
    log(sep)
    log()

    # Step 1: 连接远程数据库，检查是否有 category 列
    log(">>> 连接远程数据库...")
    remote_conn = psycopg.connect(remote_dsn)
    try:
        with remote_conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'books'
                AND column_name = 'category'
            """)
            has_category_col = cur.fetchone() is not None

            if has_category_col:
                log("[OK] 远程 books 表有 category 列，直接拉取...")
                cur.execute("""
                    SELECT book_id, book_name, category
                    FROM books
                    WHERE category IS NOT NULL AND category != ''
                """)
                remote_rows = cur.fetchall()
                log(f"[OK] 远程有分类的书籍: {len(remote_rows)} 本")

                # 构建映射
                if match_by_name:
                    remote_map = {}
                    for bid, bname, cat in remote_rows:
                        if bname:
                            remote_map[bname.strip()] = cat
                else:
                    remote_map = {}
                    for bid, bname, cat in remote_rows:
                        remote_map[str(bid).strip()] = cat
            else:
                log("[INFO] 远程 books 表没有 category 列，尝试从 book_data JSON 提取...")

                # 先诊断哪个键有分类
                cur.execute("SELECT book_id, book_data FROM books LIMIT 10")
                sample_rows = cur.fetchall()

                found_key = None
                for _, book_data_raw in sample_rows:
                    if isinstance(book_data_raw, str):
                        bd = json.loads(book_data_raw)
                    elif isinstance(book_data_raw, dict):
                        bd = book_data_raw
                    else:
                        continue

                    # 检查所有可能的分类键
                    for key in _CATEGORY_KEYS:
                        val = bd.get(key)
                        if val is not None and str(val).strip():
                            found_key = key
                            break

                    # 检查 bookInfo 嵌套
                    if not found_key:
                        book_info = bd.get("bookInfo")
                        if isinstance(book_info, dict):
                            for key in _CATEGORY_KEYS:
                                val = book_info.get(key)
                                if val is not None and str(val).strip():
                                    found_key = f"bookInfo.{key}"
                                    break

                    if found_key:
                        break

                if not found_key:
                    log("[ERROR] 无法在远程 book_data JSON 中找到分类字段！")
                    log("        请先运行: python sync_category.py --diagnose")
                    return

                log(f"[OK] 发现分类字段: {found_key}")

                # 拉取所有书籍的分类
                cur.execute("SELECT book_id, book_data FROM books")
                all_rows = cur.fetchall()

                remote_map = {}
                for bid, book_data_raw in all_rows:
                    cat = _extract_category(book_data_raw, found_key)
                    if cat:
                        if match_by_name:
                            # 需要书名
                            if isinstance(book_data_raw, str):
                                bd = json.loads(book_data_raw)
                            elif isinstance(book_data_raw, dict):
                                bd = book_data_raw
                            else:
                                continue
                            bname = (
                                bd.get("bookName")
                                or bd.get("title")
                                or bd.get("name")
                                or ""
                            )
                            if bname:
                                remote_map[bname.strip()] = cat
                        else:
                            remote_map[str(bid).strip()] = cat

                log(f"[OK] 从远程提取到分类的书籍: {len(remote_map)} 本")

    finally:
        remote_conn.close()

    if not remote_map:
        log("[ERROR] 远程数据库没有分类数据！")
        return

    log()
    log(f">>> 连接本地数据库...")

    # Step 2: 连接本地数据库，更新分类
    local_conn = psycopg.connect(local_dsn)
    try:
        with local_conn.cursor() as cur:
            if match_by_name:
                cur.execute("SELECT book_id, book_name FROM public.books")
                local_rows = cur.fetchall()
                log(f"[OK] 本地书籍总数: {len(local_rows)}")

                updated = 0
                matched = 0
                for book_id, book_name in local_rows:
                    if not book_name:
                        continue
                    cat = remote_map.get(book_name.strip())
                    if cat:
                        matched += 1
                        cur.execute(
                            "UPDATE public.books SET category = %s, updated_at = now() WHERE book_id = %s AND (category IS NULL OR category = '')",
                            (cat, book_id),
                        )
                        if cur.rowcount > 0:
                            updated += 1
            else:
                cur.execute("SELECT book_id FROM public.books")
                local_ids = [str(r[0]).strip() for r in cur.fetchall()]
                log(f"[OK] 本地书籍总数: {len(local_ids)}")

                updated = 0
                matched = 0
                for book_id in local_ids:
                    cat = remote_map.get(book_id)
                    if cat:
                        matched += 1
                        cur.execute(
                            "UPDATE public.books SET category = %s, updated_at = now() WHERE book_id = %s AND (category IS NULL OR category = '')",
                            (cat, book_id),
                        )
                        if cur.rowcount > 0:
                            updated += 1

            local_conn.commit()

            log()
            log(sep)
            log("  同步完成！")
            log(f"  远程有分类的书籍:   {len(remote_map)}")
            log(f"  本地匹配到的书籍:   {matched}")
            log(f"  实际更新分类的书籍: {updated}")
            log(sep)

            # 验证
            cur.execute("SELECT COUNT(*) FROM public.books WHERE category IS NOT NULL AND category != ''")
            total_with_cat = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM public.books")
            total = cur.fetchone()[0]
            log(f"  本地有分类的书籍:   {total_with_cat}/{total}")

            # 显示分类分布
            cur.execute("""
                SELECT category, COUNT(*) as cnt
                FROM public.books
                WHERE category IS NOT NULL AND category != ''
                GROUP BY category
                ORDER BY cnt DESC
                LIMIT 20
            """)
            log()
            log("  分类分布（前20）:")
            for cat, cnt in cur.fetchall():
                log(f"    {cat}: {cnt} 本")
            log(sep)

    finally:
        local_conn.close()


# ═══════════════════════════════════════════════════════════════
# 主函数
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="分类数据同步：从远程数据库同步分类到本地",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 诊断远程数据库分类信息
  python sync_category.py --diagnose

  # 按 book_id 匹配同步
  python sync_category.py --sync

  # 按 book_name 匹配同步（当 book_id 不一致时）
  python sync_category.py --sync --match-by-name

  # 指定连接串
  REMOTE_DSN="postgresql://user:pass@host:5432/db" \
  LOCAL_DSN="postgresql://user:pass@127.0.0.1:5432/db" \
  python sync_category.py --sync
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--diagnose", action="store_true",
                       help="诊断远程数据库，查看分类信息存储位置")
    group.add_argument("--sync", action="store_true",
                       help="执行分类同步")

    parser.add_argument("--match-by-name", action="store_true",
                        help="按 book_name 匹配（默认按 book_id 匹配）")
    parser.add_argument("--remote-dsn", default=REMOTE_DSN,
                        help=f"远程数据库连接串 (默认: {REMOTE_DSN})")
    parser.add_argument("--local-dsn", default=LOCAL_DSN,
                        help=f"本地数据库连接串 (默认: {LOCAL_DSN})")

    args = parser.parse_args()

    log("分类数据同步工具启动")
    log(f"PID: {os.getpid()}")
    log()

    try:
        if args.diagnose:
            diagnose(args.remote_dsn)
        elif args.sync:
            sync(args.remote_dsn, args.local_dsn, match_by_name=args.match_by_name)
    except KeyboardInterrupt:
        log()
        log("[WARNING] 用户中断")
        sys.exit(130)
    except Exception as e:
        log()
        log(f"[FATAL] 失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
