"""书籍管理 API。"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query
from psycopg import sql
from psycopg.types.json import Jsonb

from ..database import fetch_all, fetch_one, execute
from ..models.book import BookCreate, BookUpdate, BookTagsUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/books", tags=["书籍管理"])

# ── book_data JSON 中分类/章节列表的可能键名 ──
# ⚠️ 参考 DATABASE_GUIDE_FOR_AI.md 第3.2节：存在两套 category
#   1. book_data->'_top_level'->>'category'  — 书架分类(旧库顶层列, 覆盖率~95%, 推荐)
#   2. book_data->>'category'                — 掌阅原始分类(JSON内部, 覆盖率~23%)
# 提取顺序: _top_level.category → 顶层 category/bookCategory/... → bookInfo 嵌套
_CATEGORY_KEYS = ("category", "bookCategory", "tingCategory", "categoryId", "firstCid", "sort")
_CHAPTER_LIST_KEYS = (
    "chapters_data", "tingChapterList", "chapterList", "chapters",
    "list", "tingChapters", "sectionList",
)


def _extract_category_from_book_data(book_data: dict) -> str | None:
    """从 book_data JSON 中尝试提取分类。

    提取顺序（参考 DATABASE_GUIDE_FOR_AI.md 第3.2节）:
      1. book_data._top_level.category  — 书架分类(旧库顶层列, 覆盖率~95%)
      2. book_data.category 等          — 掌阅原始分类(JSON内部, 覆盖率~23%)
      3. book_data.bookInfo.category 等  — 嵌套结构
    """
    if not isinstance(book_data, dict):
        return None

    # 优先: _top_level.category (书架分类, 覆盖率最高 ~95%)
    top_level = book_data.get("_top_level")
    if isinstance(top_level, dict):
        val = top_level.get("category")
        if val and str(val).strip():
            return str(val).strip()

    # 回退: JSON 顶层的分类键 (掌阅原始分类, ~23%)
    for key in _CATEGORY_KEYS:
        val = book_data.get(key)
        if val and str(val).strip():
            return str(val).strip()

    # 最后回退: 嵌套在 bookInfo 中
    book_info = book_data.get("bookInfo")
    if isinstance(book_info, dict):
        for key in _CATEGORY_KEYS:
            val = book_info.get(key)
            if val and str(val).strip():
                return str(val).strip()
    return None


def _count_chapters_from_book_data(book_data: dict) -> int:
    """从 book_data JSON 中计算章节数。"""
    if not isinstance(book_data, dict):
        return 0
    for key in _CHAPTER_LIST_KEYS:
        val = book_data.get(key)
        if isinstance(val, list) and val:
            return len(val)
    book_info = book_data.get("bookInfo")
    if isinstance(book_info, dict):
        for key in _CHAPTER_LIST_KEYS:
            val = book_info.get(key)
            if isinstance(val, list) and val:
                return len(val)
    return 0


@router.get("")
async def list_books(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    category: str = Query(None),
    search: str = Query(None),
):
    """获取书籍列表（分页，含 TG 缓存章节统计）。

    分类提取使用三层 COALESCE（参考 DATABASE_GUIDE_FOR_AI.md 第3.2节）:
      1. b.category 列（已回填的缓存）
      2. book_data->'_top_level'->>'category'（书架分类，~95%覆盖率）
      3. book_data->>'category'（掌阅原始分类，~23%覆盖率）
    """
    # 三层分类回退 SQL 表达式
    cat_expr = "COALESCE(b.category, b.book_data->'_top_level'->>'category', b.book_data->>'category')"

    conditions = []
    params = []
    if category:
        conditions.append(f"{cat_expr} = %s")
        params.append(category)
    if search:
        conditions.append("(b.book_name ILIKE %s OR b.author ILIKE %s OR b.book_id ILIKE %s)")
        params.extend([f"%{search}%"] * 3)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size

    count_row = fetch_one(
        sql.SQL(f"SELECT COUNT(*) AS cnt FROM public.books b{where_clause}"),
        tuple(params),
    )
    total = count_row["cnt"] if count_row else 0

    rows = fetch_all(
        sql.SQL(f"""
            SELECT b.book_id, b.book_name, b.author,
                   {cat_expr} AS category,
                   b.total_chapters,
                   b.tags, b.status, b.note, b.created_at, b.updated_at, b.book_data,
                   COALESCE(ch.ch_total, 0)       AS ch_total,
                   COALESCE(ch.ch_uploaded, 0)    AS ch_uploaded,
                   COALESCE(ch.ch_failed, 0)      AS ch_failed,
                   COALESCE(ch.ch_has_bot_uid, 0) AS ch_has_bot_uid
            FROM public.books b
            LEFT JOIN (
                SELECT book_id,
                       COUNT(*)                                          AS ch_total,
                       COUNT(*) FILTER (WHERE upload_status = 'uploaded') AS ch_uploaded,
                       COUNT(*) FILTER (WHERE upload_status = 'failed')   AS ch_failed,
                       COUNT(*) FILTER (WHERE telegram_bot_user_id IS NOT NULL) AS ch_has_bot_uid
                FROM public.audiobook_chapters
                GROUP BY book_id
            ) ch ON ch.book_id = b.book_id
            {where_clause}
            ORDER BY b.created_at DESC
            LIMIT %s OFFSET %s
        """),
        tuple(params) + (page_size, offset),
    )
    # 对 total_chapters 为空的记录，从 book_data JSON 回退提取
    for row in rows:
        if not row.get("total_chapters"):
            raw = row.get("book_data")
            if raw:
                bd = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(bd, dict):
                    row["total_chapters"] = _count_chapters_from_book_data(bd)
        row.pop("book_data", None)  # 不返回完整 book_data 到前端
    return {"books": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/categories")
async def list_categories():
    """获取书籍分类列表。

    使用三层 COALESCE 从 JSONB 提取分类（参考 DATABASE_GUIDE_FOR_AI.md 第6.2节）:
      1. category 列（已回填的缓存）
      2. book_data->'_top_level'->>'category'（书架分类，~95%覆盖率）
      3. book_data->>'category'（掌阅原始分类，~23%覆盖率）
    """
    rows = fetch_all(
        sql.SQL("""
            SELECT COALESCE(
                       category,
                       book_data->'_top_level'->>'category',
                       book_data->>'category'
                   ) AS category,
                   COUNT(*) AS cnt
            FROM public.books
            WHERE COALESCE(
                      category,
                      book_data->'_top_level'->>'category',
                      book_data->>'category'
                  ) IS NOT NULL
            GROUP BY 1
            ORDER BY cnt DESC
        """)
    )
    categories = [{"category": r["category"], "count": r["cnt"]} for r in rows]
    return {"categories": categories}


@router.post("/backfill-category")
async def backfill_category():
    """从 book_data JSONB 回填 category 列。

    参考 DATABASE_GUIDE_FOR_AI.md 第3.2节，使用三层 COALESCE:
      1. book_data->'_top_level'->>'category'（书架分类，~95%覆盖率）
      2. book_data->>'category'（掌阅原始分类，~23%覆盖率）
      3. NULL（无分类数据）

    只更新 category 列为空的记录，不覆盖已有值。
    回填后 list_books 的 SQL COALESCE 会直接命中 category 列，查询更快。
    """
    result_row = fetch_one(
        sql.SQL("""
            WITH updated AS (
                UPDATE public.books
                SET category = COALESCE(
                        book_data->'_top_level'->>'category',
                        book_data->>'category'
                    ),
                    updated_at = now()
                WHERE (category IS NULL OR category = '')
                  AND COALESCE(
                          book_data->'_top_level'->>'category',
                          book_data->>'category'
                      ) IS NOT NULL
                RETURNING book_id
            )
            SELECT COUNT(*) AS updated_cnt FROM updated
        """)
    )
    updated_cnt = result_row["updated_cnt"] if result_row else 0

    # 统计回填后的分类覆盖情况
    verify_row = fetch_one(
        sql.SQL("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE category IS NOT NULL AND category != '') AS with_cat,
                   COUNT(*) FILTER (WHERE COALESCE(category, book_data->'_top_level'->>'category', book_data->>'category') IS NOT NULL) AS with_cat_jsonb
            FROM public.books
        """)
    )

    return {
        "message": f"回填完成：更新 {updated_cnt} 条记录的 category 列",
        "updated": updated_cnt,
        "total_books": verify_row["total"] if verify_row else 0,
        "with_category_column": verify_row["with_cat"] if verify_row else 0,
        "with_category_jsonb": verify_row["with_cat_jsonb"] if verify_row else 0,
    }


