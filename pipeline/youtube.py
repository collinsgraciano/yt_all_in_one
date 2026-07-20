"""运行核心：YouTube API 上传、播放列表、本地化与视频编码。

对应原 runtime_core.py 行 4728-6879：
- generate_video（ffmpeg 封装行 4728-4852）
- YouTube OAuth / 凭证（行 4855-5151）
- 缩略图压缩 / Category / Tag（行 5152-5257）
- 日期解析 / 格式（行 5246-5267）
- Channel Video Index / Schedule（行 5282-5607）
- Upload / Status / Body（行 5610-5841）
- 播放列表 create/update/list/sync（行 5843-6601）
- 本地化 sync / backfill（行 6079-6371）
- 共享的 _extract_youtube_video_id（行 2060）× 原文件位置
- build_youtube_payload（行 6999）

注意：以下函数会被 podcast.py 重新定义并打补丁进本模块：
  _list_owned_playlists_with_client, _create_or_update_playlist_with_client,
  _list_playlist_items_with_client, _delete_playlist_item_with_client,
  _insert_playlist_video_with_client, _update_playlist_item_position_with_client。
本模块保留原定义，podcast.py 在导入尾段做命名空间注入。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import requests
from datetime import datetime as dt_datetime, timedelta as dt_timedelta, timezone as dt_timezone
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from PIL import Image
from psycopg import sql

from . import config as cfg
from .runtime import log, write_json_file, read_json_file
from .db import get_public_table_identifier, execute_postgres_fetchone, execute_postgres, execute_postgres_fetchall

try:
    YOUTUBE_SCHEDULE_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
except Exception:
    YOUTUBE_SCHEDULE_LOCAL_TIMEZONE = dt_timezone(dt_timedelta(hours=8))

YOUTUBE_TITLE_MATCH_CACHE = {}
YOUTUBE_LOCALIZATION_CONVERTER_CACHE = {}
YOUTUBE_LOCALIZATION_INSTALL_ATTEMPTED = set()


# ============================================================================
# 视频编码（原文件行 4728-4852）
# ============================================================================

def generate_video(audio_path, image_path, output_path, resolution="1080p"):
    if not os.path.exists(audio_path):
        log.error("无法生成视频：音频文件不存在 %s", audio_path)
        return False
    if not os.path.exists(image_path):
        log.error("无法生成视频：封面文件不存在 %s", image_path)
        return False

    log.info("开始通过 FFmpeg 封装 MP4 视频...")

    target_size_map = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440), "4k": (3840, 2160)}
    target_res = target_size_map.get(str(resolution).lower(), (1920, 1080))
    tw, th = target_res

    processed_image = image_path
    needs_cleanup = False
    try:
        with Image.open(image_path) as img:
            src_w, src_h = img.size
            if src_w > tw * 1.1 or src_h > th * 1.1 or img.format != "JPEG":
                src_ratio = src_w / src_h
                target_ratio = tw / th
                if src_ratio > target_ratio:
                    new_w = int(src_h * target_ratio)
                    offset = (src_w - new_w) // 2
                    img = img.crop((offset, 0, offset + new_w, src_h))
                elif src_ratio < target_ratio:
                    new_h = int(src_w / target_ratio)
                    offset = (src_h - new_h) // 2
                    img = img.crop((0, offset, src_w, offset + new_h))
                img = img.resize(target_res, Image.Resampling.LANCZOS)
                processed_image = output_path + ".cover_cache.jpg"
                img.convert("RGB").save(processed_image, format="JPEG", quality=85)
                needs_cleanup = True
                log.info(
                    "封面预处理：%dx%d → %dx%d JPEG (quality=85)，原始约 %.1f MB",
                    src_w, src_h, tw, th,
                    os.path.getsize(image_path) / 1024 / 1024,
                )
    except Exception as e:
        log.warning("封面 PIL 预处理失败，使用原始文件：%s", e)
        processed_image = image_path

    res_to_scale = {"720p": "1280:720", "1080p": "1920:1080", "1440p": "2560:1440", "4k": "3840:2160"}
    scale_vf = res_to_scale.get(str(resolution).lower(), "1920:1080")

    base_cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-framerate", "1",
        "-i", processed_image,
        "-i", audio_path,
        "-vf", f"scale={scale_vf}:force_original_aspect_ratio=decrease,pad={scale_vf}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "stillimage",
        "-shortest",
    ]

    attempts = [
        ("copy-audio", ["-c:a", "copy"]),
        ("aac-fallback", ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]),
    ]

    last_error = ""
    for idx, (mode, audio_args) in enumerate(attempts, start=1):
        cmd = base_cmd + audio_args + [output_path]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3600,
            )
        except subprocess.TimeoutExpired:
            last_error = f"FFmpeg 在 {mode} 模式下执行超时"
            log.error(last_error)
            if os.path.exists(output_path):
                os.remove(output_path)
            continue
        except Exception as e:
            last_error = f"调用 FFmpeg 封装时发生异常: {e}"
            log.error(last_error)
            if os.path.exists(output_path):
                os.remove(output_path)
            continue

        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            log.info(
                "视频封装完成: %s (模式=%s, 大小: %.2f MB)",
                os.path.basename(output_path),
                mode,
                os.path.getsize(output_path) / 1024 / 1024,
            )
            if needs_cleanup and os.path.exists(processed_image):
                os.remove(processed_image)
            return True

        last_error = (result.stderr or "").strip()[-1500:]
        if os.path.exists(output_path):
            os.remove(output_path)

        if idx < len(attempts):
            log.warning("视频封装在 %s 模式失败，切换到下一种兼容方案。", mode)
        else:
            log.error("视频封装失败，FFmpeg 报错:\n%s", last_error)

    if needs_cleanup and os.path.exists(processed_image):
        os.remove(processed_image)
    return False


# ============================================================================
# YouTube 凭证（原文件行 5086-5151）
# ============================================================================

class MissingYouTubeCredentialsError(RuntimeError):
    """Raised when the configured YouTube channel has no usable stored credentials."""


def authenticate_youtube_from_supabase(channel_name):
    """从数据库获取指定频道的 YouTube Token，并在需要时自动刷新。"""
    from psycopg.types.json import Jsonb
    import datetime as dt_mod

    table_sql = get_public_table_identifier("youtube_credentials")
    log.info("🔐 正在连接数据库读取 YouTube '%s' 频道无人值守通行证...", channel_name)
    try:
        row = execute_postgres_fetchone(
            sql.SQL(
                """
                SELECT token_json
                FROM {}
                WHERE channel_name = %s
                LIMIT 1
                """
            ).format(table_sql),
            (channel_name,),
        )
        if not row:
            message = f"无法在数据库找到频道 {channel_name} 的授权凭证。请先在初始化单元中写入。"
            log.error("❌ %s", message)
            raise MissingYouTubeCredentialsError(message)

        token_info = row.get("token_json")
        if not token_info:
            message = f"频道 {channel_name} 的授权凭证数据为空。请先在初始化单元中写入有效凭证。"
            log.error("❌ %s", message)
            raise MissingYouTubeCredentialsError(message)

        token_dict = json.loads(token_info) if isinstance(token_info, str) else token_info
        credentials = Credentials.from_authorized_user_info(
            token_dict,
            scopes=["https://www.googleapis.com/auth/youtube"],
        )

        if credentials.expired:
            if credentials.refresh_token:
                log.info("🔄 YouTube 凭证已过期，尝试自动刷新令牌...")
                credentials.refresh(GoogleAuthRequest())
                refreshed_token = json.loads(credentials.to_json())
                try:
                    execute_postgres(
                        sql.SQL(
                            """
                            INSERT INTO {} (channel_name, token_json, updated_at)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (channel_name)
                            DO UPDATE SET
                              token_json = EXCLUDED.token_json,
                              updated_at = EXCLUDED.updated_at
                            """
                        ).format(table_sql),
                        (channel_name, Jsonb(refreshed_token), dt_mod.datetime.now().isoformat()),
                    )
                    log.info("✅ 新令牌已自动回写数据库。")
                except Exception as refresh_save_error:
                    log.warning("⚠️ 令牌刷新成功，但回写数据库失败: %s", refresh_save_error)
            else:
                log.error("❌ YouTube 凭证已过期，且缺少 refresh_token，无法自动刷新。")
                return None

        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        log.info("✅ YouTube '%s' 频道连线并授权成功。", channel_name)
        return youtube
    except Exception as e:
        log.error("❌ 初始化 YouTube 客户端失败，请检查数据库连接和表数据: %s", e)
        return None


# ============================================================================
# YouTube 标签 / Category（原文件行 5152-5257）
# ============================================================================

def compress_thumbnail_to_safe_limit(img_path, max_bytes=2 * 1024 * 1024):
    """将海报压缩到 YouTube 更容易接受的体积范围。"""
    if not img_path or not os.path.exists(img_path):
        return img_path

    size = os.path.getsize(img_path)
    if size <= max_bytes:
        return img_path

    log.warning(
        "⚠️ 警报响应！远端画师渲染了一款重型大画幅神图试图破门！(原生体积: %.2f MB) 系统自动介入，将其瘦身减压以免在 YouTube 端被击落拒收...",
        size / (1024 * 1024),
    )

    dir_name = os.path.dirname(img_path)
    base_name = os.path.basename(img_path)
    safe_path = os.path.join(dir_name, "safe_2mb_" + base_name)
    plans = [
        {"size": None, "quality": 85, "label": "原始尺寸高质量压缩"},
        {"size": (1920, 1080), "quality": 80, "label": "收缩至 1920x1080"},
        {"size": (1280, 720), "quality": 75, "label": "退守至 1280x720"},
        {"size": (1280, 720), "quality": 65, "label": "进一步降低 JPEG 质量"},
    ]

    try:
        with Image.open(img_path) as source_img:
            base_img = source_img.convert("RGB")
            final_size = size
            for plan in plans:
                candidate = base_img.copy()
                if plan["size"]:
                    candidate.thumbnail(plan["size"], Image.Resampling.LANCZOS)
                candidate.save(safe_path, format="JPEG", quality=plan["quality"], optimize=True)
                final_size = os.path.getsize(safe_path)
                log.info("🧪 海报压缩方案：%s -> %.2f MB", plan["label"], final_size / (1024 * 1024))
                if final_size <= max_bytes:
                    break

        if final_size > max_bytes:
            log.warning("⚠️ 已完成多轮压缩，但海报仍略高于安全线，仍尝试使用压缩版上传。")
        else:
            log.info("🎉 魔鬼减压计划完成！出厂新核：%.2f MB。符合云端全准入安检！", final_size / (1024 * 1024))
        return safe_path
    except Exception as e:
        log.error("❌ 拦截中削修时生病坠毁: %s。只能把原大毒饼图强塞云端图进行赌运传输...", e)
        return img_path


def normalize_youtube_category_id(category_id):
    """支持留空或占位值，表示上传时不设置分类。"""
    if category_id is None:
        return ""

    normalized = str(category_id).strip()
    if normalized.lower() in {"", "none", "null"}:
        return ""
    return normalized


def normalize_youtube_tags(tags, max_total_chars=500, max_count=30):
    """兼容空格/逗号/# 标签格式，并控制 YouTube 可接受的总体长度。"""
    if not tags:
        return []

    raw_items = []
    for chunk in str(tags).replace("\n", " ").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "#" in chunk and " " in chunk:
            raw_items.extend(part for part in chunk.split() if part.strip())
        else:
            raw_items.append(chunk)

    normalized = []
    seen = set()
    total_chars = 0
    for item in raw_items:
        cleaned = item.strip().strip("#").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue

        extra_chars = len(cleaned) + (1 if normalized else 0)
        if len(normalized) >= max_count or total_chars + extra_chars > max_total_chars:
            break

        normalized.append(cleaned)
        seen.add(key)
        total_chars += extra_chars

    return normalized


