"""运行核心：断点续跑状态管理。

对应原 runtime_core.py 中与 book_processing_states 表相关的全部读写、
分片计划构建、续跑协调、共享资产持久化。

依赖：config（配置全局）、runtime（log / make_json_compatible / 工具）、
      db（数据库操作）、audio（时长估算）、youtube（视频 ID 提取与协调）。
由于 youtube.py 层在上层，部分函数体内采用延迟 import 解决循环依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import datetime as dt_module
from urllib.parse import urlparse, parse_qs

from psycopg import sql

from . import config as cfg
from .runtime import (
    log,
    make_json_compatible,
    format_seconds_hhmmss,
    write_json_file,
    read_json_file,
    normalize_text_items,
    build_supabase_text_update,
    sanitize_filename,
)
from .db import (
    get_book_state_table_name,
    get_public_table_identifier,
    execute_postgres_fetchone,
    execute_postgres_fetchall,
    execute_postgres,
    _update_book_status_in_database,
    _update_book_tags_in_database,
    _delete_book_from_database,
)
from .audio import (
    estimate_chapter_duration_seconds,
    get_explicit_chapter_duration_seconds,
    get_explicit_total_book_duration_seconds,
)

# MIN_BOOK_DURATION_SECONDS（原文件行 1172）
MIN_BOOK_DURATION_SECONDS = 30 * 60


# ============================================================================
# _extract_youtube_video_id — 纯字符串解析，供本模块使用
# 同时会在 youtube.py 中再次定义（逻辑完全相同），本模块内联一份以
# 避免 layer-2 依赖。此处为 fallback；协调逻辑部分再从 youtube 导入。
# ============================================================================
def _extract_youtube_video_id(value):
    text = str(value or "").strip()
    if not text:
        return ""

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text

    try:
        parsed = urlparse(text)
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host:
            return parsed.path.strip("/").split("/")[0]
        if "youtube.com" in host:
            query_id = parse_qs(parsed.query).get("v", [""])[0].strip()
            if query_id:
                return query_id
            parts = [part for part in parsed.path.split("/") if part]
            if "embed" in parts:
                idx = parts.index("embed")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
            if "shorts" in parts:
                idx = parts.index("shorts")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    except Exception:
        return ""

    return ""


# ============================================================================
# 分片计划与签名（原文件行 1175-1289）
# ============================================================================

def build_split_part_plans(chapters_sorted):
    split_trigger_seconds = max(1, int(float(getattr(cfg, "LONG_AUDIO_SPLIT_TRIGGER_HOURS", 12.0) or 12.0) * 3600))
    part_target_seconds = max(1, int(float(getattr(cfg, "LONG_AUDIO_PART_TARGET_HOURS", 11.8) or 11.8) * 3600))

    chapter_items = []
    total_estimated_seconds = 0
    for source_index, chapter in enumerate(chapters_sorted, start=1):
        estimated_seconds = estimate_chapter_duration_seconds(chapter)
        total_estimated_seconds += estimated_seconds
        chapter_items.append(
            {
                "source_index": source_index,
                "chapter": chapter,
                "chapter_id": chapter.get("id", source_index),
                "title": chapter.get("title", f"chapter_{source_index:04d}"),
                "estimated_seconds": estimated_seconds,
            }
        )

    if total_estimated_seconds <= split_trigger_seconds or not chapter_items:
        return {
            "split_mode": False,
            "split_trigger_seconds": split_trigger_seconds,
            "part_target_seconds": part_target_seconds,
            "estimated_total_seconds": total_estimated_seconds,
            "parts": [
                {
                    "part_index": 1,
                    "chapter_start_index": chapter_items[0]["source_index"] if chapter_items else 1,
                    "chapter_end_index": chapter_items[-1]["source_index"] if chapter_items else 0,
                    "estimated_duration_seconds": total_estimated_seconds,
                    "items": chapter_items,
                }
            ],
        }

    parts = []
    current_items = []
    current_seconds = 0

    def flush_current():
        nonlocal current_items, current_seconds
        if not current_items:
            return
        parts.append(
            {
                "part_index": len(parts) + 1,
                "chapter_start_index": current_items[0]["source_index"],
                "chapter_end_index": current_items[-1]["source_index"],
                "estimated_duration_seconds": current_seconds,
                "items": current_items,
            }
        )
        current_items = []
        current_seconds = 0

    for item in chapter_items:
        item_seconds = item["estimated_seconds"]
        if current_items and current_seconds + item_seconds > part_target_seconds:
            flush_current()
        current_items.append(item)
        current_seconds += item_seconds
        if item_seconds > part_target_seconds:
            log.warning(
                "章节 %s 预估时长 %s 已超过单片目标时长 %s，将单独作为一个分片处理。",
                item.get("title") or item.get("chapter_id"),
                format_seconds_hhmmss(item_seconds),
                format_seconds_hhmmss(part_target_seconds),
            )
            flush_current()

    flush_current()

    return {
        "split_mode": True,
        "split_trigger_seconds": split_trigger_seconds,
        "part_target_seconds": part_target_seconds,
        "estimated_total_seconds": total_estimated_seconds,
        "parts": parts,
    }


def build_split_plan_signature(chapters_sorted, split_plan):
    payload = {
        "project_flag": getattr(cfg, "PROJECT_FLAG", ""),
        "split_trigger_hours": getattr(cfg, "LONG_AUDIO_SPLIT_TRIGGER_HOURS", 12.0),
        "part_target_hours": getattr(cfg, "LONG_AUDIO_PART_TARGET_HOURS", 11.8),
        "enable_deepfilter": getattr(cfg, "ENABLE_DEEPFILTER", True),
        "enable_bgm_mix": getattr(cfg, "ENABLE_BGM_MIX", True),
        "enable_video_generation": getattr(cfg, "ENABLE_VIDEO_GENERATION", True),
        "enable_youtube_upload": getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True),
        "video_resolution": getattr(cfg, "VIDEO_RESOLUTION", "1080p"),
        "youtube_channel_name": getattr(cfg, "YOUTUBE_CHANNEL_NAME", ""),
        "chapters": [
            {
                "id": chapter.get("id"),
                "title": chapter.get("title"),
                "long": chapter.get("long"),
            }
            for chapter in chapters_sorted
        ],
        "parts": [
            {
                "part_index": part["part_index"],
                "chapter_start_index": part["chapter_start_index"],
                "chapter_end_index": part["chapter_end_index"],
                "estimated_duration_seconds": part["estimated_duration_seconds"],
                "chapter_ids": [item.get("chapter_id") for item in part.get("items", [])],
            }
            for part in split_plan.get("parts", [])
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.md5(raw).hexdigest()


# ============================================================================
# 状态引用与布尔配置读取（原文件行 1722-1762）
# ============================================================================

def build_split_state_ref(book_id, project_flag=None):
    flag = str(getattr(cfg, "PROJECT_FLAG", "")) if project_flag is None else str(project_flag).strip()
    return f"postgres:{get_book_state_table_name()}:{flag}:{book_id}"


def _read_bool_runtime_config(name, default=False):
    value = getattr(cfg, name, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_cleanup_completed_split_states():
    return _read_bool_runtime_config("CLEANUP_COMPLETED_SPLIT_STATES", False)


def _build_split_part_lookup_key(part_like):
    if not isinstance(part_like, dict):
        return ()

    chapter_ids = part_like.get("chapter_ids") or []
    normalized_ids = tuple(str(item).strip() for item in chapter_ids if str(item).strip())
    if normalized_ids:
        return ("chapter_ids",) + normalized_ids

    start_index = str(part_like.get("chapter_start_index") or "").strip()
    end_index = str(part_like.get("chapter_end_index") or "").strip()
    if start_index or end_index:
        return ("range", start_index, end_index)

    part_index = str(part_like.get("part_index") or "").strip()
    return ("part_index", part_index) if part_index else ()


def _split_part_has_uploaded_video(part_state):
    if not isinstance(part_state, dict):
        return False
    return bool(str(part_state.get("video_id") or "").strip() or str(part_state.get("youtube_url") or "").strip())


def _is_split_playlist_required(part_count):
    return bool(
        int(part_count or 0) > 1
        and getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True)
        and str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    )


# ============================================================================
# 分片完成度判断（原文件行 1766-1833）
# ============================================================================

def _split_part_is_completed(part_state):
    if not isinstance(part_state, dict):
        return False

    if str(part_state.get("status") or "").strip().lower() == "completed":
        return True

    if getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True) and str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip():
        return _split_part_has_uploaded_video(part_state)
    if getattr(cfg, "ENABLE_VIDEO_GENERATION", True):
        return _is_nonempty_local_file(part_state.get("video_path"))
    return _is_nonempty_local_file(part_state.get("audio_path"))


def _reconcile_split_part_state(part_state):
    if not isinstance(part_state, dict):
        return False
    if not _split_part_is_completed(part_state):
        return False

    changed = False
    if str(part_state.get("status") or "").strip().lower() != "completed":
        part_state["status"] = "completed"
        changed = True
    if not str(part_state.get("completed_at") or "").strip():
        part_state["completed_at"] = dt_module.datetime.now().isoformat()
        changed = True
    if str(part_state.get("last_stage") or "").strip() != "completed":
        part_state["last_stage"] = "completed"
        changed = True
    if str(part_state.get("error") or "").strip():
        part_state["error"] = ""
        changed = True
    return changed


def evaluate_split_completion_state(state):
    if not isinstance(state, dict):
        state = {}

    parts = state.get("parts", [])
    if not isinstance(parts, list):
        parts = []

    for item in parts:
        _reconcile_split_part_state(item)

    part_count = max(1, int(state.get("part_count") or len(parts) or 1))
    completed_part_count = sum(1 for item in parts if _split_part_is_completed(item))
    playlist_state = get_split_playlist_state(state) if state.get("mode") == "split_upload" else {}
    playlist_required = bool(state.get("mode") == "split_upload" and _is_split_playlist_required(part_count))
    playlist_completed = (
        not playlist_required
        or (
            bool(str(playlist_state.get("playlist_id") or "").strip())
            and str(playlist_state.get("status") or "").strip().lower() == "completed"
        )
    )

    return {
        "part_count": part_count,
        "completed_part_count": completed_part_count,
        "all_parts_completed": completed_part_count >= part_count,
        "playlist_required": playlist_required,
        "playlist_completed": playlist_completed,
        "fully_completed": completed_part_count >= part_count and playlist_completed,
    }


# ============================================================================
# 状态反序列化（原文件行 1835-1962）
# ============================================================================

def normalize_split_state_from_row(row):
    state = row.get("state_json") or {}
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except Exception:
            state = {}
    if not isinstance(state, dict):
        state = {}
    state = make_json_compatible(state)

    book_id = str(row.get("book_id") or state.get("book_id") or "").strip()
    state["book_id"] = book_id
    state["book_name"] = row.get("book_name") or state.get("book_name", "")
    state["category"] = row.get("category") or state.get("category", "")
    state["pending_resume"] = bool(
        row.get("pending_resume") if row.get("pending_resume") is not None else state.get("pending_resume")
    )
    state["status"] = row.get("state_status") or state.get("status", "")
    state["current_part_index"] = (
        row.get("current_part_index")
        if row.get("current_part_index") is not None
        else state.get("current_part_index")
    )
    state["completed_part_count"] = int(
        row.get("completed_part_count")
        if row.get("completed_part_count") is not None
        else state.get("completed_part_count", 0)
    )
    state["part_count"] = int(row.get("part_count") if row.get("part_count") is not None else state.get("part_count", 0))
    state["updated_at"] = make_json_compatible(row.get("updated_at")) or state.get("updated_at", "")
    state["created_at"] = make_json_compatible(row.get("created_at")) or state.get("created_at", "")
    state["state_path"] = build_split_state_ref(book_id, row.get("project_flag"))
    return state


def load_split_processing_state(book_record):
    table_name = get_book_state_table_name()
    book_id = str(book_record.get("book_id") or "").strip()
    project_flag = str(getattr(cfg, "PROJECT_FLAG", "") or "").strip()

    if not book_id:
        return None

    table_sql = get_public_table_identifier(table_name)
    try:
        row = execute_postgres_fetchone(
            sql.SQL(
                """
                SELECT
                  book_id,
                  project_flag,
                  book_name,
                  category,
                  pending_resume,
                  state_status,
                  current_part_index,
                  completed_part_count,
                  part_count,
                  updated_at,
                  created_at,
                  state_json
                FROM {}
                WHERE book_id = %s AND project_flag = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).format(table_sql),
            (book_id, project_flag),
        )
        if not row:
            return None
        return normalize_split_state_from_row(row)
    except Exception as e:
        raise RuntimeError(f"从数据库读取断点状态失败，请检查表 {table_name}: {e}")


