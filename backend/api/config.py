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


@router.get("/schema")
async def get_config_schema():
    """获取完整配置 Schema。"""
    return config_service.get_config_schema()


@router.get("/global-settings")
async def get_global_settings():
    """获取全局共享设置。"""
    settings = config_service.get_global_settings()
    # 脱敏
    for s in settings:
        if s.get("is_secret") and s.get("setting_value"):
            val = s["setting_value"]
            if len(val) > 4:
                s["setting_value"] = val[:2] + "****" + val[-2:]
    return {"settings": settings}


@router.put("/global-settings")
async def save_global_setting(body: GlobalSettingUpdate):
    """保存全局共享设置。"""
    result = config_service.save_global_setting(
        body.key, body.value, body.description, body.is_secret,
    )
    return {"message": "设置已保存", "setting": result}


@router.get("/dashboard-stats")
async def get_dashboard_stats():
    """获取仪表盘统计数据。"""
    return config_service.get_dashboard_stats()
