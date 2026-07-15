"""频道管理服务。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from psycopg.types.json import Jsonb
from psycopg import sql

from ..database import fetch_one, fetch_all, execute, table_identifier
from ..config_schema import DEFAULT_CONFIG, coerce_value, CONFIG_SCHEMA


def list_channels() -> list[dict]:
    """获取所有频道列表（含统计信息）。"""
    rows = fetch_all(
        sql.SQL("""
            SELECT
                c.channel_id, c.channel_name, c.display_name, c.description,
                c.is_active, c.oauth_status, c.last_auth_at,
                c.created_at, c.updated_at,
                CASE WHEN yc.channel_name IS NOT NULL THEN true ELSE false END AS has_credentials,
                COALESCE(cc.config_version, 1) AS config_version,
                (SELECT COUNT(*) FROM public.run_tasks rt WHERE rt.channel_name = c.channel_name
                 AND rt.status = 'success') AS total_videos,
                (SELECT COUNT(*) FROM public.book_processing_states bps WHERE bps.project_flag = c.channel_name) AS total_books
            FROM public.channels c
            LEFT JOIN public.youtube_credentials yc ON yc.channel_name = c.channel_name
            LEFT JOIN public.channel_configs cc ON cc.channel_name = c.channel_name
            ORDER BY c.created_at
        """)
    )
    return rows


def get_channel(channel_name: str) -> Optional[dict]:
    """获取单个频道详情。"""
    return fetch_one(
        sql.SQL("""
            SELECT c.*, CASE WHEN yc.channel_name IS NOT NULL THEN true ELSE false END AS has_credentials
            FROM public.channels c
            LEFT JOIN public.youtube_credentials yc ON yc.channel_name = c.channel_name
            WHERE c.channel_name = %s
        """),
        (channel_name,),
    )


def create_channel(channel_name: str, display_name: str = "", description: str = "",
                   oauth_client_secret: dict = None) -> dict:
    """新增频道。"""
    row = fetch_one(
        sql.SQL("""
            INSERT INTO public.channels (channel_name, display_name, description, oauth_client_secret)
            VALUES (%s, %s, %s, %s)
            RETURNING *
        """),
        (channel_name, display_name or channel_name, description,
         Jsonb(oauth_client_secret) if oauth_client_secret else None),
    )

    # 创建默认配置（只保留频道级 Key，剔除全局 Key 防止覆盖全局设置）
    from ..config_schema import GLOBAL_CONFIG_KEYS
    channel_config = {
        k: v for k, v in DEFAULT_CONFIG.items()
        if k not in GLOBAL_CONFIG_KEYS
    }
    channel_config["YOUTUBE_CHANNEL_NAME"] = channel_name
    channel_config["PROJECT_FLAG"] = channel_name
    execute(
        sql.SQL("""
            INSERT INTO public.channel_configs (channel_name, config_json)
            VALUES (%s, %s)
            ON CONFLICT (channel_name) DO NOTHING
        """),
        (channel_name, Jsonb(channel_config)),
    )

    return row


def update_channel(channel_name: str, display_name: str = None, description: str = None,
                   is_active: bool = None) -> int:
    """更新频道信息。"""
    updates = {}
    if display_name is not None:
        updates["display_name"] = display_name
    if description is not None:
        updates["description"] = description
    if is_active is not None:
        updates["is_active"] = is_active

    if not updates:
        return 0

    set_parts = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
        for k in updates.keys()
    )
    stmt = sql.SQL("UPDATE public.channels SET {}, updated_at = now() WHERE channel_name = {}").format(
        set_parts, sql.Placeholder()
    )
    return execute(stmt, tuple(updates.values()) + (channel_name,))


def delete_channel(channel_name: str) -> bool:
    """删除频道（级联清理凭证和配置）。"""
    # 先停止运行中的任务
    execute(
        sql.SQL("UPDATE public.run_tasks SET status = 'cancelled', stop_reason = 'Channel deleted' "
                "WHERE channel_name = %s AND status IN ('queued', 'running')"),
        (channel_name,),
    )
    # 清理凭证
    execute(
        sql.SQL("DELETE FROM public.youtube_credentials WHERE channel_name = %s"),
        (channel_name,),
    )
    # 清理运行时设置
    execute(
        sql.SQL("DELETE FROM public.channel_runtime_settings WHERE channel_name = %s"),
        (channel_name,),
    )
    # 清理 ModelScope token
    execute(
        sql.SQL("DELETE FROM public.modelscope_tokens WHERE channel_name = %s"),
        (channel_name,),
    )
    # 清理断点状态
    execute(
        sql.SQL("DELETE FROM public.book_processing_states WHERE project_flag = %s"),
        (channel_name,),
    )
    # 删除频道（channel_configs 会级联删除）
    count = execute(
        sql.SQL("DELETE FROM public.channels WHERE channel_name = %s"),
        (channel_name,),
    )
    return count > 0


def get_channel_config(channel_name: str, merge_global: bool = True) -> Optional[dict]:
    """获取频道运行配置。

    merge_global=True 时会将全局设置合并到返回结果中（频道值优先），
    使频道详情页展示的是实际生效的配置而非仅数据库原始值。
    """
    row = fetch_one(
        sql.SQL("SELECT config_json, config_version FROM public.channel_configs WHERE channel_name = %s"),
        (channel_name,),
    )
    if not row:
        return None

    config = dict(row["config_json"]) if row["config_json"] else {}

    if merge_global:
        # 合并全局设置：对于全局 Key，如果频道配置里没有，从 global_settings 补全
        from ..config_schema import GLOBAL_CONFIG_KEYS, DEFAULT_CONFIG
        for key in GLOBAL_CONFIG_KEYS:
            if key not in config:
                from .config_service import get_global_setting
                global_value = get_global_setting(key)
                if global_value:
                    config[key] = global_value
                elif key in DEFAULT_CONFIG:
                    config[key] = DEFAULT_CONFIG[key]

    return {"channel_name": channel_name, "config": config,
            "config_version": row.get("config_version", 1)}


def save_channel_config(channel_name: str, config: dict) -> dict:
    """保存频道运行配置。全局 Key 会被自动剔除，只保留频道级配置。"""
    from ..config_schema import GLOBAL_CONFIG_KEYS, CONFIG_SCHEMA
    
    # 类型转换 + 剔除全局 Key（全局 Key 应通过 global_settings 表管理）
    coerced = {}
    for key, value in config.items():
        if key in GLOBAL_CONFIG_KEYS:
            continue  # 全局 Key 不存入频道配置
        coerced[key] = coerce_value(key, value)
    # 确保频道名正确
    coerced["YOUTUBE_CHANNEL_NAME"] = channel_name
    if not str(coerced.get("PROJECT_FLAG", "")).strip():
        coerced["PROJECT_FLAG"] = channel_name

    row = fetch_one(
        sql.SQL("""
            INSERT INTO public.channel_configs (channel_name, config_json, config_version, updated_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (channel_name)
            DO UPDATE SET config_json = EXCLUDED.config_json,
                          config_version = public.channel_configs.config_version + 1,
                          updated_at = now()
            RETURNING config_json, config_version
        """),
        (channel_name, Jsonb(coerced)),
    )
    return {"channel_name": channel_name, "config": row["config_json"],
            "config_version": row["config_version"]}


def get_channel_oauth_status(channel_name: str) -> dict:
    """查询频道 OAuth 授权状态。"""
    row = fetch_one(
        sql.SQL("""
            SELECT c.oauth_status, c.last_auth_at,
                   CASE WHEN yc.channel_name IS NOT NULL THEN true ELSE false END AS has_credentials,
                   yc.updated_at AS credential_updated_at
            FROM public.channels c
            LEFT JOIN public.youtube_credentials yc ON yc.channel_name = c.channel_name
            WHERE c.channel_name = %s
        """),
        (channel_name,),
    )
    if not row:
        return {"oauth_status": "not_found", "has_credentials": False}
    return row


def update_oauth_status(channel_name: str, status: str):
    """更新频道 OAuth 状态。"""
    execute(
        sql.SQL("UPDATE public.channels SET oauth_status = %s, last_auth_at = now(), updated_at = now() "
                "WHERE channel_name = %s"),
        (status, channel_name),
    )


def get_oauth_client_secret(channel_name: str) -> Optional[dict]:
    """获取频道的 OAuth client_secret（明文存储）。"""
    row = fetch_one(
        sql.SQL("SELECT oauth_client_secret FROM public.channels WHERE channel_name = %s"),
        (channel_name,),
    )
    if not row or not row.get("oauth_client_secret"):
        return None
    return row["oauth_client_secret"]
