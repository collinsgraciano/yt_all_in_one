"""运行核心：Podcast 管理（Show 封面、统一 Show 同步、Sensenova AI 引擎）+ Monkey-Patch。

对应原 runtime_core.py 行 8269-10247：
- _PODCAST_RUNTIME_DEFAULTS + 二次 apply_runtime_config（行 8278-8299）
- 所有 _podcast_* 辅助函数（行 8308-9270）
- Sensenova AI 封面/文本引擎（行 8985-9237）
- 本地文字渐变封面生成（行 9392-9456）
- _podcast_generate_named_cover_image / _show_cover_image（行 9500-9593）
- Podcast 播放列表同步（行 9596-10116）
- Unified show 单视频同步（行 9720-9862）
- Split playlist podcast 同步（行 9865-9956）
- Monkey-patch 覆盖 pipeline.py 的函数（行 9959-10129）
- 覆盖 youtube.py 的 playlist 辅助函数（底部）
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
import datetime as dt_module
from collections import defaultdict as _podcast_defaultdict
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from PIL import Image, ImageDraw, ImageFont, ImageOps
from openai import OpenAI

from . import config as cfg
from .runtime import log, sanitize_filename, runtime_console_print, write_json_file
from .db import (
    get_public_table_identifier,
    get_cloud_runtime_settings_table_name,
    execute_postgres_fetchone,
    execute_postgres,
    _podcast_load_channel_setting,
    _podcast_save_channel_setting,
)
from .youtube import (
    authenticate_youtube_from_supabase,
    normalize_playlist_privacy_status,
    is_playlist_not_found_http_error,
    build_youtube_traditional_localizations,
    _sync_playlist_localizations_with_client,
    _extract_youtube_video_id,
    _wait_for_live_video_rows_with_client,
    _build_existing_video_match_from_row,
    _apply_video_match_to_split_part,
    _reset_split_part_upload_state,
)
from .cover import (
    _2K_IMAGE_SIZES,
    _map_resolution_to_2k_size,
    _is_nonempty_local_file,
)

# ---------------------------------------------------------------------------
# Podcast 默认配置二次注入（原文件行 8278-8299）
# ---------------------------------------------------------------------------
_PODCAST_RUNTIME_DEFAULTS = {
    "ENABLE_YOUTUBE_PODCAST_RUNTIME": True,
    "ENABLE_YOUTUBE_PODCAST_UNIFIED_SHOW": True,
    "ENABLE_YOUTUBE_PODCAST_SPLIT_PLAYLIST": True,
    "YOUTUBE_PODCAST_SHOW_TITLE_TEMPLATE": "{channel_name}｜长篇有声书全集",
    "YOUTUBE_PODCAST_IMAGE_SIZE": 2048,
    "YOUTUBE_PODCAST_IMAGE_MAX_BYTES": 2097152,
    "YOUTUBE_PODCAST_SHOW_PLAYLIST_SETTING_KEY": "podcast_longform_show_playlist_id",
    "SENSENOVA_BASE_URL": "https://token.sensenova.cn/v1",
    "SENSENOVA_API_KEY": "sk-8Tr86c17YvA5jBEoem2uYYAQGXGzmpDU",
    "YOUTUBE_PODCAST_TEXT_MODEL_PRIMARY": "deepseek-v4-flash",
    "YOUTUBE_PODCAST_TEXT_MODEL_FALLBACK": "sensenova-6.7-flash-lite",
    "YOUTUBE_PODCAST_IMAGE_MODEL_PRIMARY": "sensenova-u1-fast",
    "YOUTUBE_PODCAST_TEXT_MODEL_RETRIES": 2,
    "YOUTUBE_PODCAST_IMAGE_MODEL_RETRIES": 3,
    "YOUTUBE_PODCAST_AI_RETRY_BASE_SECONDS": 30.0,
    "YOUTUBE_PODCAST_YT_RETRIES": 5,
    "YOUTUBE_PODCAST_YT_RETRY_BASE_SECONDS": 3.0,
    "YOUTUBE_PODCAST_FONT_CACHE_DIRNAME": "_podcast_font_cache",
}
cfg.DEFAULT_RUNTIME_CONFIG.update(_PODCAST_RUNTIME_DEFAULTS)
cfg.apply_runtime_config()

_PODCAST_PLAYLIST_IMAGES_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlistImages"
_PODCAST_SHOW_IMAGE_FILENAME = "podcast_longform_show_cover.jpg"
_PODCAST_PLAYLIST_ASSET_DIR = "_podcast_playlist_assets"
_PODCAST_SHOW_ASSET_DIR = "_podcast_show_assets"


# ============================================================================
# Podcast 运行时开关（原文件行 8308-8350）
# ============================================================================
def _podcast_runtime_enabled():
    return bool(getattr(cfg, "ENABLE_YOUTUBE_PODCAST_RUNTIME", False))


def _podcast_unified_show_enabled():
    return bool(getattr(cfg, "ENABLE_YOUTUBE_PODCAST_UNIFIED_SHOW", False))


def _podcast_split_playlist_enabled():
    return bool(getattr(cfg, "ENABLE_YOUTUBE_PODCAST_SPLIT_PLAYLIST", False))


def _podcast_show_setting_key():
    return str(
        getattr(cfg, "YOUTUBE_PODCAST_SHOW_PLAYLIST_SETTING_KEY", "podcast_longform_show_playlist_id") or ""
    ).strip() or "podcast_longform_show_playlist_id"


def _podcast_show_title(channel_name):
    template = str(
        getattr(cfg, "YOUTUBE_PODCAST_SHOW_TITLE_TEMPLATE", "{channel_name}｜长篇有声书全集")
        or "{channel_name}｜长篇有声书全集"
    )
    normalized = str(channel_name or "").strip()
    try:
        return template.format(channel_name=normalized)
    except Exception:
        return f"{normalized}｜长篇有声书全集"


def _podcast_image_size():
    try:
        return max(512, int(getattr(cfg, "YOUTUBE_PODCAST_IMAGE_SIZE", 2048) or 2048))
    except Exception:
        return 2048


def _podcast_image_max_bytes():
    try:
        return max(512000, int(getattr(cfg, "YOUTUBE_PODCAST_IMAGE_MAX_BYTES", 2097152) or 2097152))
    except Exception:
        return 2097152


def _podcast_progress(message):
    log.info("[podcast] %s", str(message or "").strip())


def _podcast_now_iso():
    return dt_module.datetime.now().isoformat()


def _podcast_short(text, limit=72):
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(8, limit - 1)].rstrip() + "…"


def _sanitize_filename_component(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or "item"


# ============================================================================
# Podcast YouTube 工具（原文件行 8433-8577）
# ============================================================================

def _podcast_extract_best_thumbnail_url(thumbnails):
    if not isinstance(thumbnails, dict):
        return ""
    preferred = ["maxres", "standardres", "high", "medium", "default"]
    for key in preferred:
        row = thumbnails.get(key) or {}
        url = str(row.get("url") or "").strip()
        if url:
            return url
    for row in thumbnails.values():
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if url:
            return url
    return ""


def _podcast_normalize_status(value):
    normalized = str(value or "").strip().lower()
    if normalized in {"enabled", "disabled"}:
        return normalized
    return ""


def _podcast_playlist_row_to_record(item):
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    playlist_id = str(item.get("id") or "").strip()
    return {
        "playlist_id": playlist_id,
        "playlist_url": f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else "",
        "title": str(snippet.get("title") or "").strip(),
        "description": str(snippet.get("description") or ""),
        "thumbnail_url": _podcast_extract_best_thumbnail_url(snippet.get("thumbnails") or {}),
        "privacy_status": normalize_playlist_privacy_status(status.get("privacyStatus") or "public"),
        "podcast_status": _podcast_normalize_status(status.get("podcastStatus")),
    }


def _podcast_error_text(error):
    return re.sub(r"\s+", " ", str(error or "")).strip()


def _podcast_extract_http_error_details(error):
    status = int(getattr(getattr(error, "resp", None), "status", 0) or 0)
    reason = ""
    payload_text = ""
    try:
        raw = getattr(error, "content", b"") or b""
        if isinstance(raw, (bytes, bytearray)):
            payload_text = raw.decode("utf-8", errors="ignore")
        else:
            payload_text = str(raw)
        payload = json.loads(payload_text) if payload_text else {}
        items = ((payload.get("error") or {}).get("errors") or []) if isinstance(payload, dict) else []
        if items:
            reason = str((items[0] or {}).get("reason") or "").strip()
    except Exception:
        payload_text = _podcast_error_text(error)
    if not reason:
        reason = _podcast_error_text(error)
    return status, reason, payload_text


def _podcast_is_retryable_text_error(message):
    text = str(message or "").lower()
    return any(
        token in text
        for token in [
            "timeout",
            "timed out",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "connection broken",
            "service unavailable",
            "bad gateway",
            "internal error",
        ]
    )


def _podcast_is_retryable_youtube_http_error(error):
    if not isinstance(error, HttpError):
        return False

    status, reason, payload_text = _podcast_extract_http_error_details(error)
    reason_lower = str(reason or "").lower()
    payload_lower = str(payload_text or "").lower()
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return True

    retryable_reasons = {
        "serviceUnavailable",
        "backendError",
        "internalError",
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "conflict",
    }
    if reason_lower.replace("_", "") in {item.lower().replace("_", "") for item in retryable_reasons}:
        return True
    return "service_unavailable" in payload_lower or "the operation was aborted" in payload_lower


def _podcast_youtube_retry_sleep_seconds(attempt_index):
    base = float(getattr(cfg, "YOUTUBE_PODCAST_YT_RETRY_BASE_SECONDS", 3.0) or 3.0)
    return max(1.0, base * (2 ** max(0, int(attempt_index or 0))))


def _podcast_ai_retry_sleep_seconds(_attempt_index):
    base = float(getattr(cfg, "YOUTUBE_PODCAST_AI_RETRY_BASE_SECONDS", 30.0) or 30.0)
    return max(1.0, base)


def _podcast_execute_youtube_request(request, op_name="youtube request"):
    retries = max(1, int(getattr(cfg, "YOUTUBE_PODCAST_YT_RETRIES", 5) or 5))
    last_error = None
    for attempt_index in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            last_error = e
            if attempt_index >= retries - 1 or not _podcast_is_retryable_youtube_http_error(e):
                raise
            sleep_seconds = _podcast_youtube_retry_sleep_seconds(attempt_index)
            status, reason, _payload = _podcast_extract_http_error_details(e)
            _podcast_progress(
                f"{op_name} hit transient YouTube error status={status} reason={reason or 'unknown'}, "
                f"retrying in {sleep_seconds:.0f}s ({attempt_index + 1}/{retries})"
            )
            time.sleep(sleep_seconds)
        except Exception as e:
            last_error = e
            if attempt_index >= retries - 1 or not _podcast_is_retryable_text_error(e):
                raise
            sleep_seconds = _podcast_youtube_retry_sleep_seconds(attempt_index)
            _podcast_progress(
                f"{op_name} hit transient request error, retrying in {sleep_seconds:.0f}s "
                f"({attempt_index + 1}/{retries}): {_podcast_error_text(e)}"
            )
            time.sleep(sleep_seconds)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{op_name} failed without response")


def _podcast_fetch_playlist_by_id(youtube, playlist_id, retries=6, wait_seconds=1.5):
    normalized = str(playlist_id or "").strip()
    if not normalized:
        return {}

    attempts = max(1, int(retries or 1))
    for attempt_index in range(attempts):
        response = _podcast_execute_youtube_request(
            youtube.playlists().list(part="snippet,status", id=normalized, maxResults=1),
            op_name=f"playlists.list:{normalized}",
        )
        items = response.get("items", [])
        if items:
            return _podcast_playlist_row_to_record(items[0])
        if attempt_index < attempts - 1:
            time.sleep(max(0.1, float(wait_seconds or 0.1)))
    return {}


def _podcast_wait_for_playlist_podcast_status(
    youtube,
    playlist_id,
    desired_status="enabled",
    retries=15,
    wait_seconds=3.0,
):
    normalized = str(playlist_id or "").strip()
    target = _podcast_normalize_status(desired_status)
    if not normalized or not target:
        return {}

    attempts = max(1, int(retries or 1))
    last_seen = {}
    for attempt_index in range(attempts):
        fetched = _podcast_fetch_playlist_by_id(youtube, normalized, retries=1, wait_seconds=0)
        if fetched:
            last_seen = fetched
        if str((last_seen or {}).get("podcast_status") or "").strip().lower() == target:
            return last_seen
        if attempt_index < attempts - 1:
            time.sleep(max(0.1, float(wait_seconds or 0.1)))
    return last_seen


# ============================================================================
# Podcast 版 list_owned / create_or_update / list_items / delete / insert / update
# （原文件行 8624-8818，覆盖 youtube.py 第一版；本模块末尾注入回 youtube.py）
# ============================================================================

def _list_owned_playlists_with_client(youtube):
    playlists = []
    page_token = None
    while True:
        response = _podcast_execute_youtube_request(
            youtube.playlists().list(
                part="snippet,status",
                mine=True,
                maxResults=50,
                pageToken=page_token,
            ),
            op_name="playlists.list:mine",
        )
        for item in response.get("items", []):
            row = _podcast_playlist_row_to_record(item)
            playlists.append(row)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return playlists


def _create_or_update_playlist_with_client(
    youtube,
    title,
    description="",
    privacy_status="public",
    playlist_id="",
    podcast_status=None,
):
    normalized_privacy = normalize_playlist_privacy_status(privacy_status)
    default_language, _generated_localizations = build_youtube_traditional_localizations(
        title=title,
        description=description,
    )
    body = {
        "snippet": {
            "title": str(title or "")[:150],
            "description": str(description or "")[:5000],
            "defaultLanguage": default_language,
        },
        "status": {
            "privacyStatus": normalized_privacy,
        },
    }
    normalized_podcast_status = _podcast_normalize_status(podcast_status)
    if normalized_podcast_status:
        body["status"]["podcastStatus"] = normalized_podcast_status

    if playlist_id:
        body["id"] = playlist_id
        response = _podcast_execute_youtube_request(
            youtube.playlists().update(part="snippet,status", body=body),
            op_name=f"playlists.update:{playlist_id}",
        )
    else:
        response = _podcast_execute_youtube_request(
            youtube.playlists().insert(part="snippet,status", body=body),
            op_name=f"playlists.insert:{_podcast_short(title, 48)}",
        )

    final_playlist_id = str(response.get("id") or "").strip()
    localization_sync = _sync_playlist_localizations_with_client(
        youtube,
        final_playlist_id,
        title=body["snippet"]["title"],
        description=body["snippet"]["description"],
        force_overwrite=False,
    )
    if localization_sync.get("failed_locales"):
        log.warning(
            "Playlist localization sync partially failed for %s; continuing playlist success path. failed=%s",
            final_playlist_id,
            json.dumps(localization_sync.get("failed_locales", {}), ensure_ascii=False),
        )

    fetched = _podcast_fetch_playlist_by_id(youtube, final_playlist_id, retries=8, wait_seconds=1.5) if final_playlist_id else {}
    result = {
        "playlist_id": final_playlist_id,
        "playlist_url": f"https://www.youtube.com/playlist?list={final_playlist_id}" if final_playlist_id else "",
        "title": body["snippet"]["title"],
        "description": body["snippet"]["description"],
        "privacy_status": normalized_privacy,
        "localizations_applied": localization_sync.get("applied_locales", []),
        "localizations_failed": localization_sync.get("failed_locales", {}),
        "podcast_status": normalized_podcast_status,
    }
    if fetched:
        result.update(
            {
                "thumbnail_url": fetched.get("thumbnail_url", ""),
                "podcast_status": fetched.get("podcast_status", result.get("podcast_status", "")),
            }
        )
    return result


def _list_playlist_items_with_client(youtube, playlist_id):
    items = []
    page_token = None
    playlist_not_found_retry_count = 0
    max_playlist_not_found_retries = 6
    while True:
        try:
            response = _podcast_execute_youtube_request(
                youtube.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ),
                op_name=f"playlistItems.list:{playlist_id}",
            )
        except HttpError as e:
            if is_playlist_not_found_http_error(e) and playlist_not_found_retry_count < max_playlist_not_found_retries:
                playlist_not_found_retry_count += 1
                wait_seconds = min(12, 2 + playlist_not_found_retry_count)
                log.warning(
                    "播放列表 %s 暂时还不可读，等待 %d 秒后重试读取（%d/%d）...",
                    playlist_id,
                    wait_seconds,
                    playlist_not_found_retry_count,
                    max_playlist_not_found_retries,
                )
                time.sleep(wait_seconds)
                page_token = None
                continue
            raise

        for item in response.get("items", []):
            snippet = item.get("snippet") or {}
            content_details = item.get("contentDetails") or {}
            resource_id = snippet.get("resourceId") or {}
            video_id = str(resource_id.get("videoId") or content_details.get("videoId") or "").strip()
            items.append(
                {
                    "playlist_item_id": str(item.get("id") or "").strip(),
                    "video_id": video_id,
                    "position": int(snippet.get("position") or 0),
                    "title": str(snippet.get("title") or "").strip(),
                }
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def _delete_playlist_item_with_client(youtube, playlist_item_id):
    _podcast_execute_youtube_request(
        youtube.playlistItems().delete(id=playlist_item_id),
        op_name=f"playlistItems.delete:{playlist_item_id}",
    )


def _insert_playlist_video_with_client(youtube, playlist_id, video_id):
    response = _podcast_execute_youtube_request(
        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id,
                    },
                }
            },
        ),
        op_name=f"playlistItems.insert:{playlist_id}:{video_id}",
    )
    return {
        "playlist_item_id": str(response.get("id") or "").strip(),
        "video_id": str(video_id or "").strip(),
    }


def _update_playlist_item_position_with_client(youtube, playlist_item_id, playlist_id, video_id, position):
    _podcast_execute_youtube_request(
        youtube.playlistItems().update(
            part="snippet",
            body={
                "id": playlist_item_id,
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id,
                    },
                    "position": int(position),
                },
            },
        ),
        op_name=f"playlistItems.update:{playlist_id}:{video_id}:{position}",
    )


# ============================================================================
# Sensenova 封面回退（原文件 cover 段，行 4364-4542，这里放 podcast.py 内联）
# 依赖 _podcast_* 辅助函数，自然归属 podcast.py。
# 同时 cover.py 中的 _dispatch_cover_text / _dispatch_cover_image 通过 lazy import 调用。
# ============================================================================

def _podcast_create_sensenova_client():
    return OpenAI(
        base_url=str(getattr(cfg, "SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1") or "").strip(),
        api_key=str(getattr(cfg, "SENSENOVA_API_KEY", "") or "").strip(),
    )


def _podcast_extract_chat_text(response):
    try:
        return str(response.choices[0].message.content or "").strip()
    except Exception:
        return ""


def _podcast_is_rate_limited_error(error):
    text = _podcast_error_text(error).lower()
    return any(
        token in text
        for token in [
            "429",
            "rate limit",
            "too many requests",
            "quota",
            "exceeded",
            "rate_limit",
            "call limit",
        ]
    )


def _podcast_is_security_rejection_error(error):
    text = _podcast_error_text(error).lower()
    return (
        "security reasons" in text
        or ("invalid_request_error" in text and "'code': '18'" in text)
        or ('"code": "18"' in text)
        or ('"code":18' in text)
    )


def _podcast_is_retryable_ai_error(error):
    text = _podcast_error_text(error).lower()
    if _podcast_is_rate_limited_error(text):
        return True
    return any(
        token in text
        for token in [
            "timeout",
            "timed out",
            "connection",
            "temporarily",
            "temporarily unavailable",
            "server error",
            "service unavailable",
            "bad gateway",
            "502",
            "503",
            "504",
            "internal error",
            "overloaded",
        ]
    )


def _podcast_chat_complete_with_model(client, model, prompt, system_prompt="You are a helpful assistant."):
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    )
    return _podcast_extract_chat_text(response)


def _podcast_generate_text_via_models(prompt, purpose, fallback_text=""):
    api_key = str(getattr(cfg, "SENSENOVA_API_KEY", "") or "").strip()
    if not api_key:
        return {
            "text": fallback_text,
            "model": "fallback",
            "error": "SENSENOVA_API_KEY is empty",
        }

    client = _podcast_create_sensenova_client()
    attempts_log = []
    models = [
        str(getattr(cfg, "YOUTUBE_PODCAST_TEXT_MODEL_PRIMARY", "deepseek-v4-flash") or "deepseek-v4-flash").strip(),
        str(
            getattr(cfg, "YOUTUBE_PODCAST_TEXT_MODEL_FALLBACK", "sensenova-6.7-flash-lite")
            or "sensenova-6.7-flash-lite"
        ).strip(),
    ]
    retries = max(1, int(getattr(cfg, "YOUTUBE_PODCAST_TEXT_MODEL_RETRIES", 2) or 2))

    for model_index, model in enumerate(models):
        if not model:
            continue
        for attempt_index in range(retries):
            try:
                if attempt_index > 0 or model_index > 0:
                    _podcast_progress(
                        f"{purpose}: trying text model {model} (attempt {attempt_index + 1}/{retries})"
                    )
                text = _podcast_chat_complete_with_model(client, model, prompt)
                if text:
                    return {
                        "text": text,
                        "model": model,
                        "error": " ; ".join(attempts_log),
                    }
                attempts_log.append(f"{model} attempt {attempt_index + 1}: empty response")
            except Exception as e:
                err = _podcast_error_text(e)
                attempts_log.append(f"{model} attempt {attempt_index + 1}: {err}")
                if _podcast_is_rate_limited_error(err) and model_index == 0:
                    _podcast_progress(
                        f"{purpose}: {model} hit rate limit, switching to {models[-1] or 'fallback'}"
                    )
                    break
                if _podcast_is_retryable_ai_error(err) and attempt_index < retries - 1:
                    sleep_seconds = _podcast_ai_retry_sleep_seconds(attempt_index)
                    _podcast_progress(f"{purpose}: {model} retrying in {sleep_seconds:.0f}s")
                    time.sleep(sleep_seconds)
                    continue
                break

    return {
        "text": fallback_text,
        "model": "fallback",
        "error": " ; ".join(attempts_log) or "text generation failed",
    }


# ---- Sensenova cover 回退 ----

def _sensenova_generate_cover_fallback(output_path, draw_prompt, resolution="1080p"):
    """当 ModelScope 所有 token 生图触发 429 限流时，使用 Sensenova (Podcast AI) 作为最终回退方案。"""
    width, height = _map_resolution_to_2k_size(resolution)
    size_str = f"{width}x{height}"

    client = OpenAI(
        base_url=str(getattr(cfg, "SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1") or "").strip(),
        api_key=str(getattr(cfg, "SENSENOVA_API_KEY", "") or "").strip(),
    )
    model_name = str(
        getattr(cfg, "YOUTUBE_PODCAST_IMAGE_MODEL_PRIMARY", "sensenova-u1-fast") or "sensenova-u1-fast"
    ).strip()
    retries = max(1, int(getattr(cfg, "YOUTUBE_PODCAST_IMAGE_MODEL_RETRIES", 3) or 3))

    target_size_map = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440), "4k": (3840, 2160)}
    target_res = target_size_map.get(str(resolution).lower(), (1920, 1080))

    attempts_log = []
    for attempt_index in range(retries):
        try:
            log.info(
                "🔄 [Sensenova Fallback] 使用 %s 生成 2K 封面图 (%s)...",
                model_name,
                size_str,
            )
            response = client.images.generate(
                model=model_name,
                prompt=draw_prompt,
                size=size_str,
                n=1,
            )
            image_url = str(response.data[0].url or "").strip()
            if not image_url:
                raise ValueError("Sensenova 图片接口未返回可下载的 URL。")

            tmp_path = output_path + ".sensenova_tmp"
            if not download_file_from_url(image_url, tmp_path):
                raise ValueError("Sensenova 图片下载失败。")

            try:
                with PILImage.open(tmp_path) as img:
                    tw, th = target_res
                    src_w, src_h = img.size
                    src_ratio = src_w / src_h
                    target_ratio = tw / th

                    if src_ratio > target_ratio:
                        new_w = int(src_h * target_ratio)
                        offset = (src_w - new_w) // 2
                        crop = img.crop((offset, 0, offset + new_w, src_h))
                    else:
                        new_h = int(src_w / target_ratio)
                        offset = (src_h - new_h) // 2
                        crop = img.crop((0, offset, src_w, offset + new_h))

                    resized = crop.resize(target_res, PILImage.LANCZOS)
                    resized.convert("RGB").save(output_path, format="JPEG", quality=85)
                    log.info("✅ Sensenova 原始图片已压缩为 %dx%d JPEG（quality=85）", tw, th)
            except Exception as pil_err:
                log.warning("⚠️ Sensenova 图片 PIL 压缩失败，回退使用原始文件: %s", pil_err)
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, output_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                log.info(
                    "🎉 [Sensenova Fallback] 封面图生成成功：%s (原始尺寸: %s, 约 %.1f MB)",
                    os.path.basename(output_path),
                    size_str,
                    os.path.getsize(output_path) / 1024 / 1024,
                )
                return True

            raise ValueError("Sensenova 生成的文件为空。")
        except Exception as e:
            err_text = _podcast_error_text(e)
            attempts_log.append(f"attempt {attempt_index + 1}: {err_text}")
            log.warning(
                "⚠️ [Sensenova Fallback] 第 %d/%d 次失败：%s",
                attempt_index + 1,
                retries,
                err_text,
            )
            if attempt_index < retries - 1:
                sleep_sec = _podcast_ai_retry_sleep_seconds(attempt_index)
                time.sleep(sleep_sec)

    log.error(
        "❌ [Sensenova Fallback] 全部 %d 次重试均失败。错误：%s",
        retries,
        " ; ".join(attempts_log),
    )
    return False


def _call_sensenova_for_draw_prompt(book_name, book_desc):
    """当 ModelScope 所有文本 token 触发 429 限流时，使用 Sensenova (Podcast AI) 生成封面绘图提示词。"""
    client = OpenAI(
        base_url=str(getattr(cfg, "SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1") or "").strip(),
        api_key=str(getattr(cfg, "SENSENOVA_API_KEY", "") or "").strip(),
    )
    model_name = str(
        getattr(cfg, "YOUTUBE_PODCAST_TEXT_MODEL_PRIMARY", "qwen-plus") or "qwen-plus"
    ).strip()
    retries = max(1, int(getattr(cfg, "YOUTUBE_PODCAST_TEXT_MODEL_RETRIES", 3) or 3))

    system_prompt = """角色设定：你是一位顶级 YouTube 封面设计师和 AI 绘图提示词专家。你的任务是根据我提供的书名和简介，输出一段可直接用于高质量文生图模型的英文提示词。

