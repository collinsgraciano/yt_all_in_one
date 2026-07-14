"""YouTube OAuth 服务 — 使用数据库表替代 Redis 存储 state。"""

from __future__ import annotations

import json
import secrets
from typing import Optional
from urllib.parse import urlparse, parse_qs

from psycopg import sql
from psycopg.types.json import Jsonb

from ..settings import settings as app_settings
from ..database import fetch_one, execute
from .channel_service import get_oauth_client_secret, update_oauth_status


def _cleanup_expired_states():
    """清理过期的 OAuth state（超过 TTL 的记录）。"""
    execute(
        sql.SQL("DELETE FROM public.oauth_states WHERE created_at < now() - make_interval(secs => %s)"),
        (app_settings.oauth_state_ttl_seconds,),
    )


def start_oauth(channel_name: str) -> dict:
    """发起 OAuth 授权，返回授权 URL。"""
    client_secret = get_oauth_client_secret(channel_name)
    if not client_secret:
        raise ValueError(f"频道 {channel_name} 未上传 OAuth client_secret.json")

    from google_auth_oauthlib.flow import Flow

    state = secrets.token_urlsafe(32)

    # 存入数据库 oauth_states 表
    execute(
        sql.SQL("INSERT INTO public.oauth_states (state, channel_name) VALUES (%s, %s)"),
        (state, channel_name),
    )

    # 顺手清理过期 state
    _cleanup_expired_states()

    redirect_uri = f"{app_settings.base_url}/api/oauth/callback"
    flow = Flow.from_client_config(
        client_secret,
        scopes=[app_settings.youtube_scopes],
        state=state,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    return {"auth_url": auth_url, "state": state, "redirect_uri": redirect_uri}


def handle_oauth_callback(code: str, state: str) -> str:
    """处理 OAuth 回调，返回频道名。"""
    # 从数据库查找 state
    row = fetch_one(
        sql.SQL("SELECT channel_name FROM public.oauth_states WHERE state = %s"),
        (state,),
    )
    if not row:
        raise ValueError("无效或过期的 state，请重新发起授权")

    channel_name = row["channel_name"]

    # 删除已使用的 state
    execute(
        sql.SQL("DELETE FROM public.oauth_states WHERE state = %s"),
        (state,),
    )

    client_secret = get_oauth_client_secret(channel_name)
    if not client_secret:
        raise ValueError(f"频道 {channel_name} 的 client_secret 已丢失")

    from google_auth_oauthlib.flow import Flow

    redirect_uri = f"{app_settings.base_url}/api/oauth/callback"
    flow = Flow.from_client_config(
        client_secret,
        scopes=[app_settings.youtube_scopes],
        state=state,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_dict = json.loads(creds.to_json())

    # 写入 youtube_credentials
    execute(
        sql.SQL("""
            INSERT INTO public.youtube_credentials (channel_name, token_json, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (channel_name)
            DO UPDATE SET token_json = EXCLUDED.token_json, updated_at = EXCLUDED.updated_at
        """),
        (channel_name, Jsonb(token_dict)),
    )

    # 更新频道状态
    update_oauth_status(channel_name, "authorized")

    return channel_name


def handle_manual_oauth(channel_name: str, callback_url: str) -> dict:
    """手动粘贴回调 URL 模式。"""
    parsed = urlparse(callback_url)
    code = parse_qs(parsed.query).get("code", [None])
    code = code[0] if code else None
    if not code:
        raise ValueError("回调 URL 中未解析到 code 参数")

    # 手动模式下不验证 state，直接用频道名
    client_secret = get_oauth_client_secret(channel_name)
    if not client_secret:
        raise ValueError(f"频道 {channel_name} 未上传 OAuth client_secret.json")

    from google_auth_oauthlib.flow import Flow

    # 手动模式下使用与自动回调相同的 redirect_uri
    redirect_uri = f"{app_settings.base_url}/api/oauth/callback"
    flow = Flow.from_client_config(
        client_secret,
        scopes=[app_settings.youtube_scopes],
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_dict = json.loads(creds.to_json())

    execute(
        sql.SQL("""
            INSERT INTO public.youtube_credentials (channel_name, token_json, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (channel_name)
            DO UPDATE SET token_json = EXCLUDED.token_json, updated_at = EXCLUDED.updated_at
        """),
        (channel_name, Jsonb(token_dict)),
    )
    update_oauth_status(channel_name, "authorized")

    return {"channel_name": channel_name, "status": "authorized", "message": "授权成功"}


def revoke_oauth(channel_name: str) -> bool:
    """撤销频道 OAuth 授权（删除凭证）。"""
    execute(
        sql.SQL("DELETE FROM public.youtube_credentials WHERE channel_name = %s"),
        (channel_name,),
    )
    update_oauth_status(channel_name, "revoked")
    return True


def refresh_oauth_token(channel_name: str) -> dict:
    """手动刷新 YouTube Token。"""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleAuthRequest

    row = fetch_one(
        sql.SQL("SELECT token_json FROM public.youtube_credentials WHERE channel_name = %s"),
        (channel_name,),
    )
    if not row:
        raise ValueError(f"频道 {channel_name} 没有存储的授权凭证")

    token_info = row["token_json"]
    if isinstance(token_info, str):
        token_info = json.loads(token_info)

    credentials = Credentials.from_authorized_user_info(
        token_info,
        scopes=[app_settings.youtube_scopes],
    )

    if not credentials.expired and credentials.refresh_token:
        return {"channel_name": channel_name, "status": "valid", "message": "Token 未过期，无需刷新"}

    if not credentials.refresh_token:
        raise ValueError("缺少 refresh_token，无法自动刷新，请重新授权")

    credentials.refresh(GoogleAuthRequest())
    refreshed_token = json.loads(credentials.to_json())

    execute(
        sql.SQL("""
            INSERT INTO public.youtube_credentials (channel_name, token_json, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (channel_name)
            DO UPDATE SET token_json = EXCLUDED.token_json, updated_at = EXCLUDED.updated_at
        """),
        (channel_name, Jsonb(refreshed_token)),
    )

    update_oauth_status(channel_name, "authorized")
    return {"channel_name": channel_name, "status": "refreshed", "message": "Token 已刷新并更新"}