# ============================================================================
# 日期辅助（原文件行 5246-5267）
# ============================================================================

def _parse_youtube_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None

    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt_datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)
        return parsed.astimezone(dt_timezone.utc)
    except Exception:
        return None


def _format_youtube_datetime_z(value):
    parsed = _parse_youtube_datetime(value) if not isinstance(value, dt_datetime) else value.astimezone(dt_timezone.utc)
    if not parsed:
        return ""
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ============================================================================
# Video ID 提取（原文件行 2060-2088）
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
# Video rows fetching（原文件行 2092-2144）
# ============================================================================

def _fetch_video_rows_by_id_with_client(youtube, video_ids):
    normalized_ids = []
    seen_ids = set()
    for value in video_ids or []:
        video_id = _extract_youtube_video_id(value)
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        normalized_ids.append(video_id)

    if not normalized_ids:
        return {}

    rows_by_id = {}
    for row in _fetch_video_status_rows_with_client(youtube, normalized_ids):
        video_id = str(row.get("id") or "").strip()
        if video_id:
            rows_by_id[video_id] = row
    return rows_by_id


def _wait_for_live_video_rows_with_client(youtube, video_ids, max_attempts=3, context_label=""):
    ordered_ids = []
    seen_ids = set()
    for value in video_ids or []:
        video_id = _extract_youtube_video_id(value)
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        ordered_ids.append(video_id)

    if not ordered_ids:
        return {}, []

    max_attempts = max(1, int(max_attempts or 1))
    rows_by_id = {}
    missing_ids = list(ordered_ids)
    for attempt_index in range(1, max_attempts + 1):
        rows_by_id = _fetch_video_rows_by_id_with_client(youtube, ordered_ids)
        missing_ids = [video_id for video_id in ordered_ids if video_id not in rows_by_id]
        if not missing_ids or attempt_index >= max_attempts:
            break

        wait_seconds = min(10, 1 + attempt_index)
        if context_label:
            log.warning(
                "[%s] Waiting for YouTube videos to become readable. attempt=%d/%d missing=%s sleep=%ds",
                context_label,
                attempt_index,
                max_attempts,
                ",".join(missing_ids[:10]),
                wait_seconds,
            )
        else:
            log.warning(
                "Waiting for YouTube videos to become readable. attempt=%d/%d missing=%s sleep=%ds",
                attempt_index,
                max_attempts,
                ",".join(missing_ids[:10]),
                wait_seconds,
            )
        time.sleep(wait_seconds)

    return rows_by_id, missing_ids


# ============================================================================
# Video match（原文件行 2158-2200 / 5363-5405）
# ============================================================================

def _build_existing_video_match_from_row(video_row):
    if not isinstance(video_row, dict):
        return {}

    video_id = str(video_row.get("id") or "").strip()
    title = str(((video_row.get("snippet") or {}).get("title") or "")).strip()
    if not video_id or not title:
        return {}

    uploaded_at = _format_youtube_datetime_z((video_row.get("snippet") or {}).get("publishedAt"))
    publish_at = _format_youtube_datetime_z((video_row.get("status") or {}).get("publishAt"))
    return {
        "video_id": video_id,
        "youtube_url": f"https://youtu.be/{video_id}",
        "uploaded_at": uploaded_at,
        "publish_at": publish_at,
        "schedule_reason": "existing_title_match",
        "title": title,
    }


def _normalize_youtube_title_key(title):
    text = " ".join(str(title or "").split()).strip()
    return text.casefold()


def _build_channel_video_title_index_with_client(youtube):
    uploads_playlist_id = _get_youtube_uploads_playlist_id_with_client(youtube)
    video_ids = _list_upload_video_ids_with_client(youtube, uploads_playlist_id)
    rows = _fetch_video_status_rows_with_client(youtube, video_ids)

    title_index = {}
    for row in rows:
        match = _build_existing_video_match_from_row(row)
        if not match:
            continue
        title_key = _normalize_youtube_title_key(match.get("title"))
        if not title_key:
            continue

        previous = title_index.get(title_key)
        current_uploaded = _parse_youtube_datetime(match.get("uploaded_at")) or dt_datetime.min.replace(tzinfo=dt_timezone.utc)
        previous_uploaded = _parse_youtube_datetime(previous.get("uploaded_at")) if previous else None
        previous_uploaded = previous_uploaded or dt_datetime.min.replace(tzinfo=dt_timezone.utc)
        if previous is None or current_uploaded >= previous_uploaded:
            title_index[title_key] = match
    return title_index


def _get_channel_video_title_index(channel_name, force_refresh=False):
    normalized_channel = str(channel_name or "").strip()
    if not normalized_channel:
        return {}

    if force_refresh or normalized_channel not in YOUTUBE_TITLE_MATCH_CACHE:
        youtube = authenticate_youtube_from_supabase(normalized_channel)
        if not youtube:
            return {}
        YOUTUBE_TITLE_MATCH_CACHE[normalized_channel] = _build_channel_video_title_index_with_client(youtube)

    return YOUTUBE_TITLE_MATCH_CACHE.get(normalized_channel, {})


def find_existing_channel_video_by_exact_title(channel_name, title, force_refresh=False):
    normalized_title = str(title or "").strip()[:100]
    title_key = _normalize_youtube_title_key(normalized_title)
    if not title_key:
        return {}

    title_index = _get_channel_video_title_index(channel_name, force_refresh=force_refresh)
    match = title_index.get(title_key, {})
    return dict(match) if isinstance(match, dict) else {}


def remember_existing_channel_video_title_match(channel_name, title, match):
    normalized_channel = str(channel_name or "").strip()
    normalized_title = str(title or "").strip()[:100]
    title_key = _normalize_youtube_title_key(normalized_title)
    if not normalized_channel or not title_key or not isinstance(match, dict):
        return

    cache_bucket = YOUTUBE_TITLE_MATCH_CACHE.setdefault(normalized_channel, {})
    cache_bucket[title_key] = dict(match)


# ============================================================================
# 频道发布排期（原文件行 5443-5607）
# ============================================================================

def _get_youtube_uploads_playlist_id_with_client(youtube):
    response = youtube.channels().list(part="contentDetails", mine=True, maxResults=1).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError("无法读取当前 YouTube 频道信息，未找到 uploads playlist。")

    uploads_playlist_id = (
        ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or ""
    ).strip()
    if not uploads_playlist_id:
        raise RuntimeError("当前 YouTube 频道未返回 uploads playlist ID。")
    return uploads_playlist_id


def _list_upload_video_ids_with_client(youtube, uploads_playlist_id, max_videos=100):
    video_ids = []
    page_token = None
    while True:
        response = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in response.get("items", []):
            video_id = str(((item.get("contentDetails") or {}).get("videoId") or "")).strip()
            if video_id:
                video_ids.append(video_id)
                if len(video_ids) >= max(1, int(max_videos or 100)):
                    return video_ids[: max(1, int(max_videos or 100))]
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return video_ids


def _chunk_items(items, chunk_size):
    for idx in range(0, len(items), chunk_size):
        yield items[idx:idx + chunk_size]


def _fetch_video_status_rows_with_client(youtube, video_ids):
    rows = []
    for chunk in _chunk_items(video_ids, 50):
        response = youtube.videos().list(
            part="snippet,status",
            id=",".join(chunk),
        ).execute()
        rows.extend(response.get("items", []))
    return rows


def _fetch_video_rows_with_localizations_with_client(youtube, video_ids):
    rows = []
    for chunk in _chunk_items(video_ids, 50):
        response = youtube.videos().list(
            part="snippet,localizations",
            id=",".join(chunk),
        ).execute()
        rows.extend(response.get("items", []))
    return rows


def _fetch_single_video_row_with_localizations_with_client(youtube, video_id):
    normalized_video_id = str(video_id or "").strip()
    if not normalized_video_id:
        return {}

    rows = _fetch_video_rows_with_localizations_with_client(youtube, [normalized_video_id])
    return dict(rows[0]) if rows else {}


def _get_effective_published_at_utc(video_row, now_utc):
    status_publish_at = _parse_youtube_datetime((video_row.get("status") or {}).get("publishAt"))
    if status_publish_at is not None:
        if status_publish_at <= now_utc:
            return status_publish_at
        return None

    return _parse_youtube_datetime((video_row.get("snippet") or {}).get("publishedAt"))


def _get_future_scheduled_publish_at_utc(video_row, now_utc):
    status_publish_at = _parse_youtube_datetime((video_row.get("status") or {}).get("publishAt"))
    if status_publish_at is not None and status_publish_at > now_utc:
        return status_publish_at
    return None


