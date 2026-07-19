"""测试 Worker — HF Space 上的远程测试实验执行器。

复用项目 pipeline/ 的实际生成/下载/上传逻辑，与本机自跑的 backend/api/tests.py 行为一致。
支持 4 类测试：
  1. AI 生成测试（SEO 文案 / 封面图片）
  2. YouTube 上传测试（凭证验证，经 VPS 中继）
  3. TG 音频下载测试（经 VPS 中继代理 TG API）
  4. BGM 混音测试（下载章节 → 混音 → 输出）

触发模式：
  - 队列认领（PostgreSQL FOR UPDATE SKIP LOCKED）— VPS 调度器触发
  - 同步执行（POST /run-sync）— 直接传参执行，用于快速测试

凭证安全：敏感 Token（TG Bot Token, YouTube OAuth）通过 VPS 中继，不落地 HF。
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import threading
import traceback
import uuid

import requests
import psycopg
from psycopg.types.json import Jsonb
from flask import Flask, request, jsonify, Response

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")
VPS_RELAY_URL = os.environ.get("VPS_RELAY_URL", "").rstrip("/")
WORKER_ID = f"hf_test_{uuid.uuid4().hex[:8]}"

OUTPUT_ROOT = os.environ.get("OUTPUT_ROOT", "/tmp/output")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/music")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test_worker")

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════
# 槽位管理（测试 Worker 单槽，避免并发冲突）
# ═══════════════════════════════════════════════════════════

_slots_lock = threading.Lock()
_busy = False
_current_job: dict | None = None
_current_progress: str = ""

# ═══════════════════════════════════════════════════════════
# 数据库工具
# ═══════════════════════════════════════════════════════════

TEST_JOB_TYPES = ("test_ai", "test_upload", "test_tg_download", "test_bgm")


def _get_conn():
    return psycopg.connect(POSTGRES_DSN, autocommit=False)


def _claim_next_test_job() -> dict | None:
    """原子认领一个待处理测试任务。"""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.hf_jobs
                   SET status = 'processing', worker_id = %s, claimed_at = now()
                   WHERE ctid IN (
                       SELECT ctid FROM public.hf_jobs
                       WHERE job_type = ANY(%s) AND status = 'pending'
                       ORDER BY created_at
                       LIMIT 1
                       FOR UPDATE SKIP LOCKED
                   )
                   RETURNING job_id, job_type, book_id, channel_name, params""",
                (WORKER_ID, list(TEST_JOB_TYPES)),
            )
            row = cur.fetchone()
            if not row:
                return None
            colnames = [d.name for d in cur.description]
            job = dict(zip(colnames, row))
        conn.commit()
    return job


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