def _build_split_state_completeness_rank(state):
    if not isinstance(state, dict) or not state:
        return (-1, -1, -1, -1)

    progress = evaluate_split_completion_state(state)
    playlist_state = get_split_playlist_state(state) if state.get("mode") == "split_upload" else {}
    return (
        int(progress.get("completed_part_count") or 0),
        1 if bool(progress.get("playlist_completed")) else 0,
        1 if str(playlist_state.get("playlist_id") or "").strip() else 0,
        1 if str(playlist_state.get("status") or "").strip().lower() == "completed" else 0,
    )


def reload_split_processing_state(book_record, fallback_state=None, book_name=""):
    loaded_state = load_split_processing_state(book_record)
    if not isinstance(loaded_state, dict) or not loaded_state:
        return fallback_state if isinstance(fallback_state, dict) else loaded_state

    if not isinstance(fallback_state, dict) or not fallback_state:
        return loaded_state

    loaded_rank = _build_split_state_completeness_rank(loaded_state)
    fallback_rank = _build_split_state_completeness_rank(fallback_state)
    if loaded_rank < fallback_rank:
        label = str(
            book_name
            or book_record.get("book_name")
            or fallback_state.get("book_name")
            or loaded_state.get("book_name")
            or book_record.get("book_id")
            or "unknown-book"
        ).strip()
        loaded_playlist = get_split_playlist_state(loaded_state) if loaded_state.get("mode") == "split_upload" else {}
        fallback_playlist = (
            get_split_playlist_state(fallback_state) if fallback_state.get("mode") == "split_upload" else {}
        )
        log.warning(
            "[%s] Reloaded split state looks older than the in-memory state; keeping the more complete local state. "
            "loaded_rank=%s local_rank=%s loaded_playlist_id=%s local_playlist_id=%s loaded_playlist_status=%s local_playlist_status=%s",
            label,
            loaded_rank,
            fallback_rank,
            str(loaded_playlist.get("playlist_id") or ""),
            str(fallback_playlist.get("playlist_id") or ""),
            str(loaded_playlist.get("status") or ""),
            str(fallback_playlist.get("status") or ""),
        )
        return fallback_state

    return loaded_state


