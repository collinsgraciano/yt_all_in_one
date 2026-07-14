"""YouTube OAuth API。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..services import oauth_service

router = APIRouter(prefix="/api/oauth", tags=["OAuth 认证"])


class StartOAuthBody(BaseModel):
    channel_name: str


class ManualCallbackBody(BaseModel):
    channel_name: str
    callback_url: str


@router.post("/start")
async def start_oauth(body: StartOAuthBody):
    """发起 OAuth 授权，返回授权 URL。"""
    try:
        return oauth_service.start_oauth(body.channel_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OAuth 发起失败: {e}")


@router.get("/callback")
async def oauth_callback(code: str = Query(...), state: str = Query(...)):
    """OAuth 回调端点。"""
    try:
        channel_name = oauth_service.handle_oauth_callback(code, state)
        return RedirectResponse(
            url=f"/oauth/success?channel={quote(channel_name)}",
            status_code=302,
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/oauth/error?message={quote(str(e))}",
            status_code=302,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/oauth/error?message={quote(str(e))}",
            status_code=302,
        )


@router.post("/manual-callback")
async def manual_callback(body: ManualCallbackBody):
    """手动粘贴回调 URL。"""
    try:
        return oauth_service.handle_manual_oauth(body.channel_name, body.callback_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"授权失败: {e}")


@router.post("/{channel_name}/revoke")
async def revoke_oauth(channel_name: str):
    """撤销频道 OAuth 授权。"""
    try:
        oauth_service.revoke_oauth(channel_name)
        return {"message": "授权已撤销"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{channel_name}/refresh")
async def refresh_oauth(channel_name: str):
    """手动刷新 YouTube Token。"""
    try:
        return oauth_service.refresh_oauth_token(channel_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
