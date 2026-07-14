"""任务相关 Pydantic 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    channel_names: list[str] = Field(..., description="频道名列表（支持多频道）")
    task_type: str = Field("full_pipeline", description="任务类型")
    config_overrides: dict = Field(default_factory=dict, description="临时配置覆盖")


class TaskResponse(BaseModel):
    task_id: str
    channel_name: str
    task_type: str
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    stop_reason: Optional[str] = None
    result_json: Optional[dict] = None
    error_msg: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TaskWithChannel(TaskResponse):
    """任务 + 频道显示名。"""
    channel_display_name: Optional[str] = None


class TaskLogEntry(BaseModel):
    id: Optional[int] = None
    task_id: str
    log_level: str = "INFO"
    message: str
    created_at: Optional[str] = None
