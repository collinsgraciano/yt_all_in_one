"""HF 外包任务转发层 — 流水线任务的 HF 外包入口。

轨道B 入口：把符合条件的 TG 缓存完整书写入 hf_jobs 队列，
由 VPS 中继调度器自动派发给 HF 流水线 Worker 处理。

与轨道A 的 tasks.py（本机直跑 pipeline）形成对照：
- 不启动后台线程跑 pipeline
- 不获取串行锁
- 仅写入 hf_jobs 队列 + 查询状态 + 控制 VPS 调度器

VPS 中继地址配置：
  1. global_settings 表的 VPS_RELAY_URL 键
  2. 环境变量 VPS_RELAY_URL
"""

from __future__ import annotations

import os
import logging

import requests
from fastapi import APIRouter
from pydantic import BaseModel
from psycopg import sql

from ..database import fetch_one, fetch_all, execute as db_execute

router = APIRouter(prefix="/api/tasks-hf", tags=["HF外包任务"])
logger = logging.getLogger(__name__)

_RELAY_TIMEOUT = 30


# ═══════════════════════════════════════════════════════════
# 配置读取
# ═══════════════════════════════════════════════════════════

def _get_vps_relay_url() -> str:
    """获取 VPS 中继地址。"""
    try:
        from ..services.config_service import get_global_setting
        url = get_global_setting("VPS_RELAY_URL") or ""
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return os.environ.get("VPS_RELAY_URL", "").rstrip("/")


# ═══════════════════════════════════════════════════════════
# 请求模型
# ═══════════════════════════════════════════════════════════

class SeedJobsRequest(BaseModel):
    channel_name: str
    category: str = ""  # 可选分类筛选


# ═══════════════════════════════════════════════════════════
# 任务投递
# ═══════════════════════════════════════════════════════════

@router.post("/seed")
def seed_jobs(body: SeedJobsRequest):
    """筛选 TG 缓存完整书并写入 hf_jobs 队列。

    调用 VPS 中继的 /api/seed-jobs 端点完成筛选 + 入队。
    调度器会自动派发给空闲的 HF 流水线 Worker。
    """
    relay_url = _get_vps_relay_url()
    if not relay_url:
        return {"ok": False, "error": "未配置 VPS_RELAY_URL（请在全局设置中配置）"}

    try:
        resp = requests.post(
            f"{relay_url}/api/seed-jobs",
            json={"channel_name": body.channel_name, "category": body.category},
            timeout=_RELAY_TIMEOUT,
        )
        return resp.json()
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "error": f"连接 VPS 中继失败: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"转发异常: {type(e).__name__}: {e}"}


