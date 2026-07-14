"""日志服务 — 仅从数据库读取日志。"""

from __future__ import annotations

from psycopg import sql

from ..database import fetch_all, fetch_one


def get_task_logs(task_id: str, limit: int = 200, level: str = None,
                  page: int = 1, page_size: int = 200) -> dict:
    """获取任务日志（从数据库读取）。"""
    conditions = ["task_id = %s"]
    params: list = [task_id]
    if level:
        conditions.append("log_level = %s")
        params.append(level)

    where_clause = " WHERE " + " AND ".join(conditions)
    offset = (page - 1) * page_size

    # 获取总数
    count_row = fetch_one(
        sql.SQL(f"SELECT COUNT(*) AS cnt FROM public.run_task_logs{where_clause}"),
        tuple(params),
    )
    total = count_row["cnt"] if count_row else 0

    logs = fetch_all(
        sql.SQL(f"""
            SELECT id, task_id, log_level, message,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS created_at
            FROM public.run_task_logs
            {where_clause}
            ORDER BY created_at ASC
            LIMIT %s OFFSET %s
        """),
        tuple(params) + (page_size, offset),
    )

    return {
        "task_id": task_id,
        "logs": logs,
        "total": total,
    }


def get_recent_logs(task_id: str, after_id: int = 0, limit: int = 100) -> list[dict]:
    """获取指定 ID 之后的增量日志（前端轮询用）。"""
    return fetch_all(
        sql.SQL("""
            SELECT id, task_id, log_level, message,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS created_at
            FROM public.run_task_logs
            WHERE task_id = %s AND id > %s
            ORDER BY created_at ASC
            LIMIT %s
        """),
        (task_id, after_id, limit),
    )
