"""任务调度服务 — 使用 threading 替代 Celery，使用数据库列替代 Redis stop_flag。"""

from __future__ import annotations

import os
import sys
import json
import dataclasses
import traceback
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from psycopg import sql
from psycopg.types.json import Jsonb

from ..settings import settings as app_settings
from ..database import fetch_one, fetch_all, execute

logger = logging.getLogger(__name__)

# 全局线程注册表 — 跟踪活跃的任务线程
_active_threads: dict[str, threading.Thread] = {}

# Pipeline 串行锁 — pipeline 使用模块级全局变量（非线程安全），
# 必须串行执行以避免多线程配置冲突。
_pipeline_lock = threading.Lock()

# stopping 状态超时时间（秒）—— 超过此时间自动转为 cancelled
_STOPPING_TIMEOUT_SECONDS = 300  # 5 分钟


def create_task(channel_names: list[str], task_type: str = "full_pipeline",
                config_overrides: dict = None) -> list[dict]:
    """创建并提交任务（支持多频道）— 使用 threading.Thread 执行。"""
    tasks = []
    overrides = config_overrides or {}

    for channel_name in channel_names:
        # 检查频道是否已有运行中任务
        existing = fetch_one(
            sql.SQL("SELECT task_id FROM public.run_tasks WHERE channel_name = %s AND status IN ('queued', 'running', 'stopping') LIMIT 1"),
            (channel_name,),
        )
        if existing:
            tasks.append({
                "task_id": existing["task_id"],
                "channel_name": channel_name,
                "status": "already_running",
                "message": "该频道已有运行中的任务",
            })
            continue

        # 检查频道是否存在
        channel = fetch_one(
            sql.SQL("SELECT channel_name FROM public.channels WHERE channel_name = %s"),
            (channel_name,),
        )
        if not channel:
            tasks.append({
                "task_id": "",
                "channel_name": channel_name,
                "status": "error",
                "message": f"频道 {channel_name} 不存在",
            })
            continue

        # 读取频道配置（合并全局设置 + 频道配置 + 覆盖）
        from .config_service import build_runtime_config
        runtime_config = build_runtime_config(channel_name, overrides)

        # 任务类型处理
        if task_type == "process_only":
            runtime_config["ENABLE_YOUTUBE_UPLOAD"] = False
        elif task_type == "upload_only":
            runtime_config["FORCE_REPROCESS"] = False

        # 创建任务记录
        row = fetch_one(
            sql.SQL("""
                INSERT INTO public.run_tasks (channel_name, task_type, status, config_snapshot)
                VALUES (%s, %s, 'queued', %s)
                RETURNING *
            """),
            (channel_name, task_type, Jsonb(runtime_config)),
        )
        task_id = row["task_id"]

        # 启动后台线程执行 pipeline
        thread = threading.Thread(
            target=_run_pipeline_in_thread,
            args=(task_id, channel_name, runtime_config),
            daemon=True,
            name=f"pipeline-{task_id}",
        )
        _active_threads[task_id] = thread
        thread.start()

        tasks.append({
            "task_id": task_id,
            "channel_name": channel_name,
            "status": "queued",
        })

    return tasks


def _ensure_pipeline_importable():
    """确保 pipeline 包可被导入（添加到 sys.path）。

    Docker 布局: /app/backend/ 和 /app/pipeline/（同级）
    本地开发: backend/ 和 pipeline/（同级）
    """
    backend_dir = os.path.dirname(os.path.dirname(__file__))  # .../backend
    app_dir = os.path.dirname(backend_dir)                     # ...

    for pipeline_path in [
        os.path.join(app_dir, "pipeline"),       # /app/pipeline (Docker)
        os.path.join(backend_dir, "pipeline"),    # .../backend/pipeline (备选)
    ]:
        if os.path.isdir(pipeline_path):
            parent_dir = os.path.dirname(pipeline_path)
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
            return

    logger.warning("未找到 pipeline 包目录")