@router.post("/sync-category-from-remote")
async def sync_category_from_remote(
    remote_dsn: str = Query(None),
    match_by_name: bool = Query(False),
):
    """从远程数据库同步分类信息到本地。

    连接远程数据库，读取所有有分类的书籍，然后按 book_id（或 book_name）
    匹配并更新本地数据库的 category 列。

    - remote_dsn: 远程数据库连接串（不传则用默认值）
    - match_by_name: 是否按 book_name 匹配（默认按 book_id）
    """
    import psycopg as _psycopg

    # 默认远程 DSN
    if not remote_dsn:
        remote_dsn = "postgresql://audiobook_app:inriynisse1991@85.121.241.158:5432/audiobook"

    # 安全日志：不打印密码
    safe_remote = remote_dsn.split("@")[-1] if "@" in remote_dsn else remote_dsn
    logger.info("开始从远程数据库同步分类: %s, match_by_name=%s", safe_remote, match_by_name)

    # 分类可能的 JSON 键名（用于远程没有 category 列时从 book_data 提取）
    remote_category_keys = (
        "category", "bookCategory", "tingCategory", "categoryId",
        "firstCid", "sort", "categoryName", "tagName",
    )

    # Step 1: 连接远程数据库
    try:
        remote_conn = _psycopg.connect(remote_dsn)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"无法连接远程数据库: {e}")

    try:
        with remote_conn.cursor() as cur:
            # 检查远程是否有 category 列
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'books'
                AND column_name = 'category'
            """)
            has_category_col = cur.fetchone() is not None

            remote_map: dict[str, str] = {}

            if has_category_col:
                logger.info("远程 books 表有 category 列，直接拉取...")
                if match_by_name:
                    cur.execute("""
                        SELECT book_name, category
                        FROM books
                        WHERE category IS NOT NULL AND category != ''
                    """)
                    for bname, cat in cur.fetchall():
                        if bname and cat:
                            remote_map[bname.strip()] = cat
                else:
                    cur.execute("""
                        SELECT book_id, category
                        FROM books
                        WHERE category IS NOT NULL AND category != ''
                    """)
                    for bid, cat in cur.fetchall():
                        if bid and cat:
                            remote_map[str(bid).strip()] = cat
            else:
                logger.info("远程 books 表没有 category 列，从 book_data JSON 提取...")

                # 先找到哪个键有分类值
                cur.execute("SELECT book_id, book_data FROM books LIMIT 10")
                sample_rows = cur.fetchall()

                found_key = None
                for _, book_data_raw in sample_rows:
                    bd = json.loads(book_data_raw) if isinstance(book_data_raw, str) else book_data_raw
                    if not isinstance(bd, dict):
                        continue
                    for key in remote_category_keys:
                        val = bd.get(key)
                        if val is not None and str(val).strip():
                            found_key = key
                            break
                    if found_key:
                        break

                if not found_key:
                    # 收集所有键名供调试
                    all_keys: set[str] = set()
                    for _, book_data_raw in sample_rows:
                        bd = json.loads(book_data_raw) if isinstance(book_data_raw, str) else book_data_raw
                        if isinstance(bd, dict):
                            all_keys.update(bd.keys())
                    raise HTTPException(
                        status_code=422,
                        detail=f"远程 book_data JSON 中未找到分类字段。可用键名: {sorted(all_keys)[:30]}",
                    )

                logger.info("远程分类字段: %s", found_key)

                # 拉取所有书籍
                cur.execute("SELECT book_id, book_data FROM books")
                for bid, book_data_raw in cur.fetchall():
                    bd = json.loads(book_data_raw) if isinstance(book_data_raw, str) else book_data_raw
                    if not isinstance(bd, dict):
                        continue
                    cat_val = bd.get(found_key)
                    if cat_val and str(cat_val).strip():
                        cat = str(cat_val).strip()
                        if match_by_name:
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

            remote_count = len(remote_map)
            logger.info("远程有分类的书籍: %d 本", remote_count)

            if remote_count == 0:
                raise HTTPException(status_code=422, detail="远程数据库没有分类数据")

    finally:
        remote_conn.close()

    # Step 2: 更新本地数据库
    updated = 0
    matched = 0
    if match_by_name:
        local_rows = fetch_all(
            sql.SQL("SELECT book_id, book_name FROM public.books")
        )
        for row in local_rows:
            bname = row.get("book_name", "")
            if not bname:
                continue
            cat = remote_map.get(bname.strip())
            if cat:
                matched += 1
                cnt = execute(
                    sql.SQL("UPDATE public.books SET category = %s, updated_at = now() WHERE book_id = %s AND (category IS NULL OR category = '')"),
                    (cat, row["book_id"]),
                )
                if cnt > 0:
                    updated += 1
    else:
        local_rows = fetch_all(
            sql.SQL("SELECT book_id FROM public.books")
        )
        for row in local_rows:
            bid = str(row.get("book_id", "")).strip()
            if not bid:
                continue
            cat = remote_map.get(bid)
            if cat:
                matched += 1
                cnt = execute(
                    sql.SQL("UPDATE public.books SET category = %s, updated_at = now() WHERE book_id = %s AND (category IS NULL OR category = '')"),
                    (cat, row["book_id"]),
                )
                if cnt > 0:
                    updated += 1

    # 验证
    verify_row = fetch_one(
        sql.SQL("SELECT COUNT(*) AS cnt FROM public.books WHERE category IS NOT NULL AND category != ''")
    )
    local_with_cat = verify_row["cnt"] if verify_row else 0

    return {
        "message": f"同步完成：远程 {remote_count} 本有分类，匹配 {matched} 本，更新 {updated} 本。本地现有分类 {local_with_cat} 本",
        "remote_with_category": remote_count,
        "matched": matched,
        "updated": updated,
        "local_with_category": local_with_cat,
        "match_by_name": match_by_name,
    }


@router.get("/{book_id}")
async def get_book(book_id: str):
    """获取单本书详情。"""
    row = fetch_one(
        sql.SQL("SELECT * FROM public.books WHERE book_id = %s"),
        (book_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return row


@router.post("")
async def create_book(body: BookCreate):
    """新增书籍。"""
    existing = fetch_one(
        sql.SQL("SELECT book_id FROM public.books WHERE book_id = %s"),
        (body.book_id,),
    )
    if existing:
        raise HTTPException(status_code=409, detail="book_id 已存在")

    row = fetch_one(
        sql.SQL("""
            INSERT INTO public.books (book_id, book_name, author, category, total_chapters, book_data, tags, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """),
        (body.book_id, body.book_name, body.author, body.category,
         body.total_chapters, Jsonb(body.book_data) if body.book_data else None,
         body.tags, body.note),
    )
    return {"message": "书籍添加成功", "book": row}


@router.put("/{book_id}")
async def update_book(book_id: str, body: BookUpdate):
    """更新书籍信息。"""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"message": "无更新"}

    set_parts = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
        for k in updates.keys()
    )
    count = execute(
        sql.SQL("UPDATE public.books SET {}, updated_at = now() WHERE book_id = %s").format(set_parts),
        tuple(updates.values()) + (book_id,),
    )
    if count == 0:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return {"message": "更新成功"}


@router.put("/{book_id}/tags")
async def update_book_tags(book_id: str, body: BookTagsUpdate):
    """更新书籍标签。"""
    count = execute(
        sql.SQL("UPDATE public.books SET tags = %s, updated_at = now() WHERE book_id = %s"),
        (body.tags, book_id),
    )
    if count == 0:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return {"message": "标签已更新"}


@router.delete("/{book_id}")
async def delete_book(book_id: str):
    """删除书籍。"""
    count = execute(
        sql.SQL("DELETE FROM public.books WHERE book_id = %s"),
        (book_id,),
    )
    if count == 0:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return {"message": "书籍已删除"}
