"""运行核心：PostgreSQL / 数据库操作。

对应原 runtime_core.py:
- get_postgres_dsn（行 134-138）
- get_public_table_identifier（行 141-145）
- execute_postgres_fetchone / fetchall / execute / fetchval（行 148-188）
- get_book_state_table_name（行 1426-1427）
- get_modelscope_token_table_name（行 1430-1431）
- get_cloud_runtime_settings_table_name（行 1434-1435）
- get_shared_cloud_runtime_scope_key（行 1438-1439）
- load/save/delete modelscope_token_from_supabase（行 1442-1536）
- load/save/delete cloud_runtime_setting_from_supabase（行 1538-1643）
- resolve_cloud_text_setting（行 1645-1672）
- resolve_modelscope_token（行 1675-1719）
- apply_cloud_runtime_overrides（行 2615-2644）
- _podcast_load_channel_setting / _podcast_save_channel_setting（行 8373-8430）
- _fetch_books_page_from_database / _update_book_status_* / _delete_book_*（行 7971-8009）

依赖：config（cfg.X 读配置, cfg.set_config 回写），runtime（log, make_json_compatible）。
"""

from __future__ import annotations

import json
import datetime as dt_module

from psycopg import connect, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import config as cfg
from .runtime import log, make_json_compatible, runtime_console_print


# ============================================================================
# PostgreSQL 连接与基础查询
# ============================================================================

# 连接池（惰性初始化）— 复用 TCP 连接，避免每次查询都新建/销毁连接。
# pipeline 运行在 backend 进程的子线程中，使用独立连接池（max=3），
# 与 backend 的连接池（max=5）合计不超过 PostgreSQL max_connections=20。
_pool = None
_POOL_MIN = 1
_POOL_MAX = 3
_POOL_TIMEOUT = 10


def _get_pool():
    """获取或创建全局连接池（惰性初始化）。

    DSN 来源于 cfg.POSTGRES_DSN（由 apply_runtime_config 注入）。
    若 psycopg_pool 未安装则返回 None，调用方回退到直连模式。
    """
    global _pool
    if _pool is not None:
        return _pool

    try:
        from psycopg_pool import ConnectionPool
    except ImportError:
        return None

    dsn = get_postgres_dsn(optional=True)
    if not dsn:
        return None

    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=_POOL_MIN,
        max_size=_POOL_MAX,
        timeout=_POOL_TIMEOUT,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
        },
        open=True,
    )
    return _pool


def close_pool():
    """关闭连接池（应用退出时调用）。"""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None


def get_postgres_dsn(optional=False):
    dsn = str(getattr(cfg, "POSTGRES_DSN", "") or "").strip()
    if not dsn and not optional:
        raise RuntimeError("POSTGRES_DSN 未初始化，请先配置 PostgreSQL 连接串。")
    return dsn


def get_public_table_identifier(table_name):
    normalized_name = str(table_name or "").strip()
    if not normalized_name:
        raise RuntimeError("数据库表名不能为空。")
    return sql.Identifier(cfg.POSTGRES_SCHEMA, normalized_name)


def execute_postgres_fetchone(statement, params=None, optional=False):
    dsn = get_postgres_dsn(optional=optional)
    if not dsn:
        return None

    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                row = cur.fetchone()
                return dict(row) if row else None

    # 回退：直连
    with connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            row = cur.fetchone()
            return dict(row) if row else None


def execute_postgres_fetchall(statement, params=None, optional=False):
    dsn = get_postgres_dsn(optional=optional)
    if not dsn:
        return []

    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                rows = cur.fetchall() or []
                return [dict(row) for row in rows]

    # 回退：直连
    with connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            rows = cur.fetchall() or []
            return [dict(row) for row in rows]


def execute_postgres(statement, params=None, optional=False):
    dsn = get_postgres_dsn(optional=optional)
    if not dsn:
        return 0

    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                return cur.rowcount

    # 回退：直连
    with connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            return cur.rowcount