# ============================================================================
# 状态持久化（原文件行 1965-2673）
# ============================================================================

def _save_split_processing_state_raw(book_record, state):
    from psycopg.types.json import Jsonb

    now = dt_module.datetime.now().isoformat()
    parts = state.get("parts", [])
    progress = evaluate_split_completion_state(state)
    completed_count = progress["completed_part_count"]
    state["completed_part_count"] = completed_count
    state["part_count"] = progress["part_count"]
    pending_parts = [item.get("part_index") for item in parts if not _split_part_is_completed(item)]
    state["current_part_index"] = pending_parts[0] if pending_parts else None
    state["updated_at"] = now

    if progress["fully_completed"]:
        state["status"] = "completed"
        state["pending_resume"] = False
        state["completed_at"] = state.get("completed_at") or now
    else:
        state["status"] = "in_progress"
        state["pending_resume"] = True
        state.pop("completed_at", None)

    book_id = str(book_record.get("book_id") or state.get("book_id") or "").strip()
    project_flag = str(getattr(cfg, "PROJECT_FLAG", "") or "").strip()
    table_name = get_book_state_table_name()
    table_sql = get_public_table_identifier(table_name)
    state_ref = build_split_state_ref(book_id, project_flag)
    state["state_path"] = state_ref
    state["book_id"] = book_id
    state["book_name"] = book_record.get("book_name") or state.get("book_name", "")
    state["category"] = book_record.get("category") or state.get("category", "")
    state["created_at"] = state.get("created_at") or now
    state_json_payload = make_json_compatible(state)

    try:
        execute_postgres(
            sql.SQL(
                """
                INSERT INTO {} (
                  book_id,
                  project_flag,
                  book_name,
                  category,
                  pending_resume,
                  state_status,
                  current_part_index,
                  completed_part_count,
                  part_count,
                  updated_at,
                  created_at,
                  state_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (book_id, project_flag)
                DO UPDATE SET
                  book_name = EXCLUDED.book_name,
                  category = EXCLUDED.category,
                  pending_resume = EXCLUDED.pending_resume,
                  state_status = EXCLUDED.state_status,
                  current_part_index = EXCLUDED.current_part_index,
                  completed_part_count = EXCLUDED.completed_part_count,
                  part_count = EXCLUDED.part_count,
                  updated_at = EXCLUDED.updated_at,
                  created_at = EXCLUDED.created_at,
                  state_json = EXCLUDED.state_json
                """
            ).format(table_sql),
            (
                book_id,
                project_flag,
                state["book_name"],
                state["category"],
                bool(state.get("pending_resume", False)),
                state.get("status", "in_progress"),
                state.get("current_part_index"),
                int(state.get("completed_part_count") or 0),
                int(state.get("part_count") or 1),
                state["updated_at"],
                state["created_at"],
                Jsonb(state_json_payload),
            ),
        )
    except Exception as e:
        raise RuntimeError(f"写入数据库断点状态失败，请检查表 {table_name}: {e}")

    return state_ref


