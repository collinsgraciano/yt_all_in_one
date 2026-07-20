"""VPS 中继调度器 — HF 外包架构的枢纽。

五项职能：
1. 调度流水线 Worker（后台线程筛选 TG 缓存完整书 → 写入 hf_jobs → 触发空闲 Worker）
2. TG API 中继（/tg-api/<path>）— 代理转发到 api.telegram.org
3. YouTube OAuth 中继（/yt-api/<channel>/<action>）— 持本地凭证代理 YouTube API
4. 配置/密钥分发（/api/pipeline-config, /api/test-config）— 动态分发，凭证不落地 HF
5. 结果回调（/api/callback）— 接收 Worker 完成通知 + 整书完成 TG 通知

部署：与当前项目同一 VPS，通过 docker-compose 启动，连接同一个 PostgreSQL。
"""

from __future__ import annotations

import os
import json
import time
import uuid
import logging
import threading
import tempfile
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
import psycopg
from psycopg import sql as pg_sql
from psycopg.types.json import Jsonb
from flask import Flask, request, jsonify, Response, redirect
from googleapiclient.errors import HttpError

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")
WORKER_URLS = [
    u.strip() for u in os.environ.get("WORKER_URLS", "").split(",") if u.strip()
]
TEST_MODELSCOPE_TOKEN = os.environ.get("TEST_MODELSCOPE_TOKEN", "")
WEB_PORT = int(os.environ.get("WEB_PORT", "38080"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))
STUCK_TIMEOUT_M = int(os.environ.get("STUCK_TIMEOUT_M", "1440"))
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "600"))
YT_OAUTH_DIR = os.environ.get("YT_OAUTH_DIR", "/data/oauth_tokens")

