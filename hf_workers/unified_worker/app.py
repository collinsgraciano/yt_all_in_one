"""统一 Worker — HF Space 上的远程执行器（流水线 + 测试合一）。

将原 pipeline_worker 和 test_worker 合并为单个 HF Space，通过双槽位组隔离资源：
  - pipeline_slots: 队列认领模式，处理重型流水线任务（TG下载→混音→AI→封装→上传）
  - test_slots:     同步执行模式，处理轻量测试实验（AI/上传/TG下载/BGM混音）

双槽位独立计数，互不阻塞：流水线任务运行时仍可接受测试请求。

不重写流水线逻辑，复用 pipeline/ 全部代码，与轨道A（本机自跑）完全一致。
凭证不落地 HF，所有 TG/YouTube API 调用均经 VPS 中继代理。
"""

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import zipfile
import tempfile
import logging
import threading
import traceback
import uuid
from datetime import datetime

import requests
import psycopg
from psycopg.types.json import Jsonb
from flask import Flask, request, jsonify, Response

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")
VPS_RELAY_URL = os.environ.get("VPS_RELAY_URL", "").rstrip("/")
PIPELINE_SLOTS = int(os.environ.get("PIPELINE_SLOTS", "1"))
TEST_SLOTS = int(os.environ.get("TEST_SLOTS", "1"))
WORKER_ID = f"hf_{uuid.uuid4().hex[:8]}"

OUTPUT_ROOT = os.environ.get("OUTPUT_ROOT", "/tmp/output")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/music")
MUSIC_ZIP_URL = os.environ.get(
    "MUSIC_ZIP_URL",
    "https://huggingface.co/datasets/oooooo1323/cm/resolve/main/Parisian%20Breeze.zip",
)

# BGM 支持的音频扩展名
_BGM_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("unified_worker")

app = Flask(__name__)

# 屏蔽 werkzeug 每次请求的 INFO 日志（GET / HTTP/1.1 200 等干扰信息）
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════
# 双槽位管理 — pipeline 与 test 独立计数，互不阻塞
# ═══════════════════════════════════════════════════════════

_lock = threading.Lock()

# ── 流水线槽位 ──
_pipeline_slots_in_use = 0
_pipeline_current_job: dict | None = None
_pipeline_current_progress: str = ""

# ── 测试槽位 ──
_test_slots_in_use = 0
_test_current_job: dict | None = None
_test_current_progress: str = ""

# ═══════════════════════════════════════════════════════════
# 数据库工具（共享）
# ═══════════════════════════════════════════════════════════

ALL_JOB_TYPES = ("tg_cache_pipeline", "test_ai", "test_upload", "test_tg_download", "test_bgm")
TEST_JOB_TYPES = ("test_ai", "test_upload", "test_tg_download", "test_bgm")


def _get_conn():
    return psycopg.connect(POSTGRES_DSN, autocommit=False)


def _claim_next_pipeline_job() -> dict | None:
    """原子认领一个流水线任务（FOR UPDATE SKIP LOCKED）。"""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.hf_jobs
                   SET status = 'processing', worker_id = %s, claimed_at = now()
                   WHERE ctid IN (
                       SELECT ctid FROM public.hf_jobs
                       WHERE job_type = 'tg_cache_pipeline' AND status = 'pending'
                       ORDER BY created_at
                       LIMIT 1
                       FOR UPDATE SKIP LOCKED
                   )
                   RETURNING job_id, book_id, channel_name""",
                (WORKER_ID,),
            )
            row = cur.fetchone()
            if not row:
                return None
            colnames = [d.name for d in cur.description]
            job = dict(zip(colnames, row))
        conn.commit()
    return job


def _fetch_book_record(book_id: str) -> dict | None:
    """从 books 表读取书籍记录。"""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT book_id, book_name, author, category, total_chapters, book_data, tags, status, book_status "
                "FROM public.books WHERE book_id = %s LIMIT 1",
                (str(book_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            colnames = [d.name for d in cur.description]
            return dict(zip(colnames, row))


def _record_result(job_id: int, status: str, result: dict, error_message: str = ""):
    """写回 hf_jobs 结果。"""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.hf_jobs
                   SET status = %s, result = %s, error_message = %s, finished_at = now()
                   WHERE job_id = %s""",
                (status, Jsonb(result) if result else None, error_message, job_id),
            )
        conn.commit()


