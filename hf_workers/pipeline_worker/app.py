"""流水线 Worker — HF Space 上的远程 pipeline 执行器。

核心职责：把当前项目的 pipeline/ 打包进镜像，调用 process_book() 执行单书完整流水线：
  TG下载 → BGM混音 → AI封面 → SEO文案 → MP4封装 → YouTube上传（经VPS中继）

不重写流水线逻辑，与轨道A（本机自跑）完全一致。
触发模式：队列认领（PostgreSQL FOR UPDATE SKIP LOCKED）
"""

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import logging
import threading
import traceback
import uuid
from datetime import datetime

import requests
import psycopg
from psycopg import sql as pg_sql
from psycopg.types.json import Jsonb
from flask import Flask, request, jsonify, Response

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")
VPS_RELAY_URL = os.environ.get("VPS_RELAY_URL", "").rstrip("/")
NUM_SLOTS = int(os.environ.get("NUM_SLOTS", "1"))
WORKER_ID = f"hf_{uuid.uuid4().hex[:8]}"

OUTPUT_ROOT = os.environ.get("OUTPUT_ROOT", "/tmp/output")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/music")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pipeline_worker")

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════
# 槽位管理
# ═══════════════════════════════════════════════════════════

_slots_lock = threading.Lock()
_slots_in_use = 0
_current_job: dict | None = None
_current_progress: str = ""

# ═══════════════════════════════════════════════════════════
# 数据库工具
# ═══════════════════════════════════════════════════════════

def _get_conn():
    return psycopg.connect(POSTGRES_DSN, autocommit=False)


def _claim_next_job() -> dict | None:
    """原子认领一个待处理任务（FOR UPDATE SKIP LOCKED）。"""
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
    """从 books 表读取书籍记录（含 book_data）。"""
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


# ═══════════════════════════════════════════════════════════
# 配置拉取（从 VPS 中继）
# ═══════════════════════════════════════════════════════════

_cached_config: dict | None = None
_config_lock = threading.Lock()


def _fetch_pipeline_config(channel: str) -> dict:
    """从 VPS 中继拉取流水线配置（凭证不落地 HF）。"""
    global _cached_config
    with _config_lock:
        if _cached_config and _cached_config.get("_channel") == channel:
            return _cached_config

    if not VPS_RELAY_URL:
        logger.warning("VPS_RELAY_URL 未配置，使用环境变量回退")
        return {
            "POSTGRES_DSN": POSTGRES_DSN,
            "OUTPUT_ROOT": OUTPUT_ROOT,
            "YOUTUBE_CHANNEL_NAME": channel,
        }

    try:
        resp = requests.get(
            f"{VPS_RELAY_URL}/api/pipeline-config",
            params={"channel": channel},
            timeout=15,
        )
        resp.raise_for_status()
        config = resp.json()
        # 确保 POSTGRES_DSN 正确（用 HF Secret 的值，不用 VPS 的）
        config["POSTGRES_DSN"] = POSTGRES_DSN
        config["OUTPUT_ROOT"] = OUTPUT_ROOT
        config["MUSIC_DIR"] = MUSIC_DIR
        config["_channel"] = channel
        with _config_lock:
            _cached_config = config
        logger.info("[配置] 从 VPS 拉取配置成功: channel=%s", channel)
        return config
    except Exception as e:
        logger.error("[配置] 从 VPS 拉取配置失败: %s", e)
        return {
            "POSTGRES_DSN": POSTGRES_DSN,
            "OUTPUT_ROOT": OUTPUT_ROOT,
            "YOUTUBE_CHANNEL_NAME": channel,
        }


# ═══════════════════════════════════════════════════════════
# 回调 VPS
# ═══════════════════════════════════════════════════════════

def _notify_vps(job_id: int, status: str, result: dict, error_message: str, duration_seconds: int):
    """回调通知 VPS（实时感知 + TG 通知）。"""
    if not VPS_RELAY_URL:
        return
    try:
        requests.post(
            f"{VPS_RELAY_URL}/api/callback",
            json={
                "job_id": job_id,
                "status": status,
                "worker_id": WORKER_ID,
                "result": result,
                "error_message": error_message,
                "duration_seconds": duration_seconds,
            },
            timeout=15,
        )
    except Exception as e:
        logger.warning("[回调] 通知 VPS 失败: %s", e)


# ═══════════════════════════════════════════════════════════
# 中间文件清理
# ═══════════════════════════════════════════════════════════

def _cleanup_book_dir(book_dir: str):
    """清理中间文件，释放 HF 磁盘空间。"""
    if not book_dir or not os.path.isdir(book_dir):
        return
    try:
        shutil.rmtree(book_dir, ignore_errors=True)
        logger.info("[清理] 已删除目录: %s", book_dir)
    except Exception as e:
        logger.warning("[清理] 删除失败: %s", e)


