"""日志拦截器 — 把 pipeline 的日志写入数据库。

pipeline 的 runtime.log 是 SimpleLogger 实例，基于 print() 输出日志。
标准 logging.Handler 无法捕获 print 输出，因此本模块同时使用两种机制：
1. logging.Handler — 捕获 backend 自身通过 logging 模块输出的日志
2. StdoutInterceptor — 重定向 stdout，捕获 pipeline 通过 print() 输出的日志

日志直接批量写入 PostgreSQL，前端通过 HTTP 轮询读取。
"""

from __future__ import annotations

import sys
import logging
import threading
import re
from datetime import datetime, timezone

from .database import db_connect


_lock = threading.Lock()
_active_handlers: dict[str, "TaskLogHandler"] = {}
_active_interceptors: dict[str, "StdoutInterceptor"] = {}

# 匹配 pipeline 日志格式: "HH:MM:SS [LEVEL] message"
_LOG_PATTERN = re.compile(r"^(\d{2}:\d{2}:\d{2})\s*\[(INFO|WARNING|ERROR|DEBUG)\]\s*(.*)$")


class TaskLogHandler(logging.Handler):
    """拦截标准 logging 日志并写入数据库。"""

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id
        self._buffer: list[dict] = []
        self._buffer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        try:
            log_entry = {
                "task_id": self.task_id,
                "log_level": record.levelname,
                "message": record.getMessage()[:8000],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._dispatch(log_entry)
        except Exception:
            pass

    def emit_text(self, message: str, level: str = "INFO"):
        """直接发送文本日志（从 stdout 拦截的 pipeline 输出）。"""
        try:
            log_entry = {
                "task_id": self.task_id,
                "log_level": level,
                "message": message[:8000],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._dispatch(log_entry)
        except Exception:
            pass

    def _dispatch(self, log_entry: dict):
        """加入数据库写入缓冲。"""
        with self._buffer_lock:
            self._buffer.append(log_entry)
            # 缓冲区满 30 条或超过 5KB 时批量写入
            if len(self._buffer) >= 30:
                self._flush_to_db()

    def _flush_to_db(self):
        """批量写入数据库（使用 executemany 减少 RTT）。"""
        if not self._buffer:
            return
        try:
            from .database import _get_pool
            pool = _get_pool()
            if pool is not None:
                with pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO public.run_task_logs (task_id, log_level, message, created_at) "
                            "VALUES (%s, %s, %s, %s)",
                            [
                                (entry["task_id"], entry["log_level"],
                                 entry["message"], entry["created_at"])
                                for entry in self._buffer
                            ],
                        )
            else:
                with db_connect() as conn:
                    with conn.cursor() as cur:
                        for entry in self._buffer:
                            cur.execute(
                                "INSERT INTO public.run_task_logs (task_id, log_level, message, created_at) "
                                "VALUES (%s, %s, %s, %s)",
                                (entry["task_id"], entry["log_level"],
                                 entry["message"], entry["created_at"]),
                            )
        except Exception as e:
            # 数据库写入失败时不丢弃日志，保留在缓冲区等待下次重试
            # 但限制缓冲区大小防止无限增长
            logging.getLogger(__name__).warning(
                "日志写入数据库失败，保留 %d 条日志等待重试: %s", len(self._buffer), e,
            )
            if len(self._buffer) > 500:
                self._buffer = self._buffer[-500:]
            return
        # 成功写入后清空缓冲区
        self._buffer.clear()

    def flush(self):
        with self._buffer_lock:
            self._flush_to_db()

    def stop(self):
        self.flush()


class StdoutInterceptor:
    """拦截 stdout 输出，将 pipeline 的 print 日志转发到 TaskLogHandler。

    pipeline 的 SimpleLogger 通过 print() 输出格式为:
    "HH:MM:SS [INFO] message" 或 "HH:MM:SS [WARNING] message" 等

    同时保留原始 stdout 输出（让 docker logs 也能看到）。
    """

    def __init__(self, task_id: str, handler: TaskLogHandler):
        self.task_id = task_id
        self.handler = handler
        # 如果 sys.stdout 已经是 StdoutInterceptor（前一个任务未正确清理），
        # 则追溯到真正的 stdout，避免形成拦截器链
        current_stdout = sys.stdout
        if isinstance(current_stdout, StdoutInterceptor):
            self._real_stdout = current_stdout._real_stdout
        else:
            self._real_stdout = current_stdout
        self._real_stderr = sys.stderr
        self._buffer = ""

    def write(self, text: str):
        # 先输出到原始 stdout（让 docker logs 可见）
        try:
            self._real_stdout.write(text)
        except Exception:
            pass

        self._buffer += text
        # 按行处理
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                self._process_line(line)

    def flush(self):
        try:
            self._real_stdout.flush()
        except Exception:
            pass
        # 处理缓冲区中剩余的内容
        if self._buffer.strip():
            self._process_line(self._buffer)
            self._buffer = ""

    def _process_line(self, line: str):
        """解析日志行并转发到 handler。"""
        match = _LOG_PATTERN.match(line.strip())
        if match:
            level = match.group(2)
            message = match.group(3)
            self.handler.emit_text(message, level=level)
        else:
            # 非标准格式的输出，作为 INFO 记录
            self.handler.emit_text(line.strip(), level="INFO")


def install_log_interceptor(task_id: str):
    """安装日志拦截器。

    1. 创建 TaskLogHandler 挂载到 root logger（捕获 logging 模块输出）
    2. 创建 StdoutInterceptor 替换 sys.stdout（捕获 pipeline print 输出）
    """
    handler = TaskLogHandler(task_id)
    handler.setLevel(logging.DEBUG)

    # 挂载到 root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    # 挂载到 backend 模块的 logger
    backend_logger = logging.getLogger("backend")
    backend_logger.addHandler(handler)

    # 安装 stdout 拦截器
    interceptor = StdoutInterceptor(task_id, handler)
    sys.stdout = interceptor

    with _lock:
        _active_handlers[task_id] = handler
        _active_interceptors[task_id] = interceptor

    return handler


def uninstall_log_interceptor(task_id: str):
    """卸载日志拦截器。"""
    with _lock:
        handler = _active_handlers.pop(task_id, None)
        interceptor = _active_interceptors.pop(task_id, None)

    if not handler:
        return

    # 恢复原始 stdout
    if interceptor:
        interceptor.flush()
        sys.stdout = interceptor._real_stdout

    handler.stop()

    root_logger = logging.getLogger()
    root_logger.removeHandler(handler)

    backend_logger = logging.getLogger("backend")
    backend_logger.removeHandler(handler)
