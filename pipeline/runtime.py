"""运行核心：公共工具与日志。

对应原 runtime_core.py:
- _bool_runtime_value / quiet_runtime_output_enabled / runtime_console_print /
  clear_runtime_output_if_needed（行 520-556）
- SimpleLogger / log（行 559-572）
- _ILLEGAL_CHARS / sanitize_filename（行 574-581）
- normalize_text_items / make_json_compatible / append_unique_text_items /
  build_supabase_text_update（行 584-670）
- parse_duration_to_seconds（行 1066-1085）
- format_seconds_hhmmss（行 1167-1169）

依赖：config（读 QUIET_RUNTIME_OUTPUT 等）。
"""
from __future__ import annotations

import csv
import json
import os
import re
import datetime as dt_module
from pathlib import Path

from . import config as cfg


# ---------------------------------------------------------------------------
# parse_text_list_config（原文件行 115-122）
# ---------------------------------------------------------------------------
def parse_text_list_config(value):
    items = []
    for chunk in str(value or "").replace("\r", "\n").split("\n"):
        for part in chunk.split(","):
            item = part.strip()
            if item:
                items.append(item)
    return items


# ---------------------------------------------------------------------------
# 静音输出控制（原文件行 520-536）
# ---------------------------------------------------------------------------
def _bool_runtime_value(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def quiet_runtime_output_enabled():
    return _bool_runtime_value(cfg.QUIET_RUNTIME_OUTPUT, default=True)


def runtime_console_print(message="", level="INFO", force=False, end="\n"):
    normalized_level = str(level or "INFO").strip().upper() or "INFO"
    if not force and quiet_runtime_output_enabled() and normalized_level not in {"WARNING", "ERROR"}:
        return
    print(message, end=end, flush=True)


def clear_runtime_output_if_needed():
    if not quiet_runtime_output_enabled():
        return False

    try:
        from IPython.display import clear_output

        clear_output(wait=True)
        return True
    except Exception:
        try:
            if os.name == "nt":
                os.system("cls")
            else:
                runtime_console_print("\033[2J\033[H", force=True, end="")
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 简易日志器（原文件行 559-572）
# ---------------------------------------------------------------------------
class SimpleLogger:
    def _now(self):
        return dt_module.datetime.now().strftime("%H:%M:%S")

    def info(self, msg, *args):
        text = msg % args if args else msg
        runtime_console_print(f"{self._now()} [INFO] {text}", level="INFO")

    def warning(self, msg, *args):
        text = msg % args if args else msg
        runtime_console_print(f"{self._now()} [WARNING] {text}", level="WARNING")

    def error(self, msg, *args):
        text = msg % args if args else msg
        runtime_console_print(f"{self._now()} [ERROR] {text}", level="ERROR")


log = SimpleLogger()


# ---------------------------------------------------------------------------
# 文件名净化（原文件行 574-581）
# ---------------------------------------------------------------------------
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """去除文件名中的非法字符，限制长度"""
    name = _ILLEGAL_CHARS.sub("_", name).strip()
    return name[:100] if len(name) > 100 else name


# ---------------------------------------------------------------------------
# 文本归一化（原文件行 584-670）
# ---------------------------------------------------------------------------
def normalize_text_items(value):
    """
    兼容历史云端返回的文本集合格式：
    - None / 空值
    - Python list/tuple/set
    - 普通逗号分隔字符串: "a,b"
    - PostgreSQL array literal: {"a","b"}
    """
    if value is None:
        return []

    raw_items = []

    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []

        if text.startswith("{") and text.endswith("}"):
            inner = text[1:-1].strip()
            if not inner:
                return []
            try:
                raw_items = next(
                    csv.reader(
                        [inner],
                        skipinitialspace=True,
                        quotechar='"',
                        escapechar="\\",
                    )
                )
            except Exception:
                raw_items = inner.split(",")
        else:
            raw_items = text.split(",")
    else:
        raw_items = [value]

    normalized = []
    seen = set()
    for item in raw_items:
        text = str(item).strip().strip('"').strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def make_json_compatible(value):
    """Recursively convert runtime objects into JSON-safe values."""
    if isinstance(value, dict):
        return {str(key): make_json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_compatible(item) for item in value]
    if isinstance(value, (dt_module.datetime, dt_module.date, dt_module.time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def append_unique_text_items(existing_value, additions):
    items = normalize_text_items(existing_value)
    seen = set(items)
    for item in normalize_text_items(additions):
        if item in seen:
            continue
        items.append(item)
        seen.add(item)
    return items


def build_supabase_text_update(existing_value, additions, prefer="auto"):
    merged = append_unique_text_items(existing_value, additions)

    mode = (prefer or "auto").strip().lower()
    if mode == "array":
        return merged
    if mode == "string":
        return ",".join(merged)

    if isinstance(existing_value, (list, tuple, set)):
        return merged
    return ",".join(merged)


# ---------------------------------------------------------------------------
# 时长解析（原文件行 1066-1085 / 1167-1169）
# ---------------------------------------------------------------------------
def parse_duration_to_seconds(value):
    if value is None:
        return 0

    text = str(value).strip()
    if not text:
        return 0

    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return 0

    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return 0


def format_seconds_hhmmss(total_seconds):
    seconds = max(0, int(total_seconds or 0))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# JSON 文件读写（原文件行 1291-1307）
# ---------------------------------------------------------------------------
def write_json_file(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def read_json_file(path, default=None):
    if not path or not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("JSON 读取失败 %s: %s", path, e)
        return default