def _update_book_status(book_id: str, status: str):
    """更新 books.book_status。"""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.books SET book_status = %s, updated_at = now() WHERE book_id = %s",
                (status, str(book_id)),
            )
        conn.commit()


def _update_worker_stats(worker_type: str, success: bool, duration_seconds: int):
    """更新 Worker 业绩统计表。"""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
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
                    (WORKER_ID, worker_type, 1 if success else 0, 0 if success else 1, duration_seconds,
                     1 if success else 0, 0 if success else 1, duration_seconds),
                )
            conn.commit()
    except Exception as e:
        logger.warning("更新 Worker 统计失败: %s", e)


# ═══════════════════════════════════════════════════════════
# 配置拉取（从 VPS 中继）— 凭证不落地 HF
# ═══════════════════════════════════════════════════════════

_cached_pipeline_config: dict | None = None
_cached_test_config: dict | None = None
_config_lock = threading.Lock()


def _fetch_pipeline_config(channel: str) -> dict:
    """从 VPS 中继拉取流水线配置。"""
    global _cached_pipeline_config
    with _config_lock:
        if _cached_pipeline_config and _cached_pipeline_config.get("_channel") == channel:
            return _cached_pipeline_config

    if not VPS_RELAY_URL:
        return {"POSTGRES_DSN": POSTGRES_DSN, "OUTPUT_ROOT": OUTPUT_ROOT, "YOUTUBE_CHANNEL_NAME": channel}

    try:
        resp = requests.get(f"{VPS_RELAY_URL}/api/pipeline-config", params={"channel": channel}, timeout=15)
        resp.raise_for_status()
        config = resp.json()
        config["POSTGRES_DSN"] = POSTGRES_DSN
        config["OUTPUT_ROOT"] = OUTPUT_ROOT
        config["MUSIC_DIR"] = MUSIC_DIR
        config["_channel"] = channel
        with _config_lock:
            _cached_pipeline_config = config
        logger.info("[配置] 流水线配置拉取成功: channel=%s", channel)
        return config
    except Exception as e:
        logger.error("[配置] 流水线配置拉取失败: %s", e)
        return {"POSTGRES_DSN": POSTGRES_DSN, "OUTPUT_ROOT": OUTPUT_ROOT, "YOUTUBE_CHANNEL_NAME": channel}


def _fetch_test_config(channel: str = "") -> dict:
    """从 VPS 中继拉取测试配置。"""
    global _cached_test_config
    with _config_lock:
        if _cached_test_config and _cached_test_config.get("_channel") == channel:
            return _cached_test_config

    if not VPS_RELAY_URL:
        return {"POSTGRES_DSN": POSTGRES_DSN, "OUTPUT_ROOT": OUTPUT_ROOT, "MUSIC_DIR": MUSIC_DIR, "YOUTUBE_CHANNEL_NAME": channel}

    try:
        resp = requests.get(f"{VPS_RELAY_URL}/api/test-config", params={"channel": channel} if channel else {}, timeout=15)
        resp.raise_for_status()
        config = resp.json()
        config["POSTGRES_DSN"] = POSTGRES_DSN
        config["OUTPUT_ROOT"] = OUTPUT_ROOT
        config["MUSIC_DIR"] = MUSIC_DIR
        config["_channel"] = channel
        with _config_lock:
            _cached_test_config = config
        logger.info("[配置] 测试配置拉取成功")
        return config
    except Exception as e:
        logger.error("[配置] 测试配置拉取失败: %s", e)
        return {"POSTGRES_DSN": POSTGRES_DSN, "OUTPUT_ROOT": OUTPUT_ROOT, "MUSIC_DIR": MUSIC_DIR, "YOUTUBE_CHANNEL_NAME": channel}


# ═══════════════════════════════════════════════════════════
# 回调 VPS（共享）
# ═══════════════════════════════════════════════════════════

