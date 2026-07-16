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
