"""频道相关 Pydantic 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ChannelBase(BaseModel):
    channel_name: str = Field(..., description="频道名（唯一标识）")
    display_name: Optional[str] = None
    description: Optional[str] = None


class ChannelCreate(ChannelBase):
    oauth_client_secret: Optional[dict] = Field(None, description="Google Cloud OAuth client_secret.json 内容")


class ChannelUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ChannelResponse(ChannelBase):
    channel_id: str
    is_active: bool
    oauth_status: str
    last_auth_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    has_credentials: bool = False
    config_version: int = 1

    class Config:
        from_attributes = True


class ChannelConfigResponse(BaseModel):
    channel_name: str
    config: dict
    config_version: int


class ChannelConfigUpdate(BaseModel):
    config: dict


class ChannelWithStats(ChannelResponse):
    """频道信息 + 统计数据。"""
    total_videos: int = 0
    total_books: int = 0
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[str] = None