设计原则：
1. 主体必须直接体现书的内容和情绪，适合 YouTube thumbnail 的高点击构图。
2. 书名对应的中文大字必须作为画面的核心视觉元素，要求醒目、可读、对比强烈。
3. 允许补充一个极短的中文副标题增强点击欲。
4. 输出必须强调高对比、高饱和、戏剧光影、电影感和 16:9 横版构图。

最后约束：
1. 只输出纯英文提示词本身，必须去掉行首的 "Prompt:"、"prompt:" 等前缀。
2. 不要输出任何多余的汉字解释、不需要英文引导词、不需要 Markdown 块标记。
3. 提示词长度请控制在 60-120 个英文单词之间。"""

    user_prompt = f"书名：[{book_name}]\n简介：[{book_desc}]"

    for attempt_index in range(retries):
        try:
            log.info(
                "🔄 [Sensenova Fallback Text] 使用 %s 生成封面绘图提示词 (第 %d/%d 次)...",
                model_name,
                attempt_index + 1,
                retries,
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            result = _podcast_extract_chat_text(response)
            if result:
                log.info("✅ [Sensenova Fallback Text] 封面绘图提示词生成成功。")
                return result
            raise ValueError("Sensenova 返回的文本内容为空。")
        except Exception as e:
            err_text = _podcast_error_text(e)
            log.warning(
                "⚠️ [Sensenova Fallback Text] 第 %d/%d 次失败：%s",
                attempt_index + 1,
                retries,
                err_text,
            )
            if attempt_index < retries - 1:
                sleep_sec = _podcast_ai_retry_sleep_seconds(attempt_index)
                time.sleep(sleep_sec)

    log.error("❌ [Sensenova Fallback Text] 全部 %d 次重试均失败。", retries)
    return ""


# ---- 图片 URL 下载（被 _sensenova_generate_cover_fallback 使用）----

def download_file_from_url(url, save_path, retries=3):
    """从 URL 下载文件到本地路径（用于 Sensenova fallback 图片下载）。"""
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        return True

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=180, stream=True)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    f.write(chunk)
            if os.path.getsize(save_path) > 0:
                return True
        except Exception as e:
            wait = 2 ** attempt
            log.warning("图片下载失败（第%d次）: %s", attempt + 1, e)
            time.sleep(min(wait, 30))
    return False


PILImage = Image  # alias for local use in _sensenova_generate_cover_fallback


# ============================================================================
# Podcast Show 封面生成（原文件行 9119-9581）
# ============================================================================

def _podcast_build_default_show_description(channel_name, episode_count):
    return (
        f"这里是 {channel_name} 的长篇有声书全集。我们将频道内适合完整收听的长篇有声内容整理为统一书库，"
        f"每本完整书作为一个 episode，便于连续播放、长期收藏与慢慢聆听。当前已整理 {episode_count} 本完整书，后续也会持续更新。"
    )[:5000]


def _podcast_generate_show_description(channel_name, show_title, episode_titles):
    fallback = _podcast_build_default_show_description(channel_name, len(episode_titles))
    sampled_titles = [str(item or "").strip()[:80] for item in episode_titles[:12] if str(item or "").strip()]
    titles_block = "\n".join(f"- {item}" for item in sampled_titles) or "- 暂无 episode 标题样例"
    prompt = f"""
