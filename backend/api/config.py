"""配置管理 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import config_service

router = APIRouter(prefix="/api/config", tags=["配置管理"])


class GlobalSettingUpdate(BaseModel):
    key: str
    value: str
    description: str | None = None
    is_secret: bool | None = None


class BatchSettingUpdate(BaseModel):
    settings: list[dict]


@router.get("/schema")
async def get_config_schema():
    """获取完整配置 Schema。"""
    return config_service.get_config_schema()


@router.get("/global-settings")
async def get_global_settings():
    """获取全局共享设置。

    所有字段（含密钥）均返回真实值，不做脱敏。
    本系统为单人使用，隐藏密钥反而影响配置体验。
    """
    settings = config_service.get_global_settings()
    return {"settings": settings}


@router.put("/global-settings")
async def save_global_setting(body: GlobalSettingUpdate):
    """保存单个全局共享设置。"""
    result = config_service.save_global_setting(
        body.key, body.value, body.description, body.is_secret,
    )
    return {"message": "设置已保存", "setting": result}


@router.post("/global-settings/batch")
async def save_global_settings_batch(body: BatchSettingUpdate):
    """批量保存全局共享设置（一次请求保存所有配置）。"""
    result = config_service.save_global_settings_batch(body.settings)
    if result["errors"]:
        return {"message": f"部分保存失败: 成功 {result['saved']} 项, 失败 {len(result['errors'])} 项", **result}
    return {"message": f"已保存 {result['saved']} 项设置", **result}


@router.get("/dashboard-stats")
async def get_dashboard_stats():
    """获取仪表盘统计数据。"""
    return config_service.get_dashboard_stats()