def execute_postgres_fetchval(statement, params=None, optional=False):
    row = execute_postgres_fetchone(statement, params=params, optional=optional)
    if not row:
        return None
    return next(iter(row.values()))


# ============================================================================
# 表名辅助（原文件行 1426-1439）
# ============================================================================

def get_book_state_table_name():
    return str(getattr(cfg, "BOOK_STATE_TABLE", "") or "book_processing_states").strip() or "book_processing_states"


def get_modelscope_token_table_name():
    return str(getattr(cfg, "MODELSCOPE_TOKEN_TABLE", "") or "modelscope_tokens").strip() or "modelscope_tokens"


def get_cloud_runtime_settings_table_name():
    return str(getattr(cfg, "CLOUD_RUNTIME_SETTINGS_TABLE", "") or "channel_runtime_settings").strip() or "channel_runtime_settings"


def get_shared_cloud_runtime_scope_key():
    return "__shared__"


# ============================================================================
# ModelScope Token 读写（原文件行 1442-1536）
# ============================================================================

def load_modelscope_token_from_supabase(channel_name=None):
    table_name = get_modelscope_token_table_name()
    shared_scope = get_shared_cloud_runtime_scope_key()
    channel = str(channel_name or getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    table_sql = get_public_table_identifier(table_name)

    try:
        shared_row = execute_postgres_fetchone(
            sql.SQL(
                """
                SELECT token_text
                FROM {}
                WHERE channel_name = %s
                LIMIT 1
                """
            ).format(table_sql),
            (shared_scope,),
        )
        if shared_row:
            return str(shared_row.get("token_text") or "").strip()

        if channel:
            legacy_row = execute_postgres_fetchone(
                sql.SQL(
                    """
                    SELECT token_text
                    FROM {}
                    WHERE channel_name = %s
                    LIMIT 1
                    """
                ).format(table_sql),
                (channel,),
            )
            if legacy_row:
                return str(legacy_row.get("token_text") or "").strip()

        fallback_row = execute_postgres_fetchone(
            sql.SQL(
                """
                SELECT token_text
                FROM {}
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).format(table_sql),
        )
        if fallback_row:
            return str(fallback_row.get("token_text") or "").strip()
        return ""
    except Exception as e:
        raise RuntimeError(f"从数据库读取 ModelScope Token 失败，请检查表 {table_name}: {e}")


def save_modelscope_token_to_supabase(channel_name, token_text):
    token_value = str(token_text or "").strip()
    table_name = get_modelscope_token_table_name()
    shared_scope = get_shared_cloud_runtime_scope_key()
    table_sql = get_public_table_identifier(table_name)

    if not token_value:
        raise RuntimeError("MODELSCOPE_TOKEN 为空，无法写入数据库")

    try:
        execute_postgres(
            sql.SQL(
                """
                INSERT INTO {} (channel_name, token_text, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (channel_name)
                DO UPDATE SET
                  token_text = EXCLUDED.token_text,
                  updated_at = EXCLUDED.updated_at
                """
            ).format(table_sql),
            (shared_scope, token_value, dt_module.datetime.now().isoformat()),
        )
    except Exception as e:
        raise RuntimeError(f"写入数据库 ModelScope Token 失败，请检查表 {table_name}: {e}")

    return f"postgres:{table_name}:{shared_scope}"


def delete_modelscope_token_from_supabase(channel_name):
    table_name = get_modelscope_token_table_name()
    shared_scope = get_shared_cloud_runtime_scope_key()
    table_sql = get_public_table_identifier(table_name)
    try:
        execute_postgres(
            sql.SQL("DELETE FROM {} WHERE channel_name = %s").format(table_sql),
            (shared_scope,),
        )
        return True
    except Exception as e:
        raise RuntimeError(f"删除数据库 ModelScope Token 失败，请检查表 {table_name}: {e}")


# ============================================================================
# 云端运行设置读写（原文件行 1538-1643）
# ============================================================================

def load_cloud_runtime_setting_from_supabase(channel_name, setting_key):
    key = str(setting_key or "").strip()
    channel = str(channel_name or getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    shared_scope = get_shared_cloud_runtime_scope_key()
    if not key:
        return ""

    table_name = get_cloud_runtime_settings_table_name()
    table_sql = get_public_table_identifier(table_name)

    try:
        shared_row = execute_postgres_fetchone(
            sql.SQL(
                """
                SELECT setting_value
                FROM {}
                WHERE channel_name = %s AND setting_key = %s
                LIMIT 1
                """
            ).format(table_sql),
            (shared_scope, key),
        )
        if shared_row:
            return str(shared_row.get("setting_value") or "")

        if channel:
            legacy_row = execute_postgres_fetchone(
                sql.SQL(
                    """
                    SELECT setting_value
                    FROM {}
                    WHERE channel_name = %s AND setting_key = %s
                    LIMIT 1
                    """
                ).format(table_sql),
                (channel, key),
            )
            if legacy_row:
                return str(legacy_row.get("setting_value") or "")

        fallback_row = execute_postgres_fetchone(
            sql.SQL(
                """
                SELECT setting_value
                FROM {}
                WHERE setting_key = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).format(table_sql),
            (key,),
        )
        if fallback_row:
            return str(fallback_row.get("setting_value") or "")
        return ""
    except Exception as e:
        raise RuntimeError(f"从数据库读取云端运行配置 {key} 失败，请检查表 {table_name}: {e}")


def save_cloud_runtime_setting_to_supabase(channel_name, setting_key, setting_value):
    key = str(setting_key or "").strip()
    value = str(setting_value or "")
    table_name = get_cloud_runtime_settings_table_name()
    shared_scope = get_shared_cloud_runtime_scope_key()
    table_sql = get_public_table_identifier(table_name)

    if not key:
        raise RuntimeError("setting_key 为空，无法将云端运行配置写入数据库")

    try:
        execute_postgres(
            sql.SQL(
                """
                INSERT INTO {} (channel_name, setting_key, setting_value, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (channel_name, setting_key)
                DO UPDATE SET
                  setting_value = EXCLUDED.setting_value,
                  updated_at = EXCLUDED.updated_at
                """
            ).format(table_sql),
            (shared_scope, key, value, dt_module.datetime.now().isoformat()),
        )
    except Exception as e:
        raise RuntimeError(f"写入数据库云端运行配置 {key} 失败，请检查表 {table_name}: {e}")

    return f"postgres:{table_name}:{shared_scope}:{key}"


def delete_cloud_runtime_setting_from_supabase(channel_name, setting_key):
    key = str(setting_key or "").strip()
    shared_scope = get_shared_cloud_runtime_scope_key()
    if not key:
        return False

    table_name = get_cloud_runtime_settings_table_name()
    table_sql = get_public_table_identifier(table_name)
    try:
        execute_postgres(
            sql.SQL("DELETE FROM {} WHERE channel_name = %s AND setting_key = %s").format(table_sql),
            (shared_scope, key),
        )
        return True
    except Exception as e:
        raise RuntimeError(f"删除数据库云端运行配置 {key} 失败，请检查表 {table_name}: {e}")


# ============================================================================
# 配置解析（原文件行 1645-1719 / 2615-2644）
# ============================================================================

def resolve_cloud_text_setting(setting_key, local_value="", source="database", channel_name=None):
    mode = cfg.normalize_runtime_source(source, default="database")
    local_text = str(local_value or "")

    if mode not in {"database", "local"}:
        raise RuntimeError(f"{setting_key} 的来源配置只能是 'database' 或 'local'")

    if mode == "local":
        return local_text

    try:
        cloud_value = load_cloud_runtime_setting_from_supabase(channel_name, setting_key)
    except Exception as e:
        log.warning("读取数据库运行配置 %s 失败，当前回退到本地值: %s", setting_key, e)
        return local_text

    if str(cloud_value).strip():
        log.info("已从数据库读取全局共享云端配置 %s", setting_key)
        return str(cloud_value)

    if str(local_text).strip():
        log.warning(
            "数据库中未找到全局共享云端配置 %s，当前运行临时回退到本地值；如需持久保存，请手动开启云端运行配置同步单元",
            setting_key,
        )
        return local_text

    return local_text


def resolve_modelscope_token(channel_name=None):
    """获取 ModelScope Token — 直接从全局配置读取（来源：global_settings 表）。"""
    local_token = str(getattr(cfg, "MODELSCOPE_TOKEN", "") or "").strip()

    if not local_token:
        raise RuntimeError(
            "MODELSCOPE_TOKEN 为空，无法继续 AI 生成；"
            "请在 Web 管理面板 → 全局设置 中配置"
        )
    return local_token




def _podcast_load_channel_setting(channel_name, setting_key):
    normalized_channel = str(channel_name or "").strip()
    normalized_key = str(setting_key or "").strip()
    if not normalized_channel or not normalized_key:
        return ""

    table_sql = get_public_table_identifier(get_cloud_runtime_settings_table_name())
    row = execute_postgres_fetchone(
        sql.SQL(
            """
            SELECT setting_value
            FROM {}
            WHERE channel_name = %s AND setting_key = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).format(table_sql),
        (normalized_channel, normalized_key),
        optional=True,
    )
    return str((row or {}).get("setting_value") or "").strip()


def _podcast_save_channel_setting(channel_name, setting_key, setting_value):
    normalized_channel = str(channel_name or "").strip()
    normalized_key = str(setting_key or "").strip()
    if not normalized_channel or not normalized_key:
        return False

    now = dt_module.datetime.now().isoformat()
    table_sql = get_public_table_identifier(get_cloud_runtime_settings_table_name())
    execute_postgres(
        sql.SQL(
            """
            INSERT INTO {} (
              channel_name,
              setting_key,
              setting_value,
              created_at,
              updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (channel_name, setting_key)
            DO UPDATE SET
              setting_value = EXCLUDED.setting_value,
              updated_at = EXCLUDED.updated_at
            """
        ).format(table_sql),
        (
            normalized_channel,
            normalized_key,
            str(setting_value or "").strip(),
            now,
            now,
        ),
        optional=True,
    )
    return True


# ============================================================================
# Books 表 CRUD（原文件行 7971-8009）
# ============================================================================

def _fetch_books_page_from_database(offset, page_size, target_category=""):
    table_sql = get_public_table_identifier("books")
    statement = sql.SQL(
        """
        SELECT book_id, book_name, category, book_data, status, tags
        FROM {}
        """
    ).format(table_sql)
    params = []
    cat = str(target_category or "").strip()
    if cat:
        # 直接查 category 顶层列（迁移后的数据已有真实顶层列）
        statement += sql.SQL(" WHERE category = %s")
        params.append(cat)
    statement += sql.SQL(" ORDER BY book_id LIMIT %s OFFSET %s")
    params.extend([page_size, offset])
    return execute_postgres_fetchall(statement, tuple(params))


def _update_book_status_in_database(book_id, status_value):
    table_sql = get_public_table_identifier("books")
    execute_postgres(
        sql.SQL("UPDATE {} SET status = %s WHERE book_id = %s").format(table_sql),
        (status_value, str(book_id)),
    )


def _update_book_tags_in_database(book_id, tags_value):
    table_sql = get_public_table_identifier("books")
    execute_postgres(
        sql.SQL("UPDATE {} SET tags = %s WHERE book_id = %s").format(table_sql),
        (tags_value, str(book_id)),
    )


def _delete_book_from_database(book_id):
    table_sql = get_public_table_identifier("books")
    execute_postgres(
        sql.SQL("DELETE FROM {} WHERE book_id = %s").format(table_sql),
        (str(book_id),),
    )