你现在要为一个 YouTube podcast show 撰写中文简介。

频道名：{channel_name}
Show 标题：{show_title}
Episode 标题样例：
{titles_block}

要求：
1. 直接输出 120 到 220 字左右的中文简介正文。
2. 强调"每本完整书 = 一个 episode""适合连续收听""长期更新的长篇有声书书库"。
3. 风格自然、可信、适合 YouTube podcast show，不要列表，不要 emoji，不要引号，不要口号式空话。
4. 不要输出标题，只输出简介正文。
""".strip()
    result = _podcast_generate_text_via_models(prompt, purpose="show description", fallback_text=fallback)
    text = str(result.get("text") or "").strip()
    if text and str(result.get("model") or "") != "fallback":
        return {
            "description": text[:5000],
            "source": f"ai:{result['model']}",
            "error": str(result.get("error") or ""),
        }
    return {
        "description": fallback,
        "source": "fallback",
        "error": str(result.get("error") or "AI 返回空描述"),
    }


def _podcast_build_default_cover_prompt(channel_name, _show_title):
    return (
        "YouTube podcast cover, square 1:1 composition, premium Chinese audiobook brand identity, "
        "ancient Chinese books and bamboo scrolls arranged in a cinematic library scene, warm golden light, "
        "deep red and dark wood palette, elegant but high-contrast layout, bold readable Chinese title text "
        f'"{channel_name}" with subtitle "长篇有声书全集", luxury editorial style, clean center composition, highly detailed, 2048x2048'
    )


def _podcast_generate_show_cover_prompt(channel_name, show_title, episode_titles):
    fallback = _podcast_build_default_cover_prompt(channel_name, show_title)
    sampled_titles = [str(item or "").strip()[:60] for item in episode_titles[:8] if str(item or "").strip()]
    titles_block = "\n".join(f"- {item}" for item in sampled_titles) or "- long-form Chinese audiobooks"
    prompt = f"""
Write one single English image prompt for a YouTube podcast cover.

Channel name: {channel_name}
Show title: {show_title}
Episode samples:
{titles_block}

