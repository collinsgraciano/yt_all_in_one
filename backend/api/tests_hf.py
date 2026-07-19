"""HF 外包测试实验转发层 — 轻量 HTTP 转发，零算力逻辑。

与轨道A 的 tests.py（重量级，直接跑 pipeline）形成对照：
- 选空闲测试 Worker → 转发请求 → 透传响应
- 不导入 pipeline，不获取串行锁，不消耗 VPS 算力

测试 Worker URL 配置：
  1. global_settings 表的 HF_TEST_WORKER_URLS 键
  2. 环境变量 HF_TEST_WORKER_URLS（逗号分隔）

VPS 中继地址（用于 Worker 健康检查 fallback 等）：
  1. global_settings 表的 VPS_RELAY_URL 键
  2. 环境变量 VPS_RELAY_URL
"""

from __future__ import annotations

import os
import logging

import requests
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/tests-hf", tags=["HF外包测试"])
logger = logging.getLogger(__name__)

# 测试 Worker HTTP 转发超时（秒）
_FORWARD_TIMEOUT = 300
_HEALTH_TIMEOUT = 8


# ═══════════════════════════════════════════════════════════
# 配置读取
# ═══════════════════════════════════════════════════════════

def _get_test_worker_urls() -> list[str]:
    """获取测试 Worker URL 列表（global_settings 优先，环境变量兜底）。"""
    urls: list[str] = []
    try:
        from ..services.config_service import get_global_setting
        raw = get_global_setting("HF_TEST_WORKER_URLS") or ""
        if raw:
            urls = [u.strip() for u in raw.split(",") if u.strip()]
    except Exception:
        pass
    if not urls:
        raw = os.environ.get("HF_TEST_WORKER_URLS", "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
    return urls


def _get_vps_relay_url() -> str:
    """获取 VPS 中继地址。"""
    try:
        from ..services.config_service import get_global_setting
        url = get_global_setting("VPS_RELAY_URL") or ""
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return os.environ.get("VPS_RELAY_URL", "").rstrip("/")


# ═══════════════════════════════════════════════════════════
# Worker 选择
# ═══════════════════════════════════════════════════════════

def _check_worker_health(worker_url: str) -> dict | None:
    """检查测试 Worker 健康状态，返回 {ok, worker_id, busy} 或 None。"""
    try:
        resp = requests.get(f"{worker_url}/health", timeout=_HEALTH_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.debug("测试 Worker 健康检查失败 %s: %s", worker_url, e)
    return None


def _pick_idle_test_worker() -> tuple[str | None, str]:
    """选取一个空闲的测试 Worker，返回 (worker_url, message)。"""
    urls = _get_test_worker_urls()
    if not urls:
        return None, "未配置 HF 测试 Worker URL（请在全局设置中配置 HF_TEST_WORKER_URLS）"

    for url in urls:
        health = _check_worker_health(url)
        if not health or not health.get("ok"):
            continue
        if health.get("busy"):
            continue
        return url, ""

    return None, "所有测试 Worker 均忙碌或离线，请稍后重试（HF Space 冷启动可能需 30-60 秒）"


# ═══════════════════════════════════════════════════════════
# 请求模型（与 tests.py 保持一致，便于前端复用）
# ═══════════════════════════════════════════════════════════

class AiTestRequest(BaseModel):
    book_name: str = "测试书籍：星光彼岸"
    book_desc: str = "这是一本关于勇气与冒险的奇幻小说，讲述主角穿越星海寻找自我救赎的故事。"
    test_type: str = "seo"  # seo | cover | both
    resolution: str = "1080p"


class UploadTestRequest(BaseModel):
    channel_name: str = ""


class TgDownloadTestRequest(BaseModel):
    file_id: str = ""
    bot_user_id: int | None = None
    bot_id: int | None = None
    do_download: bool = False


class BgmDownloadRequest(BaseModel):
    count: int = 1
    book_id: str = ""


class BgmMixRequest(BaseModel):
    input_file: str = ""
    volume_offset_db: int = -25
    highpass_freq: int = 150
    fade_duration_ms: int = 3000
    min_volume_db: int = -40
    dyn_vol: bool = True
    spec_shape: bool = True
    stereo_offset: float = 0.0


# ═══════════════════════════════════════════════════════════
# 转发端点
# ═══════════════════════════════════════════════════════════

@router.get("/workers")
def list_test_workers():
    """查询所有测试 Worker 的健康状态。"""
    urls = _get_test_worker_urls()
    workers = []
    for url in urls:
        health = _check_worker_health(url)
        workers.append({
            "url": url,
            "ok": bool(health and health.get("ok")),
            "busy": health.get("busy", False) if health else None,
            "worker_id": health.get("worker_id", "") if health else "",
        })
    return {
        "workers": workers,
        "total": len(workers),
        "online": sum(1 for w in workers if w["ok"]),
        "idle": sum(1 for w in workers if w["ok"] and not w["busy"]),
    }


def _forward_run_sync(worker_url: str, job_type: str, params: dict, channel: str = "") -> dict:
    """调用 HF 测试 Worker 的 /run-sync 端点同步执行测试。

    测试 Worker 统一使用 /run-sync 接口，通过 job_type 区分测试类型。
    返回格式与本机 tests.py 一致（直接返回 result 内容）。
    """
    try:
        resp = requests.post(
            f"{worker_url}/run-sync",
            json={"job_type": job_type, "params": params, "channel": channel},
            timeout=_FORWARD_TIMEOUT,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("result", data)
        # Worker 返回错误
        return {
            "success": False,
            "error": data.get("error", "Worker 返回未知错误"),
            "logs": data.get("traceback", ""),
        }
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"连接 HF Worker 失败（可能正在冷启动）: {e}", "logs": ""}
    except Exception as e:
        return {"success": False, "error": f"转发异常: {type(e).__name__}: {e}", "logs": ""}


@router.post("/ai")
def test_ai_hf(body: AiTestRequest):
    """转发 AI 测试到 HF 测试 Worker。"""
    worker_url, msg = _pick_idle_test_worker()
    if not worker_url:
        return {"success": False, "error": msg, "logs": ""}
    return _forward_run_sync(worker_url, "test_ai", body.model_dump())


@router.post("/upload")
def test_upload_hf(body: UploadTestRequest):
    """转发 YouTube 上传测试到 HF 测试 Worker。"""
    channel_name = body.channel_name.strip()
    if not channel_name:
        from ..services.config_service import get_global_setting
        channel_name = get_global_setting("YOUTUBE_CHANNEL_NAME") or ""
    if not channel_name:
        return {"success": False, "error": "未指定频道名", "logs": ""}

    worker_url, msg = _pick_idle_test_worker()
    if not worker_url:
        return {"success": False, "error": msg, "logs": ""}
    return _forward_run_sync(worker_url, "test_upload", {"channel_name": channel_name}, channel=channel_name)


@router.post("/tg-download")
def test_tg_download_hf(body: TgDownloadTestRequest):
    """转发 TG 音频下载测试到 HF 测试 Worker。"""
    worker_url, msg = _pick_idle_test_worker()
    if not worker_url:
        return {"success": False, "error": msg, "logs": ""}
    return _forward_run_sync(worker_url, "test_tg_download", body.model_dump())


@router.post("/bgm/download")
def bgm_download_hf(body: BgmDownloadRequest):
    """转发 BGM 音频下载到 HF 测试 Worker。

    Worker 下载的文件保存在 Worker 本地，返回文件名列表。
    后续混音测试可使用返回的文件名作为 input_file。
    """
    worker_url, msg = _pick_idle_test_worker()
    if not worker_url:
        return {"success": False, "error": msg, "logs": ""}
    params = {"count": body.count, "book_id": body.book_id}
    result = _forward_run_sync(worker_url, "test_bgm", params)
    # 记录 Worker URL，供后续混音使用
    if isinstance(result, dict):
        result["_worker_url"] = worker_url
    return result


@router.post("/bgm/mix")
def bgm_mix_hf(body: BgmMixRequest):
    """转发 BGM 混音到 HF 测试 Worker（同步执行）。

    测试 Worker 的 /run-sync 为同步接口，混音完成后直接返回结果。
    input_file 应为之前 /bgm/download 返回的文件名。
    """
    worker_url, msg = _pick_idle_test_worker()
    if not worker_url:
        return {"success": False, "error": msg, "logs": ""}
    if not body.input_file:
        return {"success": False, "error": "请先下载音频并选择输入文件", "logs": ""}
    return _forward_run_sync(worker_url, "test_bgm", body.model_dump())