def _collect_channel_publish_schedule_facts_with_client(youtube, now_utc):
    uploads_playlist_id = _get_youtube_uploads_playlist_id_with_client(youtube)
    video_ids = _list_upload_video_ids_with_client(youtube, uploads_playlist_id)
    if not video_ids:
        return {
            "uploads_playlist_id": uploads_playlist_id,
            "published_count_by_local_date": {},
            "future_count_by_local_date": {},
            "future_publish_times_by_local_date": {},
            "latest_future_publish_at": None,
            "video_count": 0,
        }

    rows = _fetch_video_status_rows_with_client(youtube, video_ids)
    published_count_by_local_date = {}
    future_count_by_local_date = {}
    future_publish_times_by_local_date = {}
    latest_future_publish_at = None

    for row in rows:
        published_at = _get_effective_published_at_utc(row, now_utc)
        if published_at is not None:
            local_day = published_at.astimezone(YOUTUBE_SCHEDULE_LOCAL_TIMEZONE).date().isoformat()
            published_count_by_local_date[local_day] = published_count_by_local_date.get(local_day, 0) + 1

        future_publish_at = _get_future_scheduled_publish_at_utc(row, now_utc)
        if future_publish_at is not None and (
            latest_future_publish_at is None or future_publish_at > latest_future_publish_at
        ):
            latest_future_publish_at = future_publish_at
        if future_publish_at is not None:
            local_publish_at = future_publish_at.astimezone(YOUTUBE_SCHEDULE_LOCAL_TIMEZONE).replace(microsecond=0)
            local_day = local_publish_at.date().isoformat()
            future_count_by_local_date[local_day] = future_count_by_local_date.get(local_day, 0) + 1
            future_publish_times_by_local_date.setdefault(local_day, []).append(local_publish_at)

    for local_day, items in future_publish_times_by_local_date.items():
        future_publish_times_by_local_date[local_day] = sorted(items)

    return {
        "uploads_playlist_id": uploads_playlist_id,
        "published_count_by_local_date": published_count_by_local_date,
        "future_count_by_local_date": future_count_by_local_date,
        "future_publish_times_by_local_date": future_publish_times_by_local_date,
        "latest_future_publish_at": latest_future_publish_at,
        "video_count": len(rows),
    }


def _get_youtube_daily_publish_limit():
    try:
        limit = int(getattr(cfg, "YOUTUBE_DAILY_PUBLISH_LIMIT", 3) or 3)
    except Exception:
        limit = 3
    return max(1, limit)