def _truncate_split_state_debug_value(value, limit=240):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _build_split_state_debug_payload(book_record, state):
    safe_state = state if isinstance(state, dict) else {}
    safe_book = book_record if isinstance(book_record, dict) else {}
    progress = evaluate_split_completion_state(safe_state)
    playlist_state = get_split_playlist_state(safe_state) if safe_state.get("mode") == "split_upload" else {}

    parts_summary = []
    for item in safe_state.get("parts", []) or []:
        if not isinstance(item, dict):
            continue
        audio_path = str(item.get("audio_path") or "").strip()
        video_path = str(item.get("video_path") or "").strip()
        parts_summary.append(
            {
                "part_index": item.get("part_index"),
                "status": str(item.get("status") or ""),
                "last_stage": str(item.get("last_stage") or ""),
                "error": _truncate_split_state_debug_value(item.get("error")),
                "has_audio_path": bool(audio_path),
                "has_video_path": bool(video_path),
                "has_video_id": bool(str(item.get("video_id") or "").strip()),
                "has_youtube_url": bool(str(item.get("youtube_url") or "").strip()),
                "audio_file": os.path.basename(audio_path) if audio_path else "",
                "video_file": os.path.basename(video_path) if video_path else "",
                "youtube_title": _truncate_split_state_debug_value(item.get("youtube_title"), limit=120),
            }
        )

    payload = {
        "book_id": str(safe_book.get("book_id") or safe_state.get("book_id") or "").strip(),
        "project_flag": str(getattr(cfg, "PROJECT_FLAG", "") or "").strip(),
        "book_name": str(safe_book.get("book_name") or safe_state.get("book_name") or "").strip(),
        "category": str(safe_book.get("category") or safe_state.get("category") or "").strip(),
        "state_table": str(get_book_state_table_name() or "").strip(),
        "state_status": str(safe_state.get("status") or ""),
        "pending_resume": bool(safe_state.get("pending_resume")),
        "last_stage": str(safe_state.get("last_stage") or ""),
        "last_error": _truncate_split_state_debug_value(safe_state.get("last_error")),
        "current_part_index": safe_state.get("current_part_index"),
        "completed_part_count": progress["completed_part_count"],
        "part_count": progress["part_count"],
        "playlist_required": progress["playlist_required"],
        "playlist_completed": progress["playlist_completed"],
        "playlist_status": str(playlist_state.get("status") or ""),
        "playlist_id": _truncate_split_state_debug_value(playlist_state.get("playlist_id"), limit=80),
        "playlist_url": _truncate_split_state_debug_value(playlist_state.get("playlist_url"), limit=120),
        "parts": parts_summary,
    }
    return make_json_compatible(payload)


