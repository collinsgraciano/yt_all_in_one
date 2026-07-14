"""书籍管理 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from psycopg import sql
from psycopg.types.json import Jsonb

from ..database import fetch_all, fetch_one, execute
from ..models.book import BookCreate, BookUpdate, BookTagsUpdate

router = APIRouter(prefix="/api/books", tags=["书籍管理"])


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
                   status, note, created_at, updated_at
            FROM public.books
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """),
        tuple(params) + (page_size, offset),
    )
    return {"books": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/categories")
async def list_categories():
    """获取书籍分类列表。"""
    rows = fetch_all(
        sql.SQL("""
            SELECT DISTINCT category, COUNT(*) as count
            FROM public.books
            WHERE category IS NOT NULL AND category != ''
            GROUP BY category
            ORDER BY category
        """)
    )
    return {"categories": rows}


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
