"""频道管理 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..models.channel import ChannelCreate, ChannelUpdate
from ..services import channel_service

router = APIRouter(prefix="/api/channels", tags=["频道管理"])


@router.get("")
async def list_channels():
    """获取所有频道列表。"""
    channels = channel_service.list_channels()
    return {"channels": channels, "total": len(channels)}


@router.get("/{channel_name}")
async def get_channel(channel_name: str):
    """获取单个频道详情。"""
    channel = channel_service.get_channel(channel_name)
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")
    return channel


@router.post("")
async def create_channel(body: ChannelCreate):
    """新增频道。"""
    existing = channel_service.get_channel(body.channel_name)
    if existing:
        raise HTTPException(status_code=409, detail="频道名已存在")
    channel = channel_service.create_channel(
        body.channel_name, body.display_name or "",
        body.description or "", body.oauth_client_secret,
    )
    return {"message": "频道创建成功", "channel": channel}


@router.put("/{channel_name}")
async def update_channel(channel_name: str, body: ChannelUpdate):
    """更新频道信息。"""
    count = channel_service.update_channel(
        channel_name, body.display_name, body.description, body.is_active,
    )
    if count == 0:
        raise HTTPException(status_code=404, detail="频道不存在")
    return {"message": "更新成功"}


@router.delete("/{channel_name}")
async def delete_channel(channel_name: str):
    """删除频道（级联清理）。"""
    success = channel_service.delete_channel(channel_name)
    if not success:
        raise HTTPException(status_code=404, detail="频道不存在")
    return {"message": "频道已删除"}


@router.get("/{channel_name}/config")
async def get_channel_config(channel_name: str):
    """获取频道运行配置。"""
    config = channel_service.get_channel_config(channel_name)
    if not config:
        raise HTTPException(status_code=404, detail="频道配置不存在")
    return config


@router.put("/{channel_name}/config")
async def save_channel_config(channel_name: str, body: dict):
    """保存频道运行配置。"""
    if not channel_service.get_channel(channel_name):
        raise HTTPException(status_code=404, detail="频道不存在")
    result = channel_service.save_channel_config(channel_name, body)
    return {"message": "配置已保存", **result}


@router.get("/{channel_name}/oauth-status")
async def get_oauth_status(channel_name: str):
    """获取频道 OAuth 状态。"""
    return channel_service.get_channel_oauth_status(channel_name)
