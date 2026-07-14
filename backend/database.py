"""数据库连接与查询工具。

使用连接池复用 TCP 连接，避免每次查询都新建/销毁连接。
在 1C2G VPS 上可显著降低 CPU 和内存开销。

所有函数接口与旧版完全兼容，调用方无需修改。
"""

from __future__ import annotations

from typing import Any
from psycopg import connect, sql
from psycopg.rows import dict_row

from .settings import get_dsn


# ============================================================================`
# 连接池（惰性初始化）
# ============================================================================

_pool = None
_POOL_MIN = 1
_POOL_MAX = 5
_POOL_TIMEOUT = 10


def _get_pool():
    """获取或创建全局连接池。"""
    global _pool
    if _pool is not None:
        return _pool

    try:
        from psycopg_pool import ConnectionPool
    except ImportError:
        # psycopg_pool 未安装时回退到直连模式
        return None

    _pool = ConnectionPool(
        conninfo=get_dsn(),
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


# ============================================================================
# 同步查询（供服务层和后台线程使用）
# ============================================================================

def db_connect():
    """获取一个数据库连接。

    优先使用连接池；若 psycopg_pool 未安装则回退到直连。
    """
    pool = _get_pool()
    if pool is not None:
        return pool.getconn()
    return connect(get_dsn(), autocommit=True, row_factory=dict_row)


def fetch_one(statement, params=None) -> dict[str, Any] | None:
    """执行查询，返回单行。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                row = cur.fetchone()
                return dict(row) if row else None
    # 回退：直连
    with connect(get_dsn(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            row = cur.fetchone()
            return dict(row) if row else None


def fetch_all(statement, params=None) -> list[dict[str, Any]]:
    """执行查询，返回多行。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                return [dict(row) for row in cur.fetchall()]
    with connect(get_dsn(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            return [dict(row) for row in cur.fetchall()]


def fetch_val(statement, params=None):
    """执行查询，返回单个值。"""
    row = fetch_one(statement, params)
    if not row:
        return None
    return next(iter(row.values()))


def execute(statement, params=None) -> int:
    """执行 INSERT/UPDATE/DELETE，返回受影响行数。"""
    pool = _get_pool()
    if pool is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params or ())
                return cur.rowcount
    with connect(get_dsn(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(statement, params or ())
            return cur.rowcount


def table_identifier(table_name: str):
    """返回 public.table_name 的 sql.Identifier。"""
    return sql.Identifier("public", table_name)


# ============================================================================
# 便捷 CRUD 工具
# ============================================================================

def insert_row(table_name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """插入一行并返回新记录。"""
    columns = list(data.keys())
    values = [data[c] for c in columns]
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
        table_identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    return fetch_one(stmt, values)


def update_row(table_name: str, data: dict[str, Any], where_clause: str, where_params: tuple = ()) -> int:
    """更新行，返回受影响行数。"""
    set_parts = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
        for k in data.keys()
    )
    stmt = sql.SQL("UPDATE {} SET {} WHERE {}").format(
        table_identifier(table_name),
        set_parts,
        sql.SQL(where_clause),
    )
    return execute(stmt, tuple(data.values()) + where_params)


def delete_rows(table_name: str, where_clause: str, where_params: tuple = ()) -> int:
    """删除行，返回受影响行数。"""
    stmt = sql.SQL("DELETE FROM {} WHERE {}").format(
        table_identifier(table_name),
        sql.SQL(where_clause),
    )
    return execute(stmt, where_params)