Requirements:
1. Square 1:1 cover for a podcast show, not 16:9 thumbnail.
2. Chinese long-form audiobook atmosphere.
3. Must emphasize premium readability and visible Chinese typography for the channel name and 长篇有声书全集.
4. No markdown, no explanation, output one prompt only.
5. Mention 2048x2048.
""".strip()
    result = _podcast_generate_text_via_models(prompt, purpose="show cover prompt", fallback_text=fallback)
    text = str(result.get("text") or "").strip()
    if text and str(result.get("model") or "") != "fallback":
        return {
            "prompt": text,
            "source": f"ai:{result['model']}",
            "error": str(result.get("error") or ""),
        }
    return {
        "prompt": fallback,
        "source": "fallback",
        "error": str(result.get("error") or "AI 返回空封面 prompt"),
    }


def _podcast_build_batch_playlist_cover_prompt_fallback(playlist_title, playlist_description):
    short_desc = str(playlist_description or "").strip().replace("\n", " ")[:240]
    return (
        "YouTube podcast cover, square 1:1 composition, premium Chinese audiobook or knowledge playlist visual identity, "
        f'bold readable Chinese title text "{playlist_title}" as the main focus, elegant cinematic layout, warm cinematic lighting, '
        f"rich dark red and gold palette, bookshelf and scroll atmosphere, high contrast, highly detailed, 2048x2048. "
        f"Context: {short_desc}"
    )


def _podcast_generate_batch_playlist_cover_prompt(playlist_title, playlist_description):
    fallback = _podcast_build_batch_playlist_cover_prompt_fallback(playlist_title, playlist_description)
    prompt = f"""
Write one single English image prompt for a square YouTube podcast cover.

Playlist title: {playlist_title}
Playlist description: {str(playlist_description or '').strip()[:800]}