def _maybe_log_split_state_persisted(book_record, state, state_ref):
    if not isinstance(state, dict):
        return

    last_stage = str(state.get("last_stage") or "").strip()
    if not last_stage:
        return

    book_name = str(book_record.get("book_name") or state.get("book_name") or state.get("book_id") or "unknown-book").strip()
    progress = evaluate_split_completion_state(state)
    part_count = max(1, int(progress.get("part_count") or 1))
    completed_part_count = max(0, int(progress.get("completed_part_count") or 0))
    last_error = _truncate_split_state_debug_value(state.get("last_error"), limit=160)

    match = re.fullmatch(r"part_(\d+)_(.+)", last_stage)
    if match:
        part_index = int(match.group(1))
        suffix = match.group(2)
        part_state = get_split_part_state(state, part_index) or {}

        if suffix == "upload_persisted":
            if not _split_part_has_uploaded_video(part_state):
                return
            log.info(
                "[%s] 分片 %d/%d 的上传回执已写入数据库续跑状态（进度 %d/%d，state=%s）",
                book_name, part_index, part_count, completed_part_count, part_count, state_ref,
            )
            return

        if suffix == "completed":
            if not _split_part_is_completed(part_state):
                return
            log.info(
                "[%s] 分片 %d/%d 已处理完成，当前状态已写入数据库（进度 %d/%d，state=%s）",
                book_name, part_index, part_count, completed_part_count, part_count, state_ref,
            )
            return

        if suffix == "failed":
            if str(part_state.get("status") or "").strip().lower() != "failed":
                return
            log.warning(
                "[%s] 分片 %d/%d 的失败状态已写入数据库（进度 %d/%d，state=%s，error=%s）",
                book_name, part_index, part_count, completed_part_count, part_count, state_ref, last_error,
            )
            return

    if last_stage == "playlist_completed":
        if not progress["playlist_completed"]:
            return
        log.info("[%s] 播放列表完成状态已写入数据库（进度 %d/%d，state=%s）", book_name, completed_part_count, part_count, state_ref)
        return

    if last_stage == "playlist_failed":
        log.warning("[%s] 播放列表失败状态已写入数据库（进度 %d/%d，state=%s，error=%s）", book_name, completed_part_count, part_count, state_ref, last_error)
        return

    if last_stage == "all_parts_completed":
        if not progress["fully_completed"]:
            return
        log.info("[%s] 多 P 最终完成状态已写入数据库（进度 %d/%d，state=%s）", book_name, completed_part_count, part_count, state_ref)


def save_split_processing_state(book_record, state):
    import traceback
    try:
        state_ref = _save_split_processing_state_raw(book_record, state)
    except Exception as e:
        debug_payload = _build_split_state_debug_payload(book_record, state)
        debug_text = json.dumps(debug_payload, ensure_ascii=False, sort_keys=True)
        book_label = debug_payload.get("book_name") or debug_payload.get("book_id") or "unknown-book"
        log.error("[%s] 保存续跑状态失败，调试详情: %s", book_label, debug_text)
        log.error("[%s] 保存续跑状态异常堆栈: %s", book_label, traceback.format_exc())
        raise RuntimeError(f"保存续跑状态失败，调试详情: {debug_text} | 原始异常: {e}") from e
    _maybe_log_split_state_persisted(book_record, state, state_ref)
    return state_ref


def delete_split_processing_state(book_record, only_if_completed=False):
    book_id = str(book_record.get("book_id") or "").strip()
    project_flag = str(getattr(cfg, "PROJECT_FLAG", "") or "").strip()
    if not book_id:
        return False

    table_name = get_book_state_table_name()
    table_sql = get_public_table_identifier(table_name)

    try:
        statement = sql.SQL("DELETE FROM {} WHERE book_id = %s AND project_flag = %s").format(table_sql)
        params = [book_id, project_flag]
        if only_if_completed:
            statement += sql.SQL(" AND state_status = %s")
            params.append("completed")
        execute_postgres(statement, tuple(params))
        return True
    except Exception as e:
        raise RuntimeError(f"删除数据库分片状态失败，请检查表 {table_name}: {e}")