def _notify_vps(job_id: int, status: str, result: dict, error_message: str, duration_seconds: int):
    """回调通知 VPS。"""
    if not VPS_RELAY_URL:
        return
    try:
        requests.post(
            f"{VPS_RELAY_URL}/api/callback",
            json={"job_id": job_id, "status": status, "worker_id": WORKER_ID,
                  "result": result, "error_message": error_message, "duration_seconds": duration_seconds},
            timeout=15,
        )
    except Exception as e:
        logger.warning("[回调] 通知 VPS 失败: %s", e)


# ═══════════════════════════════════════════════════════════
# 中间文件清理
# ═══════════════════════════════════════════════════════════

def _cleanup_book_dir(book_dir: str):
    if not book_dir or not os.path.isdir(book_dir):
        return
    try:
        shutil.rmtree(book_dir, ignore_errors=True)
        logger.info("[清理] 已删除目录: %s", book_dir)
    except Exception as e:
        logger.warning("[清理] 删除失败: %s", e)


def _ensure_pipeline_importable():
    """确保 pipeline 包可导入。"""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [app_dir, "/app"]:
        pipeline_path = os.path.join(candidate, "pipeline")
        if os.path.isdir(pipeline_path):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return
    logger.error("未找到 pipeline 包目录")


# ═══════════════════════════════════════════════════════════
# BGM 音乐池自动下载 — 启动时从 HF Datasets 下载 zip 到 /data/music
# ═══════════════════════════════════════════════════════════

_bgm_sync_status = {"running": False, "total": 0, "done": 0, "current": "", "error": ""}


def _count_local_music() -> int:
    """统计 MUSIC_DIR 中已有的音频文件数量。"""
    if not os.path.isdir(MUSIC_DIR):
        return 0
    count = 0
    for name in os.listdir(MUSIC_DIR):
        if name.lower().endswith(_BGM_EXTENSIONS):
            count += 1
    return count


def _sync_bgm_music():
    """从 HF Datasets 下载 BGM 音乐 zip 包并解压到本地 MUSIC_DIR。

    HF Space 重启后 /data/music 可能丢失（非持久目录），启动时自动检测：
    若本地已有音频文件则跳过，否则从 MUSIC_ZIP_URL 下载 zip 并解压。
    不依赖 VPS 中继，直接从 Hugging Face Datasets 拉取。
    """
    global _bgm_sync_status
    _bgm_sync_status = {"running": True, "total": 1, "done": 0, "current": "检查本地音乐池", "error": ""}

    try:
        os.makedirs(MUSIC_DIR, exist_ok=True)

        # 1. 检查本地是否已有音频文件
        existing = _count_local_music()
        if existing > 0:
            _bgm_sync_status["done"] = 1
            _bgm_sync_status["current"] = ""
            logger.info("[BGM下载] 本地已有 %d 个音频文件，跳过下载", existing)
            return

        # 2. 下载 zip 到临时文件
        _bgm_sync_status["current"] = "正在下载 BGM 音乐包"
        logger.info("[BGM下载] 本地音乐池为空，开始下载: %s", MUSIC_ZIP_URL)

        tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_zip_path = tmp_zip.name
        tmp_zip.close()

        try:
            resp = requests.get(MUSIC_ZIP_URL, timeout=300, stream=True)
            resp.raise_for_status()

            # 尝试从 Content-Length 获取总大小
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp_zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = downloaded * 100 // total_size
                        _bgm_sync_status["current"] = f"下载中 {pct}% ({downloaded // (1024*1024)}MB)"
                    else:
                        _bgm_sync_status["current"] = f"下载中 ({downloaded // (1024*1024)}MB)"

            logger.info("[BGM下载] zip 下载完成 (%.1f MB)", downloaded / (1024 * 1024))

            # 3. 解压到 MUSIC_DIR
            _bgm_sync_status["current"] = "正在解压音乐包"
            with zipfile.ZipFile(tmp_zip_path) as zf:
                zf.extractall(MUSIC_DIR)

            new_count = _count_local_music()
            _bgm_sync_status["done"] = 1
            _bgm_sync_status["current"] = ""
            logger.info("[BGM下载] 解压完成！本地共 %d 个音频文件", new_count)

        finally:
            # 清理临时 zip 文件
            if os.path.exists(tmp_zip_path):
                os.remove(tmp_zip_path)

    except Exception as e:
        _bgm_sync_status["error"] = str(e)
        logger.error("[BGM下载] 异常: %s", e)
    finally:
        _bgm_sync_status["running"] = False


# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════
#  流水线任务处理（队列认领模式）
# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════

def _pipeline_process_one_job(job: dict) -> tuple[str, dict, str]:
    """处理一个流水线任务，返回 (status, result, error_message)。"""
    global _pipeline_current_progress

    job_id = job["job_id"]
    book_id = str(job["book_id"])
    channel = job.get("channel_name") or ""

    # 1. 读取书籍记录
    _pipeline_current_progress = f"读取书籍 {book_id}"
    book_record = _fetch_book_record(book_id)
    if not book_record:
        return "failed", {}, f"书籍 {book_id} 不存在"

    book_name = book_record.get("book_name") or f"book_{book_id}"
    _pipeline_current_progress = f"处理: {book_name}"

    # 2. 拉取配置
    _pipeline_current_progress = f"拉取配置: {book_name}"
    config = _fetch_pipeline_config(channel)
    config["YOUTUBE_CHANNEL_NAME"] = channel
    config["PROJECT_FLAG"] = channel
    config["OUTPUT_ROOT"] = OUTPUT_ROOT
    config["MUSIC_DIR"] = MUSIC_DIR
    config["PIPELINE_TASK_ID"] = f"hf_job_{job_id}"
    config["QUIET_RUNTIME_OUTPUT"] = False
    config.setdefault("ONLY_TG_CACHED_BOOKS", True)
    config.setdefault("ENABLE_TG_AUDIO_CACHE", True)

    # 3. 应用配置并导入 pipeline
    _pipeline_current_progress = f"初始化 pipeline: {book_name}"
    try:
        _ensure_pipeline_importable()
        from pipeline.config import apply_runtime_config
        apply_runtime_config(config)
    except Exception as e:
        err = f"pipeline 初始化失败: {e}\n{traceback.format_exc()}"
        logger.error(err)
        return "failed", {}, err

    # 4. 调用 process_book
    _pipeline_current_progress = f"执行流水线: {book_name}"
    start_time = time.time()
    try:
        from pipeline.pipeline import process_book
        result = process_book(book_record, run_started_at=datetime.now())
        duration = int(time.time() - start_time)

        if hasattr(result, "error") and result.error:
            youtube_url = getattr(result, "youtube_url", "") or ""
            if youtube_url:
                result_dict = {
                    "youtube_url": youtube_url, "book_name": book_name, "book_id": book_id,
                    "video_path": getattr(result, "video_path", ""), "duration_seconds": duration,
                }
                _update_book_status(book_id, "success")
                return "done", result_dict, ""
            else:
                _update_book_status(book_id, "failed")
                return "failed", {"book_name": book_name, "duration_seconds": duration}, str(result.error)

        youtube_url = getattr(result, "youtube_url", "") or ""
        result_dict = {
            "youtube_url": youtube_url, "book_name": book_name, "book_id": book_id,
            "video_path": getattr(result, "video_path", ""),
            "publish_at": getattr(result, "youtube_publish_at", ""),
            "schedule_reason": getattr(result, "youtube_schedule_reason", ""),
            "duration_seconds": duration,
        }
        _update_book_status(book_id, "success")
        _pipeline_current_progress = f"完成: {book_name} → {youtube_url}"
        return "done", result_dict, ""

    except Exception as e:
        duration = int(time.time() - start_time)
        err = f"process_book 异常: {e}\n{traceback.format_exc()}"
        logger.error(err)
        _update_book_status(book_id, "failed")
        return "failed", {"book_name": book_name, "duration_seconds": duration}, err