def _run_pipeline_in_thread(task_id: str, channel_name: str, runtime_config: dict):
    """在后台线程中执行 pipeline（替代 Celery Worker）。

    注意：pipeline 使用模块级全局变量存储配置（非线程安全），
    因此通过 _pipeline_lock 串行执行，确保同一时间只有一个 pipeline 在运行。
    任务在获取锁之前保持 'queued' 状态，获取锁后才转为 'running'。
    """
    logger.info(f"[Task {task_id}] 线程启动: channel={channel_name}，等待执行锁...")

    # 检查任务是否已被取消（在等待锁之前）
    existing_task = get_task(task_id)
    if existing_task and existing_task.get("status") == "cancelled":
        logger.info(f"[Task {task_id}] 任务已被取消，跳过执行")
        _active_threads.pop(task_id, None)
        return

    # 等待获取 pipeline 串行锁
    # 注意：日志拦截器必须在获取锁之后才安装，因为 StdoutInterceptor 会替换
    # 全局 sys.stdout。如果在获取锁之前安装，多个排队中的任务会互相
    # 覆盖 sys.stdout，导致日志归属错误（串流 Bug）。
    _pipeline_lock.acquire()
    logger.info(f"[Task {task_id}] 获取执行锁，开始运行 pipeline")

    # 日志拦截器安装在获取锁之后 — 此时只有一个任务在运行，
    # sys.stdout 替换不会与其他任务冲突。
    from ..log_interceptor import install_log_interceptor, uninstall_log_interceptor
    handler = install_log_interceptor(task_id)

    try:
        # 获取锁后再次检查是否已被取消
        existing_task = get_task(task_id)
        if existing_task and existing_task.get("status") == "cancelled":
            logger.info(f"[Task {task_id}] 任务在排队期间已被取消，跳过执行")
            return

        # 标记任务为运行中
        update_task_status(task_id, "running")

        # 确保 pipeline 可导入
        _ensure_pipeline_importable()

        from pipeline.config import apply_runtime_config
        from pipeline.pipeline import run_pipeline

        # 注入数据库连接串
        db_url = os.environ.get("DATABASE_URL", app_settings.database_url)
        if db_url.startswith("postgresql+psycopg://"):
            db_url = db_url.replace("postgresql+psycopg://", "postgresql://", 1)
        runtime_config["POSTGRES_DSN"] = db_url

        # 注入任务 ID 到配置中（pipeline 通过此值查询数据库停止标志）
        runtime_config["PIPELINE_TASK_ID"] = task_id

        # 关闭静默模式：确保 INFO 级别日志也输出到 stdout
        runtime_config["QUIET_RUNTIME_OUTPUT"] = False

        # 先应用配置
        apply_runtime_config(runtime_config)

        # 同时设置环境变量（兼容 pipeline 旧代码的 _check_db_stop_flag）
        os.environ["PIPELINE_TASK_ID"] = task_id

        # 执行 pipeline
        result = run_pipeline(runtime_config)

        # 序列化结果
        serializable_result = _make_serializable(result)

        # 检查是否因用户停止而返回
        pipeline_stop_reason = ""
        if isinstance(serializable_result, dict):
            pipeline_stop_reason = str(serializable_result.get("stop_reason", "") or "")

        if pipeline_stop_reason == "用户手动停止" or check_stop_flag(task_id):
            update_task_status(
                task_id, "cancelled",
                result_json=serializable_result if serializable_result else None,
                stop_reason="用户手动停止",
            )
            logger.info(f"[Task {task_id}] 用户手动停止，pipeline 已优雅退出")
            return

        # 防止竞态：如果任务在 pipeline 执行期间已被用户停止（stopping），
        # 即使 pipeline 正常返回也标记为 cancelled
        current_task = get_task(task_id)
        current_status = current_task.get("status") if current_task else None
        if current_status in ("cancelled", "stopping"):
            update_task_status(
                task_id, "cancelled",
                result_json=serializable_result if serializable_result else None,
                stop_reason="用户手动停止",
            )
            logger.info(f"[Task {task_id}] 任务在执行期间已被用户停止，标记为 cancelled")
            return

        # 标记完成
        update_task_status(
            task_id, "success",
            result_json=serializable_result if serializable_result else None,
            stop_reason=pipeline_stop_reason or None,
        )
        logger.info(f"[Task {task_id}] 执行成功")

    except (KeyboardInterrupt, SystemExit):
        update_task_status(task_id, "cancelled", stop_reason="用户手动停止")
        logger.warning(f"[Task {task_id}] 被中断")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        error_tb = traceback.format_exc()
        logger.error(f"[Task {task_id}] 执行失败: {error_msg}\n{error_tb}")

        # 检查是否已被用户取消
        if check_stop_flag(task_id):
            update_task_status(task_id, "cancelled", stop_reason="用户手动停止")
            return

        # 防止竞态：不覆盖已取消/停止中的状态
        current_task = get_task(task_id)
        current_status = current_task.get("status") if current_task else None
        if current_status in ("cancelled", "stopping"):
            update_task_status(task_id, "cancelled", stop_reason="用户手动停止")
            logger.info(f"[Task {task_id}] 任务在异常前已被用户停止，标记为 cancelled")
            return

        update_task_status(task_id, "failed", error_msg=error_msg[:5000])

    finally:
        uninstall_log_interceptor(task_id)
        os.environ.pop("PIPELINE_TASK_ID", None)
        _pipeline_lock.release()
        _active_threads.pop(task_id, None)