def cleanup_completed_split_states(project_flag=None, category=None):
    if not _should_cleanup_completed_split_states():
        return 0

    table_name = get_book_state_table_name()
    table_sql = get_public_table_identifier(table_name)
    flag = str(getattr(cfg, "PROJECT_FLAG", "")) if project_flag is None else str(project_flag).strip()
    category_name = str(getattr(cfg, "TARGET_CATEGORY", "")) if category is None else str(category).strip()
    total_deleted = 0

    while True:
        try:
            statement = sql.SQL(
                """
                SELECT book_id, project_flag
                FROM {}
                WHERE state_status = %s
                """
            ).format(table_sql)
            params = ["completed"]
            if flag:
                statement += sql.SQL(" AND project_flag = %s")
                params.append(flag)
            if category_name:
                statement += sql.SQL(" AND category = %s")
                params.append(category_name)
            statement += sql.SQL(" ORDER BY updated_at ASC LIMIT %s")
            params.append(100)
            rows = execute_postgres_fetchall(statement, tuple(params))
        except Exception as e:
            raise RuntimeError(f"清理数据库已完成分片状态失败，请检查表 {table_name}: {e}")

        if not rows:
            break

        for row in rows:
            current_book_id = str(row.get("book_id") or "").strip()
            current_flag = str(row.get("project_flag") or "").strip()
            if not current_book_id:
                continue
            try:
                deleted = execute_postgres(
                    sql.SQL(
                        """
                        DELETE FROM {}
                        WHERE book_id = %s AND project_flag = %s AND state_status = %s
                        """
                    ).format(table_sql),
                    (current_book_id, current_flag, "completed"),
                )
                if deleted > 0:
                    total_deleted += 1
            except Exception as delete_error:
                log.warning("清理已完成分片状态失败 book_id=%s: %s", current_book_id, delete_error)

        if len(rows) < 100:
            break

    return total_deleted


def initialize_split_processing_state(book_record, book_dir, chapters_sorted, split_plan):
    signature = build_split_plan_signature(chapters_sorted, split_plan)
    existing = load_split_processing_state(book_record) or {}

    reuse_existing = isinstance(existing, dict) and existing.get("plan_signature") == signature
    compatible_reuse = isinstance(existing, dict) and bool(existing.get("parts"))
    existing_parts_by_index = {}
    existing_parts_by_key = {}
    if compatible_reuse:
        for item in existing.get("parts", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("part_index", "")).isdigit():
                existing_parts_by_index[int(item.get("part_index"))] = item
            part_key = _build_split_part_lookup_key(item)
            if part_key and part_key not in existing_parts_by_key:
                existing_parts_by_key[part_key] = item

    parts_state = []
    matched_existing_parts = 0
    for part in split_plan.get("parts", []):
        part_key = _build_split_part_lookup_key(
            {
                "part_index": part["part_index"],
                "chapter_start_index": part["chapter_start_index"],
                "chapter_end_index": part["chapter_end_index"],
                "chapter_ids": [item.get("chapter_id") for item in part.get("items", [])],
            }
        )
        previous = {}
        if reuse_existing:
            previous = existing_parts_by_index.get(part["part_index"], {})
        if not previous and part_key:
            previous = existing_parts_by_key.get(part_key, {})
        if previous:
            matched_existing_parts += 1
        parts_state.append(
            {
                "part_index": part["part_index"],
                "chapter_start_index": part["chapter_start_index"],
                "chapter_end_index": part["chapter_end_index"],
                "estimated_duration_seconds": part["estimated_duration_seconds"],
                "chapter_ids": [item.get("chapter_id") for item in part.get("items", [])],
                "status": previous.get("status", "pending") if previous else "pending",
                "started_at": previous.get("started_at", ""),
                "completed_at": previous.get("completed_at", ""),
                "last_stage": previous.get("last_stage", ""),
                "audio_path": previous.get("audio_path", ""),
                "video_path": previous.get("video_path", ""),
                "video_id": previous.get("video_id", ""),
                "uploaded_at": previous.get("uploaded_at", ""),
                "publish_at": previous.get("publish_at", ""),
                "schedule_reason": previous.get("schedule_reason", ""),
                "youtube_url": previous.get("youtube_url", ""),
                "youtube_title": previous.get("youtube_title", ""),
                "youtube_chapters": previous.get("youtube_chapters", ""),
                "playlist_item_id": previous.get("playlist_item_id", ""),
                "error": previous.get("error", ""),
                "actual_duration_seconds": previous.get("actual_duration_seconds", 0),
            }
        )

    structure_compatible = bool(parts_state) and matched_existing_parts == len(parts_state)
    if structure_compatible and compatible_reuse and not reuse_existing:
        log.info("检测到分片结构兼容，虽然计划签名变化，仍继续复用已有多 P 状态以避免重复上传。")
    state = {
        "state_version": 5,
        "mode": "split_upload",
        "book_id": str(book_record.get("book_id", "")),
        "book_name": book_record.get("book_name", ""),
        "category": book_record.get("category", ""),
        "plan_signature": signature,
        "split_trigger_seconds": split_plan.get("split_trigger_seconds"),
        "part_target_seconds": split_plan.get("part_target_seconds"),
        "estimated_total_seconds": split_plan.get("estimated_total_seconds", 0),
        "part_count": len(parts_state),
        "parts": parts_state,
        "shared_assets": existing.get("shared_assets", {}) if structure_compatible else {},
        "playlist": existing.get("playlist", {}) if structure_compatible else {},
        "last_stage": existing.get("last_stage", "plan_ready") if structure_compatible else "plan_ready",
        "last_error": existing.get("last_error", "") if structure_compatible else "",
        "pending_resume": bool(existing.get("pending_resume")) if structure_compatible else True,
        "created_at": existing.get("created_at") if compatible_reuse else dt_module.datetime.now().isoformat(),
    }
    state_ref = save_split_processing_state(book_record, state)
    return state_ref, state