def _pipeline_process_job_wrapper(job: dict):
    """在线程中处理流水线任务，管理槽位 + 清理 + 回调。"""
    global _pipeline_current_job, _pipeline_current_progress
    job_id = job["job_id"]
    book_id = str(job.get("book_id", ""))
    book_name = ""

    start_time = time.time()
    status = "failed"
    result: dict = {}
    error_message = ""

    book_record = None
    try:
        book_record = _fetch_book_record(book_id)
        book_name = book_record.get("book_name", "") if book_record else ""
        result["book_name"] = book_name
    except Exception:
        pass

    book_dir = None
    try:
        status, result, error_message = _pipeline_process_one_job(job)
        if book_record:
            from pipeline.runtime import sanitize_filename
            safe_name = sanitize_filename(book_name or f"book_{book_id}")
            safe_cat = sanitize_filename(book_record.get("category", "未分类"))
            book_dir = os.path.join(OUTPUT_ROOT, safe_cat, f"{safe_name}_{book_id}")
    except Exception as e:
        error_message = f"任务处理异常: {e}\n{traceback.format_exc()}"
        logger.error(error_message)
        status = "failed"
    finally:
        duration = int(time.time() - start_time)
        result["duration_seconds"] = duration

        try:
            _record_result(job_id, status, result, error_message)
        except Exception as e:
            logger.error("写回结果失败: %s", e)

        _notify_vps(job_id, status, result, error_message, duration)
        _update_worker_stats("pipeline", status == "done", duration)

        if book_dir:
            _cleanup_book_dir(book_dir)

        global _pipeline_slots_in_use
        with _lock:
            _pipeline_slots_in_use -= 1
            _pipeline_current_job = None
            _pipeline_current_progress = ""

        logger.info("[流水线完成] job=%s book=%s status=%s duration=%ds", job_id, book_name, status, duration)


# ── 批量处理 ──

_batch_stop = threading.Event()
_batch_running = False
_batch_stats = {"total": 0, "current": ""}


def _batch_loop():
    """连续认领并处理多个流水线任务，直到没有 pending 或被停止。"""
    global _batch_running
    _batch_running = True
    logger.info("[批量] 开始批量处理")

    while not _batch_stop.is_set():
        with _lock:
            if _pipeline_slots_in_use >= PIPELINE_SLOTS:
                time.sleep(2)
                continue

        job = _claim_next_pipeline_job()
        if not job:
            logger.info("[批量] 没有待处理任务，批量处理结束")
            break

        _batch_stats["total"] += 1
        _batch_stats["current"] = f"job={job['job_id']} book={job.get('book_id', '')}"

        with _lock:
            _pipeline_slots_in_use += 1
            _pipeline_current_job = job

        t = threading.Thread(target=_pipeline_process_job_wrapper, args=(job,), daemon=True)
        t.start()

        while True:
            with _lock:
                if _pipeline_slots_in_use < PIPELINE_SLOTS:
                    break
            if _batch_stop.is_set():
                break
            time.sleep(1)

    _batch_running = False
    _batch_stop.clear()
    logger.info("[批量] 批量处理已结束")


# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════
#  测试任务处理（同步执行模式）
# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════

def _execute_test(job_type: str, params: dict, config: dict) -> dict:
    """执行一个测试任务。"""
    global _test_current_progress
    _test_current_progress = f"执行测试: {job_type}"

    from runner import run_test
    result = run_test(job_type, params, config)

    if result.get("success"):
        _test_current_progress = f"完成: {job_type}"
    else:
        _test_current_progress = f"失败: {job_type}"
    return result


# ═══════════════════════════════════════════════════════════
# API 端点 — 健康检查 & 状态
# ═══════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """健康检查（VPS 调度器 + 后端转发层共用）。"""
    with _lock:
        p_free = PIPELINE_SLOTS - _pipeline_slots_in_use
        t_free = TEST_SLOTS - _test_slots_in_use
    return jsonify({
        "ok": True,
        "worker_id": WORKER_ID,
        # 流水线槽位（VPS 调度器检查 free_slots/total_slots）
        "free_slots": max(0, p_free),
        "total_slots": PIPELINE_SLOTS,
        "batch_running": _batch_running,
        # 测试槽位（后端转发层检查 busy）
        "test_free_slots": max(0, t_free),
        "test_total_slots": TEST_SLOTS,
        "busy": _test_slots_in_use >= TEST_SLOTS,
        "test_busy": _test_slots_in_use >= TEST_SLOTS,
    })


