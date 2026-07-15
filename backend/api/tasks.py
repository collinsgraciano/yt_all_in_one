"""任务管理 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..models.task import TaskCreate
from ..services import task_service

router = APIRouter(prefix="/api/tasks", tags=["任务管理"])


class BatchDeleteRequest(BaseModel):
    task_ids: list[str]


@router.get("")
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    channel_name: str = Query(None),
    status: str = Query(None),
):
    """获取任务列表（分页）。"""
    return task_service.list_tasks(page, page_size, channel_name, status)


@router.get("/running")
async def get_running_tasks():
    """获取当前运行中的任务。"""
    tasks = task_service.get_running_tasks()
    return {"tasks": tasks, "total": len(tasks)}


@router.post("")
async def create_task(body: TaskCreate):
    """创建并提交任务（支持多频道）。"""
    tasks = task_service.create_task(body.channel_names, body.task_type, body.config_overrides)
    return {"message": f"已提交 {len(tasks)} 个任务", "tasks": tasks}


@router.get("/{task_id}")
async def get_task(task_id: str):
    """获取任务详情。"""
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """删除单个任务（运行中的任务需先停止）。"""
    try:
        return task_service.delete_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/stop")
async def stop_task(task_id: str):
    """停止运行中的任务。"""
    try:
        return task_service.stop_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/batch-delete")
async def batch_delete_tasks(body: BatchDeleteRequest):
    """批量删除任务。"""
    if not body.task_ids:
        raise HTTPException(status_code=400, detail="请提供要删除的任务 ID 列表")
    return task_service.delete_tasks(body.task_ids)


@router.delete("/all")
async def delete_all_tasks():
    """一键删除所有已完成任务（运行中/排队中的任务不会被删除）。"""
    return task_service.delete_all_tasks()


@router.get("/{task_id}/logs")
async def get_task_logs(
    task_id: str,
    limit: int = Query(200, ge=1, le=5000),
    level: str = Query(None),
):
    """获取任务日志。"""
    from ..services import log_service
    return log_service.get_task_logs(task_id, limit, level)


@router.get("/{task_id}/logs/recent")
async def get_recent_logs(
    task_id: str,
    after_id: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """获取增量日志（前端轮询用）— 返回指定 ID 之后的日志。"""
    from ..services import log_service
    logs = log_service.get_recent_logs(task_id, after_id, limit)
    return {"task_id": task_id, "logs": logs, "last_id": logs[-1]["id"] if logs else after_id}