# ============================================================================
# 分片/共享状态访问（原文件行 2735-2868 / 6752-6768）
# ============================================================================

def get_split_part_state(state, part_index):
    for item in state.get("parts", []):
        if int(item.get("part_index", 0)) == int(part_index):
            return item
    return None


def _book_has_project_status(book_record_or_status, project_flag=None):
    flag = str(getattr(cfg, "PROJECT_FLAG", "")) if project_flag is None else str(project_flag).strip()
    if not flag:
        return False

    status_value = book_record_or_status
    if isinstance(book_record_or_status, dict):
        status_value = book_record_or_status.get("status")

    return flag in set(normalize_text_items(status_value))


def list_interrupted_book_states(book_rows_by_id=None):
    states = {}
    table_name = get_book_state_table_name()
    table_sql = get_public_table_identifier(table_name)
    project_flag = str(getattr(cfg, "PROJECT_FLAG", "") or "").strip()
    page_size = 100
    offset = 0
    book_rows_by_id = book_rows_by_id if isinstance(book_rows_by_id, dict) else {}

    while True:
        try:
            statement = sql.SQL(
                """
                SELECT
                  book_id,
                  project_flag,
                  book_name,
                  category,
                  pending_resume,
                  state_status,
                  current_part_index,
                  completed_part_count,
                  part_count,
                  updated_at,
                  created_at,
                  state_json
                FROM {}
                WHERE project_flag = %s
                """
            ).format(table_sql)
            params = [project_flag]
            target_cat = str(getattr(cfg, "TARGET_CATEGORY", "") or "").strip()
            if target_cat:
                statement += sql.SQL(" AND category = %s")
                params.append(target_cat)

            statement += sql.SQL(" ORDER BY updated_at DESC LIMIT %s OFFSET %s")
            params.extend([page_size, offset])
            rows = execute_postgres_fetchall(statement, tuple(params))
        except Exception as e:
            raise RuntimeError(f"查询数据库未完成断点状态失败，请检查表 {table_name}: {e}")

        if not rows:
            break

        for row in rows:
            state = normalize_split_state_from_row(row)
            state_mode = str(state.get("mode") or "").strip().lower()
            if state_mode not in {"split_upload", "standard_upload"}:
                continue

            book_id = str(state.get("book_id") or "").strip()
            if not book_id:
                continue

            book_record = book_rows_by_id.get(book_id, {})
            already_processed = _book_has_project_status(book_record, project_flag=project_flag)

            if state_mode == "standard_upload":
                if already_processed:
                    try:
                        if delete_split_processing_state({"book_id": book_id}, only_if_completed=False):
                            log.info(
                                "[%s] 检测到 books.status 已包含当前频道，已补删残留的单 P book_processing_states。",
                                state.get("book_name") or book_id,
                            )
                    except Exception as delete_error:
                        log.warning(
                            "[%s] books.status 已标记成功，但补删残留单 P book_processing_states 失败: %s",
                            state.get("book_name") or book_id,
                            delete_error,
                        )
                    continue

                existing = states.get(book_id)
                if not existing or str(state.get("updated_at", "")) > str(existing.get("updated_at", "")):
                    states[book_id] = state
                continue

            progress = evaluate_split_completion_state(state)
            state["completed_part_count"] = progress["completed_part_count"]
            state["part_count"] = progress["part_count"]
            state["status"] = "completed" if progress["fully_completed"] else "in_progress"
            state["pending_resume"] = not progress["fully_completed"]
            if already_processed and progress["fully_completed"]:
                try:
                    if delete_split_processing_state({"book_id": book_id}, only_if_completed=False):
                        log.info(
                            "[%s] 检测到 books.status 已包含当前频道，已补删残留的 book_processing_states。",
                            state.get("book_name") or book_id,
                        )
                except Exception as delete_error:
                    log.warning(
                        "[%s] books.status 已标记成功，但补删残留 book_processing_states 失败: %s",
                        state.get("book_name") or book_id,
                        delete_error,
                    )
                continue

            if already_processed:
                log.warning(
                    "[%s] books.status 已包含当前频道，但残留多 P 状态未完成；本次启动将忽略这条残留状态。",
                    state.get("book_name") or book_id,
                )
                continue

            existing = states.get(book_id)
            if not existing or str(state.get("updated_at", "")) > str(existing.get("updated_at", "")):
                states[book_id] = state

        if len(rows) < page_size:
            break
        offset += page_size

    return states


def get_split_shared_assets(state):
    shared = state.get("shared_assets")
    if isinstance(shared, dict):
        return shared

    state["shared_assets"] = {}
    return state["shared_assets"]