@app.route("/status", methods=["GET"])
def status():
    """详细状态。"""
    with _lock:
        p_free = PIPELINE_SLOTS - _pipeline_slots_in_use
        t_free = TEST_SLOTS - _test_slots_in_use
    return jsonify({
        "ok": True,
        "worker_id": WORKER_ID,
        "vps_relay_url": VPS_RELAY_URL,
        # 流水线
        "pipeline": {
            "free_slots": max(0, p_free),
            "total_slots": PIPELINE_SLOTS,
            "current_job": _pipeline_current_job,
            "current_progress": _pipeline_current_progress,
            "batch_running": _batch_running,
            "batch_stats": dict(_batch_stats),
        },
        # 测试
        "test": {
            "free_slots": max(0, t_free),
            "total_slots": TEST_SLOTS,
            "current_job": _test_current_job,
            "current_progress": _test_current_progress,
        },
    })


# ═══════════════════════════════════════════════════════════
# API 端点 — 流水线（VPS 调度器调用）
# ═══════════════════════════════════════════════════════════

@app.route("/process", methods=["POST"])
def process():
    """触发认领并处理一个流水线任务（VPS 调度器调用）。"""
    global _pipeline_slots_in_use, _pipeline_current_job
    with _lock:
        if _pipeline_slots_in_use >= PIPELINE_SLOTS:
            return jsonify({"ok": False, "error": "流水线槽位已满"}), 409
        _pipeline_slots_in_use += 1

    job = _claim_next_pipeline_job()
    if not job:
        with _lock:
            _pipeline_slots_in_use -= 1
        return jsonify({"ok": False, "error": "没有待处理任务"}), 404

    with _lock:
        _pipeline_current_job = job

    t = threading.Thread(target=_pipeline_process_job_wrapper, args=(job,), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job["job_id"], "book_id": job.get("book_id", "")})


@app.route("/process-batch", methods=["POST"])
def process_batch():
    """启动批量处理（连续认领多个流水线任务）。"""
    global _batch_running, _batch_stats
    if _batch_running:
        return jsonify({"ok": False, "error": "批量处理已在运行"})
    _batch_stop.clear()
    _batch_stats = {"total": 0, "current": ""}
    t = threading.Thread(target=_batch_loop, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "批量处理已启动"})


@app.route("/batch-status", methods=["GET"])
def batch_status():
    return jsonify({"running": _batch_running, "stats": dict(_batch_stats),
                    "current_progress": _pipeline_current_progress})


@app.route("/batch-stop", methods=["POST"])
def batch_stop():
    _batch_stop.set()
    return jsonify({"ok": True, "message": "停止请求已发送"})


# ═══════════════════════════════════════════════════════════
# API 端点 — 测试（后端转发层调用，同步执行）
# ═══════════════════════════════════════════════════════════

@app.route("/run-sync", methods=["POST"])
def run_sync():
    """同步执行测试（后端转发层直接传参，不经队列）。

    请求体: {job_type: "test_ai", params: {...}, channel: "..."}
    返回: 测试结果（含日志）
    """
    global _test_slots_in_use, _test_current_job, _test_current_progress

    data = request.get_json(silent=True) or {}
    job_type = data.get("job_type", "")
    params = data.get("params", {})
    channel = data.get("channel", "")

    if job_type not in TEST_JOB_TYPES:
        return jsonify({"ok": False, "error": f"未知的测试类型: {job_type}"}), 400

    with _lock:
        if _test_slots_in_use >= TEST_SLOTS:
            return jsonify({"ok": False, "error": "测试槽位已满，请稍后重试"}), 409
        _test_slots_in_use += 1
        _test_current_job = {"job_type": job_type, "channel_name": channel}
        _test_current_progress = f"同步执行: {job_type}"

    try:
        config = _fetch_test_config(channel)
        config["YOUTUBE_CHANNEL_NAME"] = channel or config.get("YOUTUBE_CHANNEL_NAME", "")

        from runner import run_test
        result = run_test(job_type, params, config)

        with _lock:
            _test_slots_in_use -= 1
            _test_current_job = None
            _test_current_progress = ""

        return jsonify({"ok": True, "result": result})
    except Exception as e:
        with _lock:
            _test_slots_in_use -= 1
            _test_current_job = None
            _test_current_progress = ""
        logger.error("同步测试异常: %s", e)
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