@router.post("/seed-direct")
def seed_jobs_direct(body: SeedJobsRequest):
    """直接写入 hf_jobs 队列（不经过 VPS 中继，直接操作数据库）。

    筛选所有章节均已上传 TG 的完整书，写入 hf_jobs 表。
    读取频道运行配置（TARGET_CATEGORY, PROJECT_FLAG, MAX_PROCESS_COUNT）进行过滤。
    适用于 VPS 中继不可用但数据库可直连的场景。
    """
    channel = body.channel_name.strip()
    if not channel:
        return {"ok": False, "error": "缺少 channel_name"}

    # 读取频道级配置
    ch_config_row = fetch_one(
        sql.SQL("SELECT config_json FROM public.channel_configs WHERE channel_name = %s"),
        (channel,),
    )
    ch_config = {}
    if ch_config_row and ch_config_row.get("config_json"):
        import json
        cfg_json = ch_config_row["config_json"]
        if isinstance(cfg_json, str):
            cfg_json = json.loads(cfg_json)
        if isinstance(cfg_json, dict):
            ch_config = cfg_json

    # category：请求体优先，否则用频道配置的 TARGET_CATEGORY
    category = body.category or ch_config.get("TARGET_CATEGORY", "") or ""

    # PROJECT_FLAG：频道配置优先，空时回退为频道名
    project_flag = str(ch_config.get("PROJECT_FLAG", "") or "").strip()
    if not project_flag:
        project_flag = channel

    # MAX_PROCESS_COUNT
    try:
        max_process_count = int(ch_config.get("MAX_PROCESS_COUNT", 0) or 0)
    except (ValueError, TypeError):
        max_process_count = 0

    # 读取全局配置中的 FORCE_REPROCESS
    from ..services.config_service import get_global_setting
    force_reprocess = str(get_global_setting("FORCE_REPROCESS") or "").strip().lower() in ("true", "1", "yes", "on")

    # 查询所有章节均已上传 TG 的完整书
    query = """
        SELECT b.book_id, b.book_name, b.category, b.status
        FROM public.books b
        WHERE 1=1
    """
    params = []
    if category:
        query += " AND b.category = %s"
        params.append(category)

    query += """
        AND NOT (COALESCE(b.tags, ARRAY[]::text[]) @> ARRAY['bad'])
        AND b.book_id IN (
            SELECT book_id FROM public.audiobook_chapters
            GROUP BY book_id
            HAVING COUNT(*) = COUNT(
                CASE WHEN upload_status = 'uploaded'
                     AND telegram_file_id IS NOT NULL
                     AND telegram_file_id != ''
                THEN 1 END
            )
        )
        AND b.book_id NOT IN (
            SELECT book_id FROM public.hf_jobs
            WHERE job_type = 'tg_cache_pipeline' AND status IN ('pending', 'processing')
        )
    """

    if not force_reprocess:
        query += """
            AND b.book_id NOT IN (
                SELECT book_id FROM public.hf_jobs
                WHERE job_type = 'tg_cache_pipeline' AND status = 'done'
            )
        """

    books = fetch_all(sql.SQL(query), params)

    # PROJECT_FLAG 过滤（Python 端，与本机 pipeline 逻辑一致）
    if not force_reprocess and project_flag:
        filtered = []
        for book in books:
            status_str = book.get("status") or ""
            if status_str.startswith("{") and status_str.endswith("}"):
                inner = status_str[1:-1].strip()
                existing_flags = set(s.strip().strip('"') for s in inner.split(",")) if inner else set()
            else:
                existing_flags = set(s.strip() for s in status_str.split(",")) if status_str.strip() else set()
            if project_flag not in existing_flags:
                filtered.append(book)
        logger.info("[HF外包] PROJECT_FLAG 过滤：%d → %d 本", len(books), len(filtered))
        books = filtered

    # MAX_PROCESS_COUNT 限制
    if max_process_count > 0 and len(books) > max_process_count:
        logger.info("[HF外包] MAX_PROCESS_COUNT 限制：%d → %d 本", len(books), max_process_count)
        books = books[:max_process_count]

    inserted = 0
    for book in books:
        try:
            db_execute(
                sql.SQL("""INSERT INTO public.hf_jobs (job_type, book_id, channel_name, status)
                   VALUES ('tg_cache_pipeline', %s, %s, 'pending')"""),
                (book["book_id"], channel),
            )
            inserted += 1
        except Exception as e:
            logger.warning("插入 hf_jobs 失败 book=%s: %s", book.get("book_id"), e)

    logger.info("[HF外包] 频道=%s category=%s 筛选 %d 本 TG缓存完整书，写入 %d 个任务", channel, category or "(全部)", len(books), inserted)
    return {"ok": True, "inserted": inserted, "total_candidates": len(books)}


# ═══════════════════════════════════════════════════════════
# 状态查询
# ═══════════════════════════════════════════════════════════

@router.get("/status")
def get_hf_status():
    """查询 HF 外包任务全局状态（直接读数据库）。"""
    stats = {}
    for status in ("pending", "processing", "done", "failed"):
        row = fetch_one(
            sql.SQL("SELECT COUNT(*) AS cnt FROM public.hf_jobs WHERE job_type = 'tg_cache_pipeline' AND status = %s"),
            (status,),
        )
        stats[status] = row["cnt"] if row else 0

    recent_jobs = fetch_all(
        sql.SQL(
            "SELECT job_id, job_type, book_id, channel_name, status, worker_id, "
            "created_at, finished_at, error_message "
            "FROM public.hf_jobs ORDER BY created_at DESC LIMIT 30"
        )
    )

    return {
        "stats": stats,
        "recent_jobs": recent_jobs,
    }