def _make_serializable(obj):
    """递归地将对象转换为 JSON 可序列化的值。"""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if dataclasses.is_dataclass(obj):
        return {k: _make_serializable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_serializable(item) for item in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def list_tasks(page: int = 1, page_size: int = 20, channel_name: str = None,
               status: str = None) -> dict:
    """获取任务列表（分页）。"""
    conditions = []
    params = []
    if channel_name:
        conditions.append("rt.channel_name = %s")
        params.append(channel_name)
    if status:
        conditions.append("rt.status = %s")
        params.append(status)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    count_row = fetch_one(
        sql.SQL(f"SELECT COUNT(*) AS cnt FROM public.run_tasks rt{where_clause}"),
        tuple(params),
    )
    total = count_row["cnt"] if count_row else 0

    offset = (page - 1) * page_size
    rows = fetch_all(
        sql.SQL(f"""
            SELECT rt.*, c.display_name AS channel_display_name
            FROM public.run_tasks rt
            LEFT JOIN public.channels c ON c.channel_name = rt.channel_name
            {where_clause}
            ORDER BY rt.created_at DESC
            LIMIT %s OFFSET %s
        """),
        tuple(params) + (page_size, offset),
    )

    return {"tasks": rows, "total": total, "page": page, "page_size": page_size}


def get_task(task_id: str) -> Optional[dict]:
    """获取任务详情。"""
    return fetch_one(
        sql.SQL("""
            SELECT rt.*, c.display_name AS channel_display_name
            FROM public.run_tasks rt
            LEFT JOIN public.channels c ON c.channel_name = rt.channel_name
            WHERE rt.task_id = %s
        """),
        (task_id,),
    )


def stop_task(task_id: str) -> dict:
    """停止运行中的任务 — 设置 stop_requested 标志并将状态改为 stopping。

    状态机流程:
      queued/running → stopping（用户点击停止，pipeline 尚在运行）
      stopping → cancelled（pipeline 检测到标志后优雅退出，由线程设置）

    这样避免了 pipeline 仍在运行时 UI 就显示 'cancelled' 的误导。
    """
    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")

    if task["status"] not in ("queued", "running"):
        return {"task_id": task_id, "status": task["status"], "message": "任务不在运行中"}

    # 设置数据库停止标志（pipeline 通过 _check_db_stop_flag 轮询此字段）
    execute(
        sql.SQL("UPDATE public.run_tasks SET stop_requested = true WHERE task_id = %s"),
        (task_id,),
    )

    # 将状态改为 stopping（不设置 finished_at，因为 pipeline 可能还在运行）
    affected = execute(
        sql.SQL("UPDATE public.run_tasks SET status = 'stopping', stop_reason = '用户手动停止' "
                "WHERE task_id = %s AND status IN ('queued', 'running')"),
        (task_id,),
    )

    if affected == 0:
        task = get_task(task_id)
        actual_status = task["status"] if task else "unknown"
        return {"task_id": task_id, "status": actual_status, "message": "任务已结束，无需停止"}

    return {"task_id": task_id, "status": "stopping", "message": "停止请求已发送，等待 pipeline 优雅退出"}


def get_running_tasks() -> list[dict]:
    """获取当前运行中的任务。

    同时检查是否有任务在 'stopping' 状态卡了超过 5 分钟，
    如果是则自动转为 'cancelled'（pipeline 线程可能已异常退出）。
    """
    # 自动清理超时的 stopping 任务
    _auto_cancel_stale_stopping()

    return fetch_all(
        sql.SQL("""
            SELECT rt.*, c.display_name AS channel_display_name
            FROM public.run_tasks rt
            LEFT JOIN public.channels c ON c.channel_name = rt.channel_name
            WHERE rt.status IN ('queued', 'running', 'stopping')
            ORDER BY rt.created_at DESC
        """)
    )


def _auto_cancel_stale_stopping():
    """将超过超时时间的 stopping 任务自动转为 cancelled。

    场景：pipeline 线程异常死亡（segfault、OOM kill 等），
    没有机会执行 finally 块把状态从 stopping 改为 cancelled。
    """
    execute(
        sql.SQL("""
            UPDATE public.run_tasks
            SET status = 'cancelled',
                finished_at = now(),
                stop_reason = COALESCE(stop_reason, 'pipeline 线程超时未响应，自动取消')
            WHERE status = 'stopping'
              AND updated_at < now() - make_interval(secs => %s)
        """),
        (_STOPPING_TIMEOUT_SECONDS,),
    )


def update_task_status(task_id: str, status: str, **kwargs):
    """更新任务状态。"""
    updates = {"status": status}
    if status == "running" and "started_at" not in kwargs:
        updates["started_at"] = datetime.now(timezone.utc)
    if status in ("success", "failed", "cancelled") and "finished_at" not in kwargs:
        updates["finished_at"] = datetime.now(timezone.utc)

    updates.update(kwargs)

    # 对 jsonb 类型的字段使用 Jsonb 包装
    if "result_json" in updates and updates["result_json"] is not None:
        val = updates["result_json"]
        if not isinstance(val, Jsonb):
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
            updates["result_json"] = Jsonb(val)
    if "config_snapshot" in updates and updates["config_snapshot"] is not None:
        val = updates["config_snapshot"]
        if not isinstance(val, Jsonb):
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
            updates["config_snapshot"] = Jsonb(val)

    set_parts = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder())
        for k in updates.keys()
    )
    execute(
        sql.SQL("UPDATE public.run_tasks SET {} WHERE task_id = %s").format(set_parts),
        tuple(updates.values()) + (task_id,),
    )


def check_stop_flag(task_id: str) -> bool:
    """检查任务是否被请求停止 — 查询数据库 stop_requested 列。"""
    row = fetch_one(
        sql.SQL("SELECT stop_requested FROM public.run_tasks WHERE task_id = %s"),
        (task_id,),
    )
    if not row:
        return False
    return bool(row.get("stop_requested", False))


def cleanup_old_tasks():
    """清理超过 30 天的日志和 90 天的已完成任务。"""
    execute(
        sql.SQL("DELETE FROM public.run_task_logs WHERE created_at < now() - interval '30 days'")
    )
    execute(
        sql.SQL("""
            DELETE FROM public.run_tasks
            WHERE created_at < now() - interval '90 days'
            AND status IN ('success', 'failed', 'cancelled')
        """)
    )
    # 清理过期的 OAuth state
    execute(
        sql.SQL("DELETE FROM public.oauth_states WHERE created_at < now() - interval '1 hour'")
    )
    return {"status": "cleanup_done"}
