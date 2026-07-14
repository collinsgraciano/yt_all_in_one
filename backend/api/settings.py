"""系统设置 API。"""

from __future__ import annotations

from fastapi import APIRouter
from psycopg import sql

from ..database import fetch_one
from ..settings import settings as app_settings

router = APIRouter(prefix="/api/system", tags=["系统设置"])


@router.get("/info")
async def get_system_info():
    """获取系统信息。"""
    db_version = fetch_one(sql.SQL("SELECT version() AS ver"))
    channel_count = fetch_one(sql.SQL("SELECT COUNT(*) AS cnt FROM public.channels"))
    running_count = fetch_one(
        sql.SQL("SELECT COUNT(*) AS cnt FROM public.run_tasks WHERE status IN ('queued', 'running')")
    )

    return {
        "app_name": "有声书 YouTube 频道管理系统",
        "version": "2.0.0",
        "database": db_version["ver"] if db_version else "unknown",
        "channel_count": channel_count["cnt"] if channel_count else 0,
        "running_tasks": running_count["cnt"] if running_count else 0,
        "base_url": app_settings.base_url,
    }


@router.get("/health")
async def health_check():
    """健康检查。"""
    return {"status": "healthy"}
