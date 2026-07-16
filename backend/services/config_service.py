"""配置管理服务。"""

from __future__ import annotations

import logging

from psycopg import sql
from psycopg.types.json import Jsonb

from ..database import fetch_one, fetch_all, execute
from ..config_schema import (
    CONFIG_SCHEMA, DEFAULT_CONFIG, GLOBAL_CONFIG_KEYS,
    get_config_by_category, coerce_value,
)

logger = logging.getLogger(__name__)


def get_config_schema() -> dict:
    """返回完整配置 schema（按分类分组）。"""
    return {
        "categories": get_config_by_category(),
        "global_keys": GLOBAL_CONFIG_KEYS,
        "defaults": DEFAULT_CONFIG,
    }


def get_global_settings() -> list[dict]:
    """获取全局共享设置。"""
    return fetch_all(
        sql.SQL("SELECT setting_key, setting_value, description, is_secret, updated_at "
                "FROM public.global_settings ORDER BY setting_key")
    )


def get_global_setting(key: str) -> str:
    """获取单个全局设置值。"""
    row = fetch_one(
        sql.SQL("SELECT setting_value FROM public.global_settings WHERE setting_key = %s"),
        (key,),
    )
    return row["setting_value"] if row else ""


def save_global_setting(key: str, value: str, description: str = None,
                        is_secret: bool = None) -> dict:
    """保存全局设置（UPSERT）。

    当 description / is_secret 为 None 时不覆盖已有值。
    """
    # 使用 NULL 而非空字符串/False，以便 COALESCE 保留已有值
    desc_param = description if description is not None else None
    secret_param = is_secret if is_secret is not None else None

    row = fetch_one(
        sql.SQL("""
            INSERT INTO public.global_settings (setting_key, setting_value, description, is_secret, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (setting_key)
            DO UPDATE SET setting_value = EXCLUDED.setting_value,
                          description = COALESCE(EXCLUDED.description, public.global_settings.description),
                          is_secret = COALESCE(EXCLUDED.is_secret, public.global_settings.is_secret),
                          updated_at = now()
            RETURNING *
        """),
        (key, str(value), desc_param, secret_param),
    )
    return row


def save_global_settings_batch(settings: list[dict]) -> dict:
    """批量保存全局设置。

    参数: settings - [{ key, value, description?, is_secret? }, ...]
    返回: { saved: int, errors: [...] }
    """
    saved = 0
    errors = []
    for item in settings:
        key = item.get("key", "")
        value = item.get("value", "")
        if not key:
            continue
        try:
            save_global_setting(
                key,
                str(value),
                item.get("description"),
                item.get("is_secret"),
            )
            saved += 1
        except Exception as e:
            errors.append({"key": key, "error": str(e)})
    return {"saved": saved, "errors": errors}


def seed_global_settings() -> dict:
    """启动时将 DEFAULT_CONFIG 中所有值写入 global_settings 表（幂等）。

    仅插入数据库中尚不存在的 Key，不会覆盖用户已修改的值。
    这样 global_settings 成为唯一的默认值来源，不再需要 DEFAULT_CONFIG 兜底。
    """
    seeded = 0
    skipped = 0
    for key, default_value in DEFAULT_CONFIG.items():
        existing = get_global_setting(key)
        if existing:
            skipped += 1
            continue
        try:
            save_global_setting(key, str(default_value))
            seeded += 1
        except Exception as e:
            logger.warning(f"种子配置写入失败 {key}: {e}")

    logger.info(f"global_settings 种子初始化完成: 新增 {seeded}, 已有 {skipped}")
    return {"seeded": seeded, "skipped": skipped, "total": len(DEFAULT_CONFIG)}


def build_runtime_config(channel_name: str, overrides: dict = None) -> dict:
    """构建完整的运行配置（global_settings 为唯一默认来源 + 频道覆盖 + 临时覆盖）。

    合并顺序（越后面优先级越高）：
      1. global_settings — 全局共享设置（含所有默认值，启动时自动种子化）
      2. channel_configs — 频道级配置（只覆盖非全局 Key）
      3. overrides — 临时覆盖（最高优先级）

    不再依赖 DEFAULT_CONFIG：所有默认值已种子化写入 global_settings 表，
    每次构建运行时配置时从数据库读取。
    """
    config = {}

    # 全局共享设置（唯一默认值来源，启动时已种子化所有 DEFAULT_CONFIG）
    for key in GLOBAL_CONFIG_KEYS:
        global_value = get_global_setting(key)
        if global_value:
            config[key] = coerce_value(key, global_value)

    # 频道级配置（只覆盖非全局 Key）
    from .channel_service import get_channel_config
    ch_config = get_channel_config(channel_name, merge_global=False)
    if ch_config and ch_config.get("config"):
        for key, value in ch_config["config"].items():
            if key not in GLOBAL_CONFIG_KEYS:
                config[key] = coerce_value(key, value)

    # 临时覆盖（最高优先级）
    if overrides:
        for key, value in overrides.items():
            config[key] = coerce_value(key, value)

    # 确保频道名正确
    config["YOUTUBE_CHANNEL_NAME"] = channel_name
    if not str(config.get("PROJECT_FLAG", "")).strip():
        config["PROJECT_FLAG"] = channel_name

    # 注入数据库连接串（pipeline 需要 POSTGRES_DSN）
    import os
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url and db_url.startswith("postgresql+psycopg://"):
        db_url = db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if db_url:
        config["POSTGRES_DSN"] = db_url

    return config


def get_dashboard_stats() -> dict:
    """获取仪表盘统计数据。"""
    channels = fetch_all(
        sql.SQL("""
            SELECT c.channel_name, c.display_name, c.oauth_status, c.is_active,
                   CASE WHEN yc.channel_name IS NOT NULL THEN true ELSE false END AS has_credentials,
                   (SELECT COUNT(*) FROM public.run_tasks rt WHERE rt.channel_name = c.channel_name
                    AND rt.status = 'success') AS total_videos,
                   (SELECT MAX(rt.finished_at) FROM public.run_tasks rt WHERE rt.channel_name = c.channel_name) AS last_run_at,
                   (SELECT rt.status FROM public.run_tasks rt WHERE rt.channel_name = c.channel_name
                    ORDER BY rt.created_at DESC LIMIT 1) AS last_run_status
            FROM public.channels c
            LEFT JOIN public.youtube_credentials yc ON yc.channel_name = c.channel_name
            ORDER BY c.created_at
        """)
    )

    running_tasks = fetch_all(
        sql.SQL("""
            SELECT rt.task_id, rt.channel_name, c.display_name AS channel_display_name,
                   rt.status, rt.task_type, rt.started_at, rt.created_at
            FROM public.run_tasks rt
            LEFT JOIN public.channels c ON c.channel_name = rt.channel_name
            WHERE rt.status IN ('queued', 'running')
            ORDER BY rt.created_at DESC
        """)
    )

    total_books = fetch_one(sql.SQL("SELECT COUNT(*) AS cnt FROM public.books"))
    total_tasks = fetch_one(sql.SQL("SELECT COUNT(*) AS cnt FROM public.run_tasks"))
    success_tasks = fetch_one(sql.SQL("SELECT COUNT(*) AS cnt FROM public.run_tasks WHERE status = 'success'"))

    return {
        "channels": channels,
        "running_tasks": running_tasks,
        "total_books": total_books["cnt"] if total_books else 0,
        "total_tasks": total_tasks["cnt"] if total_tasks else 0,
        "success_tasks": success_tasks["cnt"] if success_tasks else 0,
    }