# ═══════════════════════════════════════════════════════════
# 核心：执行单书处理
# ═══════════════════════════════════════════════════════════

def _ensure_pipeline_importable():
    """确保 pipeline 包可导入。"""
    # pipeline/ 在 /app/pipeline/
    app_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [app_dir, "/app"]:
        pipeline_path = os.path.join(candidate, "pipeline")
        if os.path.isdir(pipeline_path):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return
    logger.error("未找到 pipeline 包目录")


def _process_one_job(job: dict) -> tuple[str, dict, str]:
    """处理一个任务，返回 (status, result, error_message)。

    核心流程：
    1. 从 books 表读取 book_record
    2. 从 VPS 拉取配置
    3. apply_runtime_config 注入配置
    4. 调用 pipeline.process_book(book_record)
    5. 提取结果（youtube_url 等）
    """
    job_id = job["job_id"]
    book_id = str(job["book_id"])
    channel = job.get("channel_name") or ""

    global _current_progress

    # 1. 读取书籍记录
    _current_progress = f"读取书籍 {book_id}"
    book_record = _fetch_book_record(book_id)
    if not book_record:
        return "failed", {}, f"书籍 {book_id} 不存在"

    book_name = book_record.get("book_name") or f"book_{book_id}"
    _current_progress = f"处理: {book_name}"

    # 2. 拉取配置
    _current_progress = f"拉取配置: {book_name}"
    config = _fetch_pipeline_config(channel)

    # 确保关键配置
    config["YOUTUBE_CHANNEL_NAME"] = channel
    config["PROJECT_FLAG"] = channel
    config["OUTPUT_ROOT"] = OUTPUT_ROOT
    config["MUSIC_DIR"] = MUSIC_DIR
    config["PIPELINE_TASK_ID"] = f"hf_job_{job_id}"  # 停止标志关联
    config["QUIET_RUNTIME_OUTPUT"] = False
    config.setdefault("ONLY_TG_CACHED_BOOKS", True)
    config.setdefault("ENABLE_TG_AUDIO_CACHE", True)

    # 3. 应用配置并导入 pipeline
    _current_progress = f"初始化 pipeline: {book_name}"
    try:
        _ensure_pipeline_importable()
        from pipeline.config import apply_runtime_config
        apply_runtime_config(config)
    except Exception as e:
        err = f"pipeline 初始化失败: {e}\n{traceback.format_exc()}"
        logger.error(err)
        return "failed", {}, err

    # 4. 调用 process_book
    _current_progress = f"执行流水线: {book_name}"
    start_time = time.time()
    try:
        from pipeline.pipeline import process_book
        result = process_book(book_record, run_started_at=datetime.now())
        duration = int(time.time() - start_time)

        # 检查结果
        if hasattr(result, "error") and result.error:
            # 有错误但可能部分成功
            youtube_url = getattr(result, "youtube_url", "") or ""
            if youtube_url:
                # 有 YouTube URL 说明上传成功，即使有其他警告
                result_dict = {
                    "youtube_url": youtube_url,
                    "book_name": book_name,
                    "book_id": book_id,
                    "video_path": getattr(result, "video_path", ""),
                    "duration_seconds": duration,
                }
                _update_book_status(book_id, "success")
                return "done", result_dict, ""
            else:
                _update_book_status(book_id, "failed")
                return "failed", {"book_name": book_name, "duration_seconds": duration}, str(result.error)

        # 成功
        youtube_url = getattr(result, "youtube_url", "") or ""
        result_dict = {
            "youtube_url": youtube_url,
            "book_name": book_name,
            "book_id": book_id,
            "video_path": getattr(result, "video_path", ""),
            "publish_at": getattr(result, "youtube_publish_at", ""),
            "schedule_reason": getattr(result, "youtube_schedule_reason", ""),
            "duration_seconds": duration,
        }
        _update_book_status(book_id, "success")
        _current_progress = f"完成: {book_name} → {youtube_url}"
        return "done", result_dict, ""

    except Exception as e:
        duration = int(time.time() - start_time)
        err = f"process_book 异常: {e}\n{traceback.format_exc()}"
        logger.error(err)
        _update_book_status(book_id, "failed")
        return "failed", {"book_name": book_name, "duration_seconds": duration}, err


