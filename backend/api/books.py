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
_CATEGORY_KEYS = ("category", "bookCategory", "tingCategory", "categoryId", "firstCid", "sort")
_CHAPTER_LIST_KEYS = (
    "chapters_data", "tingChapterList", "chapterList", "chapters",
    "list", "tingChapters", "sectionList",
)


def _extract_category_from_book_data(book_data: dict) -> str | None:
    """从 book_data JSON 中尝试提取分类。"""
    if not isinstance(book_data, dict):
        return None
    for key in _CATEGORY_KEYS:
        val = book_data.get(key)
        if val and str(val).strip():
            return str(val).strip()
    # 嵌套在 bookInfo 中
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
    """获取书籍列表（分页）。"""
    conditions = []
    params = []
    if category:
        conditions.append("category = %s")
        params.append(category)
    if search:
        conditions.append("(book_name ILIKE %s OR author ILIKE %s OR book_id ILIKE %s)")
        params.extend([f"%{search}%"] * 3)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size

    count_row = fetch_one(
        sql.SQL(f"SELECT COUNT(*) AS cnt FROM public.books{where_clause}"),
        tuple(params),
    )
    total = count_row["cnt"] if count_row else 0

    rows = fetch_all(
        sql.SQL(f"""
            SELECT book_id, book_name, author, category, total_chapters, tags,
                   status, note, created_at, updated_at, book_data
            FROM public.books
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """),
        tuple(params) + (page_size, offset),
    )
    # 对 category/total_chapters 为空的记录，从 book_data JSON 回退提取
    for row in rows:
        if not row.get("category") or not row.get("total_chapters"):
            raw = row.get("book_data")
            if raw:
                bd = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(bd, dict):
                    if not row.get("category"):
                        row["category"] = _extract_category_from_book_data(bd)
                    if not row.get("total_chapters"):
                        row["total_chapters"] = _count_chapters_from_book_data(bd)
        row.pop("book_data", None)  # 不返回完整 book_data 到前端
    return {"books": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/categories")
async def list_categories():
    """获取书籍分类列表（含 book_data JSON 回退提取）。"""
    rows = fetch_all(
        sql.SQL("SELECT book_id, category, book_data FROM public.books")
    )
    cat_map: dict[str, int] = {}
    for row in rows:
        cat = row.get("category")
        if not cat:
            raw = row.get("book_data")
            if raw:
                bd = json.loads(raw) if isinstance(raw, str) else raw
                cat = _extract_category_from_book_data(bd)
        if cat:
            cat_map[cat] = cat_map.get(cat, 0) + 1
    categories = [{"category": k, "count": v} for k, v in sorted(cat_map.items())]
    return {"categories": categories}


@router.post("/repair-metadata")
async def repair_book_metadata():
    """修复书籍元数据：从 book_data JSON 提取 category 和 total_chapters 回填到列。

    迁移脚本可能未填充 category 列，本接口遍历所有 books，从 book_data JSON
    中提取分类和章节数，UPDATE 到对应列。
    """
    rows = fetch_all(
        sql.SQL("SELECT book_id, book_data, category, total_chapters FROM public.books")
    )
    repaired_category = 0
    repaired_chapters = 0
    skipped = 0
    unmatched_keys: set[str] = set()

    for row in rows:
        raw = row.get("book_data")
        if not raw:
            skipped += 1
            continue
        bd = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(bd, dict):
            skipped += 1
            continue

        updates = {}
        if not row.get("category"):
            cat = _extract_category_from_book_data(bd)
            if cat:
                updates["category"] = cat
                repaired_category += 1
            else:
                unmatched_keys.update(bd.keys())

        if not row.get("total_chapters"):
            count = _count_chapters_from_book_data(bd)
            if count > 0:
                updates["total_chapters"] = count
                repaired_chapters += 1

        if updates:
            set_parts = sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
                for k in updates.keys()
            )
            execute(
                sql.SQL("UPDATE public.books SET {}, updated_at = now() WHERE book_id = %s").format(set_parts),
                tuple(updates.values()) + (row["book_id"],),
            )

    return {
        "message": f"修复完成：分类 {repaired_category} 条，章节数 {repaired_chapters} 条，跳过 {skipped} 条",
        "repaired_category": repaired_category,
        "repaired_chapters": repaired_chapters,
        "skipped": skipped,
        "total_books": len(rows),
        "unmatched_book_data_keys": sorted(unmatched_keys)[:30] if unmatched_keys else [],
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