def get_split_playlist_state(state):
    playlist = state.get("playlist")
    if isinstance(playlist, dict):
        return playlist

    state["playlist"] = {}
    return state["playlist"]


# ============================================================================
# 非空文件检测（原文件行 3783，供多模块复用）
# ============================================================================
def _is_nonempty_local_file(path):
    return bool(path and os.path.exists(path) and os.path.getsize(path) > 0)


# ============================================================================
# 标准上传状态构建（原文件行 6846-6865）
# ============================================================================

def build_standard_processing_state(book_record):
    existing = load_split_processing_state(book_record) or {}
    existing_mode = str(existing.get("mode") or "").strip().lower() if isinstance(existing, dict) else ""
    existing_shared_assets = existing.get("shared_assets") if isinstance(existing.get("shared_assets"), dict) else {}
    now = dt_module.datetime.now().isoformat()

    return {
        "state_version": 5,
        "mode": "standard_upload",
        "book_id": str(book_record.get("book_id", "")),
        "book_name": book_record.get("book_name", ""),
        "category": book_record.get("category", ""),
        "part_count": 1,
        "parts": [],
        "shared_assets": existing_shared_assets,
        "last_stage": existing.get("last_stage", "standard_assets_pending") if existing_mode == "standard_upload" else "standard_assets_pending",
        "last_error": existing.get("last_error", "") if existing_mode == "standard_upload" else "",
        "pending_resume": True,
        "created_at": existing.get("created_at") if existing_mode == "standard_upload" else now,
    }


# ============================================================================
# 共享资产持久化/恢复（原文件行 6770-6843）
# ============================================================================

def restore_split_shared_assets_from_state(result, state, book_dir, safe_name, book_name):
    import base64

    shared = get_split_shared_assets(state)
    restored_items = []

    seo_title = str(shared.get("seo_title") or "").strip()
    seo_description = str(shared.get("seo_description") or "")
    seo_tags = str(shared.get("seo_tags") or "")
    if seo_title or seo_description or seo_tags:
        seo_path = os.path.join(book_dir, f"{safe_name}_seo_description.json")
        seo_dict = {
            "title": seo_title,
            "Description": seo_description,
            "label": seo_tags,
        }
        try:
            if not (os.path.exists(seo_path) and os.path.getsize(seo_path) > 0):
                write_json_file(seo_path, seo_dict)
            result.seo_text_path = seo_path
            result.seo_title = seo_title
            result.seo_description = seo_description
            result.seo_tags = seo_tags
            restored_items.append("SEO")
        except Exception as e:
            log.warning("[%s] 从数据库状态恢复 SEO 文案失败: %s", book_name, e)

    cover_base64 = str(shared.get("cover_image_base64") or "").strip()
    cover_filename = str(shared.get("cover_filename") or f"{safe_name}_cover.jpg").strip()
    if cover_base64:
        cover_path = os.path.join(book_dir, os.path.basename(cover_filename))
        try:
            if not (os.path.exists(cover_path) and os.path.getsize(cover_path) > 0):
                os.makedirs(os.path.dirname(cover_path), exist_ok=True)
                with open(cover_path, "wb") as handle:
                    handle.write(base64.b64decode(cover_base64.encode("ascii")))
            if os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
                result.cover_image_path = cover_path
                restored_items.append("封面")
        except Exception as e:
            log.warning("[%s] 从数据库状态恢复共享封面失败: %s", book_name, e)

    if restored_items:
        log.info("[%s] 已从数据库状态恢复长音频共享%s。", book_name, "与".join(restored_items))

    return result


def persist_split_shared_assets_to_state(book_record, state, result, book_dir, safe_name, book_name):
    import base64

    shared = get_split_shared_assets(state)

    shared["seo_title"] = str(result.seo_title or "")
    shared["seo_description"] = str(result.seo_description or "")
    shared["seo_tags"] = str(result.seo_tags or "")
    shared["cover_filename"] = ""

    if result.seo_title or result.seo_description or result.seo_tags:
        shared["seo_json_filename"] = f"{safe_name}_seo_description.json"

    if result.cover_image_path and os.path.exists(result.cover_image_path) and os.path.getsize(result.cover_image_path) > 0:
        try:
            shared["cover_filename"] = os.path.basename(result.cover_image_path)
            with open(result.cover_image_path, "rb") as handle:
                shared["cover_image_base64"] = base64.b64encode(handle.read()).decode("ascii")
        except Exception as e:
            log.warning("[%s] 写入数据库共享封面前读取本地文件失败: %s", book_name, e)

    shared["shared_title_without_prefix"] = str(result.seo_title or book_name or "")
    shared["shared_description"] = str(result.seo_description or "")
    shared["shared_tags"] = str(result.seo_tags or "")
    shared["shared_cover_path"] = str(result.cover_image_path or "")
    shared["synced_at"] = dt_module.datetime.now().isoformat()

    state_ref = save_split_processing_state(book_record, state)
    result.state_path = state_ref
    return state_ref