def _process_job_wrapper(job: dict):
    """在线程中处理任务，管理槽位 + 清理 + 回调。"""
    global _current_job, _current_progress
    job_id = job["job_id"]
    book_id = str(job.get("book_id", ""))
    book_name = ""

    start_time = time.time()
    status = "failed"
    result = {}
    error_message = ""

    try:
        book_record = _fetch_book_record(book_id)
        book_name = book_record.get("book_name", "") if book_record else ""
        result["book_name"] = book_name
    except Exception:
        pass

    book_dir = None
    try:
        status, result, error_message = _process_one_job(job)

        # 计算 book_dir 用于清理
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

        # 写回数据库
        try:
            _record_result(job_id, status, result, error_message)
        except Exception as e:
            logger.error("写回结果失败: %s", e)

        # 回调 VPS
        _notify_vps(job_id, status, result, error_message, duration)

        # 清理中间文件
        if book_dir:
            _cleanup_book_dir(book_dir)

        # 释放槽位
        global _slots_in_use
        with _slots_lock:
            _slots_in_use -= 1
            _current_job = None
            _current_progress = ""

        logger.info("[完成] job=%s book=%s status=%s duration=%ds", job_id, book_name, status, duration)


# ═══════════════════════════════════════════════════════════
# 批量处理
# ═══════════════════════════════════════════════════════════

_batch_stop = threading.Event()
_batch_running = False
_batch_stats = {"total": 0, "done": 0, "failed": 0, "current": ""}


def _batch_loop():
    """连续认领并处理多个任务，直到没有 pending 或被停止。"""
    global _batch_running
    _batch_running = True
    logger.info("[批量] 开始批量处理")

    while not _batch_stop.is_set():
        # 检查槽位
        with _slots_lock:
            if _slots_in_use >= NUM_SLOTS:
                time.sleep(2)
                continue

        # 认领任务
        job = _claim_next_job()
        if not job:
            logger.info("[批量] 没有待处理任务，批量处理结束")
            break

        _batch_stats["total"] += 1
        _batch_stats["current"] = f"job={job['job_id']} book={job.get('book_id', '')}"

        # 占用槽位
        with _slots_lock:
            _slots_in_use += 1
            _current_job = job

        # 启动处理线程
        t = threading.Thread(target=_process_job_wrapper, args=(job,), daemon=True)
        t.start()

        # 等待槽位释放（单槽模式：等当前完成）
        while True:
            with _slots_lock:
                if _slots_in_use < NUM_SLOTS:
                    break
            if _batch_stop.is_set():
                break
            time.sleep(1)

    _batch_running = False
    _batch_stop.clear()
    logger.info("[批量] 批量处理已结束")


# ═══════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """健康检查。"""
    with _slots_lock:
        free = NUM_SLOTS - _slots_in_use
    return jsonify({
        "ok": True,
        "worker_id": WORKER_ID,
        "free_slots": max(0, free),
        "total_slots": NUM_SLOTS,
        "batch_running": _batch_running,
    })


@app.route("/status", methods=["GET"])
def status():
    """详细状态。"""
    with _slots_lock:
        free = NUM_SLOTS - _slots_in_use
    return jsonify({
        "ok": True,
        "worker_id": WORKER_ID,
        "vps_relay_url": VPS_RELAY_URL,
        "free_slots": max(0, free),
        "total_slots": NUM_SLOTS,
        "current_job": _current_job,
        "current_progress": _current_progress,
        "batch_running": _batch_running,
        "batch_stats": dict(_batch_stats),
    })


@app.route("/process", methods=["POST"])
def process():
    """触发认领并处理一个任务（调度器调用）。"""
    with _slots_lock:
        if _slots_in_use >= NUM_SLOTS:
            return jsonify({"ok": False, "error": "槽位已满"}), 409
        _slots_in_use += 1

    job = _claim_next_job()
    if not job:
        with _slots_lock:
            _slots_in_use -= 1
        return jsonify({"ok": False, "error": "没有待处理任务"}), 404

    with _slots_lock:
        _current_job = job

    t = threading.Thread(target=_process_job_wrapper, args=(job,), daemon=True)
    t.start()

    return jsonify({"ok": True, "job_id": job["job_id"], "book_id": job.get("book_id", "")})


@app.route("/process-batch", methods=["POST"])
def process_batch():
    """启动批量处理（连续认领多个任务）。"""
    global _batch_running, _batch_stats
    if _batch_running:
        return jsonify({"ok": False, "error": "批量处理已在运行"})
    _batch_stop.clear()
    _batch_stats = {"total": 0, "done": 0, "failed": 0, "current": ""}
    t = threading.Thread(target=_batch_loop, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "批量处理已启动"})


@app.route("/batch-status", methods=["GET"])
def batch_status():
    """批量处理进度。"""
    return jsonify({
        "running": _batch_running,
        "stats": dict(_batch_stats),
        "current_progress": _current_progress,
    })


