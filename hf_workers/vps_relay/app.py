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
from datetime import datetime, timezone

import requests
import psycopg
from psycopg import sql as pg_sql
from psycopg.types.json import Jsonb
from flask import Flask, request, jsonify, Response, redirect

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")
PIPELINE_WORKER_URLS = [
    u.strip() for u in os.environ.get("PIPELINE_WORKER_URLS", "").split(",") if u.strip()
]
TEST_WORKER_URLS = [
    u.strip() for u in os.environ.get("TEST_WORKER_URLS", "").split(",") if u.strip()
]
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_BOT_TOKENS = [
    t.strip() for t in os.environ.get("TG_BOT_TOKENS", "").split(",") if t.strip()
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
    "pipeline_worker_urls": PIPELINE_WORKER_URLS,
    "test_worker_urls": TEST_WORKER_URLS,
    "tg_chat_id": TG_CHAT_ID,
    "tg_bot_tokens": TG_BOT_TOKENS,
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


@app.route("/yt-api/<channel>/<action>", methods=["GET", "POST"])
def yt_relay(channel, action):
    """YouTube OAuth 中继。

    HF Worker 不持有 token.json，YouTube API 调用经 VPS 中继：
    - POST /yt-api/<channel>/upload  上传视频（multipart: video + metadata）
    - GET  /yt-api/<channel>/info    获取频道信息（上传测试用）
    """
    try:
        youtube = _load_youtube_client(channel)
    except Exception as e:
        logger.error("[YT中继] 凭证加载失败: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500

    if action == "upload":
        return _yt_relay_upload(youtube, channel)
    elif action == "info":
        return _yt_relay_info(youtube, channel)
    else:
        return jsonify({"success": False, "error": f"未知 action: {action}"}), 400


def _yt_relay_upload(youtube, channel):
    """接收 HF Worker 发来的视频文件 + 元数据，代理上传到 YouTube。"""
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    # 元数据从 form 字段读取
    title = request.form.get("title", "")[:100]
    description = request.form.get("description", "")[:5000]
    tags_raw = request.form.get("tags", "")
    privacy_status = request.form.get("privacy_status", "unlisted")
    category_id = request.form.get("category_id", "")
    publish_at = request.form.get("publish_at", "")
    schedule_reason = request.form.get("schedule_reason", "")

    # 标签
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
    if len(tags) > 30:
        tags = tags[:30]

    snippet = {"title": title, "description": description, "defaultLanguage": "zh-CN"}
    if tags:
        snippet["tags"] = tags
    if category_id:
        snippet["categoryId"] = category_id

    # 隐私状态
    status = {"privacyStatus": privacy_status}
    if privacy_status == "schedule" and publish_at:
        status = {"privacyStatus": "private", "publishAt": publish_at}
    elif privacy_status == "schedule":
        # 简化：默认 24h 后公开
        from datetime import timedelta
        calc = (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        status = {"privacyStatus": "private", "publishAt": calc}

    body = {"snippet": snippet, "status": status}

    # 视频文件
    video_file = request.files.get("video")
    if not video_file:
        return jsonify({"success": False, "error": "缺少 video 文件"}), 400

    tmp_path = None
    try:
        # 保存到临时文件
        suffix = ".mp4"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        video_file.save(tmp_path)

        media = MediaFileUpload(tmp_path, chunksize=1024 * 1024 * 20, resumable=True)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        retry_count = 0
        max_retries = 5
        while response is None:
            try:
                status_obj, response = req.next_chunk()
                if status_obj:
                    logger.info("[YT中继] 上传进度: %d%%", int(status_obj.progress() * 100))
            except HttpError as e:
                sc = getattr(getattr(e, "resp", None), "status", None)
                if sc in {500, 502, 503, 504} and retry_count < max_retries:
                    retry_count += 1
                    time.sleep(2 ** retry_count)
                    continue
                raise

        video_id = response["id"]
        youtube_url = f"https://youtu.be/{video_id}"
        uploaded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        # 封面图（可选）
        cover_file = request.files.get("cover")
        if cover_file:
            cover_tmp = None
            try:
                fd2, cover_tmp = tempfile.mkstemp(suffix=".jpg")
                os.close(fd2)
                cover_file.save(cover_tmp)
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(cover_tmp),
                ).execute()
            except Exception as e:
                logger.warning("[YT中继] 封面设置失败（非致命）: %s", e)
            finally:
                if cover_tmp and os.path.exists(cover_tmp):
                    os.remove(cover_tmp)

        result = {
            "success": True,
            "video_id": video_id,
            "youtube_url": youtube_url,
            "uploaded_at": uploaded_at,
            "title": title,
            "publish_at": publish_at,
            "schedule_reason": schedule_reason,
        }
        logger.info("[YT中继] 上传成功: %s → %s", title, youtube_url)
        return jsonify(result)

    except Exception as e:
        logger.error("[YT中继] 上传失败: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


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
        "TG_CHAT_ID": _fetch_global_setting("TG_CHAT_ID"),
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
        "TG_CHAT_ID": _fetch_global_setting("TG_CHAT_ID"),
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

        # 整书完成 TG 通知
        if status == "done" and result.get("youtube_url"):
            _notify_telegram_book_done(result, job_id)

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


def _notify_telegram_book_done(result: dict, job_id: int):
    """整书完成 TG 通知管理员。"""
    chat_id = _cfg("tg_chat_id") or TG_CHAT_ID
    tokens = _cfg("tg_bot_tokens") or TG_BOT_TOKENS
    if not chat_id or not tokens:
        return

    book_name = result.get("book_name", "未知书名")
    youtube_url = result.get("youtube_url", "")
    text = (
        f"📚 整书处理完成\n"
        f"书名: {book_name}\n"
        f"YouTube: {youtube_url}\n"
        f"任务ID: {job_id}\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{tokens[0]}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        logger.warning("[TG通知] 发送失败: %s", e)


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
                # 检查所有流水线 Worker 健康状态
                worker_urls = _cfg("pipeline_worker_urls") or []
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

    # Worker 健康
    workers = []
    for url in (_cfg("pipeline_worker_urls") or []):
        health = _check_worker_health(url)
        workers.append({"url": url, "health": health})

    test_workers = []
    for url in (_cfg("test_worker_urls") or []):
        health = _check_worker_health(url)
        test_workers.append({"url": url, "health": health})

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
        "test_workers": test_workers,
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
        if data.get("tg_bot_tokens"):
            data["tg_bot_tokens"] = [f"{t[:8]}***" for t in data["tg_bot_tokens"]]
        if data.get("test_modelscope_token"):
            data["test_modelscope_token"] = f"{data['test_modelscope_token'][:8]}***"
        return jsonify(data)

    data = request.get_json(silent=True) or {}
    # 更新配置（敏感字段单独处理）
    if "pipeline_worker_urls" in data:
        urls = data["pipeline_worker_urls"]
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split(",") if u.strip()]
        _set_cfg("pipeline_worker_urls", urls)
    if "test_worker_urls" in data:
        urls = data["test_worker_urls"]
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split(",") if u.strip()]
        _set_cfg("test_worker_urls", urls)
    if "tg_chat_id" in data:
        _set_cfg("tg_chat_id", str(data["tg_chat_id"]))
    if "tg_bot_tokens" in data:
        tokens = data["tg_bot_tokens"]
        if isinstance(tokens, str):
            tokens = [t.strip() for t in tokens.split(",") if t.strip()]
        # 只有非脱敏值才更新
        if tokens and not all("***" in t for t in tokens):
            _set_cfg("tg_bot_tokens", tokens)
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
  <h2>流水线 Worker</h2>
  <div id="workers"></div>
</div>

<div class="section">
  <h2>测试 Worker</h2>
  <div id="test-workers"></div>
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
    // workers
    const wh = (d.workers||[]).map(w=>{
      const h=w.health||{}; const ok=h&&h.ok;
      return `<div class="card"><h3>${ok?'🟢':'🔴'} Worker</h3>
        <div class="url">${w.url}</div>
        <p>状态: ${ok?'在线':'离线'} | 空闲槽: ${h.free_slots||0}/${h.total_slots||0}</p>
        <button onclick="trigger('${w.url}')">触发认领</button></div>`;
    }).join('');
    document.getElementById('workers').innerHTML = wh || '<p>未配置流水线 Worker</p>';
    // test workers
    const th = (d.test_workers||[]).map(w=>{
      const h=w.health||{}; const ok=h&&h.ok;
      return `<div class="card"><h3>${ok?'🟢':'🔴'} 测试Worker</h3>
        <div class="url">${w.url}</div>
        <p>状态: ${ok?'在线':'离线'} | 忙: ${h.busy||false}</p></div>`;
    }).join('');
    document.getElementById('test-workers').innerHTML = th || '<p>未配置测试 Worker</p>';
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
