"""书籍管理 API。"""

from __future__ import annotations

import json
import os
import logging

from fastapi import APIRouter, HTTPException, Query
from psycopg import sql, connect as pg_connect
from psycopg.types.json import Jsonb

from ..database import fetch_all, fetch_one, execute
from ..models.book import BookCreate, BookUpdate, BookTagsUpdate
from ..settings import get_dsn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/books", tags=["书籍管理"])

# ── book_data JSON 中章节列表的可能键名 ──
# 用于 total_chapters 为空时从 book_data JSON 回退提取章节数
_CHAPTER_LIST_KEYS = (
    "chapters_data", "tingChapterList", "chapterList", "chapters",
    "list", "tingChapters", "sectionList",
)


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
    book_status: str = Query(None),
):
    """获取书籍列表（分页，含 TG 缓存章节统计）。

    分类直接查 category 顶层列（迁移后的数据已有真实顶层列，无需 COALESCE JSON 回退）。
    """
    conditions = []
    params = []
    if category:
        conditions.append("b.category = %s")
        params.append(category)
    if search:
        conditions.append("(b.book_name ILIKE %s OR b.author ILIKE %s OR b.book_id ILIKE %s)")
        params.extend([f"%{search}%"] * 3)
    if book_status:
        conditions.append("b.book_status = %s")
        params.append(book_status)

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
                   b.category,
                   b.total_chapters,
                   b.tags, b.status, b.book_status, b.note, b.created_at, b.updated_at, b.book_data,
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

    直接查 category 顶层列（迁移后的数据已有真实顶层列，无需 COALESCE JSON 回退）。
    """
    rows = fetch_all(
        sql.SQL("""
            SELECT category, COUNT(*) AS cnt
            FROM public.books
            WHERE category IS NOT NULL AND category != ''
            GROUP BY category
            ORDER BY cnt DESC
        """)
    )
    categories = [{"category": r["category"], "count": r["cnt"]} for r in rows]
    return {"categories": categories}


@router.post("/sync-book-status")
async def sync_book_status():
    """同步章节上传状态到书籍完成标记。

    扫描 audiobook_chapters 表，找出所有章节都已成功上传到 Telegram 的书籍
    （所有章节 upload_status='uploaded'），将其 books.book_status 标记为 'success'。

    - 不影响 books.status 列（项目处理标记/频道防重复标记）
    - 只更新 book_status 仍为 'pending' 但章节已全部上传完成的书
    """
    result_row = fetch_one(
        sql.SQL("""
            WITH updated AS (
                UPDATE public.books
                SET book_status = 'success', updated_at = now()
                WHERE book_id IN (
                    SELECT book_id
                    FROM public.audiobook_chapters
                    GROUP BY book_id
                    HAVING COUNT(*) > 0
                       AND COUNT(*) = COUNT(CASE WHEN upload_status = 'uploaded' THEN 1 END)
                )
                AND book_status != 'success'
                RETURNING book_id
            )
            SELECT COUNT(*) AS updated_cnt FROM updated
        """)
    )
    updated_cnt = result_row["updated_cnt"] if result_row else 0

    # 统计同步后的状态分布
    verify_row = fetch_one(
        sql.SQL("""
            SELECT
                COUNT(*) AS total_books,
                COUNT(*) FILTER (WHERE book_status = 'success') AS success_books,
                COUNT(*) FILTER (WHERE book_status = 'pending')  AS pending_books
            FROM public.books
        """)
    )

    return {
        "message": f"同步完成：{updated_cnt} 本书标记为已完成（book_status=success）",
        "updated": updated_cnt,
        "total_books": verify_row["total_books"] if verify_row else 0,
        "success_books": verify_row["success_books"] if verify_row else 0,
        "pending_books": verify_row["pending_books"] if verify_row else 0,
    }


@router.post("/sync-chapters-from-remote")
async def sync_chapters_from_remote():
    """从远程 audiobook_pipeline 数据库拉取已上传章节信息到本地。

    连接远程库，读取 audiobook_chapters 表中 upload_status='uploaded' 的章节，
    批量更新到本地 audiobook_chapters 表。

    同步字段: telegram_file_id, telegram_message_id, telegram_bot_id,
              telegram_bot_user_id, upload_status, uploaded_at, error_message

    远程库 DSN 优先从 global_settings 表的 REMOTE_DATABASE_URL 读取，
    未配置时回退到环境变量或内置默认值。
    """
    # 1. 获取远程库 DSN（global_settings → 环境变量 → 默认值）
    remote_row = fetch_one(
        sql.SQL("SELECT setting_value FROM public.global_settings WHERE setting_key = %s"),
        ("REMOTE_DATABASE_URL",),
    )
    remote_dsn = ""
    if remote_row and remote_row.get("setting_value"):
        remote_dsn = remote_row["setting_value"]
    if not remote_dsn:
        remote_dsn = os.environ.get(
            "REMOTE_DATABASE_URL",
            "postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook",
        )

    safe_host = remote_dsn.split("@")[-1].split("/")[0] if "@" in remote_dsn else "unknown"
    logger.info("开始从远程库拉取已上传章节: %s", safe_host)

    # 2. 同步前的本地章节统计
    local_stats_before = fetch_one(
        sql.SQL("""
            SELECT 
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE upload_status = 'uploaded') AS uploaded,
                COUNT(*) FILTER (WHERE upload_status = 'failed')  AS failed,
                COUNT(*) FILTER (WHERE upload_status = 'pending')  AS pending
            FROM public.audiobook_chapters
        """)
    )

    # 3. 连接远程库，读取已上传的章节
    try:
        remote_conn = pg_connect(remote_dsn, autocommit=True)
    except Exception as e:
        logger.error("连接远程数据库失败: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"无法连接远程数据库 ({safe_host}): {type(e).__name__}: {str(e)[:200]}",
        )

    remote_chapters: list[dict] = []
    try:
        with remote_conn:
            with remote_conn.cursor() as remote_cur:
                remote_cur.execute(
                    """SELECT book_id, chapter_id, book_name, chapter_name,
                              audio_url, telegram_file_id, telegram_message_id,
                              telegram_bot_id, telegram_bot_user_id,
                              upload_status, uploaded_at, worker_id, claimed_at,
                              error_message
                       FROM audiobook_chapters
                       WHERE upload_status = 'uploaded'
                       ORDER BY book_id, chapter_id"""
                )
                cols = [desc[0] for desc in remote_cur.description]
                for row in remote_cur.fetchall():
                    remote_chapters.append(dict(zip(cols, row)))
    except Exception as e:
        logger.error("从远程库读取章节失败: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"从远程库读取章节失败: {type(e).__name__}: {str(e)[:200]}",
        )

    total_to_sync = len(remote_chapters)
    logger.info("远程库已上传章节: %d 条", total_to_sync)

    if total_to_sync == 0:
        return {
            "message": f"远程库 ({safe_host}) 无已上传章节，无需同步",
            "remote_host": safe_host,
            "remote_uploaded": 0,
            "synced": 0,
            "local_stats_before": {
                "total": local_stats_before["total"] if local_stats_before else 0,
                "uploaded": local_stats_before["uploaded"] if local_stats_before else 0,
                "failed": local_stats_before["failed"] if local_stats_before else 0,
                "pending": local_stats_before["pending"] if local_stats_before else 0,
            },
        }

    # 4. 批量写入本地库（upsert: 不存在则插入，存在则合并 TG 缓存字段）
    BATCH_SIZE = 500
    total_synced = 0

    UPSERT_SQL = """
        INSERT INTO public.audiobook_chapters (
            book_id, chapter_id, book_name, chapter_name, audio_url,
            telegram_file_id, telegram_message_id, telegram_bot_id, telegram_bot_user_id,
            upload_status, uploaded_at, worker_id, claimed_at, error_message
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (book_id, chapter_id) DO UPDATE SET
            telegram_file_id     = COALESCE(EXCLUDED.telegram_file_id,     audiobook_chapters.telegram_file_id),
            telegram_message_id  = COALESCE(EXCLUDED.telegram_message_id,  audiobook_chapters.telegram_message_id),
            telegram_bot_id      = COALESCE(EXCLUDED.telegram_bot_id,      audiobook_chapters.telegram_bot_id),
            telegram_bot_user_id = COALESCE(EXCLUDED.telegram_bot_user_id, audiobook_chapters.telegram_bot_user_id),
            upload_status        = EXCLUDED.upload_status,
            uploaded_at          = COALESCE(EXCLUDED.uploaded_at,          audiobook_chapters.uploaded_at),
            book_name            = COALESCE(EXCLUDED.book_name,            audiobook_chapters.book_name),
            chapter_name         = COALESCE(EXCLUDED.chapter_name,         audiobook_chapters.chapter_name),
            audio_url            = COALESCE(EXCLUDED.audio_url,            audiobook_chapters.audio_url),
            worker_id            = COALESCE(EXCLUDED.worker_id,            audiobook_chapters.worker_id),
            claimed_at           = COALESCE(EXCLUDED.claimed_at,           audiobook_chapters.claimed_at),
            error_message        = COALESCE(EXCLUDED.error_message,        audiobook_chapters.error_message)
    """

    try:
        local_conn = pg_connect(get_dsn(), autocommit=False)
    except Exception as e:
        logger.error("连接本地数据库失败: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"连接本地数据库失败: {type(e).__name__}: {str(e)[:200]}",
        )

    try:
        with local_conn:
            with local_conn.cursor() as local_cur:
                for i in range(0, total_to_sync, BATCH_SIZE):
                    batch = remote_chapters[i:i + BATCH_SIZE]
                    params = [
                        (
                            ch["book_id"],
                            ch["chapter_id"],
                            ch["book_name"],
                            ch["chapter_name"],
                            ch["audio_url"],
                            ch["telegram_file_id"],
                            ch["telegram_message_id"],
                            ch["telegram_bot_id"],
                            ch["telegram_bot_user_id"],
                            ch["upload_status"],
                            ch["uploaded_at"],
                            ch["worker_id"],
                            ch["claimed_at"],
                            ch["error_message"],
                        )
                        for ch in batch
                    ]
                    local_cur.executemany(UPSERT_SQL, params)
                    total_synced += len(batch)
                    logger.info("已同步 %d/%d 条章节到本地库", total_synced, total_to_sync)
            local_conn.commit()
    except Exception as e:
        logger.error("批量更新本地库失败: %s", e)
        return {
            "message": f"同步过程中出错（已同步 {total_synced}/{total_to_sync}）: "
                       f"{type(e).__name__}: {str(e)[:200]}",
            "remote_host": safe_host,
            "remote_uploaded": total_to_sync,
            "synced": total_synced,
            "error": f"{type(e).__name__}: {str(e)[:200]}",
            "local_stats_before": {
                "total": local_stats_before["total"] if local_stats_before else 0,
                "uploaded": local_stats_before["uploaded"] if local_stats_before else 0,
                "failed": local_stats_before["failed"] if local_stats_before else 0,
                "pending": local_stats_before["pending"] if local_stats_before else 0,
            },
        }

    # 5. 同步后的本地章节统计
    local_stats_after = fetch_one(
        sql.SQL("""
            SELECT 
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE upload_status = 'uploaded') AS uploaded,
                COUNT(*) FILTER (WHERE upload_status = 'failed')  AS failed,
                COUNT(*) FILTER (WHERE upload_status = 'pending')  AS pending
            FROM public.audiobook_chapters
        """)
    )

    logger.info("同步完成: 从远程库 %s 拉取 %d 条章节到本地", safe_host, total_synced)

    # 6. 自动同步书籍完成状态（所有章节已 uploaded 的书标记为 success）
    status_row = fetch_one(
        sql.SQL("""
            WITH updated AS (
                UPDATE public.books
                SET book_status = 'success', updated_at = now()
                WHERE book_id IN (
                    SELECT book_id
                    FROM public.audiobook_chapters
                    GROUP BY book_id
                    HAVING COUNT(*) > 0
                       AND COUNT(*) = COUNT(CASE WHEN upload_status = 'uploaded' THEN 1 END)
                )
                AND book_status != 'success'
                RETURNING book_id
            )
            SELECT COUNT(*) AS updated_cnt FROM updated
        """)
    )
    books_updated = status_row["updated_cnt"] if status_row else 0
    if books_updated > 0:
        logger.info("自动同步书籍状态: %d 本书标记为已完成", books_updated)

    msg = f"同步完成：从远程库 ({safe_host}) 拉取 {total_synced} 条已上传章节到本地"
    if books_updated > 0:
        msg += f"，{books_updated} 本书标记为已完成"

    return {
        "message": msg,
        "remote_host": safe_host,
        "remote_uploaded": total_to_sync,
        "synced": total_synced,
        "books_status_updated": books_updated,
        "local_stats_before": {
            "total": local_stats_before["total"] if local_stats_before else 0,
            "uploaded": local_stats_before["uploaded"] if local_stats_before else 0,
            "failed": local_stats_before["failed"] if local_stats_before else 0,
            "pending": local_stats_before["pending"] if local_stats_before else 0,
        },
        "local_stats_after": {
            "total": local_stats_after["total"] if local_stats_after else 0,
            "uploaded": local_stats_after["uploaded"] if local_stats_after else 0,
            "failed": local_stats_after["failed"] if local_stats_after else 0,
            "pending": local_stats_after["pending"] if local_stats_after else 0,
        },
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