@app.route("/batch-stop", methods=["POST"])
def batch_stop():
    """停止批量处理。"""
    _batch_stop.set()
    return jsonify({"ok": True, "message": "停止请求已发送"})


@app.route("/refresh-config", methods=["POST"])
def refresh_config():
    """从 VPS 拉取最新配置。"""
    global _cached_config
    with _config_lock:
        _cached_config = None
    channel = request.args.get("channel", "")
    config = _fetch_pipeline_config(channel)
    return jsonify({"ok": True, "config_keys": list(config.keys())})


@app.route("/test-telegram", methods=["GET"])
def test_telegram():
    """测试 TG 中继连通性。"""
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
<title>流水线 Worker</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0f1117;color:#e0e0e0;padding:20px}
h1{color:#7c83fd;margin-bottom:16px}
.card{background:#1a1d29;border-radius:8px;padding:16px;margin-bottom:16px;border:1px solid #2a2d39}
.card h3{color:#8b8dff;font-size:13px;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
.info{font-size:14px;line-height:1.8}
.info .key{color:#8b8dff}.info .val{color:#4ade80}
.progress{background:#0f3460;padding:8px;border-radius:4px;color:#fbbf24;font-size:13px;margin:8px 0}
button{background:#533483;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;margin:4px 4px 4px 0}
button:hover{background:#6b46a3}button.danger{background:#dc2626}button.success{background:#16a34a}
</style></head><body>
<h1>🔧 流水线 Worker</h1>
<div class="card">
  <h3>Worker 信息</h3>
  <div class="info" id="worker-info"></div>
</div>
<div class="card">
  <h3>当前任务</h3>
  <div class="progress" id="progress">空闲</div>
</div>
<div class="card">
  <h3>操作</h3>
  <button class="success" onclick="batch()">启动批量处理</button>
  <button class="danger" onclick="stopBatch()">停止批量</button>
  <button onclick="refresh()">刷新配置</button>
  <button onclick="testTg()">测试TG中继</button>
  <span id="batch-info" style="margin-left:12px"></span>
</div>
<div class="card">
  <h3>批量统计</h3>
  <div class="info" id="batch-stats"></div>
</div>
<script>
async function load(){
  try{
    const r=await fetch('/status');const d=await r.json();
    document.getElementById('worker-info').innerHTML=`
      <span class="key">Worker ID:</span> <span class="val">${d.worker_id}</span><br>
      <span class="key">VPS 中继:</span> <span class="val">${d.vps_relay_url||'未配置'}</span><br>
      <span class="key">槽位:</span> <span class="val">${d.free_slots}/${d.total_slots}</span><br>
      <span class="key">批量运行:</span> <span class="val">${d.batch_running?'是':'否'}</span>`;
    document.getElementById('progress').textContent=d.current_progress||'空闲';
    if(d.current_job) document.getElementById('progress').textContent+=' (job='+d.current_job.job_id+')';
    const bs=d.batch_stats||{};
    document.getElementById('batch-stats').innerHTML=`
      <span class="key">总数:</span> ${bs.total||0} |
      <span class="key">当前:</span> ${bs.current||'无'}`;
    document.getElementById('batch-info').textContent=d.batch_running?'● 批量运行中':'';
  }catch(e){console.error(e)}
}
async function batch(){await fetch('/process-batch',{method:'POST'});load();}
async function stopBatch(){await fetch('/batch-stop',{method:'POST'});load();}
async function refresh(){const r=await fetch('/refresh-config',{method:'POST'});const d=await r.json();alert(d.ok?'配置已刷新':'刷新失败');load();}
async function testTg(){const r=await fetch('/test-telegram');const d=await r.json();alert(d.ok?'TG中继可达':'失败: '+d.error);}
load();setInterval(load,3000);
</script></body></html>"""


@app.route("/", methods=["GET"])
def panel():
    return Response(_PANEL_HTML, content_type="text/html; charset=utf-8")


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 确保 pipeline 可导入
    _ensure_pipeline_importable()

    # 预热 pipeline 模块（减少首次任务冷启动时间）
    try:
        logger.info("[启动] 预热 pipeline 模块...")
        from pipeline.config import apply_runtime_config
        apply_runtime_config({"POSTGRES_DSN": POSTGRES_DSN, "OUTPUT_ROOT": OUTPUT_ROOT})
        logger.info("[启动] pipeline 模块预热完成")
    except Exception as e:
        logger.warning("[启动] pipeline 预热失败（首次任务时会重试）: %s", e)

    # 创建输出目录
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    os.makedirs(MUSIC_DIR, exist_ok=True)

    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