def _update_worker_stats(success: bool, duration_seconds: int):
    """更新 Worker 业绩统计。"""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO public.hf_worker_stats (worker_id, worker_type, total_jobs, success_jobs, failed_jobs, total_seconds, last_job_at, last_seen_at, updated_at)
                       VALUES (%s, 'test', 1, %s, %s, %s, now(), now(), now())
                       ON CONFLICT (worker_id) DO UPDATE SET
                         total_jobs = public.hf_worker_stats.total_jobs + 1,
                         success_jobs = public.hf_worker_stats.success_jobs + %s,
                         failed_jobs = public.hf_worker_stats.failed_jobs + %s,
                         total_seconds = public.hf_worker_stats.total_seconds + %s,
                         last_job_at = now(),
                         last_seen_at = now(),
                         updated_at = now()
                    """,
                    (WORKER_ID, 1 if success else 0, 0 if success else 1, duration_seconds,
                     1 if success else 0, 0 if success else 1, duration_seconds),
                )
            conn.commit()
    except Exception as e:
        logger.warning("更新 Worker 统计失败: %s", e)


# ═══════════════════════════════════════════════════════════
# 配置拉取（从 VPS 中继）
# ═══════════════════════════════════════════════════════════

_cached_config: dict | None = None
_config_lock = threading.Lock()


def _fetch_test_config(channel: str = "") -> dict:
    """从 VPS 中继拉取测试配置。"""
    global _cached_config
    with _config_lock:
        if _cached_config and _cached_config.get("_channel") == channel:
            return _cached_config

    if not VPS_RELAY_URL:
        logger.warning("VPS_RELAY_URL 未配置，使用环境变量回退")
        return {
            "POSTGRES_DSN": POSTGRES_DSN,
            "OUTPUT_ROOT": OUTPUT_ROOT,
            "MUSIC_DIR": MUSIC_DIR,
            "YOUTUBE_CHANNEL_NAME": channel,
        }

    try:
        resp = requests.get(
            f"{VPS_RELAY_URL}/api/test-config",
            params={"channel": channel} if channel else {},
            timeout=15,
        )
        resp.raise_for_status()
        config = resp.json()
        config["POSTGRES_DSN"] = POSTGRES_DSN
        config["OUTPUT_ROOT"] = OUTPUT_ROOT
        config["MUSIC_DIR"] = MUSIC_DIR
        config["_channel"] = channel
        with _config_lock:
            _cached_config = config
        logger.info("[配置] 从 VPS 拉取测试配置成功")
        return config
    except Exception as e:
        logger.error("[配置] 从 VPS 拉取配置失败: %s", e)
        return {
            "POSTGRES_DSN": POSTGRES_DSN,
            "OUTPUT_ROOT": OUTPUT_ROOT,
            "MUSIC_DIR": MUSIC_DIR,
            "YOUTUBE_CHANNEL_NAME": channel,
        }


# ═══════════════════════════════════════════════════════════
# 回调 VPS
# ═══════════════════════════════════════════════════════════

def _notify_vps(job_id: int, status: str, result: dict, error_message: str, duration_seconds: int):
    """回调通知 VPS。"""
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
# 核心：执行测试任务
# ═══════════════════════════════════════════════════════════

def _execute_test(job: dict) -> tuple[str, dict, str]:
    """执行一个测试任务，返回 (status, result, error_message)。"""
    global _current_progress

    job_type = job.get("job_type", "")
    channel = job.get("channel_name") or ""
    params = job.get("params") or {}

    _current_progress = f"拉取配置: {job_type}"
    config = _fetch_test_config(channel)
    config["YOUTUBE_CHANNEL_NAME"] = channel or config.get("YOUTUBE_CHANNEL_NAME", "")

    _current_progress = f"执行测试: {job_type}"
    from runner import run_test
    result = run_test(job_type, params, config)

    if result.get("success"):
        _current_progress = f"完成: {job_type}"
        return "done", result, ""
    else:
        _current_progress = f"失败: {job_type}"
        return "failed", result, result.get("error", "")


def _process_test_job(job: dict):
    """在线程中处理测试任务。"""
    global _current_job, _current_progress, _busy
    job_id = job["job_id"]
    job_type = job.get("job_type", "")

    start_time = time.time()
    status = "failed"
    result = {}
    error_message = ""

    try:
        status, result, error_message = _execute_test(job)
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
        _update_worker_stats(status == "done", duration)

        with _slots_lock:
            _busy = False
            _current_job = None
            _current_progress = ""

        logger.info("[完成] job=%s type=%s status=%s duration=%ds", job_id, job_type, status, duration)


# ═══════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """健康检查。"""
    with _slots_lock:
        free = 0 if _busy else 1
    return jsonify({
        "ok": True,
        "worker_id": WORKER_ID,
        "free_slots": free,
        "total_slots": 1,
        "busy": _busy,
    })


@app.route("/status", methods=["GET"])
def status():
    """详细状态。"""
    with _slots_lock:
        free = 0 if _busy else 1
    return jsonify({
        "ok": True,
        "worker_id": WORKER_ID,
        "vps_relay_url": VPS_RELAY_URL,
        "free_slots": free,
        "total_slots": 1,
        "busy": _busy,
        "current_job": _current_job,
        "current_progress": _current_progress,
    })


@app.route("/process", methods=["POST"])
def process():
    """触发认领并处理一个测试任务（VPS 调度器调用）。"""
    global _busy, _current_job

    with _slots_lock:
        if _busy:
            return jsonify({"ok": False, "error": "忙"}), 409
        _busy = True

    job = _claim_next_test_job()
    if not job:
        with _slots_lock:
            _busy = False
        return jsonify({"ok": False, "error": "没有待处理测试任务"}), 404

    with _slots_lock:
        _current_job = job

    t = threading.Thread(target=_process_test_job, args=(job,), daemon=True)
    t.start()

    return jsonify({"ok": True, "job_id": job["job_id"], "job_type": job.get("job_type", "")})


@app.route("/run-sync", methods=["POST"])
def run_sync():
    """同步执行测试（直接传参，不经队列）。

    请求体: {job_type: "test_ai", params: {...}, channel: "..."}
    返回: 测试结果（含日志）
    """
    global _busy, _current_job, _current_progress

    data = request.get_json(silent=True) or {}
    job_type = data.get("job_type", "")
    params = data.get("params", {})
    channel = data.get("channel", "")

    if job_type not in TEST_JOB_TYPES:
        return jsonify({"ok": False, "error": f"未知的测试类型: {job_type}"}), 400

    with _slots_lock:
        if _busy:
            return jsonify({"ok": False, "error": "Worker 忙，请稍后重试"}), 409
        _busy = True
        _current_job = {"job_type": job_type, "channel_name": channel}
        _current_progress = f"同步执行: {job_type}"

    try:
        config = _fetch_test_config(channel)
        config["YOUTUBE_CHANNEL_NAME"] = channel or config.get("YOUTUBE_CHANNEL_NAME", "")

        from runner import run_test
        result = run_test(job_type, params, config)

        with _slots_lock:
            _busy = False
            _current_job = None
            _current_progress = ""

        return jsonify({"ok": True, "result": result})
    except Exception as e:
        with _slots_lock:
            _busy = False
            _current_job = None
            _current_progress = ""
        logger.error("同步测试异常: %s", e)
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/refresh-config", methods=["POST"])
def refresh_config():
    """从 VPS 拉取最新配置。"""
    global _cached_config
    with _config_lock:
        _cached_config = None
    channel = request.args.get("channel", "")
    config = _fetch_test_config(channel)
    return jsonify({"ok": True, "config_keys": list(config.keys())})


@app.route("/test-telegram", methods=["GET"])
def test_telegram():
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
<title>测试 Worker</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0f1117;color:#e0e0e0;padding:20px}
h1{color:#f59e0b;margin-bottom:16px}
.card{background:#1a1d29;border-radius:8px;padding:16px;margin-bottom:16px;border:1px solid #2a2d39}
.card h3{color:#fbbf24;font-size:13px;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
.info{font-size:14px;line-height:1.8}
.info .key{color:#fbbf24}.info .val{color:#4ade80}
.progress{background:#0f3460;padding:8px;border-radius:4px;color:#fbbf24;font-size:13px;margin:8px 0}
button{background:#533483;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;margin:4px 4px 4px 0}
button:hover{background:#6b46a3}button.danger{background:#dc2626}button.success{background:#16a34a}
</style></head><body>
<h1>🧪 测试 Worker</h1>
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
  <button onclick="refresh()">刷新配置</button>
  <button onclick="testTg()">测试VPS中继</button>
</div>
<script>
async function load(){
  try{
    const r=await fetch('/status');const d=await r.json();
    document.getElementById('worker-info').innerHTML=`
      <span class="key">Worker ID:</span> <span class="val">${d.worker_id}</span><br>
      <span class="key">VPS 中继:</span> <span class="val">${d.vps_relay_url||'未配置'}</span><br>
      <span class="key">状态:</span> <span class="val">${d.busy?'忙':'空闲'}</span>`;
    document.getElementById('progress').textContent=d.current_progress||'空闲';
  }catch(e){console.error(e)}
}
async function refresh(){const r=await fetch('/refresh-config',{method:'POST'});const d=await r.json();alert(d.ok?'配置已刷新':'刷新失败');load();}
async function testTg(){const r=await fetch('/test-telegram');const d=await r.json();alert(d.ok?'VPS中继可达':'失败: '+d.error);}
load();setInterval(load,3000);
</script></body></html>"""


@app.route("/", methods=["GET"])
def panel():
    return Response(_PANEL_HTML, content_type="text/html; charset=utf-8")


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    os.makedirs(MUSIC_DIR, exist_ok=True)

    # 确保 runner 模块可导入
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