# ═══════════════════════════════════════════════════════════
# API 端点 — 通用
# ═══════════════════════════════════════════════════════════

@app.route("/refresh-config", methods=["POST"])
def refresh_config():
    """从 VPS 拉取最新配置。"""
    global _cached_pipeline_config, _cached_test_config
    with _config_lock:
        _cached_pipeline_config = None
        _cached_test_config = None
    channel = request.args.get("channel", "")
    config = _fetch_test_config(channel)
    return jsonify({"ok": True, "config_keys": list(config.keys())})


@app.route("/test-relay", methods=["GET"])
def test_relay():
    """测试 VPS 中继连通性。"""
    if not VPS_RELAY_URL:
        return jsonify({"ok": False, "error": "VPS_RELAY_URL 未配置"})
    try:
        resp = requests.get(f"{VPS_RELAY_URL}/api/status", timeout=10)
        return jsonify({"ok": True, "vps_status": resp.status_code, "vps_reachable": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ═══════════════════════════════════════════════════════════
# 状态面板
# ═══════════════════════════════════════════════════════════

_PANEL_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>统一 Worker</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0f1117;color:#e0e0e0;padding:20px}
h1{color:#7c83fd;margin-bottom:16px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.card{background:#1a1d29;border-radius:8px;padding:16px;margin-bottom:16px;border:1px solid #2a2d39}
.card h3{font-size:13px;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
.card.pipeline h3{color:#8b8dff}.card.test h3{color:#fbbf24}
.info{font-size:14px;line-height:1.8}
.info .key{color:#8b8dff}.info .val{color:#4ade80}
.progress{background:#0f3460;padding:8px;border-radius:4px;color:#fbbf24;font-size:13px;margin:8px 0}
button{background:#533483;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;margin:4px 4px 4px 0}
button:hover{background:#6b46a3}button.danger{background:#dc2626}button.success{background:#16a34a}
.badge{display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px}
.badge.idle{background:#22c55e}.badge.busy{background:#f59e0b}
</style></head><body>
<h1>🔧 统一 Worker</h1>

<div class="card">
  <h3 style="color:#8b8dff">Worker 信息</h3>
  <div class="info" id="worker-info"></div>
</div>

<div class="grid">
  <div class="card pipeline">
    <h3>🚀 流水线槽位</h3>
    <div class="info" id="pipeline-info"></div>
    <div class="progress" id="pipeline-progress">空闲</div>
    <div>
      <button class="success" onclick="batch()">启动批量处理</button>
      <button class="danger" onclick="stopBatch()">停止批量</button>
    </div>
    <div class="info" id="batch-stats" style="margin-top:8px"></div>
  </div>

  <div class="card test">
    <h3>🧪 测试槽位</h3>
    <div class="info" id="test-info"></div>
    <div class="progress" id="test-progress">空闲</div>
  </div>
</div>

<div class="card">
  <h3 style="color:#8b8dff">操作</h3>
  <button onclick="refresh()">刷新配置</button>
  <button onclick="testRelay()">测试VPS中继</button>
  <button onclick="syncBgm()">同步BGM音乐</button>
  <span id="batch-info" style="margin-left:12px"></span>
  <span id="bgm-info" style="margin-left:12px"></span>
</div>

<script>
async function load(){
  try{
    const r=await fetch('/status');const d=await r.json();
    document.getElementById('worker-info').innerHTML=`
      <span class="key">Worker ID:</span> <span class="val">${d.worker_id}</span><br>
      <span class="key">VPS 中继:</span> <span class="val">${d.vps_relay_url||'未配置'}</span>`;
    const p=d.pipeline||{};
    document.getElementById('pipeline-info').innerHTML=`
      <span class="key">槽位:</span> <span class="val">${p.free_slots}/${p.total_slots}</span>
      <span class="badge ${p.free_slots>0?'idle':'busy'}">${p.free_slots>0?'空闲':'忙'}</span>`;
    document.getElementById('pipeline-progress').textContent=p.current_progress||'空闲';
    if(p.current_job) document.getElementById('pipeline-progress').textContent+=' (job='+p.current_job.job_id+')';
    const bs=p.batch_stats||{};
    document.getElementById('batch-stats').innerHTML=`<span class="key">批量总数:</span> ${bs.total||0} | <span class="key">当前:</span> ${bs.current||'无'}`;
    document.getElementById('batch-info').textContent=p.batch_running?'● 批量运行中':'';
    const t=d.test||{};
    document.getElementById('test-info').innerHTML=`
      <span class="key">槽位:</span> <span class="val">${t.free_slots}/${t.total_slots}</span>
      <span class="badge ${t.free_slots>0?'idle':'busy'}">${t.free_slots>0?'空闲':'忙'}</span>`;
    document.getElementById('test-progress').textContent=t.current_progress||'空闲';
  }catch(e){console.error(e)}
}
async function batch(){await fetch('/process-batch',{method:'POST'});load();}
async function stopBatch(){await fetch('/batch-stop',{method:'POST'});load();}
async function refresh(){const r=await fetch('/refresh-config',{method:'POST'});const d=await r.json();alert(d.ok?'配置已刷新':'刷新失败');load();}
async function testRelay(){const r=await fetch('/test-relay');const d=await r.json();alert(d.ok?'VPS中继可达':'失败: '+d.error);}
async function syncBgm(){const r=await fetch('/sync-bgm',{method:'POST'});const d=await r.json();alert(d.ok?'BGM下载已启动':'失败: '+d.error);}
async function loadBgm(){try{const r=await fetch('/bgm-status');const d=await r.json();const s=d.sync_status||{};let txt=`🎵 BGM: ${d.music_count}首`;if(s.running){txt+=` | 下载中 ${s.done}/${s.total} ${s.current||''}`;}else if(s.error){txt+=` | ❌${s.error}`;}document.getElementById('bgm-info').textContent=txt;}catch(e){}}
load();loadBgm();setInterval(load,3000);setInterval(loadBgm,5000);
</script></body></html>"""


@app.route("/sync-bgm", methods=["POST"])
def sync_bgm():
    """手动触发 BGM 音乐同步。"""
    if _bgm_sync_status["running"]:
        return jsonify({"ok": False, "error": "BGM 同步已在运行中"})
    t = threading.Thread(target=_sync_bgm_music, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "BGM 同步已启动"})


@app.route("/bgm-status", methods=["GET"])
def bgm_status():
    """查询 BGM 下载状态 + 本地音乐文件数。"""
    music_count = _count_local_music()
    return jsonify({
        "sync_status": _bgm_sync_status,
        "music_count": music_count,
        "music_dir": MUSIC_DIR,
    })


@app.route("/", methods=["GET"])
def panel():
    return Response(_PANEL_HTML, content_type="text/html; charset=utf-8")


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    _ensure_pipeline_importable()

    # 预热 pipeline 模块
    try:
        logger.info("[启动] 预热 pipeline 模块...")
        from pipeline.config import apply_runtime_config
        apply_runtime_config({"POSTGRES_DSN": POSTGRES_DSN, "OUTPUT_ROOT": OUTPUT_ROOT})
        logger.info("[启动] pipeline 模块预热完成")
    except Exception as e:
        logger.warning("[启动] pipeline 预热失败（首次任务时会重试）: %s", e)

    # 确保 runner 模块可导入
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    os.makedirs(MUSIC_DIR, exist_ok=True)

    # 后台下载 BGM 音乐池（从 HF Datasets 下载 zip 并解压到 /data/music）
    bgm_thread = threading.Thread(target=_sync_bgm_music, daemon=True)
    bgm_thread.start()

    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
