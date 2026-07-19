"""系统设置 API。"""

from __future__ import annotations

import os
import shutil
import logging

from fastapi import APIRouter, HTTPException
from psycopg import sql
from pydantic import BaseModel

from ..database import fetch_one, fetch_all
from ..settings import settings as app_settings

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# 磁盘空间管理 — 获取 OUTPUT_ROOT 使用情况 & 手动清空
# ---------------------------------------------------------------------------

def _get_output_root() -> str:
    """从 global_settings 读取 OUTPUT_ROOT，回退到应用默认值。"""
    from ..services.config_service import get_global_setting
    output_root = str(get_global_setting("OUTPUT_ROOT") or "").strip()
    if not output_root:
        output_root = str(app_settings.output_root or "/data/output").strip()
    return output_root


def _dir_size_bytes(path: str) -> int:
    """递归计算目录总大小（字节）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _count_items(path: str) -> int:
    """统计目录下的顶层条目数（分类目录数）。"""
    try:
        return len(os.listdir(path))
    except OSError:
        return 0


@router.get("/disk-usage")
async def get_disk_usage():
    """获取 OUTPUT_ROOT 目录的磁盘使用情况。

    返回:
      - output_root: 输出根目录路径
      - total_bytes: 目录总大小（字节）
      - total_mb: 目录总大小（MB）
      - item_count: 顶层条目数
      - disk_total_bytes: 所在分区总容量
      - disk_used_bytes: 所在分区已用容量
      - disk_free_bytes: 所在分区剩余容量
    """
    output_root = _get_output_root()

    if not os.path.isdir(output_root):
        return {
            "output_root": output_root,
            "total_bytes": 0,
            "total_mb": 0.0,
            "item_count": 0,
            "disk_total_bytes": 0,
            "disk_used_bytes": 0,
            "disk_free_bytes": 0,
            "exists": False,
        }

    total_bytes = _dir_size_bytes(output_root)
    item_count = _count_items(output_root)

    disk_total, disk_used, disk_free = shutil.disk_usage(output_root)

    return {
        "output_root": output_root,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 1),
        "item_count": item_count,
        "disk_total_bytes": disk_total,
        "disk_used_bytes": disk_used,
        "disk_free_bytes": disk_free,
        "disk_total_gb": round(disk_total / (1024 * 1024 * 1024), 1),
        "disk_used_gb": round(disk_used / (1024 * 1024 * 1024), 1),
        "disk_free_gb": round(disk_free / (1024 * 1024 * 1024), 1),
        "disk_used_percent": round(disk_used / disk_total * 100, 1) if disk_total else 0,
        "exists": True,
    }


class ClearOutputRootRequest(BaseModel):
    confirm: bool = False


@router.post("/clear-output")
async def clear_output_root(body: ClearOutputRootRequest):
    """清空 OUTPUT_ROOT 目录中的所有文件和子目录。

    安全检查:
      1. 必须传入 confirm=true 才执行
      2. 检查是否有正在运行的任务，有则拒绝（避免删除正在使用的文件）
      3. 保留 OUTPUT_ROOT 目录本身（只清空内容）

    返回清空的文件数和释放的空间。
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail="请确认清空操作（confirm=true）")

    # 安全检查：拒绝在有运行中任务时清空
    running_count = fetch_one(
        sql.SQL("SELECT COUNT(*) AS cnt FROM public.run_tasks WHERE status IN ('queued', 'running', 'stopping')")
    )
    running = running_count["cnt"] if running_count else 0
    if running > 0:
        raise HTTPException(
            status_code=409,
            detail=f"当前有 {running} 个任务正在运行/排队，请等待任务完成后再清空，避免删除正在使用的文件",
        )

    output_root = _get_output_root()
    if not os.path.isdir(output_root):
        raise HTTPException(status_code=404, detail=f"输出目录不存在: {output_root}")

    # 统计清空前的大小
    size_before = _dir_size_bytes(output_root)
    removed_items = 0

    # 清空目录内容（保留目录本身）
    for name in os.listdir(output_root):
        target = os.path.join(output_root, name)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                os.remove(target)
            removed_items += 1
        except Exception as e:
            logger.warning("清空 OUTPUT_ROOT 时删除 %s 失败: %s", name, e)

    freed_mb = round(size_before / (1024 * 1024), 1)
    logger.info("手动清空 OUTPUT_ROOT 完成: 删除 %d 项，释放 %.1f MB", removed_items, freed_mb)

    return {
        "success": True,
        "message": f"已清空输出目录，删除 {removed_items} 项，释放 {freed_mb} MB 磁盘空间",
        "removed_items": removed_items,
        "freed_mb": freed_mb,
        "output_root": output_root,
    }