def _build_youtube_daily_publish_slots(target_date, base_publish_at_local, daily_limit):
    base_time = base_publish_at_local.timetz().replace(microsecond=0)
    day_start = dt_datetime.combine(target_date, base_time, tzinfo=YOUTUBE_SCHEDULE_LOCAL_TIMEZONE).replace(microsecond=0)
    day_end = day_start.replace(hour=23, minute=55, second=0, microsecond=0)
    if day_end <= day_start:
        day_end = day_start + dt_timedelta(minutes=10 * max(0, daily_limit - 1))

    if daily_limit <= 1:
        return [day_start]

    interval_seconds = max(600, int((day_end - day_start).total_seconds() // max(1, daily_limit - 1)))
    slots = []
    for slot_index in range(daily_limit):
        candidate = day_start + dt_timedelta(seconds=interval_seconds * slot_index)
        if candidate > day_end:
            candidate = day_end
        candidate = candidate.replace(microsecond=0)
        if slots and candidate <= slots[-1]:
            candidate = (slots[-1] + dt_timedelta(minutes=10)).replace(microsecond=0)
        slots.append(candidate)
    return slots


def resolve_youtube_publish_schedule_with_client(youtube, privacy_status="unlisted", schedule_after_hours=0):
    normalized_privacy = str(privacy_status or "unlisted").strip().lower()
    if normalized_privacy != "schedule":
        return {
            "publish_at": "",
            "schedule_reason": "",
            "local_publish_at": "",
            "base_publish_at": "",
            "latest_future_publish_at": "",
        }

    hours = max(1, int(schedule_after_hours or 0))
    now_utc = dt_datetime.now(dt_timezone.utc)
    base_publish_at_utc = (now_utc + dt_timedelta(hours=hours)).replace(microsecond=0)
    base_publish_at_local = base_publish_at_utc.astimezone(YOUTUBE_SCHEDULE_LOCAL_TIMEZONE).replace(microsecond=0)

    facts = _collect_channel_publish_schedule_facts_with_client(youtube, now_utc)
    latest_future_publish_at = facts.get("latest_future_publish_at")
    published_count_by_local_date = facts.get("published_count_by_local_date", {})
    future_count_by_local_date = facts.get("future_count_by_local_date", {})
    future_publish_times_by_local_date = facts.get("future_publish_times_by_local_date", {})
    daily_limit = _get_youtube_daily_publish_limit()

    schedule_reason = "base_schedule"
    final_publish_at_local = base_publish_at_local
    final_publish_at_utc = base_publish_at_utc

    candidate_day = base_publish_at_local.date()
    base_day = candidate_day
    found_slot = False
    for day_offset in range(370):
        current_day = candidate_day + dt_timedelta(days=day_offset)
        local_day_key = current_day.isoformat()
        reserved_count = int(published_count_by_local_date.get(local_day_key, 0) or 0) + int(
            future_count_by_local_date.get(local_day_key, 0) or 0
        )
        if reserved_count >= daily_limit:
            continue

        occupied_times = list(future_publish_times_by_local_date.get(local_day_key, []) or [])
        slots = _build_youtube_daily_publish_slots(current_day, base_publish_at_local, daily_limit)
        earliest_allowed = base_publish_at_local if current_day == base_day else slots[0]
        for slot in slots:
            if slot < earliest_allowed:
                continue
            if any(abs((slot - occupied).total_seconds()) < 60 for occupied in occupied_times):
                continue
            final_publish_at_local = slot
            final_publish_at_utc = slot.astimezone(dt_timezone.utc).replace(microsecond=0)
            schedule_reason = f"daily_slot_{reserved_count + 1}_of_{daily_limit}"
            found_slot = True
            break

        if not found_slot and reserved_count < daily_limit:
            fallback_anchor = max([earliest_allowed] + occupied_times) if occupied_times else earliest_allowed
            fallback_slot = (fallback_anchor + dt_timedelta(minutes=10)).replace(microsecond=0)
            if fallback_slot.date() == current_day:
                final_publish_at_local = fallback_slot
                final_publish_at_utc = fallback_slot.astimezone(dt_timezone.utc).replace(microsecond=0)
                schedule_reason = f"daily_fallback_{reserved_count + 1}_of_{daily_limit}"
                found_slot = True

        if found_slot:
            break

    publish_at = _format_youtube_datetime_z(final_publish_at_utc)
    base_publish_at = _format_youtube_datetime_z(base_publish_at_utc)
    latest_future_text = _format_youtube_datetime_z(latest_future_publish_at) if latest_future_publish_at else ""

    log.info(
        "📅 YouTube 排期决策：reason=%s | 本地发布时间=%s | UTC发布时间=%s | 基础UTC=%s | 最晚未来定时=%s | 已扫描视频=%d",
        schedule_reason,
        final_publish_at_local.isoformat(),
        publish_at,
        base_publish_at,
        latest_future_text or "无",
        int(facts.get("video_count", 0) or 0),
    )

    return {
        "publish_at": publish_at,
        "schedule_reason": schedule_reason,
        "local_publish_at": final_publish_at_local.isoformat(),
        "base_publish_at": base_publish_at,
        "latest_future_publish_at": latest_future_text,
    }


# ============================================================================
# 上传构建（原文件行 5610-5841）
# ============================================================================

def build_youtube_status(privacy_status="unlisted", schedule_after_hours=0, publish_at=""):
    normalized = str(privacy_status or "unlisted").strip().lower()
    if normalized not in {"private", "unlisted", "public", "schedule"}:
        log.warning("未知的 YouTube 隐私设置 '%s'，已回退为 unlisted。", privacy_status)
        normalized = "unlisted"

    if normalized == "schedule":
        if publish_at:
            normalized_publish_at = _format_youtube_datetime_z(publish_at)
            log.info("📅 YouTube 预约公开已启用：使用显式 publishAt (%s)", normalized_publish_at)
            return {
                "privacyStatus": "private",
                "publishAt": normalized_publish_at,
            }

        hours = max(1, int(schedule_after_hours or 0))
        calculated_publish_at = (
            dt_datetime.now(dt_timezone.utc) + dt_timedelta(hours=hours)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        log.info("📅 YouTube 预约公开已启用：%d 小时后自动公开 (%s)", hours, calculated_publish_at)
        return {
            "privacyStatus": "private",
            "publishAt": calculated_publish_at,
        }

    return {
        "privacyStatus": normalized,
    }


# ---- 本地化辅助（原文件行 4876-5069）----

def get_youtube_default_language():
    value = str(getattr(cfg, "YOUTUBE_DEFAULT_LANGUAGE", "zh-CN") or "zh-CN").strip()
    return value or "zh-CN"


def youtube_traditional_localization_enabled():
    return bool(getattr(cfg, "ENABLE_YOUTUBE_TRADITIONAL_LOCALIZATION", True))


def get_youtube_localization_locales():
    raw_value = str(getattr(cfg, "YOUTUBE_LOCALIZATION_LOCALES", "") or "").strip()
    if not raw_value:
        raw_value = get_youtube_traditional_locale()

    default_language = get_youtube_default_language()
    locales = []
    for chunk in raw_value.replace("\r", "\n").split("\n"):
        for part in chunk.split(","):
            locale = str(part or "").strip()
            if not locale or locale == default_language or locale in locales:
                continue
            locales.append(locale)
    return locales


def get_youtube_traditional_locale():
    value = str(getattr(cfg, "YOUTUBE_TRADITIONAL_LOCALE", "zh-TW") or "zh-TW").strip()
    return value or "zh-TW"


def get_youtube_traditional_opencc_config():
    value = str(getattr(cfg, "YOUTUBE_TRADITIONAL_OPENCC_CONFIG", "s2t") or "s2t").strip()
    return value or "s2t"


def youtube_traditional_opencc_auto_install_enabled():
    return bool(getattr(cfg, "ENABLE_AUTO_INSTALL_OPENCC", True))


def _get_youtube_localization_converter(config_name=""):
    config_name = str(config_name or get_youtube_traditional_opencc_config() or "").strip()
    if not config_name:
        return None
    if config_name in YOUTUBE_LOCALIZATION_CONVERTER_CACHE:
        cached = YOUTUBE_LOCALIZATION_CONVERTER_CACHE.get(config_name)
        return cached or None

    try:
        from opencc import OpenCC
    except ImportError:
        if not youtube_traditional_opencc_auto_install_enabled():
            log.warning(
                "OpenCC is unavailable and ENABLE_AUTO_INSTALL_OPENCC is disabled. zh-TW localization will be skipped."
            )
            YOUTUBE_LOCALIZATION_CONVERTER_CACHE[config_name] = False
            return None
        if config_name not in YOUTUBE_LOCALIZATION_INSTALL_ATTEMPTED:
            YOUTUBE_LOCALIZATION_INSTALL_ATTEMPTED.add(config_name)
            try:
                import sys as _sys

                log.warning(
                    "Missing opencc for zh-TW localization. Attempting automatic install: opencc-python-reimplemented"
                )
                install_result = subprocess.run(
                    [_sys.executable, "-m", "pip", "install", "-q", "opencc-python-reimplemented"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if install_result.returncode == 0:
                    from opencc import OpenCC

                    log.info("Installed opencc-python-reimplemented automatically for zh-TW localization.")
                else:
                    detail = (install_result.stderr or install_result.stdout or "").strip()[-500:]
                    log.warning(
                        "Automatic OpenCC install failed. zh-TW localization will be skipped for this run: %s",
                        detail or "no pip output",
                    )
                    YOUTUBE_LOCALIZATION_CONVERTER_CACHE[config_name] = False
                    return None
            except Exception as install_error:
                log.warning(
                    "Unable to auto-install OpenCC. zh-TW localization will be skipped for this run: %s",
                    install_error,
                )
                YOUTUBE_LOCALIZATION_CONVERTER_CACHE[config_name] = False
                return None
        else:
            log.warning(
                "OpenCC is unavailable and auto-install already failed earlier in this run. zh-TW localization will be skipped."
            )
            YOUTUBE_LOCALIZATION_CONVERTER_CACHE[config_name] = False
            return None

    converter = _build_opencc_converter_with_fallback(config_name)
    YOUTUBE_LOCALIZATION_CONVERTER_CACHE[config_name] = converter
    return converter


def _get_youtube_locale_conversion_config(locale):
    normalized_locale = str(locale or "").strip()
    if not normalized_locale:
        return ""
    if normalized_locale == "zh-HK":
        return "s2hk"
    if normalized_locale in {"zh-TW", "zh-Hant"}:
        return get_youtube_traditional_opencc_config()
    return ""


def _build_opencc_converter_with_fallback(config_name):
    try:
        from opencc import OpenCC

        return OpenCC(config_name)
    except Exception as exc:
        fallback_config = get_youtube_traditional_opencc_config()
        if config_name != fallback_config:
            try:
                from opencc import OpenCC

                log.warning(
                    "OpenCC config %s is unavailable. Falling back to %s for YouTube localization: %s",
                    config_name,
                    fallback_config,
                    exc,
                )
                return OpenCC(fallback_config)
            except Exception:
                pass
        raise


def _build_youtube_localization_entry_for_locale(locale, normalized_title, normalized_description):
    conversion_config = _get_youtube_locale_conversion_config(locale)
    if not conversion_config:
        return {
            "title": normalized_title,
            "description": normalized_description,
        }

    converter = _get_youtube_localization_converter(conversion_config)
    if converter is None:
        return None
    return {
        "title": converter.convert(normalized_title),
        "description": converter.convert(normalized_description),
    }


def build_youtube_traditional_localizations(title="", description=""):
    default_language = get_youtube_default_language()
    if not youtube_traditional_localization_enabled():
        return default_language, {}

    normalized_title = str(title or "")[:100]
    normalized_description = str(description or "")[:5000]
    if not normalized_title and not normalized_description:
        return default_language, {}

    target_locales = get_youtube_localization_locales()
    if not target_locales:
        return default_language, {}

    generated = {}
    for target_locale in target_locales:
        entry = _build_youtube_localization_entry_for_locale(
            target_locale,
            normalized_title,
            normalized_description,
        )
        if entry is None:
            continue
        generated[target_locale] = entry
    return default_language, generated


def merge_youtube_localizations(existing_localizations=None, title="", description="", force_overwrite=False):
    merged = dict(existing_localizations or {})
    default_language, generated = build_youtube_traditional_localizations(title=title, description=description)
    if not generated:
        return default_language, merged, False

    changed = False
    for target_locale, localized_entry in generated.items():
        if merged.get(target_locale) and not force_overwrite:
            continue
        if merged.get(target_locale) != localized_entry:
            merged[target_locale] = localized_entry
            changed = True
    return default_language, merged, changed


def _build_youtube_mutable_video_snippet(snippet, default_language=""):
    body_snippet = {
        "title": str((snippet or {}).get("title") or "")[:100],
        "description": str((snippet or {}).get("description") or "")[:5000],
        "defaultLanguage": str((snippet or {}).get("defaultLanguage") or default_language or get_youtube_default_language()).strip(),
    }
    tags = (snippet or {}).get("tags")
    if tags:
        body_snippet["tags"] = list(tags)
    category_id = str((snippet or {}).get("categoryId") or "").strip()
    if category_id:
        body_snippet["categoryId"] = category_id
    return body_snippet


# ---- 上传体与执行 ----

def _build_video_upload_request_body(title, description, tags, privacy_status="unlisted", category_id="",
                                     schedule_after_hours=0, publish_at=""):
    tags_list = normalize_youtube_tags(tags)
    normalized_category_id = normalize_youtube_category_id(category_id)
    default_language, _generated_localizations = build_youtube_traditional_localizations(title=title, description=description)
    snippet = {
        "title": title[:100],
        "description": description[:5000],
        "defaultLanguage": default_language,
    }

    if tags_list:
        snippet["tags"] = tags_list
    if normalized_category_id:
        snippet["categoryId"] = normalized_category_id
        log.info("YouTube 分类已设置为: %s", normalized_category_id)
    else:
        log.info("YOUTUBE_CATEGORY_ID 留空，上传时不设置 categoryId。")

    return {
        "snippet": snippet,
        "status": build_youtube_status(privacy_status, schedule_after_hours, publish_at=publish_at),
    }


def _upload_to_youtube_with_client(
    youtube,
    video_path,
    title,
    description,
    tags,
    cover_path,
    privacy_status="unlisted",
    category_id="",
    schedule_after_hours=0,
    publish_at="",
    schedule_reason="",
):
    body = _build_video_upload_request_body(
        title=title,
        description=description,
        tags=tags,
        privacy_status=privacy_status,
        category_id=category_id,
        schedule_after_hours=schedule_after_hours,
        publish_at=publish_at,
    )

    log.info("🚀 开启跨国深空打孔传送视频大本尊: %s", os.path.basename(video_path))
    media = MediaFileUpload(video_path, chunksize=1024 * 1024 * 20, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retry_count = 0
    max_retries = 5
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info("   ⏳ [发送塔台播报进度]：%d%%", int(status.progress() * 100))
        except HttpError as e:
            status_code = getattr(getattr(e, "resp", None), "status", None)
            if status_code in {500, 502, 503, 504} and retry_count < max_retries:
                retry_count += 1
                wait = 2 ** retry_count
                log.warning("⚠️ 上传分片遭遇 HTTP %s，准备第 %d 次重试，等待 %d 秒...", status_code, retry_count, wait)
                time.sleep(wait)
                continue
            raise

    video_id = response["id"]
    uploaded_at = dt_datetime.now(dt_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    youtube_url = f"https://youtu.be/{video_id}"
    log.info("🎉 本身巨盒已被 Youtube 安全收纳进柜里！影视 ID 为: %s", video_id)

    if cover_path and os.path.exists(cover_path):
        safe_cover = compress_thumbnail_to_safe_limit(cover_path)
        log.info("🖼 拦截成功！对换新生成的特种压缩皮肤套向官网推去覆盖...")
        max_thumb_retries = 3
        for attempt in range(1, max_thumb_retries + 1):
            try:
                thumb_req = youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(safe_cover),
                )
                thumb_req.execute()
                log.info("🎉 Youtube 收下了我们的大画幅爆款神眼罩！前置门面搭建收工完毕！")
                break
            except HttpError as e:
                if attempt < max_thumb_retries:
                    log.warning("⚠️ 海报被 YouTube 大气网拦下(第 %d 遭打)，下落 5 秒冷凝...", attempt)
                    time.sleep(5)
                else:
                    log.error("❌ 尽管做尽处理，但这块门面历经 %d 轮抛投后依然被封杀: %s", max_thumb_retries, e)

    localization_sync = _sync_video_localizations_with_client(
        youtube,
        video_id,
        title=title,
        description=description,
        force_overwrite=False,
    )
    if localization_sync.get("applied_locales"):
        log.info(
            "Video localizations applied for %s: %s",
            video_id,
            ", ".join(localization_sync.get("applied_locales", [])),
        )
    if localization_sync.get("failed_locales"):
        log.warning(
            "Video localization sync partially failed for %s; continuing upload success path. failed=%s",
            video_id,
            json.dumps(localization_sync.get("failed_locales", {}), ensure_ascii=False),
        )

    return {
        "video_id": video_id,
        "youtube_url": youtube_url,
        "uploaded_at": uploaded_at,
        "title": title[:100],
        "publish_at": _format_youtube_datetime_z(publish_at) if publish_at else "",
        "schedule_reason": str(schedule_reason or ""),
        "localizations_applied": localization_sync.get("applied_locales", []),
        "localizations_failed": localization_sync.get("failed_locales", {}),
    }


def _upload_via_youtube_relay(
    video_path,
    title,
    description,
    tags,
    cover_path,
    channel_name,
    privacy_status="unlisted",
    category_id="",
    schedule_after_hours=0,
):
    """通过 VPS 中继获取 access_token，直连 YouTube Data API 上传视频。

    HF Worker 不持有 refresh_token，向 VPS 中继请求短期 access_token +
    排期决策，然后直接向 YouTube 上传大文件（不经 VPS 中转）。

    流程：
    1. GET VPS /yt-api/<channel>/token → access_token + publish_at + schedule_reason
    2. 用 access_token 构建 YouTube 客户端
    3. 直连 YouTube Data API 上传视频 + 封面 + 本地化
    4. 返回与 _upload_to_youtube_with_client 相同格式的 dict，或 False
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    oauth_base = str(getattr(cfg, "YOUTUBE_OAUTH_BASE", "") or "").strip().rstrip("/")
    if not oauth_base:
        log.error("❌ YOUTUBE_OAUTH_BASE 未配置，无法使用中继上传。")
        return False

    # ── 1. 向 VPS 请求 access_token + 排期决策 ──
    token_url = f"{oauth_base}/{channel_name}/token"
    params = {
        "privacy_status": str(privacy_status or "unlisted"),
        "schedule_after_hours": str(int(schedule_after_hours or 0)),
    }
    log.info("🛰️ [token中继] 向 VPS 请求 access_token: %s", token_url)

    try:
        resp = requests.get(token_url, params=params, timeout=30)
        if resp.status_code != 200:
            log.error("❌ [token中继] HTTP %d: %s", resp.status_code, resp.text[:500])
            return False
        token_data = resp.json()
        if not token_data.get("success"):
            log.error("❌ [token中继] VPS 返回失败: %s", token_data.get("error", "未知错误"))
            return False
    except Exception as e:
        log.error("❌ [token中继] 请求 VPS 失败: %s", e)
        return False

    access_token = token_data.get("access_token", "")
    publish_at = token_data.get("publish_at", "")
    schedule_reason = token_data.get("schedule_reason", "")
    if not access_token:
        log.error("❌ [token中继] VPS 未返回 access_token")
        return False
    log.info("✅ [token中继] access_token 获取成功，排期: %s", publish_at or "无")

    # ── 2. 用 access_token 构建 YouTube 客户端 ──
    try:
        credentials = Credentials(
            token=access_token,
            scopes=["https://www.googleapis.com/auth/youtube"],
        )
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    except Exception as e:
        log.error("❌ [token中继] 构建 YouTube 客户端失败: %s", e)
        return False

    # ── 3. 直连 YouTube 上传（复用已有逻辑）──
    try:
        upload_result = _upload_to_youtube_with_client(
            youtube=youtube,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            cover_path=cover_path,
            privacy_status=privacy_status,
            category_id=category_id,
            schedule_after_hours=schedule_after_hours,
            publish_at=publish_at,
            schedule_reason=schedule_reason,
        )
        if upload_result:
            log.info("🎉 [token中继] 直连上传成功! video_id=%s", upload_result.get("video_id", ""))
        return upload_result
    except Exception as e:
        log.error("❌ [token中继] 直连上传失败: %s", e)
        return False


def upload_to_youtube_detailed(
    video_path,
    title,
    description,
    tags,
    cover_path,
    channel_name,
    privacy_status="unlisted",
    category_id="",
    schedule_after_hours=0,
):
    if not channel_name:
        log.error("未指定信标频道代码，自动丢弃发行工作。")
        return False

    # ── 中继模式：HF Worker 不持有 YouTube OAuth 凭证，经 VPS 中继上传 ──
    oauth_base = str(getattr(cfg, "YOUTUBE_OAUTH_BASE", "") or "").strip()
    if oauth_base:
        log.info("🛰️ 检测到 YOUTUBE_OAUTH_BASE 配置，启用 VPS 中继上传模式。")
        relay_result = _upload_via_youtube_relay(
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            cover_path=cover_path,
            channel_name=channel_name,
            privacy_status=privacy_status,
            category_id=category_id,
            schedule_after_hours=schedule_after_hours,
        )
        if relay_result:
            remember_existing_channel_video_title_match(channel_name, title, relay_result)
        return relay_result

    # ── 直连模式：本机自跑，持本地凭证直接调用 YouTube API ──
    youtube = authenticate_youtube_from_supabase(channel_name)
    if not youtube:
        return False

    try:
        existing_match = find_existing_channel_video_by_exact_title(channel_name, title)
        if existing_match:
            log.info("检测到频道内已存在同标题视频，直接复用并跳过上传：%s", str(title or "").strip()[:100])
            remember_existing_channel_video_title_match(channel_name, title, existing_match)
            return existing_match

        resolved_schedule = resolve_youtube_publish_schedule_with_client(
            youtube,
            privacy_status=privacy_status,
            schedule_after_hours=schedule_after_hours,
        )
        upload_result = _upload_to_youtube_with_client(
            youtube=youtube,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            cover_path=cover_path,
            privacy_status=privacy_status,
            category_id=category_id,
            schedule_after_hours=schedule_after_hours,
            publish_at=resolved_schedule.get("publish_at", ""),
            schedule_reason=resolved_schedule.get("schedule_reason", ""),
        )
        if upload_result:
            remember_existing_channel_video_title_match(channel_name, title, upload_result)
        return upload_result
    except Exception as e:
        log.error("❌ 主力信封管线在传输时遭受强击崩溃: %s", e)
        return False


def upload_to_youtube(
    video_path,
    title,
    description,
    tags,
    cover_path,
    channel_name,
    privacy_status="unlisted",
    category_id="",
    schedule_after_hours=0,
):
    result = upload_to_youtube_detailed(
        video_path=video_path,
        title=title,
        description=description,
        tags=tags,
        cover_path=cover_path,
        channel_name=channel_name,
        privacy_status=privacy_status,
        category_id=category_id,
        schedule_after_hours=schedule_after_hours,
    )
    return result.get("youtube_url") if isinstance(result, dict) else False


# ============================================================================
# 播放列表（原文件行 5843-6034，第一版；会被 podcast.py 注新版本）
# ============================================================================

def normalize_playlist_privacy_status(privacy_status="public"):
    normalized = str(privacy_status or "public").strip().lower()
    if normalized not in {"private", "unlisted", "public"}:
        log.warning("未知的播放列表隐私设置 '%s'，已回退为 public。", privacy_status)
        normalized = "public"
    return normalized


def is_playlist_not_found_http_error(error):
    if not isinstance(error, HttpError):
        return False

    status_code = getattr(getattr(error, "resp", None), "status", None)
    raw_text = str(error)
    if "playlistNotFound" in raw_text:
        return True

    try:
        content = getattr(error, "content", b"")
        if isinstance(content, bytes):
            payload = json.loads(content.decode("utf-8", errors="ignore"))
        elif isinstance(content, str):
            payload = json.loads(content)
        else:
            payload = {}
        reasons = [
            str(item.get("reason") or "").strip()
            for item in ((payload.get("error") or {}).get("errors") or [])
            if isinstance(item, dict)
        ]
        if "playlistNotFound" in reasons:
            return True
    except Exception:
        pass

    return status_code == 404 and "playlistId" in raw_text


def _create_or_update_playlist_with_client(youtube, title, description="", privacy_status="public", playlist_id=""):
    """（第一版 — 会被 podcast.py 的版本覆盖）"""
    normalized_privacy = normalize_playlist_privacy_status(privacy_status)
    default_language, _generated_localizations = build_youtube_traditional_localizations(title=title, description=description)
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

    if playlist_id:
        body["id"] = playlist_id
        response = youtube.playlists().update(part="snippet,status", body=body).execute()
    else:
        response = youtube.playlists().insert(part="snippet,status", body=body).execute()

    final_playlist_id = response["id"]
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
    return {
        "playlist_id": final_playlist_id,
        "playlist_url": f"https://www.youtube.com/playlist?list={final_playlist_id}",
        "title": body["snippet"]["title"],
        "description": body["snippet"]["description"],
        "privacy_status": normalized_privacy,
        "localizations_applied": localization_sync.get("applied_locales", []),
        "localizations_failed": localization_sync.get("failed_locales", {}),
    }


def _list_playlist_items_with_client(youtube, playlist_id):
    """（第一版 — 会被 podcast.py 的版本覆盖）"""
    items = []
    page_token = None
    playlist_not_found_retry_count = 0
    max_playlist_not_found_retries = 6
    while True:
        try:
            response = youtube.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            ).execute()
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
                items = []
                continue
            raise

        for item in response.get("items", []):
            resource = ((item.get("snippet") or {}).get("resourceId") or {})
            video_id = resource.get("videoId") or (item.get("contentDetails") or {}).get("videoId") or ""
            items.append(
                {
                    "playlist_item_id": item.get("id", ""),
                    "video_id": video_id,
                    "position": int((item.get("snippet") or {}).get("position", 0) or 0),
                }
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def _list_owned_playlists_with_client(youtube):
    """（第一版 — 会被 podcast.py 的版本覆盖）"""
    playlists = []
    page_token = None
    while True:
        response = youtube.playlists().list(
            part="snippet,status",
            mine=True,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in response.get("items", []):
            playlists.append(
                {
                    "playlist_id": str(item.get("id") or "").strip(),
                    "playlist_url": f"https://www.youtube.com/playlist?list={str(item.get('id') or '').strip()}",
                    "title": str((item.get("snippet") or {}).get("title") or "").strip(),
                    "description": str((item.get("snippet") or {}).get("description") or ""),
                    "privacy_status": normalize_playlist_privacy_status(
                        (item.get("status") or {}).get("privacyStatus") or "public"
                    ),
                }
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return playlists


def _list_owned_playlist_rows_with_localizations_with_client(youtube):
    rows = []
    page_token = None
    while True:
        response = youtube.playlists().list(
            part="snippet,status,localizations",
            mine=True,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        rows.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return rows


def _load_playlist_localizations_with_client(youtube, playlist_id):
    normalized_playlist_id = str(playlist_id or "").strip()
    if not normalized_playlist_id:
        return {}

    response = youtube.playlists().list(
        part="localizations",
        id=normalized_playlist_id,
        maxResults=1,
    ).execute()
    items = response.get("items", [])
    if not items:
        return {}
    return dict(items[0].get("localizations") or {})


def _fetch_single_playlist_row_with_localizations_with_client(youtube, playlist_id):
    normalized_playlist_id = str(playlist_id or "").strip()
    if not normalized_playlist_id:
        return {}

    playlist_not_found_retry_count = 0
    max_playlist_not_found_retries = 6
    while True:
        try:
            response = youtube.playlists().list(
                part="snippet,localizations",
                id=normalized_playlist_id,
                maxResults=1,
            ).execute()
        except HttpError as e:
            if is_playlist_not_found_http_error(e) and playlist_not_found_retry_count < max_playlist_not_found_retries:
                playlist_not_found_retry_count += 1
                wait_seconds = min(12, 2 + playlist_not_found_retry_count)
                log.warning(
                    "播放列表 %s 暂时还不可读，等待 %d 秒后重试读取（%d/%d）...",
                    normalized_playlist_id,
                    wait_seconds,
                    playlist_not_found_retry_count,
                    max_playlist_not_found_retries,
                )
                time.sleep(wait_seconds)
                continue
            raise

        items = response.get("items", [])
        if items:
            return dict(items[0])
        if playlist_not_found_retry_count < max_playlist_not_found_retries:
            playlist_not_found_retry_count += 1
            wait_seconds = min(12, 2 + playlist_not_found_retry_count)
            log.warning(
                "播放列表 %s 暂时还不可读，等待 %d 秒后重试读取（%d/%d）...",
                normalized_playlist_id,
                wait_seconds,
                playlist_not_found_retry_count,
                max_playlist_not_found_retries,
            )
            time.sleep(wait_seconds)
            continue
        return {}


# ============================================================================
# 本地化同步（原文件行 6079-6371）
# ============================================================================

def _sync_video_localizations_with_client(youtube, video_id, title="", description="", force_overwrite=False):
    normalized_video_id = str(video_id or "").strip()
    if not normalized_video_id:
        return {"applied_locales": [], "skipped_locales": [], "failed_locales": {}}

    video_row = _fetch_single_video_row_with_localizations_with_client(youtube, normalized_video_id)
    if not video_row:
        log.warning("Unable to fetch uploaded video row for localization sync: video_id=%s", normalized_video_id)
        return {"applied_locales": [], "skipped_locales": [], "failed_locales": {}}

    snippet = dict(video_row.get("snippet") or {})
    effective_title = str(title or snippet.get("title") or "")[:100]
    effective_description = str(description or snippet.get("description") or "")[:5000]
    default_language, generated = build_youtube_traditional_localizations(
        title=effective_title,
        description=effective_description,
    )
    if not generated:
        return {"applied_locales": [], "skipped_locales": [], "failed_locales": {}}

    base_snippet = _build_youtube_mutable_video_snippet(snippet, default_language=default_language)
    base_snippet["title"] = effective_title
    base_snippet["description"] = effective_description
    current_localizations = dict(video_row.get("localizations") or {})
    applied_locales = []
    skipped_locales = []
    failed_locales = {}

    for target_locale, localized_entry in generated.items():
        if current_localizations.get(target_locale) and not force_overwrite:
            skipped_locales.append(target_locale)
            continue
        if current_localizations.get(target_locale) == localized_entry:
            skipped_locales.append(target_locale)
            continue

        body = {
            "id": normalized_video_id,
            "snippet": dict(base_snippet),
            "localizations": dict(current_localizations),
        }
        body["localizations"][target_locale] = localized_entry
        try:
            youtube.videos().update(part="snippet,localizations", body=body).execute()
            current_localizations[target_locale] = localized_entry
            applied_locales.append(target_locale)
        except HttpError as e:
            failed_locales[target_locale] = str(e)
            log.warning(
                "Skipping rejected video localization locale=%s video_id=%s title=%s error=%s",
                target_locale,
                normalized_video_id,
                effective_title,
                e,
            )
        except Exception as e:
            failed_locales[target_locale] = str(e)
            log.warning(
                "Video localization sync failed locale=%s video_id=%s title=%s error=%s",
                target_locale,
                normalized_video_id,
                effective_title,
                e,
            )

    return {
        "applied_locales": applied_locales,
        "skipped_locales": skipped_locales,
        "failed_locales": failed_locales,
    }


def _sync_playlist_localizations_with_client(youtube, playlist_id, title="", description="", force_overwrite=False):
    normalized_playlist_id = str(playlist_id or "").strip()
    if not normalized_playlist_id:
        return {"applied_locales": [], "skipped_locales": [], "failed_locales": {}}

    playlist_row = _fetch_single_playlist_row_with_localizations_with_client(youtube, normalized_playlist_id)
    if not playlist_row:
        log.warning("Unable to fetch playlist row for localization sync after retries: playlist_id=%s", normalized_playlist_id)
        return {"applied_locales": [], "skipped_locales": [], "failed_locales": {}}

    snippet = dict(playlist_row.get("snippet") or {})
    effective_title = str(title or snippet.get("title") or "")[:150]
    effective_description = str(description or snippet.get("description") or "")[:5000]
    default_language, generated = build_youtube_traditional_localizations(
        title=effective_title,
        description=effective_description,
    )
    if not generated:
        return {"applied_locales": [], "skipped_locales": [], "failed_locales": {}}

    base_snippet = {
        "title": effective_title,
        "description": effective_description,
        "defaultLanguage": str(
            snippet.get("defaultLanguage") or default_language or get_youtube_default_language()
        ).strip(),
    }
    current_localizations = dict(playlist_row.get("localizations") or {})
    applied_locales = []
    skipped_locales = []
    failed_locales = {}

    for target_locale, localized_entry in generated.items():
        if current_localizations.get(target_locale) and not force_overwrite:
            skipped_locales.append(target_locale)
            continue
        if current_localizations.get(target_locale) == localized_entry:
            skipped_locales.append(target_locale)
            continue

        body = {
            "id": normalized_playlist_id,
            "snippet": dict(base_snippet),
            "localizations": dict(current_localizations),
        }
        body["localizations"][target_locale] = localized_entry
        try:
            youtube.playlists().update(part="snippet,localizations", body=body).execute()
            current_localizations[target_locale] = localized_entry
            applied_locales.append(target_locale)
        except HttpError as e:
            failed_locales[target_locale] = str(e)
            log.warning(
                "Skipping rejected playlist localization locale=%s playlist_id=%s title=%s error=%s",
                target_locale,
                normalized_playlist_id,
                effective_title,
                e,
            )
        except Exception as e:
            failed_locales[target_locale] = str(e)
            log.warning(
                "Playlist localization sync failed locale=%s playlist_id=%s title=%s error=%s",
                target_locale,
                normalized_playlist_id,
                effective_title,
                e,
            )

    return {
        "applied_locales": applied_locales,
        "skipped_locales": skipped_locales,
        "failed_locales": failed_locales,
    }


def _build_playlist_localizations_update_body_from_row(playlist_row, force_overwrite=False):
    if not isinstance(playlist_row, dict):
        return {}

    playlist_id = str(playlist_row.get("id") or "").strip()
    snippet = dict(playlist_row.get("snippet") or {})
    title = str(snippet.get("title") or "")[:150]
    description = str(snippet.get("description") or "")[:5000]
    if not playlist_id or (not title and not description):
        return {}

    default_language, merged_localizations, changed = merge_youtube_localizations(
        existing_localizations=playlist_row.get("localizations") or {},
        title=title,
        description=description,
        force_overwrite=force_overwrite,
    )
    if not changed:
        return {}

    return {
        "id": playlist_id,
        "snippet": {
            "title": title,
            "description": description,
            "defaultLanguage": str(snippet.get("defaultLanguage") or default_language or get_youtube_default_language()).strip(),
        },
        "localizations": merged_localizations,
    }


def _build_video_localizations_update_body_from_row(video_row, force_overwrite=False):
    if not isinstance(video_row, dict):
        return {}

    video_id = str(video_row.get("id") or "").strip()
    snippet = dict(video_row.get("snippet") or {})
    title = str(snippet.get("title") or "")[:100]
    description = str(snippet.get("description") or "")[:5000]
    if not video_id or (not title and not description):
        return {}

    default_language, merged_localizations, changed = merge_youtube_localizations(
        existing_localizations=video_row.get("localizations") or {},
        title=title,
        description=description,
        force_overwrite=force_overwrite,
    )
    if not changed:
        return {}

    return {
        "id": video_id,
        "snippet": _build_youtube_mutable_video_snippet(snippet, default_language=default_language),
        "localizations": merged_localizations,
    }


def backfill_youtube_traditional_localizations(
    channel_name="",
    apply=False,
    max_videos=0,
    include_videos=True,
    include_playlists=True,
    force_overwrite=False,
):
    normalized_channel = str(channel_name or getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    if not normalized_channel:
        raise RuntimeError("YOUTUBE_CHANNEL_NAME is required to backfill YouTube localizations.")

    youtube = authenticate_youtube_from_supabase(normalized_channel)
    if not youtube:
        raise RuntimeError(f"Unable to initialize YouTube client for channel {normalized_channel!r}.")

    summary = {
        "channel_name": normalized_channel,
        "apply": bool(apply),
        "video_updated": 0,
        "video_skipped": 0,
        "playlist_updated": 0,
        "playlist_skipped": 0,
        "target_locales": get_youtube_localization_locales(),
        "default_language": get_youtube_default_language(),
    }

    if include_videos:
        uploads_playlist_id = _get_youtube_uploads_playlist_id_with_client(youtube)
        video_limit = int(max_videos or 0)
        video_ids = _list_upload_video_ids_with_client(
            youtube,
            uploads_playlist_id,
            max_videos=video_limit if video_limit > 0 else 10 ** 9,
        )
        for video_row in _fetch_video_rows_with_localizations_with_client(youtube, video_ids):
            body = _build_video_localizations_update_body_from_row(video_row, force_overwrite=force_overwrite)
            if not body:
                summary["video_skipped"] += 1
                continue
            if apply:
                youtube.videos().update(part="snippet,localizations", body=body).execute()
                log.info(
                    "Updated Chinese locale localizations for video %s: %s",
                    body.get("id"),
                    str((body.get("snippet") or {}).get("title") or ""),
                )
            else:
                log.info(
                    "Dry-run: video %s would receive Chinese locale localizations: %s",
                    body.get("id"),
                    str((body.get("snippet") or {}).get("title") or ""),
                )
            summary["video_updated"] += 1

    if include_playlists:
        builtin_playlist_ids = _get_builtin_playlist_ids_with_client(youtube)
        for playlist_row in _list_owned_playlist_rows_with_localizations_with_client(youtube):
            playlist_id = str(playlist_row.get("id") or "").strip()
            if playlist_id in builtin_playlist_ids:
                summary["playlist_skipped"] += 1
                continue
            body = _build_playlist_localizations_update_body_from_row(
                playlist_row,
                force_overwrite=force_overwrite,
            )
            if not body:
                summary["playlist_skipped"] += 1
                continue
            if apply:
                youtube.playlists().update(part="snippet,localizations", body=body).execute()
                log.info(
                    "Updated Chinese locale localizations for playlist %s: %s",
                    body.get("id"),
                    str((body.get("snippet") or {}).get("title") or ""),
                )
            else:
                log.info(
                    "Dry-run: playlist %s would receive Chinese locale localizations: %s",
                    body.get("id"),
                    str((body.get("snippet") or {}).get("title") or ""),
                )
            summary["playlist_updated"] += 1

    log.info("YouTube Chinese locale localization backfill summary: %s", summary)
    return summary


def _get_builtin_playlist_ids_with_client(youtube):
    """返回频道内置播放列表（watch later, likes 等）的 ID 集合，避免 backfill 时误改。"""
    builtin = set()
    try:
        for playlist_name in ("WL", "LL", "FL", "HL"):
            try:
                response = youtube.channels().list(part="contentDetails", mine=True, maxResults=1).execute()
                related = ((response.get("items", [{}])[0].get("contentDetails") or {}).get("relatedPlaylists") or {})
                pid = str(related.get(playlist_name.lower()) or "").strip()
                if pid:
                    builtin.add(pid)
            except Exception:
                pass
    except Exception:
        pass
    return builtin


# ============================================================================
# 播放列表同步（原文件行 6374-6601）
# ============================================================================

def _find_matching_owned_playlist_with_client(youtube, title, ordered_video_ids=None, privacy_status="public"):
    normalized_title = str(title or "").strip()
    desired_video_ids = [str(video_id).strip() for video_id in (ordered_video_ids or []) if str(video_id).strip()]
    normalized_privacy = normalize_playlist_privacy_status(privacy_status)
    if not normalized_title:
        return {}

    title_matches = []
    for playlist in _list_owned_playlists_with_client(youtube):
        if str(playlist.get("title") or "").strip() != normalized_title:
            continue
        title_matches.append(playlist)

    if not title_matches:
        return {}

    exact_content_match = {}
    privacy_match = {}
    for playlist in title_matches:
        playlist_id = str(playlist.get("playlist_id") or "").strip()
        if not playlist_id:
            continue
        if desired_video_ids:
            try:
                playlist_items = _list_playlist_items_with_client(youtube, playlist_id)
            except Exception:
                playlist_items = []
            existing_video_ids = [str(item.get("video_id") or "").strip() for item in playlist_items if str(item.get("video_id") or "").strip()]
            if existing_video_ids == desired_video_ids:
                exact_content_match = playlist
                if str(playlist.get("privacy_status") or "").strip().lower() == normalized_privacy:
                    return playlist
        if not privacy_match and str(playlist.get("privacy_status") or "").strip().lower() == normalized_privacy:
            privacy_match = playlist

    if exact_content_match:
        return exact_content_match
    if privacy_match:
        return privacy_match
    return title_matches[0]


def _delete_playlist_item_with_client(youtube, playlist_item_id):
    """（第一版 — 会被 podcast.py 的版本覆盖）"""
    youtube.playlistItems().delete(id=playlist_item_id).execute()


def _insert_playlist_video_with_client(youtube, playlist_id, video_id):
    """（第一版 — 会被 podcast.py 的版本覆盖）"""
    response = youtube.playlistItems().insert(
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
    ).execute()
    return {
        "playlist_item_id": response.get("id", ""),
        "video_id": video_id,
    }


def _update_playlist_item_position_with_client(youtube, playlist_item_id, playlist_id, video_id, position):
    """（第一版 — 会被 podcast.py 的版本覆盖）"""
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
    ).execute()


def _sync_playlist_via_youtube_relay(
    channel_name, title, description, ordered_video_ids, privacy_status, playlist_id,
):
    """通过 VPS 中继同步 YouTube 播放列表（HF Worker 外包模式）。

    HF Worker 不持有 YouTube OAuth 凭证，将播放列表同步请求以 JSON
    POST 到 VPS 中继 /yt-api/<channel>/playlist-sync，由 VPS 持本地凭证
    执行完整的播放列表创建/更新/视频添加/排序逻辑。

    返回与 sync_youtube_playlist 相同格式的 dict。
    """
    oauth_base = str(getattr(cfg, "YOUTUBE_OAUTH_BASE", "") or "").strip().rstrip("/")
    if not oauth_base:
        log.error("❌ YOUTUBE_OAUTH_BASE 未配置，无法使用中继同步播放列表。")
        return False

    sync_url = f"{oauth_base}/{channel_name}/playlist-sync"
    log.info("🛰️ [中继播放列表] 通过 VPS 中继同步: %s → %s", str(title or "")[:60], sync_url)

    payload = {
        "title": str(title or "")[:150],
        "description": str(description or "")[:5000],
        "ordered_video_ids": [str(v).strip() for v in ordered_video_ids if str(v).strip()],
        "privacy_status": str(privacy_status or "public"),
        "playlist_id": str(playlist_id or ""),
    }

    try:
        resp = requests.post(sync_url, json=payload, timeout=300)
        if resp.status_code != 200:
            log.error("❌ [中继播放列表] HTTP %d: %s", resp.status_code, resp.text[:500])
            return {
                "playlist_id": str(playlist_id or ""),
                "playlist_url": f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else "",
                "title": str(title or "")[:150],
                "description": str(description or "")[:5000],
                "privacy_status": normalize_playlist_privacy_status(privacy_status),
                "success": False,
                "error": f"中继 HTTP {resp.status_code}",
            }

        data = resp.json()
        # 中继返回的格式与 sync_youtube_playlist 一致
        if not data.get("success"):
            log.error("❌ [中继播放列表] 中继返回失败: %s", data.get("error", "未知错误"))
        else:
            log.info("🎉 [中继播放列表] 同步成功! playlist_id=%s", data.get("playlist_id", ""))
        return data

    except requests.exceptions.ConnectionError as e:
        log.error("❌ [中继播放列表] 连接 VPS 中继失败: %s", e)
        return {
            "playlist_id": str(playlist_id or ""),
            "playlist_url": "",
            "title": str(title or "")[:150],
            "description": str(description or "")[:5000],
            "privacy_status": normalize_playlist_privacy_status(privacy_status),
            "success": False,
            "error": f"中继连接失败: {e}",
        }
    except Exception as e:
        log.error("❌ [中继播放列表] 异常: %s", e)
        return {
            "playlist_id": str(playlist_id or ""),
            "playlist_url": "",
            "title": str(title or "")[:150],
            "description": str(description or "")[:5000],
            "privacy_status": normalize_playlist_privacy_status(privacy_status),
            "success": False,
            "error": str(e),
        }


def sync_youtube_playlist(channel_name, title, description, ordered_video_ids, privacy_status="public", playlist_id=""):
    if not channel_name:
        log.error("未指定信标频道代码，无法同步 YouTube 播放列表。")
        return False

    ordered_video_ids = [str(video_id).strip() for video_id in ordered_video_ids if str(video_id).strip()]
    if not ordered_video_ids:
        log.warning("播放列表同步跳过：没有可加入的 YouTube 视频 ID。")
        return False

    # ── 中继模式：HF Worker 不持有 YouTube OAuth 凭证，经 VPS 中继同步 ──
    oauth_base = str(getattr(cfg, "YOUTUBE_OAUTH_BASE", "") or "").strip()
    if oauth_base:
        log.info("🛰️ 检测到 YOUTUBE_OAUTH_BASE 配置，启用 VPS 中继播放列表同步。")
        return _sync_playlist_via_youtube_relay(
            channel_name=channel_name,
            title=title,
            description=description,
            ordered_video_ids=ordered_video_ids,
            privacy_status=privacy_status,
            playlist_id=playlist_id,
        )

    # ── 直连模式：本机自跑，持本地凭证直接调用 YouTube API ──
    youtube = authenticate_youtube_from_supabase(channel_name)
    if not youtube:
        return False

    playlist_result = {
        "playlist_id": str(playlist_id or ""),
        "playlist_url": f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else "",
        "title": str(title or "")[:150],
        "description": str(description or "")[:5000],
        "privacy_status": normalize_playlist_privacy_status(privacy_status),
        "success": False,
        "error": "",
    }
    live_rows_by_id, missing_video_ids = _wait_for_live_video_rows_with_client(
        youtube,
        ordered_video_ids,
        max_attempts=3,
        context_label=str(title or "").strip()[:80] or "playlist-sync",
    )
    if missing_video_ids:
        playlist_result["error"] = (
            "One or more uploaded YouTube videos are no longer accessible: "
            + ",".join(missing_video_ids)
        )
        log.error(
            "Cannot sync YouTube playlist because some uploaded videos are missing. title=%s missing_video_ids=%s",
            str(title or "").strip()[:150],
            ",".join(missing_video_ids),
        )
        return playlist_result

    original_playlist_id = str(playlist_id or "").strip()
    if not original_playlist_id:
        recovered_playlist = _find_matching_owned_playlist_with_client(
            youtube,
            title=title,
            ordered_video_ids=ordered_video_ids,
            privacy_status=privacy_status,
        )
        recovered_playlist_id = str(recovered_playlist.get("playlist_id") or "").strip() if isinstance(recovered_playlist, dict) else ""
        if recovered_playlist_id:
            playlist_id = recovered_playlist_id
            playlist_result.update(recovered_playlist)
            log.info(
                "Detected an existing owned playlist with the same title and adopted it for sync: playlist_id=%s title=%s",
                recovered_playlist_id,
                str(recovered_playlist.get("title") or title or ""),
            )

    for attempt_index in range(2):
        current_video_id = ""
        current_action = ""
        try:
            playlist_result = _create_or_update_playlist_with_client(
                youtube,
                title=title,
                description=description,
                privacy_status=privacy_status,
                playlist_id=playlist_id,
            )
            playlist_id = playlist_result["playlist_id"]
            desired_set = set(ordered_video_ids)

            existing_items = _list_playlist_items_with_client(youtube, playlist_id)
            grouped_items = {}
            for item in existing_items:
                grouped_items.setdefault(item["video_id"], []).append(item)

            for video_id, items in grouped_items.items():
                items.sort(key=lambda x: x["position"])
                items_to_delete = []
                if video_id not in desired_set:
                    items_to_delete = items
                elif len(items) > 1:
                    items_to_delete = items[1:]

                for item in items_to_delete:
                    if item.get("playlist_item_id"):
                        _delete_playlist_item_with_client(youtube, item["playlist_item_id"])

            existing_items = _list_playlist_items_with_client(youtube, playlist_id)
            existing_video_ids = {item["video_id"] for item in existing_items}
            for video_id in ordered_video_ids:
                if video_id not in existing_video_ids:
                    current_video_id = video_id
                    current_action = "insert"
                    _insert_playlist_video_with_client(youtube, playlist_id, video_id)

            latest_items = _list_playlist_items_with_client(youtube, playlist_id)
            item_map = {}
            for item in latest_items:
                if item["video_id"] in desired_set and item["video_id"] not in item_map:
                    item_map[item["video_id"]] = item

            for position, video_id in enumerate(ordered_video_ids):
                item = item_map.get(video_id)
                if not item:
                    continue
                if int(item.get("position", -1)) != position:
                    current_video_id = video_id
                    current_action = "reorder"
                    _update_playlist_item_position_with_client(
                        youtube,
                        playlist_item_id=item["playlist_item_id"],
                        playlist_id=playlist_id,
                        video_id=video_id,
                        position=position,
                    )

            latest_items = _list_playlist_items_with_client(youtube, playlist_id)
            final_item_map = {}
            for item in latest_items:
                if item["video_id"] in desired_set and item["video_id"] not in final_item_map:
                    final_item_map[item["video_id"]] = item["playlist_item_id"]

            playlist_result["video_ids"] = ordered_video_ids
            playlist_result["playlist_item_map"] = final_item_map
            playlist_result["success"] = True
            return playlist_result
        except HttpError as e:
            if original_playlist_id and attempt_index == 0 and is_playlist_not_found_http_error(e):
                log.warning(
                    "检测到状态里保存的旧 playlist_id=%s 已失效，将自动放弃旧 ID 并重建播放列表。",
                    original_playlist_id,
                )
                playlist_id = ""
                playlist_result["playlist_id"] = ""
                playlist_result["playlist_url"] = ""
                continue
            log.error("❌ 同步 YouTube 播放列表失败: %s", e)
            return playlist_result
        except Exception as e:
            log.error("❌ 同步 YouTube 播放列表失败: %s", e)
            return playlist_result

    return playlist_result


# ============================================================================
# build_youtube_payload（原文件行 6999-7030）
# ============================================================================

def build_youtube_payload(
    result,
    book_name,
    category,
    youtube_chapters="",
    title_prefix="",
    part_hint="",
    include_youtube_chapters=True,
    include_part_hint=True,
):
    final_title = result.seo_title or book_name
    final_tags = result.seo_tags or category
    final_desc = result.seo_description or ""

    if part_hint and include_part_hint:
        final_desc = f"{part_hint}\n\n{final_desc}".strip()

    if youtube_chapters and include_youtube_chapters:
        final_desc += "\n\n精彩章节时间轴:\n" + youtube_chapters

    if getattr(cfg, "APPEND_TAGS_TO_DESC", True) and final_tags:
        final_desc += "\n\n" + final_tags

    if getattr(cfg, "APPEND_TAGS_TO_TITLE", False) and final_tags:
        some_tags = " ".join([t for t in final_tags.split() if t.startswith("#")][:2])
        if some_tags and len(final_title) + len(some_tags) < 95:
            final_title += " " + some_tags

    if title_prefix:
        final_title = f"{title_prefix}{final_title}"

    return final_title[:100], final_desc[:5000], final_tags


# ============================================================================
# 上传回执（原文件行 1291-1424）
# ============================================================================

def _normalize_local_path_for_compare(path):
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.abspath(text))


def _capture_local_file_signature(path):
    normalized_path = _normalize_local_path_for_compare(path)
    signature = {"path": normalized_path}
    if not normalized_path or not os.path.exists(path):
        return signature

    stat = os.stat(path)
    signature["size"] = int(stat.st_size)
    signature["mtime_ns"] = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
    return signature


def persist_youtube_upload_receipt(
    receipt_path,
    video_path,
    upload_result,
    channel_name="",
    title="",
    privacy_status="",
    category_id="",
    schedule_after_hours=0,
):
    if not isinstance(upload_result, dict):
        return ""

    youtube_url = str(upload_result.get("youtube_url") or "").strip()
    video_id = str(upload_result.get("video_id") or "").strip()
    if not youtube_url and not video_id:
        return ""

    payload = {
        "receipt_version": 1,
        "saved_at": dt_datetime.now().isoformat(),
        "channel_name": str(channel_name or "").strip(),
        "title": str(title or upload_result.get("title") or "").strip(),
        "privacy_status": str(privacy_status or "").strip(),
        "category_id": str(category_id or "").strip(),
        "schedule_after_hours": int(schedule_after_hours or 0),
        "video_file": _capture_local_file_signature(video_path),
        "video_id": video_id,
        "youtube_url": youtube_url,
        "uploaded_at": str(upload_result.get("uploaded_at") or "").strip(),
        "publish_at": str(upload_result.get("publish_at") or "").strip(),
        "schedule_reason": str(upload_result.get("schedule_reason") or "").strip(),
    }
    write_json_file(receipt_path, payload)
    return receipt_path


def load_youtube_upload_receipt(receipt_path, video_path="", channel_name=""):
    receipt = read_json_file(receipt_path, default={}) or {}
    if not isinstance(receipt, dict) or (not receipt.get("youtube_url") and not receipt.get("video_id")):
        fallback_report = read_json_file(os.path.join(os.path.dirname(receipt_path), "book_result.json"), default={}) or {}
        fallback_result = fallback_report.get("result") if isinstance(fallback_report, dict) else {}
        if isinstance(fallback_result, dict) and (
            str(fallback_result.get("youtube_url") or "").strip()
            or str(fallback_result.get("youtube_urls") or "").strip()
        ):
            fallback_url = str(fallback_result.get("youtube_url") or "").strip()
            if "\n" in fallback_url:
                fallback_url = fallback_url.splitlines()[0].strip()
            receipt = {
                "channel_name": "",
                "title": str(fallback_result.get("seo_title") or "").strip(),
                "video_file": _capture_local_file_signature(fallback_result.get("video_path")),
                "video_id": "",
                "youtube_url": fallback_url,
                "uploaded_at": str(fallback_result.get("youtube_publish_at") or "").strip(),
                "publish_at": str(fallback_result.get("youtube_publish_at") or "").strip(),
                "schedule_reason": str(fallback_result.get("youtube_schedule_reason") or "").strip(),
            }
    if not isinstance(receipt, dict):
        return {}

    youtube_url = str(receipt.get("youtube_url") or "").strip()
    video_id = str(receipt.get("video_id") or "").strip()
    if not youtube_url and not video_id:
        return {}

    expected_channel = str(channel_name or "").strip()
    receipt_channel = str(receipt.get("channel_name") or "").strip()
    if expected_channel and receipt_channel and receipt_channel != expected_channel:
        return {}

    if video_path:
        current_signature = _capture_local_file_signature(video_path)
        if not current_signature.get("path") or int(current_signature.get("size") or 0) <= 0:
            return {}

        receipt_signature = receipt.get("video_file") or {}
        receipt_path_text = _normalize_local_path_for_compare(receipt_signature.get("path"))
        if receipt_path_text and receipt_path_text != current_signature["path"]:
            return {}

        if receipt_signature.get("size") is not None and int(receipt_signature.get("size") or 0) != int(current_signature.get("size") or 0):
            return {}

        if receipt_signature.get("mtime_ns") is not None and int(receipt_signature.get("mtime_ns") or 0) != int(current_signature.get("mtime_ns") or 0):
            return {}

    return {
        "video_id": video_id,
        "youtube_url": youtube_url,
        "uploaded_at": str(receipt.get("uploaded_at") or "").strip(),
        "publish_at": str(receipt.get("publish_at") or "").strip(),
        "schedule_reason": str(receipt.get("schedule_reason") or "").strip(),
        "title": str(receipt.get("title") or "").strip(),
    }


# ============================================================================
# 分片上传状态协调辅助（原文件行 2158-2228）
# 供 pipeline.py reconcile_split_part_upload_states 使用。
# ============================================================================

def _apply_video_match_to_split_part(part_state, match):
    if not isinstance(part_state, dict) or not isinstance(match, dict):
        return False

    changed = False
    old_video_id = str(part_state.get("video_id") or "").strip()
    updated_values = {
        "video_id": str(match.get("video_id") or "").strip(),
        "youtube_url": str(match.get("youtube_url") or "").strip(),
        "uploaded_at": str(match.get("uploaded_at") or "").strip(),
        "publish_at": str(match.get("publish_at") or "").strip(),
        "schedule_reason": str(match.get("schedule_reason") or "").strip(),
    }
    resolved_title = str(match.get("title") or part_state.get("youtube_title") or "").strip()
    if resolved_title:
        updated_values["youtube_title"] = resolved_title

    for key, value in updated_values.items():
        if str(part_state.get(key) or "").strip() == value:
            continue
        part_state[key] = value
        changed = True

    if old_video_id and old_video_id != updated_values.get("video_id", "") and str(part_state.get("playlist_item_id") or "").strip():
        part_state["playlist_item_id"] = ""
        changed = True

    # 如果已有上传的视频 ID/URL，标记该 part 为 completed
    if _split_part_has_uploaded_video(part_state):
        if str(part_state.get("status") or "").strip().lower() != "completed":
            part_state["status"] = "completed"
            changed = True
        if not str(part_state.get("completed_at") or "").strip():
            part_state["completed_at"] = str(dt_datetime.now().isoformat())
            changed = True
        if str(part_state.get("last_stage") or "").strip() != "completed":
            part_state["last_stage"] = "completed"
            changed = True
        if str(part_state.get("error") or "").strip():
            part_state["error"] = ""
            changed = True

    return changed


def _reset_split_part_upload_state(part_state, reason=""):
    if not isinstance(part_state, dict):
        return False

    changed = False
    for key in ["video_id", "youtube_url", "uploaded_at", "publish_at", "schedule_reason", "playlist_item_id"]:
        if not str(part_state.get(key) or "").strip():
            continue
        part_state[key] = ""
        changed = True

    if str(part_state.get("status") or "").strip().lower() != "pending":
        part_state["status"] = "pending"
        changed = True
    if str(part_state.get("completed_at") or "").strip():
        part_state["completed_at"] = ""
        changed = True
    if str(part_state.get("last_stage") or "").strip() != "upload_recovery_pending":
        part_state["last_stage"] = "upload_recovery_pending"
        changed = True

    normalized_reason = str(reason or "").strip()
    if str(part_state.get("error") or "").strip() != normalized_reason:
        part_state["error"] = normalized_reason
        changed = True

    return changed


def _split_part_has_uploaded_video(part_state):
    if not isinstance(part_state, dict):
        return False
    return bool(str(part_state.get("video_id") or "").strip() or str(part_state.get("youtube_url") or "").strip())