@router.get("/jobs")
def list_hf_jobs(
    status: str = "",
    channel_name: str = "",
    page: int = 1,
    page_size: int = 20,
):
    """分页查询 HF 外包任务列表。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size

    conditions = []
    params = []
    if status:
        conditions.append("status = %s")
        params.append(status)
    if channel_name:
        conditions.append("channel_name = %s")
        params.append(channel_name)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = fetch_all(
        sql.SQL(
            f"SELECT job_id, job_type, book_id, channel_name, status, worker_id, "
            f"result, error_message, retry_count, created_at, claimed_at, finished_at "
            f"FROM public.hf_jobs{where_clause} "
            f"ORDER BY created_at DESC LIMIT %s OFFSET %s"
        ),
        params + [page_size, offset],
    )

    count_row = fetch_one(sql.SQL(f"SELECT COUNT(*) AS cnt FROM public.hf_jobs{where_clause}"), params)
    total = count_row["cnt"] if count_row else 0

    return {
        "jobs": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/jobs/{job_id}")
def get_hf_job(job_id: int):
    """查询单个 HF 外包任务详情。"""
    row = fetch_one(
        sql.SQL("SELECT * FROM public.hf_jobs WHERE job_id = %s"),
        (job_id,),
    )
    if not row:
        return {"ok": False, "error": f"任务 {job_id} 不存在"}
    return {"ok": True, "job": row}


# ═══════════════════════════════════════════════════════════
# 调度器控制（转发到 VPS 中继）
# ═══════════════════════════════════════════════════════════

@router.post("/scheduler/start")
def scheduler_start():
    """启动 VPS 中继调度器。"""
    relay_url = _get_vps_relay_url()
    if not relay_url:
        return {"ok": False, "error": "未配置 VPS_RELAY_URL"}
    try:
        resp = requests.post(f"{relay_url}/api/scheduler/start", timeout=_RELAY_TIMEOUT)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/scheduler/stop")
def scheduler_stop():
    """停止 VPS 中继调度器。"""
    relay_url = _get_vps_relay_url()
    if not relay_url:
        return {"ok": False, "error": "未配置 VPS_RELAY_URL"}
    try:
        resp = requests.post(f"{relay_url}/api/scheduler/stop", timeout=_RELAY_TIMEOUT)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/reset-stuck")
def reset_stuck():
    """重置卡住的 HF 外包任务（转发到 VPS 中继）。"""
    relay_url = _get_vps_relay_url()
    if not relay_url:
        return {"ok": False, "error": "未配置 VPS_RELAY_URL"}
    try:
        resp = requests.post(f"{relay_url}/api/reset-stuck", timeout=_RELAY_TIMEOUT)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/delete-pending")
def delete_pending(channel_name: str = ""):
    """删除所有待处理（pending）的 HF 外包任务。

    可选参数 channel_name 限定频道，不传则删除所有频道。
    优先转发到 VPS 中继，中继不可用时直接操作数据库。
    """
    relay_url = _get_vps_relay_url()
    if relay_url:
        try:
            resp = requests.post(
                f"{relay_url}/api/delete-pending",
                json={"channel_name": channel_name} if channel_name else {},
                timeout=_RELAY_TIMEOUT,
            )
            return resp.json()
        except Exception as e:
            logger.warning("转发删除 pending 失败，尝试直接操作数据库: %s", e)

    # 直接操作数据库（中继不可用时的降级方案）
    try:
        if channel_name:
            count = db_execute(
                sql.SQL("DELETE FROM public.hf_jobs WHERE job_type = 'tg_cache_pipeline' AND status = 'pending' AND channel_name = %s"),
                (channel_name,),
            )
        else:
            count = db_execute(
                sql.SQL("DELETE FROM public.hf_jobs WHERE job_type = 'tg_cache_pipeline' AND status = 'pending'"),
            )
        return {"ok": True, "deleted": count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/relay-status")
def relay_status():
    """查询 VPS 中继全局状态（转发到 VPS 中继 /api/status）。"""
    relay_url = _get_vps_relay_url()
    if not relay_url:
        return {"ok": False, "error": "未配置 VPS_RELAY_URL"}
    try:
        resp = requests.get(f"{relay_url}/api/status", timeout=_RELAY_TIMEOUT)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/trigger")
def trigger_worker(worker_url: str = ""):
    """手动触发指定流水线 Worker 认领任务。"""
    relay_url = _get_vps_relay_url()
    if not relay_url:
        return {"ok": False, "error": "未配置 VPS_RELAY_URL"}
    try:
        resp = requests.post(
            f"{relay_url}/api/trigger",
            json={"worker_url": worker_url},
            timeout=_RELAY_TIMEOUT,
        )
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