Requirements:
1. Square 1:1 cover for a podcast playlist, not a 16:9 thumbnail.
2. Keep the playlist title as the main visible Chinese typography element.
3. Style should fit Chinese long-form audio, audiobooks, lectures, or serialized knowledge content.
4. Strong readability, premium editorial design, highly detailed.
5. Output one prompt only, no explanation, mention 2048x2048.
""".strip()
    result = _podcast_generate_text_via_models(prompt, purpose="playlist cover prompt", fallback_text=fallback)
    text = str(result.get("text") or "").strip()
    if text and str(result.get("model") or "") != "fallback":
        return {
            "prompt": text,
            "source": f"ai:{result['model']}",
            "error": str(result.get("error") or ""),
        }
    return {
        "prompt": fallback,
        "source": "fallback",
        "error": str(result.get("error") or "AI 返回空封面 prompt"),
    }


def _podcast_download_bytes(url):
    response = requests.get(url, timeout=180)
    response.raise_for_status()
    return response.content


def _podcast_save_square_cover_image(image_bytes, output_path, max_bytes=None):
    max_bytes = int(max_bytes or _podcast_image_max_bytes())
    os_path = Path(output_path)
    os_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(BytesIO(image_bytes)) as img:
        base = ImageOps.fit(
            img.convert("RGB"),
            (_podcast_image_size(), _podcast_image_size()),
            method=Image.Resampling.LANCZOS,
        )
        for quality in [92, 88, 84, 80, 76, 72, 68, 64, 60]:
            base.save(os_path, format="JPEG", quality=quality, optimize=True, progressive=True)
            if os_path.stat().st_size <= max_bytes:
                return str(os_path)
    raise RuntimeError(f"生成的 podcast cover 超过 2MB 限制：{output_path}")


def _font_cache_dir():
    cache_dir = Path.cwd() / str(getattr(cfg, "YOUTUBE_PODCAST_FONT_CACHE_DIRNAME", "_podcast_font_cache") or "_podcast_font_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _download_font_if_missing(url, target_path):
    try:
        if target_path.exists() and target_path.stat().st_size > 1024 * 1024:
            return target_path
        _podcast_progress(f"Downloading fallback Chinese font: {target_path.name}")
        response = requests.get(url, timeout=180)
        response.raise_for_status()
        target_path.write_bytes(response.content)
        if target_path.exists() and target_path.stat().st_size > 1024 * 1024:
            return target_path
    except Exception as e:
        _podcast_progress(f"Font download skipped: {_podcast_error_text(e)}")
    return None


def _resolve_cover_font_path(prefer_bold=True):
    local_candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if prefer_bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if prefer_bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf" if prefer_bold else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc" if prefer_bold else "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in local_candidates:
        path = Path(candidate)
        if path.exists():
            return path

    cache_dir = _font_cache_dir()
    remote_candidates = [
        (
            "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Bold.otf",
            cache_dir / "NotoSansCJKsc-Bold.otf",
        )
        if prefer_bold
        else (
            "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
            cache_dir / "NotoSansCJKsc-Regular.otf",
        ),
        (
            "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
            cache_dir / "NotoSansCJKsc-Regular.otf",
        ),
    ]
    for url, path in remote_candidates:
        resolved = _download_font_if_missing(url, path)
        if resolved is not None:
            return resolved
    return None


def _pick_local_cover_font(size, prefer_bold=True):
    resolved_path = _resolve_cover_font_path(prefer_bold=prefer_bold)
    if resolved_path is not None:
        try:
            return ImageFont.truetype(str(resolved_path), size=size)
        except Exception as e:
            _podcast_progress(f"Font load fallback triggered: {_podcast_error_text(e)}")
    return ImageFont.load_default()


def _measure_text(draw, text, font):
    if not text:
        return (0, 0)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return (max(0, right - left), max(0, bottom - top))


def _draw_vertical_gradient(size, top_rgb, bottom_rgb):
    gradient = Image.new("RGB", (1, size))
    pixels = gradient.load()
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(int(top_rgb[i] * (1.0 - ratio) + bottom_rgb[i] * ratio) for i in range(3))
        pixels[0, y] = color
    return gradient.resize((size, size))


def _wrap_text_to_width(draw, text, font, max_width, max_lines):
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if not cleaned:
        return []

    lines = []
    current = ""
    for ch in cleaned:
        candidate = current + ch
        width, _height = _measure_text(draw, candidate, font)
        if current and width > max_width:
            lines.append(current)
            current = ch
            if len(lines) >= max_lines - 1:
                break
        else:
            current = candidate

    remaining = cleaned[len("".join(lines)) :].strip()
    if current and len(lines) < max_lines:
        remaining = current + remaining[len(current) :]
    if remaining and len(lines) < max_lines:
        while remaining:
            candidate = ""
            for ch in remaining:
                probe = candidate + ch
                width, _height = _measure_text(draw, probe, font)
                if candidate and width > max_width:
                    break
                candidate = probe
            if not candidate:
                break
            lines.append(candidate)
            remaining = remaining[len(candidate) :].strip()
            if len(lines) >= max_lines:
                break
    if remaining and lines:
        lines[-1] = lines[-1].rstrip("，。、；：,. ") + "…"
    return [line for line in lines if line]


def _podcast_generate_local_text_gradient_cover(output_path, cover_title, cover_subtitle="", max_bytes=None):
    size = _podcast_image_size()
    max_bytes = int(max_bytes or _podcast_image_max_bytes())
    canvas = _draw_vertical_gradient(size, (16, 28, 54), (112, 48, 20)).convert("RGBA")
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    draw.rounded_rectangle((96, 96, size - 96, size - 96), radius=72, outline=(242, 211, 148, 255), width=5)
    draw.rounded_rectangle((180, 260, size - 180, size - 320), radius=56, fill=(20, 16, 26, 118))
    draw.ellipse((size - 700, 150, size - 210, 640), fill=(255, 214, 120, 255))
    draw.ellipse((size - 660, 190, size - 250, 600), fill=(250, 193, 88, 255))
    draw.rectangle((180, size - 315, size - 180, size - 286), fill=(224, 186, 104, 230))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    normalized_title = re.sub(r"\s+", " ", str(cover_title or "").strip()) or "Podcast"
    max_text_width = size - 520
    title_lines = []
    title_font = None
    for font_size in [320, 300, 280, 260, 240, 220, 200, 180, 168]:
        font = _pick_local_cover_font(font_size, prefer_bold=True)
        wrapped = _wrap_text_to_width(draw, normalized_title, font, max_text_width, max_lines=3)
        if wrapped:
            title_lines = wrapped
            title_font = font
        if wrapped and len(wrapped) <= 3:
            break
    if title_font is None:
        title_font = _pick_local_cover_font(180, prefer_bold=True)
        title_lines = [normalized_title[:18] or "Podcast"]

    subtitle_text = str(cover_subtitle or "").strip()
    subtitle_font = _pick_local_cover_font(112, prefer_bold=False)
    title_line_height = max(_measure_text(draw, "国", title_font)[1], 140)
    subtitle_line_height = max(_measure_text(draw, "国", subtitle_font)[1], 86)
    title_block_height = len(title_lines) * title_line_height + max(0, len(title_lines) - 1) * 26
    subtitle_block_height = subtitle_line_height if subtitle_text else 0
    total_height = title_block_height + subtitle_block_height + (54 if subtitle_text else 0)
    start_y = max(430, int((size - total_height) * 0.52))

    y = start_y
    for line in title_lines:
        width, _height = _measure_text(draw, line, title_font)
        x = int((size - width) / 2)
        draw.text((x + 8, y + 10), line, font=title_font, fill=(10, 8, 12, 235))
        draw.text((x, y), line, font=title_font, fill=(252, 239, 208, 255))
        y += title_line_height + 26

    if subtitle_text:
        width, _height = _measure_text(draw, subtitle_text, subtitle_font)
        x = int((size - width) / 2)
        draw.text((x + 5, y + 6), subtitle_text, font=subtitle_font, fill=(10, 8, 12, 220))
        draw.text((x, y), subtitle_text, font=subtitle_font, fill=(255, 246, 228, 255))

    canvas = canvas.convert("RGB")
    temp = BytesIO()
    for quality in [92, 88, 84, 80, 76, 72, 68, 64]:
        temp.seek(0)
        temp.truncate(0)
        canvas.save(temp, format="JPEG", quality=quality, optimize=True, progressive=True)
        if temp.tell() <= max_bytes:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(temp.getvalue())
            return str(output_path)
    raise RuntimeError(f"本地方图封面超过 2MB 限制：{output_path}")


def _podcast_build_safe_retry_cover_prompt(cover_title, cover_subtitle=""):
    safe_title = str(cover_title or "知识播客").strip()[:80]
    safe_subtitle = str(cover_subtitle or "长篇有声书").strip()[:40]
    return (
        "Safe family-friendly YouTube podcast cover, square 1:1 composition, neutral library atmosphere, elegant bookshelf background, "
        f"soft golden light, clean editorial typography, large readable Chinese title text \"{safe_title}\", subtitle \"{safe_subtitle}\", "
        "non-violent, non-political, no blood, no weapons, no disturbing scene, premium but calm, 2048x2048"
    )


def _podcast_generate_cover_from_existing_thumbnail(thumbnail_url, output_path):
    normalized = str(thumbnail_url or "").strip()
    if not normalized:
        raise RuntimeError("playlist thumbnail URL 为空，无法裁剪生成 podcast 封面。")
    image_bytes = _podcast_download_bytes(normalized)
    return _podcast_save_square_cover_image(image_bytes, output_path)


def _podcast_generate_cover_from_local_image(image_path, output_path):
    normalized = str(image_path or "").strip()
    if not normalized or not _is_nonempty_local_file(normalized):
        raise RuntimeError("local cover image is not available")
    return _podcast_save_square_cover_image(Path(normalized).read_bytes(), output_path)


def _podcast_log_image_source(scope_label, image_result):
    source = str((image_result or {}).get("source") or "").strip()
    if not source:
        return
    if source.startswith("ai:"):
        _podcast_progress(f"{scope_label}: ai image used ({source})")
    elif source == "playlist_thumbnail_crop_fallback":
        _podcast_progress(f"{scope_label}: playlist thumbnail crop fallback")
    elif source == "local_cover_crop_fallback":
        _podcast_progress(f"{scope_label}: local cover crop fallback")
    elif source == "local_text_gradient_fallback":
        _podcast_progress(f"{scope_label}: local text gradient fallback")
    else:
        _podcast_progress(f"{scope_label}: image source {source}")


def _podcast_generate_named_cover_image(
    filename,
    cover_prompt,
    subdir=_PODCAST_PLAYLIST_ASSET_DIR,
    cover_title="",
    cover_subtitle="",
    thumbnail_fallback_url="",
    local_fallback_image_path="",
):
    output_dir = Path.cwd() / str(subdir or _PODCAST_PLAYLIST_ASSET_DIR)
    output_path = str(output_dir / _sanitize_filename_component(filename))
    client = _podcast_create_sensenova_client()
    prompt_in_use = str(cover_prompt or "").strip()
    attempts_log = []
    retries = max(1, int(getattr(cfg, "YOUTUBE_PODCAST_IMAGE_MODEL_RETRIES", 3) or 3))
    model_name = str(getattr(cfg, "YOUTUBE_PODCAST_IMAGE_MODEL_PRIMARY", "sensenova-u1-fast") or "sensenova-u1-fast").strip()

    for attempt_index in range(retries):
        try:
            response = client.images.generate(
                model=model_name,
                prompt=prompt_in_use,
                size=f"{_podcast_image_size()}x{_podcast_image_size()}",
                n=1,
            )
            image_url = str(response.data[0].url or "").strip()
            if not image_url:
                raise RuntimeError("图片接口没有返回可下载的 URL。")
            image_bytes = _podcast_download_bytes(image_url)
            final_path = _podcast_save_square_cover_image(image_bytes, output_path)
            return {
                "path": final_path,
                "url": image_url,
                "source": f"ai:{model_name}",
                "error": " ; ".join(attempts_log),
            }
        except Exception as e:
            err = _podcast_error_text(e)
            attempts_log.append(f"{model_name} attempt {attempt_index + 1}: {err}")
            if _podcast_is_security_rejection_error(err):
                prompt_in_use = _podcast_build_safe_retry_cover_prompt(cover_title or filename, cover_subtitle)
                _podcast_progress(
                    f"Image generation hit security filter, retrying with a safer prompt ({attempt_index + 1}/{retries})"
                )
            if attempt_index < retries - 1:
                sleep_seconds = _podcast_ai_retry_sleep_seconds(attempt_index)
                _podcast_progress(f"Image generation retrying in {sleep_seconds:.0f}s")
                time.sleep(sleep_seconds)
                continue

    if str(thumbnail_fallback_url or "").strip():
        _podcast_progress("AI image generation exhausted retries, switching to original playlist thumbnail crop")
        thumbnail_path = _podcast_generate_cover_from_existing_thumbnail(thumbnail_fallback_url, output_path)
        return {
            "path": thumbnail_path,
            "url": str(thumbnail_fallback_url or "").strip(),
            "source": "playlist_thumbnail_crop_fallback",
            "error": " ; ".join(attempts_log),
        }

    if _is_nonempty_local_file(local_fallback_image_path):
        _podcast_progress("AI image generation exhausted retries, switching to local cover crop")
        local_path = _podcast_generate_cover_from_local_image(local_fallback_image_path, output_path)
        return {
            "path": local_path,
            "url": "",
            "source": "local_cover_crop_fallback",
            "error": " ; ".join(attempts_log),
        }

    _podcast_progress("AI image generation exhausted retries, switching to local text/gradient cover")
    fallback_path = _podcast_generate_local_text_gradient_cover(
        output_path,
        cover_title=cover_title or filename,
        cover_subtitle=cover_subtitle,
    )
    return {
        "path": fallback_path,
        "url": "",
        "source": "local_text_gradient_fallback",
        "error": " ; ".join(attempts_log),
    }


def _podcast_generate_show_cover_image(channel_name, _show_title, cover_prompt, thumbnail_fallback_url="", local_fallback_image_path=""):
    return _podcast_generate_named_cover_image(
        _PODCAST_SHOW_IMAGE_FILENAME,
        cover_prompt,
        subdir=_PODCAST_SHOW_ASSET_DIR,
        cover_title=channel_name,
        cover_subtitle="长篇有声书全集",
        thumbnail_fallback_url=thumbnail_fallback_url,
        local_fallback_image_path=local_fallback_image_path,
    )


# ============================================================================
# Podcast 播放列表操作（原文件行 9596-9862 / 9865-9956）
# ============================================================================

def _podcast_create_plain_playlist(youtube, title, description, enable_podcast=False):
    body = {
        "snippet": {
            "title": str(title or "")[:150],
            "description": str(description or "")[:5000],
        },
        "status": {
            "privacyStatus": "public",
        },
    }
    if enable_podcast:
        body["status"]["podcastStatus"] = "enabled"

    response = _podcast_execute_youtube_request(
        youtube.playlists().insert(part="snippet,status", body=body),
        op_name=f"playlists.insert:{_podcast_short(title, 48)}",
    )
    created = _podcast_playlist_row_to_record(response)
    if created.get("playlist_id"):
        fetched = _podcast_fetch_playlist_by_id(youtube, created["playlist_id"], retries=8, wait_seconds=1.5)
        return fetched or created
    return created


def _podcast_update_playlist(youtube, playlist_id, title, description, privacy_status="public", enable_podcast=True):
    normalized_privacy = normalize_playlist_privacy_status(privacy_status)
    body = {
        "id": playlist_id,
        "snippet": {
            "title": str(title or "")[:150],
            "description": str(description or "")[:5000],
        },
        "status": {
            "privacyStatus": normalized_privacy,
        },
    }
    if enable_podcast:
        body["status"]["podcastStatus"] = "enabled"

    response = _podcast_execute_youtube_request(
        youtube.playlists().update(part="snippet,status", body=body),
        op_name=f"playlists.update:{playlist_id}",
    )
    updated = _podcast_playlist_row_to_record(response)
    if enable_podcast:
        confirmed = _podcast_wait_for_playlist_podcast_status(
            youtube,
            playlist_id,
            desired_status="enabled",
            retries=15,
            wait_seconds=3.0,
        )
        if str((confirmed or {}).get("podcast_status") or "") == "enabled":
            return confirmed
        merged = {**updated, **confirmed}
        merged["podcast_status"] = "enabled"
        merged["podcast_status_pending"] = True
        return merged
    fetched = _podcast_fetch_playlist_by_id(youtube, playlist_id, retries=6, wait_seconds=1.0)
    return fetched or updated


def _podcast_resolve_existing_show_playlist(youtube, channel_name, title):
    saved_playlist_id = _podcast_load_channel_setting(channel_name, _podcast_show_setting_key())
    if saved_playlist_id:
        playlist = _podcast_fetch_playlist_by_id(youtube, saved_playlist_id)
        if playlist:
            playlist["source"] = "channel_runtime_settings"
            return playlist

    for playlist in _list_owned_playlists_with_client(youtube):
        if str(playlist.get("title") or "").strip() == str(title or "").strip():
            playlist["source"] = "title_match"
            return playlist
    return {}


def _podcast_ensure_video_in_playlist(youtube, playlist_id, video_id):
    normalized_video_id = _extract_youtube_video_id(video_id)
    if not normalized_video_id:
        raise RuntimeError("missing video_id for unified podcast show sync")

    current_items = _list_playlist_items_with_client(youtube, playlist_id)
    matches = [item for item in current_items if str(item.get("video_id") or "").strip() == normalized_video_id]
    if matches:
        ordered = sorted(matches, key=lambda item: int(item.get("position") or 0))
        for item in ordered[1:]:
            if item.get("playlist_item_id"):
                _delete_playlist_item_with_client(youtube, item["playlist_item_id"])
        return {
            "inserted": False,
            "already_present": True,
            "playlist_item_id": ordered[0].get("playlist_item_id", ""),
        }

    insert_result = _insert_playlist_video_with_client(youtube, playlist_id, normalized_video_id)
    return {
        "inserted": True,
        "already_present": False,
        "playlist_item_id": str(insert_result.get("playlist_item_id") or "").strip(),
    }


def _podcast_get_show_state_container(state):
    if not isinstance(state, dict):
        return {}
    value = state.get("podcast_show")
    if isinstance(value, dict):
        return value
    state["podcast_show"] = {}
    return state["podcast_show"]


def _podcast_apply_show_state_to_result(result, show_state):
    if not isinstance(show_state, dict):
        return result
    result.show_playlist_id = str(show_state.get("show_playlist_id") or "")
    result.show_image_source = str(show_state.get("show_image_source") or "")
    result.show_podcast_status = str(show_state.get("show_podcast_status") or "")
    result.show_last_synced_at = str(show_state.get("show_last_synced_at") or "")
    result.show_last_error = str(show_state.get("show_last_error") or "")
    return result


def sync_single_video_into_unified_podcast_show(
    channel_name,
    video_id,
    book_name="",
    cover_image_path="",
    show_thumbnail_hint="",
):
    normalized_channel = str(channel_name or "").strip()
    normalized_video_id = _extract_youtube_video_id(video_id)
    if not _podcast_runtime_enabled() or not _podcast_unified_show_enabled():
        return {
            "skipped": True,
            "reason": "podcast unified show disabled",
            "show_playlist_id": "",
            "show_image_source": "",
            "show_podcast_status": "",
            "show_last_synced_at": "",
            "show_last_error": "",
        }
    if not normalized_channel or not normalized_video_id:
        raise RuntimeError("single-video unified show sync requires channel_name and video_id")

    youtube = authenticate_youtube_from_supabase(normalized_channel)
    show_title = _podcast_show_title(normalized_channel)
    episode_titles = [str(book_name or "").strip()] if str(book_name or "").strip() else [show_title]
    show = _podcast_resolve_existing_show_playlist(youtube, normalized_channel, show_title)
    result = {
        "show_title": show_title,
        "show_playlist_id": str(show.get("playlist_id") or ""),
        "show_created": False,
        "show_metadata_updated": False,
        "show_image_uploaded": False,
        "show_image_source": "",
        "show_podcast_status": str(show.get("podcast_status") or ""),
        "video_inserted": False,
        "video_already_present": False,
        "show_last_synced_at": "",
        "show_last_error": "",
    }

    _podcast_progress(f"Unified show sync started for single video {normalized_video_id} -> {show_title}")
    description_result = None
    need_metadata_refresh = bool(
        not show
        or str(show.get("title") or "").strip() != show_title
        or not str(show.get("description") or "").strip()
        or str(show.get("privacy_status") or "").strip() != "public"
    )
    if need_metadata_refresh:
        description_result = _podcast_generate_show_description(normalized_channel, show_title, episode_titles)
    description = (
        description_result["description"]
        if isinstance(description_result, dict)
        else str(show.get("description") or "").strip() or _podcast_build_default_show_description(normalized_channel, 1)
    )

    if not show:
        _podcast_progress("Unified show: creating playlist shell")
        show = _podcast_create_plain_playlist(youtube, show_title, description, enable_podcast=False)
        result["show_created"] = True

    if not show.get("playlist_id"):
        raise RuntimeError(f"未能创建或定位统一 podcast show。show={json.dumps(show, ensure_ascii=False)}")

    _podcast_save_channel_setting(normalized_channel, _podcast_show_setting_key(), show["playlist_id"])
    result["show_playlist_id"] = show["playlist_id"]
    show_is_podcast = str(show.get("podcast_status") or "") == "enabled"

    if need_metadata_refresh and not result["show_created"]:
        _podcast_progress("Unified show: updating title/description/privacy")
        show = _podcast_update_playlist(
            youtube,
            show["playlist_id"],
            show_title,
            description,
            privacy_status="public",
            enable_podcast=show_is_podcast,
        )
        result["show_metadata_updated"] = True
        show_is_podcast = str(show.get("podcast_status") or "") == "enabled"

    existing_images = _podcast_list_playlist_images(youtube, show["playlist_id"]) if show_is_podcast else []
    image_status = _podcast_resolve_playlist_image_status(existing_images, show_is_podcast)
    has_existing_image = bool(image_status.get("has_image"))

    if not has_existing_image:
        cover_prompt_result = _podcast_generate_show_cover_prompt(normalized_channel, show_title, episode_titles)
        _podcast_progress("Unified show: generating square cover image")
        image_result = _podcast_generate_show_cover_image(
            normalized_channel,
            show_title,
            cover_prompt_result["prompt"],
            thumbnail_fallback_url=str(show.get("thumbnail_url") or show_thumbnail_hint or ""),
            local_fallback_image_path=cover_image_path,
        )
        _podcast_log_image_source("Unified show", image_result)
        _podcast_progress("Unified show: uploading playlist image")
        _podcast_sync_playlist_image(
            youtube,
            show["playlist_id"],
            image_result["path"],
            existing_images=existing_images if show_is_podcast else None,
            blind_insert=not show_is_podcast,
        )
        result["show_image_uploaded"] = True
        result["show_image_source"] = str(image_result.get("source") or "")
        has_existing_image = True
    elif show_is_podcast and image_status.get("assumed"):
        result["show_image_source"] = "existing_assumed"
    elif has_existing_image:
        result["show_image_source"] = "existing"

    if not show_is_podcast:
        _podcast_progress("Unified show: enabling podcast status")
        show = _podcast_update_playlist(
            youtube,
            show["playlist_id"],
            show_title,
            description,
            privacy_status="public",
            enable_podcast=True,
        )
        result["show_metadata_updated"] = True
        if bool(show.get("podcast_status_pending")):
            raise RuntimeError("统一 show 的 podcastStatus 请求已提交，但当前还未回显 enabled。")
        show_is_podcast = str(show.get("podcast_status") or "") == "enabled"

    if not show_is_podcast:
        raise RuntimeError("统一 show 尚未成功切换为 podcast。")

    result["show_podcast_status"] = str(show.get("podcast_status") or "enabled") or "enabled"
    _podcast_progress("Unified show: inserting single video if missing")
    ensure_result = _podcast_ensure_video_in_playlist(youtube, show["playlist_id"], normalized_video_id)
    result["video_inserted"] = bool(ensure_result.get("inserted"))
    result["video_already_present"] = bool(ensure_result.get("already_present"))
    result["show_last_synced_at"] = _podcast_now_iso()
    result["show_last_error"] = ""
    _podcast_progress(
        f"Unified show sync finished: playlist_id={show['playlist_id']} inserted={result['video_inserted']} already_present={result['video_already_present']}"
    )
    return result


# ============================================================================
# Podcast 图片管理（原文件行 8841-8982）
# ============================================================================

def _podcast_extract_youtube_credentials(youtube):
    http_obj = getattr(youtube, "_http", None)
    candidates = [
        getattr(http_obj, "credentials", None),
        getattr(getattr(http_obj, "request", None), "credentials", None),
        getattr(getattr(http_obj, "http", None), "credentials", None),
        getattr(getattr(getattr(http_obj, "http", None), "request", None), "credentials", None),
    ]
    credentials = next((item for item in candidates if item is not None), None)
    if credentials is None:
        raise RuntimeError("无法从 YouTube client 提取 OAuth credentials。")
    if (getattr(credentials, "expired", False) or not getattr(credentials, "valid", True)) and getattr(
        credentials, "refresh_token", None
    ):
        credentials.refresh(GoogleAuthRequest())
    return credentials


def _podcast_playlist_image_row(item, fallback_playlist_id):
    snippet = item.get("snippet") or {}
    return {
        "image_id": str(item.get("id") or "").strip(),
        "playlist_id": str(snippet.get("playlistId") or fallback_playlist_id or "").strip(),
        "type": str(snippet.get("type") or "").strip().lower(),
        "width": int(snippet.get("width") or 0),
        "height": int(snippet.get("height") or 0),
    }


def _podcast_is_playlist_images_unsupported_error(message):
    return "PLAYLIST_TYPE_UNSUPPORTED" in str(message or "")


def _podcast_list_playlist_images_via_rest(youtube, playlist_id, filter_params):
    credentials = _podcast_extract_youtube_credentials(youtube)
    session = AuthorizedSession(credentials)
    images = []
    page_token = None
    retries = max(1, int(getattr(cfg, "YOUTUBE_PODCAST_YT_RETRIES", 5) or 5))
    while True:
        params = {
            "part": "snippet",
            "maxResults": 50,
            **filter_params,
        }
        if page_token:
            params["pageToken"] = page_token

        last_error = None
        payload = None
        for attempt_index in range(retries):
            response = session.get(_PODCAST_PLAYLIST_IMAGES_ENDPOINT, params=params, timeout=60)
            if response.status_code < 400:
                payload = response.json()
                break

            try:
                payload = response.json()
            except Exception:
                payload = response.text
            last_error = RuntimeError(
                f"playlistImages.list failed: status={response.status_code} params={params} payload={payload}"
            )
            if response.status_code not in {408, 409, 429, 500, 502, 503, 504} or attempt_index >= retries - 1:
                raise last_error
            sleep_seconds = _podcast_youtube_retry_sleep_seconds(attempt_index)
            _podcast_progress(
                f"playlistImages.list retrying in {sleep_seconds:.0f}s for playlist={playlist_id} status={response.status_code}"
            )
            time.sleep(sleep_seconds)
        if payload is None and last_error is not None:
            raise last_error

        for item in (payload or {}).get("items", []):
            images.append(_podcast_playlist_image_row(item, playlist_id))
        page_token = (payload or {}).get("nextPageToken")
        if not page_token:
            break
    return images


def _podcast_list_playlist_images(youtube, playlist_id):
    normalized = str(playlist_id or "").strip()
    if not normalized:
        return []

    errors = []
    for filter_params in (
        {"playlistId": normalized},
        {"parent": f"playlists/{normalized}"},
    ):
        try:
            return _podcast_list_playlist_images_via_rest(youtube, normalized, filter_params)
        except Exception as e:
            errors.append(str(e))

    if any(_podcast_is_playlist_images_unsupported_error(item) for item in errors):
        return []

    raise RuntimeError(" ; ".join(errors) or f"无法列出 playlist images: {normalized}")


def _podcast_resolve_playlist_image_status(images, podcast_enabled):
    detected = bool(images)
    assumed = bool(podcast_enabled and not detected)
    has_image = bool(detected or assumed)
    if detected:
        label = "yes"
    elif assumed:
        label = "yes(assumed)"
    else:
        label = "no"
    return {
        "detected": detected,
        "assumed": assumed,
        "has_image": has_image,
        "label": label,
    }


def _podcast_sync_playlist_image(
    youtube,
    playlist_id,
    image_path,
    existing_images=None,
    blind_insert=False,
):
    hero_image = {}
    if not blind_insert:
        existing_images = existing_images if existing_images is not None else _podcast_list_playlist_images(youtube, playlist_id)
        hero_image = next((item for item in existing_images if item.get("type") == "hero"), {})

    body = {
        "snippet": {
            "playlistId": playlist_id,
            "type": "hero",
        }
    }
    media = MediaFileUpload(image_path, mimetype="image/jpeg")

    if hero_image.get("image_id"):
        body["id"] = hero_image["image_id"]
        response = _podcast_execute_youtube_request(
            youtube.playlistImages().update(part="snippet", body=body, media_body=media),
            op_name=f"playlistImages.update:{playlist_id}",
        )
    else:
        response = _podcast_execute_youtube_request(
            youtube.playlistImages().insert(part="snippet", body=body, media_body=media),
            op_name=f"playlistImages.insert:{playlist_id}",
        )

    snippet = response.get("snippet") or {}
    return {
        "image_id": str(response.get("id") or body.get("id") or "").strip(),
        "playlist_id": str(snippet.get("playlistId") or playlist_id),
        "type": str(snippet.get("type") or "hero").strip().lower(),
        "width": int(snippet.get("width") or 0),
        "height": int(snippet.get("height") or 0),
    }


def _podcast_sync_split_playlist_podcast(result, state, book_record, book_name):
    playlist_state = get_split_playlist_state(state)  # defined in pipeline.py
    playlist_id = str(playlist_state.get("playlist_id") or "").strip()
    if not playlist_id:
        raise RuntimeError("split playlist podcast sync requires playlist_id")

    channel_name = str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    if not channel_name:
        raise RuntimeError("YOUTUBE_CHANNEL_NAME 未配置，无法同步 split podcast playlist")

    youtube = authenticate_youtube_from_supabase(channel_name)
    playlist_title = str(playlist_state.get("title") or result.playlist_title or book_name or "").strip()
    playlist_description = str(playlist_state.get("description") or result.seo_description or "").strip()
    playlist = _podcast_fetch_playlist_by_id(youtube, playlist_id, retries=8, wait_seconds=1.5)
    if playlist:
        playlist_title = str(playlist.get("title") or playlist_title).strip()
        playlist_description = str(playlist.get("description") or playlist_description)
        playlist_state["title"] = playlist_title
        playlist_state["description"] = playlist_description
        playlist_state["privacy_status"] = str(playlist.get("privacy_status") or "public")

    podcast_enabled = str((playlist or {}).get("podcast_status") or playlist_state.get("podcast_status") or "").strip().lower() == "enabled"
    existing_images = _podcast_list_playlist_images(youtube, playlist_id) if podcast_enabled else []
    image_status = _podcast_resolve_playlist_image_status(existing_images, podcast_enabled)
    has_existing_image = bool(image_status.get("has_image"))
    if str(playlist_state.get("podcast_image_status") or "").strip().lower() == "completed":
        has_existing_image = True

    if not has_existing_image:
        prompt_result = _podcast_generate_batch_playlist_cover_prompt(playlist_title, playlist_description)
        image_filename = f"{_sanitize_filename_component(playlist_id)}_podcast_cover.jpg"
        _podcast_progress(f"[{book_name}] Split playlist: generating square cover image")
        image_result = _podcast_generate_named_cover_image(
            image_filename,
            prompt_result["prompt"],
            subdir=_PODCAST_PLAYLIST_ASSET_DIR,
            cover_title=playlist_title,
            cover_subtitle="Podcast",
            thumbnail_fallback_url=str((playlist or {}).get("thumbnail_url") or ""),
            local_fallback_image_path=str(getattr(result, "cover_image_path", "") or ""),
        )
        _podcast_log_image_source(f"[{book_name}] Split playlist", image_result)
        _podcast_progress(f"[{book_name}] Split playlist: uploading podcast square image")
        _podcast_sync_playlist_image(
            youtube,
            playlist_id,
            image_result["path"],
            existing_images=existing_images if podcast_enabled else None,
            blind_insert=not podcast_enabled,
        )
        playlist_state["podcast_image_status"] = "completed"
        playlist_state["podcast_image_source"] = str(image_result.get("source") or "")
        has_existing_image = True
    else:
        playlist_state["podcast_image_status"] = "completed"
        if image_status.get("assumed"):
            playlist_state["podcast_image_source"] = str(playlist_state.get("podcast_image_source") or "existing_assumed")
        else:
            playlist_state["podcast_image_source"] = str(playlist_state.get("podcast_image_source") or "existing")

    if not podcast_enabled:
        _podcast_progress(f"[{book_name}] Split playlist: enabling podcast status")
        updated = _podcast_update_playlist(
            youtube,
            playlist_id,
            playlist_title,
            playlist_description,
            privacy_status=str(playlist_state.get("privacy_status") or "public"),
            enable_podcast=True,
        )
        if bool(updated.get("podcast_status_pending")):
            raise RuntimeError("split playlist 的 podcastStatus 请求已提交，但当前还未回显 enabled。")
        podcast_enabled = str(updated.get("podcast_status") or "").strip().lower() == "enabled"
        playlist = updated
    if not podcast_enabled:
        raise RuntimeError("split playlist 尚未成功切换为 podcast。")

    playlist_state["podcast_status"] = "enabled"
    playlist_state["podcast_last_synced_at"] = _podcast_now_iso()
    playlist_state["podcast_last_error"] = ""
    result.playlist_podcast_status = "enabled"
    result.playlist_podcast_image_status = str(playlist_state.get("podcast_image_status") or "")
    result.playlist_podcast_image_source = str(playlist_state.get("podcast_image_source") or "")
    result.playlist_podcast_last_synced_at = str(playlist_state.get("podcast_last_synced_at") or "")
    result.playlist_podcast_last_error = ""
    return {
        "playlist_id": playlist_id,
        "podcast_status": "enabled",
        "podcast_image_status": str(playlist_state.get("podcast_image_status") or ""),
        "podcast_image_source": str(playlist_state.get("podcast_image_source") or ""),
        "podcast_last_synced_at": str(playlist_state.get("podcast_last_synced_at") or ""),
    }


# ============================================================================
# Monkey-patch pipeline.py 函数（原文件行 9959-10129）
# ============================================================================

# 延迟导入，防止在 pipeline.py 尚未导入时就访问 get_split_playlist_state
# 这些在 monkey-patch wrapper 内部惰性解决，或者直接从 pipeline 动态获取
# 我们这里先在底部做真正的导入和覆盖

# 为了方便后续 import，在此声明延迟引用
_PODCAST_RUNTIME_ORIGINAL_PROCESS_STANDARD_BOOK = None
_PODCAST_RUNTIME_ORIGINAL_SYNC_SPLIT_PLAYLIST = None
_PODCAST_RUNTIME_ORIGINAL_SYNC_RESULT_FROM_SPLIT_STATE = None
_PODCAST_RUNTIME_ORIGINAL_FINALIZE_BOOK_RESULT = None
_get_split_playlist_state = None
_get_split_shared_assets = None
_save_split_processing_state = None
_reload_split_processing_state = None
_build_standard_processing_state = None
_load_youtube_upload_receipt = None
_sync_split_playlist = None
_sync_result_from_split_state = None


def _podcast_install_monkey_patches():
    """在 pipeline.py / state.py / youtube.py 均已导入后调用，完成覆盖。"""
    global _PODCAST_RUNTIME_ORIGINAL_PROCESS_STANDARD_BOOK
    global _PODCAST_RUNTIME_ORIGINAL_SYNC_SPLIT_PLAYLIST
    global _PODCAST_RUNTIME_ORIGINAL_SYNC_RESULT_FROM_SPLIT_STATE
    global _PODCAST_RUNTIME_ORIGINAL_FINALIZE_BOOK_RESULT
    global _get_split_playlist_state
    global _get_split_shared_assets
    global _save_split_processing_state
    global _reload_split_processing_state
    global _build_standard_processing_state
    global _load_youtube_upload_receipt
    global _sync_split_playlist
    global _sync_result_from_split_state

    from . import pipeline as _pl
    from . import state as _st
    from . import youtube as _yt

    _PODCAST_RUNTIME_ORIGINAL_PROCESS_STANDARD_BOOK = _pl.process_standard_book
    _PODCAST_RUNTIME_ORIGINAL_SYNC_SPLIT_PLAYLIST = _pl.sync_split_playlist
    _PODCAST_RUNTIME_ORIGINAL_SYNC_RESULT_FROM_SPLIT_STATE = _pl.sync_result_from_split_state
    _PODCAST_RUNTIME_ORIGINAL_FINALIZE_BOOK_RESULT = _pl.finalize_book_result
    _get_split_playlist_state = _st.get_split_playlist_state
    _get_split_shared_assets = _st.get_split_shared_assets
    _save_split_processing_state = _st.save_split_processing_state
    _reload_split_processing_state = _st.reload_split_processing_state
    _build_standard_processing_state = _st.build_standard_processing_state
    _load_youtube_upload_receipt = _yt.load_youtube_upload_receipt  # 需要在 youtube.py 暴露（暂无，fallback）
    _sync_split_playlist = _pl.sync_split_playlist  # 备份原始以备 wrapper 调用
    _sync_result_from_split_state = _pl.sync_result_from_split_state

    # ---- 覆盖 process_standard_book ----
    def _wrapped_process_standard_book(result, book_record, book_data, chapters_sorted, book_dir, safe_name, book_name, category):
        result = _PODCAST_RUNTIME_ORIGINAL_PROCESS_STANDARD_BOOK(
            result, book_record, book_data, chapters_sorted, book_dir, safe_name, book_name, category,
        )
        if not _podcast_runtime_enabled() or not _podcast_unified_show_enabled():
            return result
        if not bool(getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True)) or not str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip():
            return result
        if not bool(getattr(result, "upload_ready", False)):
            return result

        # 从 youtube.py 的 load_youtube_upload_receipt 或 fallback 获取 video_id
        video_id = ""
        try:
            from .youtube import load_youtube_upload_receipt
            upload_receipt_path = os.path.join(book_dir, "youtube_upload_receipt.json")
            receipt = load_youtube_upload_receipt(
                upload_receipt_path,
                video_path=getattr(result, "video_path", ""),
                channel_name=str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip(),
            )
            video_id = str(receipt.get("video_id") or "").strip()
        except Exception:
            pass
        if not video_id:
            video_id = _extract_youtube_video_id(getattr(result, "youtube_url", ""))
        if not video_id:
            log.warning("[%s] 单 P 上传成功，但未能从上传回执中解析 video_id，跳过 unified podcast show 同步。", book_name)
            return result

        state = _reload_split_processing_state(
            book_record,
            fallback_state=_build_standard_processing_state(book_record),
            book_name=book_name,
        )
        if not isinstance(state, dict):
            state = _build_standard_processing_state(book_record)
        show_state = _get_split_playlist_state(state)  # podcast_show 嵌套在 state 中
        # 实际上原代码用 _podcast_get_show_state_container(state)

        from .state import save_split_processing_state as _sps, reload_split_processing_state as _rps, build_standard_processing_state as _bsp

        state["pending_resume"] = True
        state["last_stage"] = "standard_unified_show_syncing"
        state["last_error"] = ""
        state_ref = _sps(book_record, state)
        result.state_path = state_ref

        try:
            sync_result = sync_single_video_into_unified_podcast_show(
                channel_name=str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip(),
                video_id=video_id,
                book_name=str(getattr(result, "seo_title", "") or book_name or "").strip(),
                cover_image_path=str(getattr(result, "cover_image_path", "") or ""),
            )
            show_state.update({
                "show_playlist_id": str(sync_result.get("show_playlist_id") or ""),
                "show_image_source": str(sync_result.get("show_image_source") or ""),
                "show_podcast_status": str(sync_result.get("show_podcast_status") or ""),
                "show_last_synced_at": str(sync_result.get("show_last_synced_at") or _podcast_now_iso()),
                "show_last_error": "",
            })
            state["pending_resume"] = False
            state["last_stage"] = "standard_unified_show_completed"
            state["last_error"] = ""
            state_ref = _sps(book_record, state)
            result.state_path = state_ref
            result.pending_resume = False
            if str(getattr(result, "error", "") or "").startswith("Single-video unified podcast show sync failed:"):
                result.error = ""
            _podcast_apply_show_state_to_result(result, show_state)
            _podcast_progress(
                f"[{book_name}] Single-video unified show sync done: show={show_state.get('show_playlist_id') or ''} inserted={bool(sync_result.get('video_inserted'))}"
            )
            return result
        except Exception as e:
            show_state.update({
                "show_playlist_id": str(show_state.get("show_playlist_id") or ""),
                "show_image_source": str(show_state.get("show_image_source") or ""),
                "show_podcast_status": str(show_state.get("show_podcast_status") or ""),
                "show_last_synced_at": str(show_state.get("show_last_synced_at") or _podcast_now_iso()),
                "show_last_error": str(e),
            })
            state["pending_resume"] = True
            state["last_stage"] = "standard_unified_show_failed"
            state["last_error"] = str(e)
            state_ref = _sps(book_record, state)
            result.state_path = state_ref
            result.pending_resume = True
            result.error = f"Single-video unified podcast show sync failed: {e}"
            _podcast_apply_show_state_to_result(result, show_state)
            _podcast_progress(f"[{book_name}] Single-video unified show sync failed: {e}")
            return result

    _pl.process_standard_book = _wrapped_process_standard_book

    # ---- 覆盖 sync_split_playlist ----
    def _wrapped_sync_split_playlist(result, state, split_plan, book_record, book_name):
        result = _PODCAST_RUNTIME_ORIGINAL_SYNC_SPLIT_PLAYLIST(result, state, split_plan, book_record, book_name)
        if not _podcast_runtime_enabled() or not _podcast_split_playlist_enabled():
            return result

        playlist_state = _get_split_playlist_state(state)
        playlist_id = str(playlist_state.get("playlist_id") or "").strip()
        if not playlist_id:
            return result

        playlist_state["podcast_status"] = str(playlist_state.get("podcast_status") or "")
        playlist_state["podcast_image_status"] = str(playlist_state.get("podcast_image_status") or "")
        playlist_state["podcast_last_synced_at"] = str(playlist_state.get("podcast_last_synced_at") or "")
        playlist_state["podcast_last_error"] = ""
        state["pending_resume"] = True
        playlist_state["status"] = "podcast_syncing"
        state["last_stage"] = "playlist_podcast_syncing"
        state["last_error"] = ""
        state_ref = _save_split_processing_state(book_record, state)
        result.state_path = state_ref

        try:
            podcast_result = _podcast_sync_split_playlist_podcast(result, state, book_record, book_name)
            playlist_state["podcast_status"] = str(podcast_result.get("podcast_status") or "enabled")
            playlist_state["podcast_image_status"] = str(podcast_result.get("podcast_image_status") or "completed")
            playlist_state["podcast_image_source"] = str(podcast_result.get("podcast_image_source") or "")
            playlist_state["podcast_last_synced_at"] = str(
                podcast_result.get("podcast_last_synced_at") or _podcast_now_iso()
            )
            playlist_state["podcast_last_error"] = ""
            playlist_state["status"] = "completed"
            playlist_state["last_error"] = ""
            playlist_state["last_synced_at"] = _podcast_now_iso()
            state["pending_resume"] = False
            state["last_stage"] = "playlist_completed"
            state["last_error"] = ""
            state_ref = _save_split_processing_state(book_record, state)
            result.state_path = state_ref
            result.playlist_completed = True
            result.pending_resume = False
            result.error = ""
            return result
        except Exception as e:
            playlist_state["status"] = "failed"
            playlist_state["last_error"] = str(e)
            playlist_state["podcast_last_error"] = str(e)
            state["pending_resume"] = True
            state["last_stage"] = "playlist_failed"
            state["last_error"] = str(e)
            state_ref = _save_split_processing_state(book_record, state)
            result.state_path = state_ref
            result.playlist_completed = False
            result.pending_resume = True
            result.error = str(e)
            _podcast_progress(f"[{book_name}] Split playlist podcast sync failed: {e}")
            return result

    _pl.sync_split_playlist = _wrapped_sync_split_playlist

    # ---- 覆盖 sync_result_from_split_state ----
    def _wrapped_sync_result_from_split_state(result, state, split_plan):
        result = _PODCAST_RUNTIME_ORIGINAL_SYNC_RESULT_FROM_SPLIT_STATE(result, state, split_plan)
        playlist_state = _get_split_playlist_state(state)
        result.playlist_podcast_status = str(playlist_state.get("podcast_status") or "")
        result.playlist_podcast_image_status = str(playlist_state.get("podcast_image_status") or "")
        result.playlist_podcast_image_source = str(playlist_state.get("podcast_image_source") or "")
        result.playlist_podcast_last_synced_at = str(playlist_state.get("podcast_last_synced_at") or "")
        result.playlist_podcast_last_error = str(playlist_state.get("podcast_last_error") or "")
        if isinstance(state, dict):
            _podcast_apply_show_state_to_result(result, state.get("podcast_show") or {})
        return result
    _pl.sync_result_from_split_state = _wrapped_sync_result_from_split_state

    # ---- 覆盖 finalize_book_result（podcast 版完全重写，直接替换）----
    _pl.finalize_book_result = finalize_book_result

    # ---- 把 podcast 版 playlist helpers 注入 youtube.py 命名空间 ----
    _yt._list_owned_playlists_with_client = _list_owned_playlists_with_client
    _yt._create_or_update_playlist_with_client = _create_or_update_playlist_with_client
    _yt._list_playlist_items_with_client = _list_playlist_items_with_client
    _yt._delete_playlist_item_with_client = _delete_playlist_item_with_client
    _yt._insert_playlist_video_with_client = _insert_playlist_video_with_client
    _yt._update_playlist_item_position_with_client = _update_playlist_item_position_with_client


# ============================================================================
# podcast 版本 finalize_book_result（原文件行 10132-10247）
# ============================================================================

# ---- 延迟引用 ----
_get_split_playlist_state_ref = None


def finalize_book_result(result, book_dir, book_record=None):
    if bool(getattr(result, "skipped", False)):
        result.audio_ready = False
        result.video_ready = False
        result.upload_ready = False
        result.pending_resume = False
        result.success = False
        return result

    part_count = max(1, int(getattr(result, "part_count", 1) or 1))
    completed_part_count = max(0, int(getattr(result, "completed_part_count", 0) or 0))

    if getattr(result, "split_mode", False) or part_count > 1:
        playlist_required = bool(getattr(result, "playlist_required", False))
        playlist_completed = not playlist_required or bool(getattr(result, "playlist_completed", False))
        all_parts_completed = completed_part_count >= part_count

        result.audio_ready = all_parts_completed
        result.video_ready = all_parts_completed if getattr(cfg, "ENABLE_VIDEO_GENERATION", True) else result.audio_ready
        result.upload_ready = (
            all_parts_completed and (not playlist_required or playlist_completed)
            if getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True)
            else result.video_ready
        )
        computed_pending_resume = (not all_parts_completed) or (playlist_required and not playlist_completed)
        stale_pending_resume = bool(getattr(result, "pending_resume", False)) and not computed_pending_resume
        result.pending_resume = computed_pending_resume
        required_stages = [result.audio_ready]
        if getattr(cfg, "ENABLE_VIDEO_GENERATION", True):
            required_stages.append(result.video_ready)
        if getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True):
            required_stages.append(result.upload_ready)
        result.success = all(required_stages) and all_parts_completed and playlist_completed and not result.pending_resume
        if stale_pending_resume:
            log.warning(
                "[%s] Clearing stale pending_resume during final split evaluation. completed=%d/%d playlist_required=%s playlist_completed=%s state=%s",
                result.book_name,
                completed_part_count,
                part_count,
                playlist_required,
                playlist_completed,
                getattr(result, "state_path", ""),
            )
    else:
        result.audio_ready = bool(result.merged_audio_path and os.path.exists(result.merged_audio_path))
        result.video_ready = bool(result.video_path and os.path.exists(result.video_path))
        result.upload_ready = bool(result.youtube_url)

        required_stages = [result.audio_ready]
        if getattr(cfg, "ENABLE_VIDEO_GENERATION", True):
            required_stages.append(result.video_ready)
        if getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True):
            required_stages.append(result.upload_ready)

        result.success = all(required_stages) and not bool(getattr(result, "pending_resume", False))

    if not result.success and not result.error:
        if bool(getattr(result, "pending_resume", False)):
            if getattr(result, "split_mode", False) or part_count > 1:
                result.error = "长音频分片处理中断，已记录进度，等待下次续跑"
            else:
                result.error = "单 P 上传后的 podcast 后置同步尚未完成，已记录进度，等待下次续跑"
        elif not result.audio_ready:
            result.error = "音频成品未准备完成"
        elif getattr(cfg, "ENABLE_VIDEO_GENERATION", True) and not result.video_ready:
            result.error = "MP4 成品未准备完成"
        elif getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True) and not result.upload_ready:
            result.error = "YouTube 上传未完成"

    if getattr(result, "split_mode", False) and not result.success:
        log.error(
            "[%s] Split finalization failed: completed_part_count=%d part_count=%d pending_resume=%s playlist_required=%s playlist_completed=%s audio_ready=%s video_ready=%s upload_ready=%s state=%s error=%s",
            result.book_name,
            completed_part_count,
            part_count,
            bool(getattr(result, "pending_resume", False)),
            bool(getattr(result, "playlist_required", False)),
            bool(getattr(result, "playlist_completed", False)),
            bool(getattr(result, "audio_ready", False)),
            bool(getattr(result, "video_ready", False)),
            bool(getattr(result, "upload_ready", False)),
            getattr(result, "state_path", ""),
            str(getattr(result, "error", "") or ""),
        )
    elif not getattr(result, "split_mode", False) and bool(getattr(result, "pending_resume", False)):
        log.warning(
            "[%s] Standard finalization paused for podcast follow-up. audio_ready=%s video_ready=%s upload_ready=%s state=%s error=%s",
            result.book_name,
            bool(getattr(result, "audio_ready", False)),
            bool(getattr(result, "video_ready", False)),
            bool(getattr(result, "upload_ready", False)),
            getattr(result, "state_path", ""),
            str(getattr(result, "error", "") or ""),
        )

    report = {
        "generated_at": dt_module.datetime.now().isoformat(),
        "book_dir": book_dir,
        "result": dict(result.__dict__),
    }
    if book_record is not None:
        report["source"] = {
            "book_id": book_record.get("book_id"),
            "book_name": book_record.get("book_name"),
            "category": book_record.get("category"),
        }

    report_path = os.path.join(book_dir, "book_result.json")
    try:
        write_json_file(report_path, report)
    except Exception as e:
        log.warning("单书结果写入失败: %s", e)

    log.info("🏁 本书《%s》全程线走完。状态：%s", result.book_name, "✅" if result.success else "❌")
    return result