# 面板配置文件（运行时可修改，不依赖环境变量重启）
_CONFIG_FILE = os.environ.get("RELAY_CONFIG_FILE", "/data/relay_config.json")
_runtime_config_lock = threading.Lock()
_runtime_config: dict = {
    "worker_urls": WORKER_URLS,
    "test_modelscope_token": TEST_MODELSCOPE_TOKEN,
    "scheduler_running": False,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vps_relay")

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════
# 数据库工具
# ═══════════════════════════════════════════════════════════

_conn_lock = threading.Lock()


def _get_conn():
    """获取 PostgreSQL 连接（每次新建，psycopg3 连接池可选）。"""
    if not POSTGRES_DSN:
        raise RuntimeError("POSTGRES_DSN 未配置")
    return psycopg.connect(POSTGRES_DSN, autocommit=False)


def _fetch_one(query, params=None):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            row = cur.fetchone()
            if row:
                cols = [d.name for d in cur.description]
                return dict(zip(cols, row))
    return None


def _fetch_all(query, params=None):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
    return []


def _execute(query, params=None):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
        conn.commit()
    return cur.rowcount


def _fetch_global_setting(key: str) -> str:
    """从 global_settings 读取（复用当前项目的全局设置表）。"""
    row = _fetch_one("SELECT setting_value FROM public.global_settings WHERE setting_key = %s", (key,))
    return row["setting_value"] if row else ""


# ═══════════════════════════════════════════════════════════
# 配置持久化（面板可修改）
# ═══════════════════════════════════════════════════════════

def _load_runtime_config():
    global _runtime_config
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            with _runtime_config_lock:
                for k, v in saved.items():
                    _runtime_config[k] = v
    except Exception as e:
        logger.warning("加载 relay_config 失败: %s", e)


def _save_runtime_config():
    try:
        os.makedirs(os.path.dirname(_CONFIG_FILE) or ".", exist_ok=True)
        with _runtime_config_lock:
            data = dict(_runtime_config)
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("保存 relay_config 失败: %s", e)


def _cfg(key: str, default=None):
    with _runtime_config_lock:
        return _runtime_config.get(key, default)


def _set_cfg(key: str, value):
    with _runtime_config_lock:
        _runtime_config[key] = value


_load_runtime_config()


# ═══════════════════════════════════════════════════════════
# 1. TG API 中继 — 代理转发到 api.telegram.org
# ═══════════════════════════════════════════════════════════

@app.route("/tg-api/<path:path>", methods=["GET", "POST"])
def tg_relay(path):
    """Telegram API 中继。

    HF Worker 请求 /tg-api/bot<token>/getFile?file_id=xxx
    → VPS 转发到 https://api.telegram.org/bot<token>/getFile?file_id=xxx
    → 返回结果给 HF Worker

    HF 无法直连 api.telegram.org，经 VPS 中继。
    Token 在 URL path 中，HF Worker 从 /api/pipeline-config 拉取时已获得。
    """
    target_url = f"https://api.telegram.org/{path}"

    # 转发 query string
    if request.query_string:
        target_url += "?" + request.query_string.decode()

    try:
        # 文件下载路径 /tg-api/file/bot<token>/<file_path>
        if path.startswith("file/bot"):
            resp = requests.get(target_url, timeout=120, stream=True)
            return Response(
                resp.iter_content(chunk_size=64 * 1024),
                content_type=resp.headers.get("Content-Type", "application/octet-stream"),
                status=resp.status_code,
            )

        # API 调用 (getFile 等)
        if request.method == "POST":
            data = request.get_data()
            headers = {"Content-Type": request.headers.get("Content-Type", "application/json")}
            resp = requests.post(target_url, data=data, headers=headers, timeout=60)
        else:
            resp = requests.get(target_url, timeout=60)

        return Response(resp.content, status=resp.status_code,
                        content_type=resp.headers.get("Content-Type", "application/json"))
    except requests.exceptions.ConnectionError as e:
        logger.warning("[TG中继] 连接失败: %s", e)
        return jsonify({"ok": False, "error": f"中继连接失败: {e}"}), 502
    except Exception as e:
        logger.error("[TG中继] 异常: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════
# 2. YouTube OAuth 中继 — 持本地凭证代理 YouTube API
# ═══════════════════════════════════════════════════════════

def _load_youtube_client(channel_name: str):
    """从数据库读取频道凭证并构建 YouTube 客户端。"""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build

    row = _fetch_one(
        "SELECT token_json FROM public.youtube_credentials WHERE channel_name = %s LIMIT 1",
        (channel_name,),
    )
    if not row or not row.get("token_json"):
        raise RuntimeError(f"频道 {channel_name} 无 YouTube 凭证")

    token_info = row["token_json"]
    if isinstance(token_info, str):
        token_info = json.loads(token_info)

    credentials = Credentials.from_authorized_user_info(
        token_info, scopes=["https://www.googleapis.com/auth/youtube"]
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        # 回写刷新后的 token
        refreshed = json.loads(credentials.to_json())
        _execute(
            "UPDATE public.youtube_credentials SET token_json = %s, updated_at = now() WHERE channel_name = %s",
            (Jsonb(refreshed), channel_name),
        )

    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


# ═══════════════════════════════════════════════════════════
# YouTube 中继辅助：排期决策 + 播放列表同步
# 自包含实现，仅依赖 google API 客户端 + 标准库，复刻 pipeline/youtube.py 直连逻辑
# ═══════════════════════════════════════════════════════════

try:
    _YT_SCHEDULE_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _YT_SCHEDULE_TZ = timezone(timedelta(hours=8))

_YT_DAILY_PUBLISH_LIMIT = 3


def _yt_parse_datetime(value):
    """解析 YouTube API 日期时间为 UTC datetime。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _yt_format_datetime_z(value):
    """格式化为 YouTube API 接受的 ISO8601 Z 格式。"""
    if not value:
        return ""
    parsed = value if isinstance(value, datetime) else _yt_parse_datetime(value)
    if not parsed:
        return ""
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _yt_get_uploads_playlist_id(youtube):
    """获取当前频道 uploads 播放列表 ID。"""
    response = youtube.channels().list(part="contentDetails", mine=True, maxResults=1).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError("无法读取当前 YouTube 频道信息")
    uploads_id = (
        ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or ""
    ).strip()
    if not uploads_id:
        raise RuntimeError("当前 YouTube 频道未返回 uploads playlist ID")
    return uploads_id


def _yt_list_upload_video_ids(youtube, uploads_playlist_id, max_videos=100):
    """列出频道上传视频 ID（最多 max_videos 个）。"""
    video_ids = []
    page_token = None
    while True:
        response = youtube.playlistItems().list(
            part="contentDetails", playlistId=uploads_playlist_id, maxResults=50, pageToken=page_token,
        ).execute()
        for item in response.get("items", []):
            vid = str(((item.get("contentDetails") or {}).get("videoId") or "")).strip()
            if vid:
                video_ids.append(vid)
                if len(video_ids) >= max_videos:
                    return video_ids[:max_videos]
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return video_ids


def _yt_fetch_video_status_rows(youtube, video_ids):
    """批量查询视频状态行（分片 50）。"""
    rows = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        response = youtube.videos().list(part="snippet,status", id=",".join(chunk)).execute()
        rows.extend(response.get("items", []))
    return rows


def _yt_get_effective_published_at_utc(video_row, now_utc):
    """获取已生效的发布时间（过去或刚发布）。"""
    status_publish_at = _yt_parse_datetime((video_row.get("status") or {}).get("publishAt"))
    if status_publish_at is not None:
        if status_publish_at <= now_utc:
            return status_publish_at
        return None
    return _yt_parse_datetime((video_row.get("snippet") or {}).get("publishedAt"))


def _yt_get_future_scheduled_publish_at_utc(video_row, now_utc):
    """获取未来的定时发布时间。"""
    status_publish_at = _yt_parse_datetime((video_row.get("status") or {}).get("publishAt"))
    if status_publish_at is not None and status_publish_at > now_utc:
        return status_publish_at
    return None


def _yt_collect_schedule_facts(youtube, now_utc):
    """收集频道发布排期事实：已发布/未来定时的按本地日期计数。"""
    uploads_playlist_id = _yt_get_uploads_playlist_id(youtube)
    video_ids = _yt_list_upload_video_ids(youtube, uploads_playlist_id)
    if not video_ids:
        return {
            "published_count_by_local_date": {},
            "future_count_by_local_date": {},
            "future_publish_times_by_local_date": {},
            "latest_future_publish_at": None,
            "video_count": 0,
        }

    rows = _yt_fetch_video_status_rows(youtube, video_ids)
    published_count = {}
    future_count = {}
    future_times = {}
    latest_future = None

    for row in rows:
        published_at = _yt_get_effective_published_at_utc(row, now_utc)
        if published_at is not None:
            local_day = published_at.astimezone(_YT_SCHEDULE_TZ).date().isoformat()
            published_count[local_day] = published_count.get(local_day, 0) + 1

        future_publish_at = _yt_get_future_scheduled_publish_at_utc(row, now_utc)
        if future_publish_at is not None:
            if latest_future is None or future_publish_at > latest_future:
                latest_future = future_publish_at
            local_publish = future_publish_at.astimezone(_YT_SCHEDULE_TZ).replace(microsecond=0)
            local_day = local_publish.date().isoformat()
            future_count[local_day] = future_count.get(local_day, 0) + 1
            future_times.setdefault(local_day, []).append(local_publish)

    for day, items in future_times.items():
        future_times[day] = sorted(items)

    return {
        "published_count_by_local_date": published_count,
        "future_count_by_local_date": future_count,
        "future_publish_times_by_local_date": future_times,
        "latest_future_publish_at": latest_future,
        "video_count": len(rows),
    }


def _yt_build_daily_slots(target_date, base_publish_at_local, daily_limit):
    """构建某天的发布槽位列表。"""
    base_time = base_publish_at_local.timetz().replace(microsecond=0)
    day_start = datetime.combine(target_date, base_time, tzinfo=_YT_SCHEDULE_TZ).replace(microsecond=0)
    day_end = day_start.replace(hour=23, minute=55, second=0, microsecond=0)
    if day_end <= day_start:
        day_end = day_start + timedelta(minutes=10 * max(0, daily_limit - 1))
    if daily_limit <= 1:
        return [day_start]
    interval = max(600, int((day_end - day_start).total_seconds() // max(1, daily_limit - 1)))
    slots = []
    for idx in range(daily_limit):
        candidate = day_start + timedelta(seconds=interval * idx)
        if candidate > day_end:
            candidate = day_end
        candidate = candidate.replace(microsecond=0)
        if slots and candidate <= slots[-1]:
            candidate = (slots[-1] + timedelta(minutes=10)).replace(microsecond=0)
        slots.append(candidate)
    return slots


def _yt_resolve_publish_schedule(youtube, privacy_status="unlisted", schedule_after_hours=0):
    """完整的 YouTube 排期决策（复刻 pipeline/youtube.py resolve_youtube_publish_schedule_with_client）。

    当 privacy_status == 'schedule' 时，扫描频道已有视频的定时发布情况，
    在每日发布上限内寻找最近的可用槽位，避免同日超额。
    """
    normalized = str(privacy_status or "unlisted").strip().lower()
    if normalized != "schedule":
        return {"publish_at": "", "schedule_reason": ""}

    hours = max(1, int(schedule_after_hours or 0))
    now_utc = datetime.now(timezone.utc)
    base_publish_at_utc = (now_utc + timedelta(hours=hours)).replace(microsecond=0)
    base_publish_at_local = base_publish_at_utc.astimezone(_YT_SCHEDULE_TZ).replace(microsecond=0)

    facts = _yt_collect_schedule_facts(youtube, now_utc)
    published_count = facts.get("published_count_by_local_date", {})
    future_count = facts.get("future_count_by_local_date", {})
    future_times = facts.get("future_publish_times_by_local_date", {})
    daily_limit = _YT_DAILY_PUBLISH_LIMIT

    schedule_reason = "base_schedule"
    final_publish_at_local = base_publish_at_local
    final_publish_at_utc = base_publish_at_utc

    candidate_day = base_publish_at_local.date()
    base_day = candidate_day
    found_slot = False
    for day_offset in range(370):
        current_day = candidate_day + timedelta(days=day_offset)
        local_day_key = current_day.isoformat()
        reserved = int(published_count.get(local_day_key, 0)) + int(future_count.get(local_day_key, 0))
        if reserved >= daily_limit:
            continue

        occupied = list(future_times.get(local_day_key, []) or [])
        slots = _yt_build_daily_slots(current_day, base_publish_at_local, daily_limit)
        earliest = base_publish_at_local if current_day == base_day else slots[0]
        for slot in slots:
            if slot < earliest:
                continue
            if any(abs((slot - occ).total_seconds()) < 60 for occ in occupied):
                continue
            final_publish_at_local = slot
            final_publish_at_utc = slot.astimezone(timezone.utc).replace(microsecond=0)
            schedule_reason = f"daily_slot_{reserved + 1}_of_{daily_limit}"
            found_slot = True
            break

        if not found_slot and reserved < daily_limit:
            fallback_anchor = max([earliest] + occupied) if occupied else earliest
            fallback_slot = (fallback_anchor + timedelta(minutes=10)).replace(microsecond=0)
            if fallback_slot.date() == current_day:
                final_publish_at_local = fallback_slot
                final_publish_at_utc = fallback_slot.astimezone(timezone.utc).replace(microsecond=0)
                schedule_reason = f"daily_fallback_{reserved + 1}_of_{daily_limit}"
                found_slot = True

        if found_slot:
            break

    publish_at = _yt_format_datetime_z(final_publish_at_utc)
    logger.info(
        "[YT中继] 排期决策: reason=%s publish_at=%s base=%s videos_scanned=%d",
        schedule_reason, publish_at, _yt_format_datetime_z(base_publish_at_utc),
        int(facts.get("video_count", 0)),
    )
    return {"publish_at": publish_at, "schedule_reason": schedule_reason}


# ── 播放列表同步辅助 ──

def _yt_normalize_playlist_privacy(privacy_status="public"):
    normalized = str(privacy_status or "public").strip().lower()
    if normalized not in {"private", "unlisted", "public"}:
        normalized = "public"
    return normalized


def _yt_is_playlist_not_found_error(error):
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


def _yt_execute(request_obj, op_name="youtube request", retries=5):
    """执行 YouTube API 请求，带瞬时错误重试（复刻 podcast._podcast_execute_youtube_request）。"""
    last_error = None
    for attempt in range(max(1, retries)):
        try:
            return request_obj.execute()
        except HttpError as e:
            last_error = e
            if attempt >= retries - 1:
                raise
            status_code = getattr(getattr(e, "resp", None), "status", None)
            raw = getattr(e, "content", b"") or b""
            payload_text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            retryable_tokens = (
                "serviceUnavailable", "backendError", "internalError",
                "rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
            )
            if status_code not in {408, 409, 429, 500, 502, 503, 504} and not any(tok in payload_text for tok in retryable_tokens):
                raise
            sleep_s = max(1.0, 3.0 * (2 ** attempt))
            logger.warning("[YT中继] %s 瞬时错误(status=%s)，%ds 后重试 (%d/%d)", op_name, status_code, sleep_s, attempt + 1, retries)
            time.sleep(sleep_s)
        except Exception as e:
            last_error = e
            if attempt >= retries - 1:
                raise
            text = str(e).lower()
            retryable_tokens = (
                "timeout", "timed out", "temporarily unavailable", "connection reset",
                "connection aborted", "connection broken", "service unavailable",
                "bad gateway", "internal error",
            )
            if not any(tok in text for tok in retryable_tokens):
                raise
            sleep_s = max(1.0, 3.0 * (2 ** attempt))
            logger.warning("[YT中继] %s 瞬时请求错误，%ds 后重试 (%d/%d): %s", op_name, sleep_s, attempt + 1, retries, str(e)[:200])
            time.sleep(sleep_s)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{op_name} failed without response")


def _yt_list_owned_playlists(youtube):
    playlists = []
    page_token = None
    while True:
        response = _yt_execute(
            youtube.playlists().list(part="snippet,status", mine=True, maxResults=50, pageToken=page_token),
            op_name="playlists.list:mine",
        )
        for item in response.get("items", []):
            snippet = item.get("snippet") or {}
            status = item.get("status") or {}
            pid = str(item.get("id") or "").strip()
            playlists.append({
                "playlist_id": pid,
                "playlist_url": f"https://www.youtube.com/playlist?list={pid}" if pid else "",
                "title": str(snippet.get("title") or "").strip(),
                "description": str(snippet.get("description") or ""),
                "privacy_status": _yt_normalize_playlist_privacy(status.get("privacyStatus") or "public"),
            })
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return playlists


def _yt_list_playlist_items(youtube, playlist_id):
    items = []
    page_token = None
    not_found_retry = 0
    max_not_found_retries = 6
    while True:
        try:
            response = _yt_execute(
                youtube.playlistItems().list(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50, pageToken=page_token),
                op_name=f"playlistItems.list:{playlist_id}",
            )
        except HttpError as e:
            if _yt_is_playlist_not_found_error(e) and not_found_retry < max_not_found_retries:
                not_found_retry += 1
                wait_s = min(12, 2 + not_found_retry)
                logger.warning("[YT中继] 播放列表 %s 暂不可读，%ds 后重试 (%d/%d)", playlist_id, wait_s, not_found_retry, max_not_found_retries)
                time.sleep(wait_s)
                page_token = None
                items = []
                continue
            raise
        for item in response.get("items", []):
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            resource = snippet.get("resourceId") or {}
            vid = str(resource.get("videoId") or content.get("videoId") or "").strip()
            items.append({
                "playlist_item_id": str(item.get("id") or "").strip(),
                "video_id": vid,
                "position": int(snippet.get("position") or 0),
            })
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def _yt_create_or_update_playlist(youtube, title, description="", privacy_status="public", playlist_id=""):
    normalized = _yt_normalize_playlist_privacy(privacy_status)
    body = {
        "snippet": {
            "title": str(title or "")[:150],
            "description": str(description or "")[:5000],
            "defaultLanguage": "zh-CN",
        },
        "status": {"privacyStatus": normalized},
    }
    if playlist_id:
        body["id"] = playlist_id
        response = _yt_execute(
            youtube.playlists().update(part="snippet,status", body=body),
            op_name=f"playlists.update:{playlist_id}",
        )
    else:
        response = _yt_execute(
            youtube.playlists().insert(part="snippet,status", body=body),
            op_name=f"playlists.insert:{str(title or '')[:48]}",
        )
    final_id = str(response.get("id") or "").strip()
    return {
        "playlist_id": final_id,
        "playlist_url": f"https://www.youtube.com/playlist?list={final_id}" if final_id else "",
        "title": body["snippet"]["title"],
        "description": body["snippet"]["description"],
        "privacy_status": normalized,
    }


def _yt_find_matching_playlist(youtube, title, ordered_video_ids=None, privacy_status="public"):
    """匹配已有播放列表（标题完全一致 + 内容/隐私优先）。"""
    normalized_title = str(title or "").strip()
    desired = [str(v).strip() for v in (ordered_video_ids or []) if str(v).strip()]
    normalized_privacy = _yt_normalize_playlist_privacy(privacy_status)
    if not normalized_title:
        return {}

    title_matches = []
    for playlist in _yt_list_owned_playlists(youtube):
        if str(playlist.get("title") or "").strip() == normalized_title:
            title_matches.append(playlist)
    if not title_matches:
        return {}

    exact_match = {}
    privacy_match = {}
    for playlist in title_matches:
        pid = str(playlist.get("playlist_id") or "").strip()
        if not pid:
            continue
        if desired:
            try:
                items = _yt_list_playlist_items(youtube, pid)
            except Exception:
                items = []
            existing = [str(it.get("video_id") or "").strip() for it in items if str(it.get("video_id") or "").strip()]
            if existing == desired:
                exact_match = playlist
                if str(playlist.get("privacy_status") or "").lower() == normalized_privacy:
                    return playlist
        if not privacy_match and str(playlist.get("privacy_status") or "").lower() == normalized_privacy:
            privacy_match = playlist

    if exact_match:
        return exact_match
    if privacy_match:
        return privacy_match
    return title_matches[0]


def _yt_wait_for_live_video_rows(youtube, video_ids, max_attempts=3):
    """等待上传的视频可读，返回 (rows_by_id, missing_ids)。"""
    ordered = []
    seen = set()
    for v in video_ids or []:
        vid = str(v).strip()
        if vid and vid not in seen:
            seen.add(vid)
            ordered.append(vid)
    if not ordered:
        return {}, []

    rows_by_id = {}
    missing = list(ordered)
    for attempt in range(1, max(1, max_attempts) + 1):
        rows_by_id = {}
        for chunk_start in range(0, len(ordered), 50):
            chunk = ordered[chunk_start:chunk_start + 50]
            response = youtube.videos().list(part="status", id=",".join(chunk)).execute()
            for row in response.get("items", []):
                vid = str(row.get("id") or "").strip()
                if vid:
                    rows_by_id[vid] = row
        missing = [v for v in ordered if v not in rows_by_id]
        if not missing or attempt >= max_attempts:
            break
        time.sleep(min(10, 1 + attempt))
    return rows_by_id, missing


def _yt_sync_playlist(youtube, title, description, ordered_video_ids, privacy_status="public", playlist_id=""):
    """完整的播放列表同步逻辑（复刻 pipeline/youtube.py sync_youtube_playlist 直连模式）。

    流程：等待视频可读 → 匹配/创建播放列表 → 删除多余项 → 插入缺失项 → 重排序。
    """
    ordered_video_ids = [str(v).strip() for v in ordered_video_ids if str(v).strip()]
    normalized_privacy = _yt_normalize_playlist_privacy(privacy_status)

    result = {
        "playlist_id": str(playlist_id or ""),
        "playlist_url": f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else "",
        "title": str(title or "")[:150],
        "description": str(description or "")[:5000],
        "privacy_status": normalized_privacy,
        "success": False,
        "error": "",
    }

    # 等待视频可读
    _rows, missing = _yt_wait_for_live_video_rows(youtube, ordered_video_ids, max_attempts=3)
    if missing:
        result["error"] = "One or more uploaded YouTube videos are no longer accessible: " + ",".join(missing)
        logger.error("[YT中继] 播放列表同步失败，部分视频不可读: %s", ",".join(missing))
        return result

    original_playlist_id = str(playlist_id or "").strip()
    # 无 playlist_id 时尝试匹配已有播放列表
    if not original_playlist_id:
        recovered = _yt_find_matching_playlist(youtube, title=title, ordered_video_ids=ordered_video_ids, privacy_status=privacy_status)
        recovered_id = str(recovered.get("playlist_id") or "").strip() if isinstance(recovered, dict) else ""
        if recovered_id:
            playlist_id = recovered_id
            result.update(recovered)
            logger.info("[YT中继] 匹配到已有播放列表: %s title=%s", recovered_id, str(title or "")[:60])

    desired_set = set(ordered_video_ids)
    for attempt in range(2):
        try:
            result = _yt_create_or_update_playlist(youtube, title=title, description=description, privacy_status=privacy_status, playlist_id=playlist_id)
            playlist_id = result["playlist_id"]
            result["privacy_status"] = normalized_privacy
            result["success"] = False
            result["error"] = ""

            existing_items = _yt_list_playlist_items(youtube, playlist_id)
            grouped = {}
            for item in existing_items:
                grouped.setdefault(item["video_id"], []).append(item)

            for vid, items in grouped.items():
                items.sort(key=lambda x: x["position"])
                to_delete = []
                if vid not in desired_set:
                    to_delete = items
                elif len(items) > 1:
                    to_delete = items[1:]
                for item in to_delete:
                    if item.get("playlist_item_id"):
                        _yt_execute(
                            youtube.playlistItems().delete(id=item["playlist_item_id"]),
                            op_name=f"playlistItems.delete:{item['playlist_item_id']}",
                        )

            existing_items = _yt_list_playlist_items(youtube, playlist_id)
            existing_ids = {item["video_id"] for item in existing_items}
            for vid in ordered_video_ids:
                if vid not in existing_ids:
                    _yt_execute(
                        youtube.playlistItems().insert(part="snippet", body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": vid}}}),
                        op_name=f"playlistItems.insert:{playlist_id}:{vid}",
                    )

            latest_items = _yt_list_playlist_items(youtube, playlist_id)
            item_map = {}
            for item in latest_items:
                if item["video_id"] in desired_set and item["video_id"] not in item_map:
                    item_map[item["video_id"]] = item

            for position, vid in enumerate(ordered_video_ids):
                item = item_map.get(vid)
                if not item:
                    continue
                if int(item.get("position", -1)) != position:
                    _yt_execute(
                        youtube.playlistItems().update(part="snippet", body={"id": item["playlist_item_id"], "snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": vid}, "position": int(position)}}),
                        op_name=f"playlistItems.update:{playlist_id}:{vid}",
                    )

            latest_items = _yt_list_playlist_items(youtube, playlist_id)
            final_map = {}
            for item in latest_items:
                if item["video_id"] in desired_set and item["video_id"] not in final_map:
                    final_map[item["video_id"]] = item["playlist_item_id"]

            result["video_ids"] = ordered_video_ids
            result["playlist_item_map"] = final_map
            result["success"] = True
            logger.info("[YT中继] 播放列表同步成功: playlist_id=%s videos=%d", playlist_id, len(ordered_video_ids))
            return result
        except HttpError as e:
            if original_playlist_id and attempt == 0 and _yt_is_playlist_not_found_error(e):
                logger.warning("[YT中继] 旧 playlist_id=%s 已失效，重建播放列表", original_playlist_id)
                playlist_id = ""
                result["playlist_id"] = ""
                result["playlist_url"] = ""
                continue
            result["error"] = str(e)
            logger.error("[YT中继] 播放列表同步失败: %s", e)
            return result
        except Exception as e:
            result["error"] = str(e)
            logger.error("[YT中继] 播放列表同步失败: %s", e)
            return result

    return result


@app.route("/yt-api/<channel>/<action>", methods=["GET", "POST"])
def yt_relay(channel, action):
    """YouTube OAuth 中继。

    VPS 持 refresh_token，为 HF Worker 分发短期 access_token + 排期决策：
    - GET  /yt-api/<channel>/token          获取 access_token + 排期决策（HF Worker 直连 YouTube 上传）
    - GET  /yt-api/<channel>/info            获取频道信息（上传测试用）
    - POST /yt-api/<channel>/playlist-sync   同步播放列表（轻量元数据，VPS 代行）
    """
    try:
        youtube = _load_youtube_client(channel)
    except Exception as e:
        logger.error("[YT中继] 凭证加载失败: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500

    if action == "token":
        return _yt_relay_token(youtube, channel)
    elif action == "info":
        return _yt_relay_info(youtube, channel)
    elif action == "playlist-sync":
        return _yt_relay_playlist_sync(youtube, channel)
    else:
        return jsonify({"success": False, "error": f"未知 action: {action}"}), 400


def _yt_relay_token(youtube, channel):
    """分发 access_token + 排期决策给 HF Worker。

    VPS 持 refresh_token，刷新后返回短期 access_token（1小时有效期）。
    HF Worker 拿 token 直连 YouTube Data API 上传大文件，不经 VPS 中转。

    返回:
      access_token: 短期访问令牌
      token_expiry: 过期时间（ISO8601）
      channel_id: 频道 ID
      channel_title: 频道标题
      publish_at: 排期时间（schedule 模式下）
      schedule_reason: 排期原因
    """
    try:
        # 提取 access_token（_load_youtube_client 已确保刷新）
        creds = youtube._http.credentials  # type: ignore
        access_token = creds.token
        expiry = creds.expiry
        expiry_str = expiry.isoformat() if expiry else ""

        # 频道信息
        resp = youtube.channels().list(part="snippet", mine=True, maxResults=1).execute()
        items = resp.get("items", [])
        channel_id = items[0]["id"] if items else ""
        channel_title = (items[0].get("snippet", {}) or {}).get("title", "") if items else ""

        # 排期决策（VPS 有 YouTube 客户端，直接扫描频道已有视频）
        privacy_status = request.args.get("privacy_status", "schedule")
        schedule_after_hours = int(request.args.get("schedule_after_hours", "0") or "0")
        publish_at = ""
        schedule_reason = ""

        normalized_privacy = str(privacy_status or "unlisted").strip().lower()
        if normalized_privacy == "schedule":
            schedule_info = _yt_resolve_publish_schedule(
                youtube, privacy_status=normalized_privacy, schedule_after_hours=schedule_after_hours,
            )
            publish_at = schedule_info.get("publish_at", "")
            schedule_reason = schedule_info.get("schedule_reason", "")
            logger.info("[YT中继] token 分发 + 排期: publish_at=%s reason=%s", publish_at, schedule_reason)

        result = {
            "success": True,
            "access_token": access_token,
            "token_expiry": expiry_str,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "publish_at": publish_at,
            "schedule_reason": schedule_reason,
        }
        logger.info("[YT中继] token 分发成功: channel=%s expiry=%s", channel, expiry_str)
        return jsonify(result)

    except Exception as e:
        logger.error("[YT中继] token 分发失败: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


def _yt_relay_info(youtube, channel):
    """获取频道信息（上传测试用）。"""
    try:
        resp = youtube.channels().list(part="snippet,contentDetails", mine=True, maxResults=1).execute()
        items = resp.get("items", [])
        if not items:
            return jsonify({"success": False, "error": "未找到频道信息"}), 404
        item = items[0]
        return jsonify({
            "success": True,
            "channel_name": channel,
            "channel_id": item.get("id", ""),
            "title": ((item.get("snippet") or {}).get("title") or ""),
            "uploads_playlist_id": ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads", ""),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def _yt_relay_playlist_sync(youtube, channel):
    """接收 HF Worker 发来的播放列表同步请求，代理执行完整同步逻辑。

    HF Worker POST JSON: {title, description, ordered_video_ids, privacy_status, playlist_id}
    VPS 持本地凭证执行：创建/更新播放列表 → 删除多余项 → 插入缺失项 → 重排序。
    """
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", ""))[:150]
    description = str(data.get("description", ""))[:5000]
    ordered_video_ids = [str(v).strip() for v in (data.get("ordered_video_ids") or []) if str(v).strip()]
    privacy_status = str(data.get("privacy_status", "public") or "public")
    playlist_id = str(data.get("playlist_id", "") or "")

    if not ordered_video_ids:
        return jsonify({
            "playlist_id": playlist_id,
            "playlist_url": f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else "",
            "title": title,
            "description": description,
            "privacy_status": _yt_normalize_playlist_privacy(privacy_status),
            "success": False,
            "error": "缺少 ordered_video_ids",
        }), 400

    logger.info("[YT中继] 播放列表同步: channel=%s title=%s videos=%d playlist_id=%s", channel, title[:60], len(ordered_video_ids), playlist_id or "(新建)")
    result = _yt_sync_playlist(
        youtube,
        title=title,
        description=description,
        ordered_video_ids=ordered_video_ids,
        privacy_status=privacy_status,
        playlist_id=playlist_id,
    )
    return jsonify(result)


# ═══════════════════════════════════════════════════════════
# 3. 配置/密钥分发 — 凭证不落地 HF
# ═══════════════════════════════════════════════════════════

@app.route("/api/pipeline-config", methods=["GET"])
def pipeline_config():
    """分发流水线运行配置（HF Worker 拉取）。

    HF Worker 启动/认领任务前调用，获取 TG_BOT_TOKEN / MODELSCOPE_TOKEN 等。
    凭证不存储在 HF Space，动态拉取。
    """
    channel = request.args.get("channel", "")
    relay_base = request.host_url.rstrip("/")

    # 从 global_settings 读取共享配置
    config = {
        "TELEGRAM_API_BASE": f"{relay_base}/tg-api",
        "YOUTUBE_OAUTH_BASE": f"{relay_base}/yt-api",
        "TG_BOT_TOKEN": _fetch_global_setting("TG_BOT_TOKEN"),
        "MODELSCOPE_TOKEN": _fetch_global_setting("MODELSCOPE_TOKEN"),
        "POSTGRES_DSN": POSTGRES_DSN,
        "OUTPUT_ROOT": "/tmp/output",
        "ENABLE_DEEPFILTER": False,  # TG 缓存模式跳过降噪
        "ENABLE_TG_AUDIO_CACHE": True,
        "ONLY_TG_CACHED_BOOKS": True,
        "TG_SERIAL_DOWNLOAD": True,
        "TG_DOWNLOAD_INTERVAL_SECONDS": 3,
        "SKIP_EXISTING": True,
        "FORCE_REPROCESS": False,
        "QUIET_RUNTIME_OUTPUT": False,
        "CLEANUP_INTERMEDIATE_FILES_AFTER_SUCCESS": True,
        "YOUTUBE_CHANNEL_NAME": channel,
    }

    # 合并频道级配置（从 channel_runtime_settings + channel_configs）
    if channel:
        try:
            rows = _fetch_all(
                "SELECT setting_key, setting_value FROM public.channel_runtime_settings WHERE channel_name = %s",
                (channel,),
            )
            for row in rows:
                config[row["setting_key"]] = row["setting_value"]
        except Exception:
            pass

        try:
            row = _fetch_one(
                "SELECT config_json FROM public.channel_configs WHERE channel_name = %s",
                (channel,),
            )
            if row and row.get("config_json"):
                ch_config = row["config_json"]
                if isinstance(ch_config, str):
                    ch_config = json.loads(ch_config)
                if isinstance(ch_config, dict):
                    config.update(ch_config)
        except Exception:
            pass

    # 敏感字段标记（仅供 Worker 日志脱敏参考）
    return jsonify(config)


@app.route("/api/test-config", methods=["GET"])
def test_config():
    """分发测试实验配置（测试 Worker 拉取）。"""
    relay_base = request.host_url.rstrip("/")
    return jsonify({
        "TELEGRAM_API_BASE": f"{relay_base}/tg-api",
        "YOUTUBE_OAUTH_BASE": f"{relay_base}/yt-api",
        "TG_BOT_TOKEN": _fetch_global_setting("TG_BOT_TOKEN"),
        "MODELSCOPE_TOKEN": _cfg("test_modelscope_token") or _fetch_global_setting("MODELSCOPE_TOKEN"),
        "POSTGRES_DSN": POSTGRES_DSN,
    })


# ═══════════════════════════════════════════════════════════
# 4. 结果回调 — 接收 Worker 完成通知
# ═══════════════════════════════════════════════════════════

@app.route("/api/callback", methods=["POST"])
def result_callback():
    """接收 Worker 完成通知。

    Worker 处理完一个任务后 POST 回调：
    {job_id, status, worker_id, result, error_message, duration_seconds}
    """
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    status = data.get("status", "done")
    worker_id = data.get("worker_id", "")
    result = data.get("result", {})
    error_message = data.get("error_message", "")
    duration_seconds = int(data.get("duration_seconds", 0) or 0)

    if not job_id:
        return jsonify({"ok": False, "error": "缺少 job_id"}), 400

    try:
        # 更新 hf_jobs
        _execute(
            """UPDATE public.hf_jobs
               SET status = %s, result = %s, error_message = %s, finished_at = now()
               WHERE job_id = %s""",
            (status, Jsonb(result) if result else None, error_message, job_id),
        )

        # 更新 books.book_status（流水线任务成功时）
        if status == "done" and result:
            job = _fetch_one("SELECT book_id, channel_name FROM public.hf_jobs WHERE job_id = %s", (job_id,))
            if job and job.get("book_id"):
                _execute(
                    "UPDATE public.books SET book_status = 'success', updated_at = now() WHERE book_id = %s",
                    (job["book_id"],),
                )

        # 更新 Worker 业绩统计
        if worker_id:
            _update_worker_stats(worker_id, "pipeline", status == "done", duration_seconds)

        logger.info("[回调] job=%s status=%s worker=%s", job_id, status, worker_id)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("[回调] 处理失败: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


def _update_worker_stats(worker_id: str, worker_type: str, success: bool, duration_seconds: int):
    """更新 Worker 业绩统计表。"""
    try:
        _execute(
            """INSERT INTO public.hf_worker_stats (worker_id, worker_type, total_jobs, success_jobs, failed_jobs, total_seconds, last_job_at, last_seen_at, updated_at)
               VALUES (%s, %s, 1, %s, %s, %s, now(), now(), now())
               ON CONFLICT (worker_id) DO UPDATE SET
                 total_jobs = public.hf_worker_stats.total_jobs + 1,
                 success_jobs = public.hf_worker_stats.success_jobs + %s,
                 failed_jobs = public.hf_worker_stats.failed_jobs + %s,
                 total_seconds = public.hf_worker_stats.total_seconds + %s,
                 last_job_at = now(),
                 last_seen_at = now(),
                 updated_at = now()
            """,
            (worker_id, worker_type, 1 if success else 0, 0 if success else 1, duration_seconds,
             1 if success else 0, 0 if success else 1, duration_seconds),
        )
    except Exception as e:
        logger.warning("更新 Worker 统计失败: %s", e)


# ═══════════════════════════════════════════════════════════
# 5. 调度器 — 后台线程
# ═══════════════════════════════════════════════════════════

_scheduler_thread = None
_scheduler_stop = threading.Event()
_last_cleanup = 0


def _check_worker_health(worker_url: str) -> dict | None:
    """检查 Worker 健康状态，返回 {ok, free_slots, total_slots} 或 None。"""
    try:
        resp = requests.get(f"{worker_url}/health", timeout=8)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _trigger_worker_process(worker_url: str) -> bool:
    """触发 Worker 认领并处理一个任务。"""
    try:
        resp = requests.post(f"{worker_url}/process", timeout=30)
        return resp.status_code == 200
    except Exception as e:
        logger.debug("触发 Worker %s 失败: %s", worker_url, e)
        return False


def _reset_stuck_jobs():
    """重置超时的 processing 任务为 pending。"""
    try:
        count = _execute(
            """UPDATE public.hf_jobs
               SET status = 'pending', worker_id = NULL, claimed_at = NULL,
                   retry_count = retry_count + 1
               WHERE job_type = 'tg_cache_pipeline'
                 AND status = 'processing'
                 AND claimed_at < now() - make_interval(mins => %s)""",
            (STUCK_TIMEOUT_M,),
        )
        if count > 0:
            logger.info("[调度] 重置 %d 个超时任务", count)
    except Exception as e:
        logger.warning("[调度] 重置超时任务失败: %s", e)


def _scheduler_loop():
    """调度主循环。"""
    global _last_cleanup
    logger.info("[调度] 调度器启动，间隔 %ds", CHECK_INTERVAL)

    while not _scheduler_stop.is_set():
        try:
            # 定期清理卡住任务
            now_ts = time.time()
            if now_ts - _last_cleanup > CLEANUP_INTERVAL:
                _reset_stuck_jobs()
                _last_cleanup = now_ts

            # 查询 pending 任务数
            row = _fetch_one(
                "SELECT COUNT(*) AS cnt FROM public.hf_jobs WHERE job_type = 'tg_cache_pipeline' AND status = 'pending'"
            )
            pending = row["cnt"] if row else 0

            if pending > 0:
                # 检查所有 Worker 健康状态
                worker_urls = _cfg("worker_urls") or []
                for url in worker_urls:
                    if _scheduler_stop.is_set():
                        break
                    health = _check_worker_health(url)
                    if not health or not health.get("ok"):
                        continue
                    free = int(health.get("free_slots", 0) or 0)
                    if free <= 0:
                        continue
                    # 触发 Worker 认领处理
                    if _trigger_worker_process(url):
                        logger.info("[调度] 触发 Worker %s 认领任务 (pending=%d)", url, pending)
                        # 触发后短暂等待，避免同时触发多个
                        time.sleep(2)

        except Exception as e:
            logger.error("[调度] 循环异常: %s", e)

        _scheduler_stop.wait(CHECK_INTERVAL)

    logger.info("[调度] 调度器已停止")


def start_scheduler():
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    _scheduler_thread.start()
    _set_cfg("scheduler_running", True)
    _save_runtime_config()


def stop_scheduler():
    _scheduler_stop.set()
    _set_cfg("scheduler_running", False)
    _save_runtime_config()
    logger.info("[调度] 调度器停止请求已发送")


# ═══════════════════════════════════════════════════════════
# 管理 API
# ═══════════════════════════════════════════════════════════

@app.route("/api/status", methods=["GET"])
def api_status():
    """全局状态。"""
    # 任务统计
    stats = {}
    for status in ("pending", "processing", "done", "failed"):
        row = _fetch_one(
            "SELECT COUNT(*) AS cnt FROM public.hf_jobs WHERE job_type = 'tg_cache_pipeline' AND status = %s",
            (status,),
        )
        stats[status] = row["cnt"] if row else 0

    # Worker 健康（统一 Worker，同时展示流水线 + 测试槽位）
    workers = []
    for url in (_cfg("worker_urls") or []):
        health = _check_worker_health(url)
        workers.append({"url": url, "health": health})

    # Worker 业绩
    worker_stats = _fetch_all("SELECT * FROM public.hf_worker_stats ORDER BY last_job_at DESC NULLS LAST LIMIT 20")

    # 最近任务
    recent_jobs = _fetch_all(
        "SELECT job_id, job_type, book_id, channel_name, status, worker_id, created_at, finished_at, error_message "
        "FROM public.hf_jobs ORDER BY created_at DESC LIMIT 30"
    )

    return jsonify({
        "stats": stats,
        "workers": workers,
        "worker_stats": worker_stats,
        "recent_jobs": recent_jobs,
        "scheduler_running": _cfg("scheduler_running", False),
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """读写面板配置。"""
    if request.method == "GET":
        with _runtime_config_lock:
            data = dict(_runtime_config)
        # 脱敏
        if data.get("test_modelscope_token"):
            data["test_modelscope_token"] = f"{data['test_modelscope_token'][:8]}***"
        return jsonify(data)

    data = request.get_json(silent=True) or {}
    # 更新配置
    if "worker_urls" in data:
        urls = data["worker_urls"]
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split(",") if u.strip()]
        _set_cfg("worker_urls", urls)
    if "test_modelscope_token" in data:
        val = data["test_modelscope_token"]
        if val and "***" not in val:
            _set_cfg("test_modelscope_token", val)
    _save_runtime_config()
    return jsonify({"ok": True})


@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    start_scheduler()
    return jsonify({"ok": True, "message": "调度器已启动"})


@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    stop_scheduler()
    return jsonify({"ok": True, "message": "调度器已停止"})


@app.route("/api/trigger", methods=["POST"])
def api_trigger():
    """手动触发指定 Worker 认领任务。"""
    data = request.get_json(silent=True) or {}
    worker_url = data.get("worker_url", "")
    if not worker_url:
        return jsonify({"ok": False, "error": "缺少 worker_url"}), 400
    ok = _trigger_worker_process(worker_url)
    return jsonify({"ok": ok})


@app.route("/api/reset-stuck", methods=["POST"])
def api_reset_stuck():
    """手动重置卡住任务。"""
    count = _reset_stuck_jobs()
    return jsonify({"ok": True, "reset": count})


@app.route("/api/seed-jobs", methods=["POST"])
def api_seed_jobs():
    """筛选 TG 缓存完整书并写入 hf_jobs 队列。

    请求体: {channel_name: "频道名", category: "可选分类筛选"}
    """
    data = request.get_json(silent=True) or {}
    channel = data.get("channel_name", "")
    category = data.get("category", "")

    if not channel:
        return jsonify({"ok": False, "error": "缺少 channel_name"}), 400

    # 查询所有章节均已上传 TG 的完整书
    query = """
        SELECT b.book_id, b.book_name, b.category
        FROM public.books b
        WHERE b.book_status != 'success'
    """
    params = []
    if category:
        query += " AND b.category = %s"
        params.append(category)

    query += """
        AND b.book_id IN (
            SELECT book_id FROM public.audiobook_chapters
            GROUP BY book_id
            HAVING COUNT(*) = COUNT(
                CASE WHEN upload_status = 'uploaded'
                     AND telegram_file_id IS NOT NULL
                     AND telegram_file_id != ''
                THEN 1 END
            )
        )
        AND b.book_id NOT IN (
            SELECT book_id FROM public.hf_jobs
            WHERE job_type = 'tg_cache_pipeline' AND status IN ('pending', 'processing')
        )
    """
    books = _fetch_all(query, params)

    inserted = 0
    for book in books:
        try:
            _execute(
                """INSERT INTO public.hf_jobs (job_type, book_id, channel_name, status)
                   VALUES ('tg_cache_pipeline', %s, %s, 'pending')""",
                (book["book_id"], channel),
            )
            inserted += 1
        except Exception as e:
            logger.warning("插入 hf_jobs 失败 book=%s: %s", book.get("book_id"), e)

    logger.info("[投递] 频道=%s 筛选 %d 本 TG缓存完整书，写入 %d 个任务", channel, len(books), inserted)
    return jsonify({"ok": True, "inserted": inserted, "total_candidates": len(books)})


# ═══════════════════════════════════════════════════════════
# Web 管理面板
# ═══════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def panel():
    """管理面板 HTML。"""
    if WEB_PASSWORD:
        # 简单 cookie 认证
        auth = request.cookies.get("relay_auth")
        if auth != "ok":
            return redirect("/login")

    return Response(_PANEL_HTML, content_type="text/html; charset=utf-8")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not WEB_PASSWORD:
        return redirect("/")
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == WEB_PASSWORD:
            resp = redirect("/")
            resp.set_cookie("relay_auth", "ok", max_age=86400, httponly=True)
            return resp
        return Response(_LOGIN_HTML, content_type="text/html; charset=utf-8")
    return Response(_LOGIN_HTML, content_type="text/html; charset=utf-8")


_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>VPS 中继调度器 - 登录</title>
<style>body{font-family:sans-serif;max-width:400px;margin:80px auto;background:#1a1a2e;color:#eee}
.box{background:#16213e;padding:30px;border-radius:8px}
input{width:100%;padding:10px;margin:8px 0;box-sizing:border-box;background:#0f3460;color:#eee;border:1px solid #533483;border-radius:4px}
button{width:100%;padding:10px;background:#533483;color:#fff;border:none;border-radius:4px;cursor:pointer}
</style></head><body><div class="box">
<h2>🔐 VPS 中继调度器</h2>
<form method="post"><input type="password" name="password" placeholder="密码" autofocus>
<button type="submit">登录</button></form></div></body></html>"""


_PANEL_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HF 外包调度器</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;padding:20px}
h1{color:#7c83fd;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:15px;margin-bottom:20px}
.card{background:#1a1d29;border-radius:8px;padding:18px;border:1px solid #2a2d39}
.card h3{color:#8b8dff;font-size:13px;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
.card .val{font-size:28px;font-weight:bold;color:#4ade80}
.card .val.red{color:#f87171}.card .val.yellow{color:#fbbf24}.card .val.blue{color:#60a5fa}
button{background:#533483;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;margin:4px 4px 4px 0}
button:hover{background:#6b46a3}button.danger{background:#dc2626}button.success{background:#16a34a}
table{width:100%;border-collapse:collapse;margin-top:12px;background:#1a1d29;border-radius:8px;overflow:hidden}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #2a2d39;font-size:13px}
th{background:#252836;color:#8b8dff}
.worker-ok{color:#4ade80}.worker-down{color:#f87171}
.section{background:#1a1d29;border-radius:8px;padding:18px;margin-bottom:20px;border:1px solid #2a2d39}
.section h2{color:#8b8dff;font-size:16px;margin-bottom:12px}
.url{color:#60a5fa;word-break:break-all;font-size:11px}
.tag{display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;margin:2px}
.tag.pending{background:#3b82f6}.tag.processing{background:#f59e0b;color:#000}
.tag.done{background:#22c55e}.tag.failed{background:#ef4444}
</style></head><body>
<h1>🛰️ HF 外包调度器</h1>

<div class="grid" id="stats"></div>

<div class="section">
  <h2>调度器</h2>
  <button class="success" onclick="sched('start')">启动调度</button>
  <button class="danger" onclick="sched('stop')">停止调度</button>
  <button onclick="resetStuck()">重置卡住任务</button>
  <span id="sched-status" style="margin-left:12px"></span>
</div>

<div class="section">
  <h2>统一 Worker（流水线 + 测试）</h2>
  <div id="workers"></div>
</div>

<div class="section">
  <h2>Worker 业绩统计</h2>
  <table id="worker-stats"><thead><tr>
    <th>Worker ID</th><th>类型</th><th>总任务</th><th>成功</th><th>失败</th>
    <th>累计耗时</th><th>最后任务时间</th>
  </tr></thead><tbody></tbody></table>
</div>

<div class="section">
  <h2>最近任务</h2>
  <table id="recent-jobs"><thead><tr>
    <th>ID</th><th>类型</th><th>书ID</th><th>频道</th><th>状态</th><th>Worker</th><th>创建时间</th><th>错误</th>
  </tr></thead><tbody></tbody></table>
</div>

<script>
const API = '/api';
async function loadStatus(){
  try{
    const r = await fetch(API+'/status');
    const d = await r.json();
    // stats
    const s = d.stats||{};
    document.getElementById('stats').innerHTML = `
      <div class="card"><h3>待处理</h3><div class="val blue">${s.pending||0}</div></div>
      <div class="card"><h3>处理中</h3><div class="val yellow">${s.processing||0}</div></div>
      <div class="card"><h3>已完成</h3><div class="val">${s.done||0}</div></div>
      <div class="card"><h3>失败</h3><div class="val red">${s.failed||0}</div></div>
    `;
    document.getElementById('sched-status').innerHTML = d.scheduler_running ?
      '<span class="worker-ok">● 运行中</span>' : '<span class="worker-down">● 已停止</span>';
    // workers (统一 Worker)
    const wh = (d.workers||[]).map(w=>{
      const h=w.health||{}; const ok=h&&h.ok;
      return `<div class="card"><h3>${ok?'🟢':'🔴'} 统一 Worker</h3>
        <div class="url">${w.url}</div>
        <p>状态: ${ok?'在线':'离线'}</p>
        <p>流水线槽位: ${h.free_slots||0}/${h.total_slots||0} | 测试槽位: ${h.test_free_slots??'-'}/${h.test_total_slots??'-'}</p>
        <button onclick="trigger('${w.url}')">触发认领</button></div>`;
    }).join('');
    document.getElementById('workers').innerHTML = wh || '<p>未配置 Worker</p>';
    // worker stats
    const ws = (d.worker_stats||[]).map(r=>`<tr>
      <td>${r.worker_id}</td><td>${r.worker_type||''}</td>
      <td>${r.total_jobs}</td><td>${r.success_jobs}</td><td>${r.failed_jobs}</td>
      <td>${r.total_seconds}s</td><td>${r.last_job_at||''}</td></tr>`).join('');
    document.querySelector('#worker-stats tbody').innerHTML = ws;
    // recent jobs
    const rj = (d.recent_jobs||[]).map(r=>`<tr>
      <td>${r.job_id}</td><td>${r.job_type}</td><td>${(r.book_id||'').substring(0,12)}</td>
      <td>${r.channel_name||''}</td>
      <td><span class="tag ${r.status}">${r.status}</span></td>
      <td>${(r.worker_id||'').substring(0,12)}</td>
      <td>${(r.created_at||'').substring(0,19)}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${(r.error_message||'').substring(0,60)}</td>
    </tr>`).join('');
    document.querySelector('#recent-jobs tbody').innerHTML = rj;
  }catch(e){console.error(e)}
}
async function sched(action){
  await fetch(API+'/scheduler/'+action,{method:'POST'}); loadStatus();
}
async function resetStuck(){
  const r=await fetch(API+'/reset-stuck',{method:'POST'}); const d=await r.json();
  alert('重置了 '+d.reset+' 个任务'); loadStatus();
}
async function trigger(url){
  await fetch(API+'/trigger',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({worker_url:url})}); loadStatus();
}
loadStatus(); setInterval(loadStatus,5000);
</script>
</body></html>"""


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 如果配置了调度器自动启动
    if os.environ.get("AUTO_START_SCHEDULER", "1") == "1":
        start_scheduler()
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, threaded=True)
