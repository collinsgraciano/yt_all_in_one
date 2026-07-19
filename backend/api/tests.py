"""测试实验 API — AI 生成、YouTube 上传、TG 音频下载的可视化测试。

每个测试端点：
1. 从当前全局设置读取配置 → 构建 runtime config
2. 获取 pipeline 串行锁（避免与运行中的任务冲突）
3. 应用 runtime config（pipeline 模块用模块级全局读取配置）
4. 捕获 stdout 日志（pipeline 的 SimpleLogger 基于 print）
5. 执行测试并返回结果、日志、错误信息
"""

from __future__ import annotations

import io
import os
import sys
import json
import time
import base64
import logging
import tempfile
import traceback
import contextlib
import threading
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/tests", tags=["测试实验"])

logger = logging.getLogger(__name__)


# ============================================================================
# 日志捕获：拦截 stdout（pipeline 的 SimpleLogger 基于 print 输出）
# ============================================================================

class _LogCapture:
    """捕获 stdout 输出，同时保留原始输出（让 docker logs 可见）。"""

    def __init__(self, real_stdout):
        self._real = real_stdout
        self.lines: list[str] = []

    def write(self, text):
        try:
            self._real.write(text)
        except Exception:
            pass
        self.lines.append(text)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    @property
    def text(self) -> str:
        return "".join(self.lines)


class _CapturingHandler(logging.Handler):
    """同时捕获标准 logging 模块输出的 Handler。"""

    def __init__(self, capture: _LogCapture):
        super().__init__()
        self._capture = capture

    def emit(self, record):
        try:
            self._capture.lines.append(self.format(record) + "\n")
        except Exception:
            pass


@contextlib.contextmanager
def _capture_logs():
    """上下文管理器：捕获 stdout + logging 输出。"""
    real_stdout = sys.stdout
    capture = _LogCapture(real_stdout)
    handler = _CapturingHandler(capture)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    root = logging.getLogger()

    sys.stdout = capture
    root.addHandler(handler)
    try:
        yield capture
    finally:
        sys.stdout = real_stdout
        root.removeHandler(handler)


# ============================================================================
# Pipeline 准备：导入路径 + 串行锁 + runtime config
# ============================================================================

def _acquire_pipeline_lock() -> tuple[bool, str]:
    """获取 pipeline 串行锁（与任务执行互斥）。"""
    from ..services.task_service import _pipeline_lock, _ensure_pipeline_importable

    _ensure_pipeline_importable()
    if not _pipeline_lock.acquire(timeout=5):
        return False, "Pipeline 正忙（有任务运行中），请稍后再试"
    return True, ""


def _build_test_config(channel_name: str = "") -> dict:
    """从全局设置构建测试用运行配置。"""
    from ..services.config_service import build_runtime_config

    config = build_runtime_config(channel_name)
    # 关闭静默模式，确保 INFO 级别日志也输出
    config["QUIET_RUNTIME_OUTPUT"] = False

    # 注入数据库连接串
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgresql+psycopg://"):
        db_url = db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if db_url:
        config["POSTGRES_DSN"] = db_url

    return config


def _release_pipeline_lock():
    """释放 pipeline 串行锁。"""
    try:
        from ..services.task_service import _pipeline_lock
        _pipeline_lock.release()
    except Exception:
        pass


def _logs_text(cap) -> str:
    """从捕获对象提取日志文本（截断到 8000 字符）。"""
    if not cap or not cap.text:
        return ""
    return cap.text[-8000:] if len(cap.text) > 8000 else cap.text


# ============================================================================
# 请求模型
# ============================================================================

class AiTestRequest(BaseModel):
    book_name: str = "测试书籍：星光彼岸"
    book_desc: str = "这是一本关于勇气与冒险的奇幻小说，讲述主角穿越星海寻找自我救赎的故事。"
    test_type: str = "seo"  # seo | cover | both
    resolution: str = "1080p"


class UploadTestRequest(BaseModel):
    channel_name: str = ""  # 留空则用全局 YOUTUBE_CHANNEL_NAME


class TgDownloadTestRequest(BaseModel):
    file_id: str = ""
    bot_user_id: int | None = None  # 从样本获取，用于匹配正确的 Bot Token
    bot_id: int | None = None  # 从样本获取（备用匹配）
    do_download: bool = False  # 是否实际下载文件（getFile 验证之外）


# ============================================================================
# AI 生成测试
# ============================================================================

@router.post("/ai")
def test_ai(body: AiTestRequest):
    """测试 AI 生成（SEO 文案 / 封面图片）。

    调用 pipeline 的实际生成函数（单轮尝试，非无限重试），
    返回生成结果、错误信息和运行日志。
    """
    ok, err = _acquire_pipeline_lock()
    if not ok:
        return {"success": False, "error": err, "logs": ""}

    cap = None
    try:
        from pipeline.config import apply_runtime_config
        config = _build_test_config()
        apply_runtime_config(config)

        with _capture_logs() as cap:
            print(f"[测试] AI 生成测试开始（类型: {body.test_type}，书名: {body.book_name}）", flush=True)
            results = {}
            errors = []

            # ── SEO 文案测试 ──
            if body.test_type in ("seo", "both"):
                print("[测试] → 开始 SEO 文案测试...", flush=True)
                try:
                    from pipeline.cover import (
                        _get_modelscope_usage_token_pool,
                        _run_text_task_with_model_fallback,
                        _get_modelscope_text_model_sequence,
                        _create_modelscope_openai_client,
                        _extract_modelscope_chat_content,
                        _strip_markdown_code_fences,
                    )

                    token_pool = _get_modelscope_usage_token_pool(
                        config.get("MODELSCOPE_TOKEN", ""), "text"
                    )
                    if not token_pool:
                        print("[测试] ✗ MODELSCOPE_TOKEN 未配置，跳过 SEO 测试", flush=True)
                        results["seo"] = {
                            "success": False,
                            "error": "MODELSCOPE_TOKEN 未配置，无法生成 SEO 文案",
                        }
                        errors.append("SEO: MODELSCOPE_TOKEN 未配置")
                    else:
                        print(f"[测试] ✓ Token 池就绪（{len(token_pool)} 个），开始调用 AI...", flush=True)

                        def _seo_runner(current_token, text_model):
                            client = _create_modelscope_openai_client(current_token)
                            system_prompt = (
                                "你是YouTube运营专家。根据书名和简介返回JSON，"
                                '包含 title(标题)、Description(描述)、label(标签)。'
                                "只返回JSON，不要其他文字。"
                            )
                            user_prompt = f"书名：[{body.book_name}]\n简介：[{body.book_desc}]"
                            response = client.chat.completions.create(
                                model=text_model,
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt},
                                ],
                            )
                            reply = _strip_markdown_code_fences(
                                _extract_modelscope_chat_content(response)
                            )
                            return json.loads(reply)

                        seo_dict, gen_errors = _run_text_task_with_model_fallback(
                            task_label="SEO测试",
                            token_pool=token_pool,
                            attempt=1,
                            runner=_seo_runner,
                            model_sequence=_get_modelscope_text_model_sequence(),
                        )
                        if seo_dict:
                            print("[测试] ✓ SEO 文案生成成功", flush=True)
                            results["seo"] = {
                                "success": True,
                                "content": seo_dict,
                            }
                        else:
                            err_summary = " | ".join(gen_errors[-5:]) if gen_errors else "未知错误"
                            print(f"[测试] ✗ SEO 文案生成失败: {err_summary}", flush=True)
                            results["seo"] = {"success": False, "error": err_summary}
                            errors.append(f"SEO: {err_summary}")
                except Exception as e:
                    tb = traceback.format_exc()
                    results["seo"] = {
                        "success": False,
                        "error": f"{type(e).__name__}: {e}",
                        "traceback": tb,
                    }
                    errors.append(f"SEO 异常: {type(e).__name__}: {e}")

            # ── 封面图片测试 ──
            if body.test_type in ("cover", "both"):
                print("[测试] → 开始封面图片测试...", flush=True)
                try:
                    from pipeline.cover import (
                        _dispatch_cover_text,
                        _dispatch_cover_image,
                        _get_modelscope_usage_token_pool,
                    )

                    token = config.get("MODELSCOPE_TOKEN", "")
                    text_pool = _get_modelscope_usage_token_pool(token, "text")
                    image_pool = _get_modelscope_usage_token_pool(token, "image")

                    if not text_pool and not image_pool:
                        print("[测试] ✗ MODELSCOPE_TOKEN 未配置，跳过封面测试", flush=True)
                        results["cover"] = {
                            "success": False,
                            "error": "MODELSCOPE_TOKEN 未配置，无法生成封面",
                        }
                        errors.append("封面: MODELSCOPE_TOKEN 未配置")
                    else:
                        # 1. 生成绘图提示词
                        print("[测试] → 生成绘图提示词...", flush=True)
                        draw_prompt, prompt_errors = _dispatch_cover_text(
                            book_name=body.book_name,
                            book_desc=body.book_desc,
                            text_token_pool=text_pool,
                            prompt_generation_attempt=1,
                        )
                        if not draw_prompt:
                            print("[测试] ✗ 绘图提示词生成失败", flush=True)
                            err_summary = (
                                " | ".join(prompt_errors[-5:])
                                if prompt_errors
                                else "提示词生成失败"
                            )
                            results["cover"] = {"success": False, "error": err_summary}
                            errors.append(f"封面提示词: {err_summary}")
                        else:
                            print(f"[测试] ✓ 提示词生成成功，开始生成图片（{body.resolution}）...", flush=True)
                            results["cover"] = {"draw_prompt": draw_prompt}

                            # 2. 生成封面图片
                            cover_path = os.path.join(
                                tempfile.gettempdir(),
                                f"test_cover_{int(time.time())}.jpg",
                            )
                            image_ok, image_errors = _dispatch_cover_image(
                                output_path=cover_path,
                                draw_prompt=draw_prompt,
                                resolution=body.resolution,
                                image_token_pool=image_pool,
                            )
                            if image_ok and os.path.exists(cover_path):
                                size = os.path.getsize(cover_path)
                                print(f"[测试] ✓ 封面图片生成成功（{size // 1024} KB）", flush=True)
                                preview = ""
                                try:
                                    from PIL import Image

                                    with Image.open(cover_path) as img:
                                        img.thumbnail((400, 400))
                                        buf = io.BytesIO()
                                        img.convert("RGB").save(
                                            buf, format="JPEG", quality=70
                                        )
                                        preview = (
                                            "data:image/jpeg;base64,"
                                            + base64.b64encode(buf.getvalue()).decode()
                                        )
                                except Exception:
                                    pass
                                results["cover"].update({
                                    "success": True,
                                    "file": cover_path,
                                    "size": size,
                                    "preview": preview,
                                })
                            else:
                                err_summary = (
                                    " | ".join(image_errors[-5:])
                                    if image_errors
                                    else "图片生成失败"
                                )
                                print(f"[测试] ✗ 封面图片生成失败: {err_summary}", flush=True)
                                results["cover"].update({
                                    "success": False,
                                    "error": err_summary,
                                })
                                errors.append(f"封面图片: {err_summary}")
                except Exception as e:
                    tb = traceback.format_exc()
                    results["cover"] = {
                        "success": False,
                        "error": f"{type(e).__name__}: {e}",
                        "traceback": tb,
                    }
                    errors.append(f"封面异常: {type(e).__name__}: {e}")

            print(f"[测试] AI 测试完成，共 {len(errors)} 个错误", flush=True)

        return {
            "success": len(errors) == 0,
            "results": results,
            "logs": _logs_text(cap),
            "errors": errors,
            "config_used": {
                "MODELSCOPE_TOKEN": "***已配置***" if config.get("MODELSCOPE_TOKEN") else "未配置",
                "API_PRIORITY_ORDER": config.get("API_PRIORITY_ORDER", ""),
                "ENABLE_COVER_GENERATION": config.get("ENABLE_COVER_GENERATION"),
                "ENABLE_SEO_GENERATION": config.get("ENABLE_SEO_GENERATION"),
            },
        }
    except ImportError as e:
        return {
            "success": False,
            "error": f"Pipeline 模块导入失败（依赖可能未安装）: {e}",
            "logs": _logs_text(cap),
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": _logs_text(cap),
        }
    finally:
        _release_pipeline_lock()


# ============================================================================
# YouTube 上传测试
# ============================================================================

@router.post("/upload")
def test_upload(body: UploadTestRequest):
    """测试 YouTube 上传凭证是否有效。

    通过认证 + 获取频道信息 + 读取上传列表来验证上传能力。
    凭证有效即代表上传功能可用（上传使用同一套 OAuth 凭证）。
    """
    channel_name = body.channel_name.strip()
    if not channel_name:
        from ..services.config_service import get_global_setting
        channel_name = get_global_setting("YOUTUBE_CHANNEL_NAME") or ""
    if not channel_name:
        return {
            "success": False,
            "error": "未指定频道名，请在下方设置中配置 YOUTUBE_CHANNEL_NAME",
            "logs": "",
        }

    ok, err = _acquire_pipeline_lock()
    if not ok:
        return {"success": False, "error": err, "logs": ""}

    cap = None
    try:
        from pipeline.config import apply_runtime_config
        config = _build_test_config(channel_name)
        apply_runtime_config(config)

        with _capture_logs() as cap:
            print(f"[测试] YouTube 上传测试开始（频道: {channel_name}）", flush=True)
            from pipeline.youtube import (
                authenticate_youtube_from_supabase,
                MissingYouTubeCredentialsError,
            )

            print("[测试] → 正在初始化 YouTube 客户端...", flush=True)
            youtube = authenticate_youtube_from_supabase(channel_name)
            if not youtube:
                print("[测试] ✗ YouTube 客户端初始化失败", flush=True)
                return {
                    "success": False,
                    "error": f"无法初始化 YouTube 客户端（频道「{channel_name}」凭证无效或缺失）。"
                             f"请先在频道管理中完成 OAuth 授权。",
                    "logs": _logs_text(cap),
                }
            print("[测试] ✓ YouTube 客户端初始化成功", flush=True)

            # 获取频道信息
            print("[测试] → 获取频道信息...", flush=True)
            channel_info = {}
            try:
                resp = youtube.channels().list(
                    part="snippet,statistics,contentDetails",
                    mine=True,
                    maxResults=1,
                ).execute()
                items = resp.get("items", [])
                if items:
                    item = items[0]
                    snippet = item.get("snippet", {}) or {}
                    stats = item.get("statistics", {}) or {}
                    content = item.get("contentDetails", {}) or {}
                    related = content.get("relatedPlaylists", {}) or {}
                    channel_info = {
                        "channel_id": item.get("id", ""),
                        "title": snippet.get("title", ""),
                        "description": (snippet.get("description", "") or "")[:300],
                        "subscriber_count": stats.get("subscriberCount", "隐藏"),
                        "video_count": stats.get("videoCount", "0"),
                        "view_count": stats.get("viewCount", "0"),
                        "uploads_playlist_id": related.get("uploads", ""),
                    }
                    print(f"[测试] ✓ 频道: {channel_info.get('title', '')}，视频: {channel_info.get('video_count', '0')} 个", flush=True)
            except Exception as e:
                print(f"[测试] ✗ 获取频道信息失败: {type(e).__name__}: {e}", flush=True)
                return {
                    "success": False,
                    "error": f"获取频道信息失败: {type(e).__name__}: {e}",
                    "logs": _logs_text(cap),
                }

            # 获取最近上传的视频（验证 uploads 列表可读）
            print("[测试] → 读取最近上传列表...", flush=True)
            recent_uploads = []
            try:
                uploads_pid = channel_info.get("uploads_playlist_id", "")
                if uploads_pid:
                    resp = youtube.playlistItems().list(
                        part="contentDetails,snippet",
                        playlistId=uploads_pid,
                        maxResults=5,
                    ).execute()
                    for it in resp.get("items", []):
                        cd = it.get("contentDetails", {}) or {}
                        sn = it.get("snippet", {}) or {}
                        vid = cd.get("videoId", "")
                        if vid:
                            recent_uploads.append({
                                "video_id": vid,
                                "title": sn.get("title", ""),
                                "url": f"https://youtu.be/{vid}",
                            })
                    print(f"[测试] ✓ 获取到 {len(recent_uploads)} 个最近上传视频", flush=True)
            except Exception as e:
                print(f"[测试] ⚠ 读取上传列表失败（非致命）: {e}", flush=True)
                logger.warning("读取上传列表失败（非致命）: %s", e)

            print("[测试] YouTube 上传测试完成", flush=True)

        return {
            "success": True,
            "channel_name": channel_name,
            "channel_info": channel_info,
            "recent_uploads": recent_uploads,
            "message": (
                f"✅ 频道「{channel_name}」凭证有效，上传功能可用。\n"
                f"频道: {channel_info.get('title', '')}\n"
                f"视频总数: {channel_info.get('video_count', '0')}\n"
                f"订阅数: {channel_info.get('subscriber_count', '隐藏')}"
            ),
            "logs": _logs_text(cap),
        }
    except MissingYouTubeCredentialsError as e:
        return {"success": False, "error": str(e), "logs": _logs_text(cap)}
    except ImportError as e:
        return {
            "success": False,
            "error": f"Pipeline 模块导入失败（依赖可能未安装）: {e}",
            "logs": _logs_text(cap),
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": _logs_text(cap),
        }
    finally:
        _release_pipeline_lock()


# ============================================================================
# TG 音频下载测试
# ============================================================================

@router.post("/tg-download")
def test_tg_download(body: TgDownloadTestRequest):
    """测试 Telegram 音频下载。

    使用全局 TG_BOT_TOKEN 配置，根据样本中的 bot_user_id / bot_id 匹配正确的 Bot Token
    （与 pipeline 正式下载逻辑完全一致）。
    1. getFile 验证：验证 Bot Token 能否访问指定 file_id
    2. 可选实际下载：将文件下载到临时目录，验证后自动清理
    """
    file_id = body.file_id.strip()

    # 如果未提供 file_id，尝试从数据库取一个样本
    if not file_id:
        from ..database import fetch_one
        from psycopg import sql

        row = fetch_one(
            sql.SQL(
                """SELECT telegram_file_id, telegram_bot_user_id,
                          telegram_bot_id, book_name, chapter_name
                   FROM public.audiobook_chapters
                   WHERE telegram_file_id IS NOT NULL
                     AND telegram_file_id != ''
                     AND upload_status = 'uploaded'
                   ORDER BY uploaded_at DESC NULLS LAST
                   LIMIT 1"""
            )
        )
        if row and row.get("telegram_file_id"):
            return {
                "success": False,
                "need_sample": True,
                "error": "未输入 file_id，已从数据库取到一条样本，点击「使用样本」按钮后重试",
                "sample": {
                    "file_id": row["telegram_file_id"],
                    "bot_user_id": row.get("telegram_bot_user_id"),
                    "bot_id": row.get("telegram_bot_id"),
                    "book_name": row.get("book_name", ""),
                    "chapter_name": row.get("chapter_name", ""),
                },
                "logs": "",
            }
        return {
            "success": False,
            "error": "未输入 file_id，且数据库中无已上传的 TG 缓存样本",
            "logs": "",
        }

    ok, err = _acquire_pipeline_lock()
    if not ok:
        return {"success": False, "error": err, "logs": ""}

    save_path = None  # 用于 finally 清理临时文件
    cap = None
    try:
        from pipeline.config import apply_runtime_config
        config = _build_test_config()
        apply_runtime_config(config)

        with _capture_logs() as cap:
            print(f"[测试] TG 音频下载测试开始（file_id: {file_id[:40]}...）", flush=True)
            from pipeline.tg_audio import (
                _get_tg_bot_tokens,
                _find_correct_bot_token,
                _tg_get_file_path,
                _try_all_tokens_get_file_path,
                download_audio_from_telegram,
            )

            bot_tokens = _get_tg_bot_tokens()
            if not bot_tokens:
                print("[测试] ✗ 全局 TG_BOT_TOKEN 未配置", flush=True)
                return {
                    "success": False,
                    "error": "全局 TG_BOT_TOKEN 未配置，请在「相关设置」中配置",
                    "logs": _logs_text(cap),
                }
            print(f"[测试] ✓ 已加载 {len(bot_tokens)} 个 Bot Token", flush=True)

            # 用 pipeline 的匹配逻辑找到正确的 token（与正式下载逻辑一致）
            matched_token, matched_idx = _find_correct_bot_token(
                file_id,
                bot_tokens,
                known_bot_id=body.bot_id,
                known_bot_user_id=body.bot_user_id,
            )
            if matched_token:
                print(f"[测试] → 匹配到 Bot Token #{matched_idx}（通过 bot_user_id={body.bot_user_id} 或 bot_id={body.bot_id}）", flush=True)
            else:
                print("[测试] → 未找到匹配的 Token，将全量尝试", flush=True)

            # 第一步：getFile 验证（先试匹配到的 token）
            file_path = None
            if matched_token:
                print("[测试] → 调用 getFile 验证...", flush=True)
                file_path = _tg_get_file_path(
                    file_id, matched_token, max_retries=2, suppress_invalid=True
                )

            used_token_idx = matched_idx if matched_idx is not None else 0

            if not file_path:
                # 全量尝试所有 token（跳过已试过的）
                skip = {matched_idx} if matched_idx is not None else None
                print("[测试] → 匹配的 Token 失败，全量尝试所有 Token...", flush=True)
                file_path, found_token, found_idx = _try_all_tokens_get_file_path(
                    file_id, bot_tokens, skip_indices=skip, max_retries=2
                )
                if found_token:
                    matched_token = found_token
                    used_token_idx = found_idx

            if not file_path:
                print("[测试] ✗ 所有 Bot Token 均无法获取此 file_id", flush=True)
                return {
                    "success": False,
                    "error": (
                        "getFile 失败：所有 Bot Token 均无法获取此 file_id。"
                        "可能原因：file_id 无效、文件已被删除、或 file_id 属于其他 Bot。"
                    ),
                    "file_id": file_id,
                    "token_count": len(bot_tokens),
                    "logs": _logs_text(cap),
                }

            print(f"[测试] ✓ getFile 成功！Token #{used_token_idx}，路径: {file_path}", flush=True)

            result = {
                "success": True,
                "file_id": file_id,
                "tg_file_path": file_path,
                "download_url": f"https://api.telegram.org/file/bot{matched_token}/{file_path}",
                "token_index": used_token_idx,
                "token_count": len(bot_tokens),
                "message": f"✅ getFile 成功！Bot Token #{used_token_idx}（共 {len(bot_tokens)} 个）可访问此文件。\n文件路径: {file_path}",
                "logs": _logs_text(cap),
            }

            # 第二步：可选实际下载
            if body.do_download:
                save_path = os.path.join(
                    tempfile.gettempdir(), f"tg_test_{int(time.time())}.mp3"
                )
                print("[测试] → 开始实际下载文件...", flush=True)
                dl_result = download_audio_from_telegram(
                    file_id, save_path, max_retries=2,
                    bot_id=body.bot_id, bot_user_id=body.bot_user_id,
                )
                result["download"] = {
                    "success": dl_result.get("ok", False),
                    "file_size": dl_result.get("file_size", 0),
                    "error": dl_result.get("error", ""),
                    "cleaned": True,
                }
                if dl_result.get("ok"):
                    size_kb = dl_result.get("file_size", 0) // 1024
                    print(f"[测试] ✓ 下载成功，文件大小: {size_kb} KB", flush=True)
                    result["message"] += f"\n📥 下载成功，文件大小: {size_kb} KB（测试文件已自动清理）"
                else:
                    print(f"[测试] ✗ 下载失败: {dl_result.get('error', '')}", flush=True)
                    result["message"] += f"\n❌ 下载失败: {dl_result.get('error', '')}"

            print("[测试] TG 音频下载测试完成", flush=True)
            return result
    except ImportError as e:
        return {
            "success": False,
            "error": f"Pipeline 模块导入失败（依赖可能未安装）: {e}",
            "logs": _logs_text(cap),
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": _logs_text(cap),
        }
    finally:
        # 清理临时下载文件（测试只需验证，不需保留文件）
        if save_path and os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception:
                pass
        _release_pipeline_lock()


# ============================================================================
# BGM 混音测试 — 随机下载章节、缓存管理、混音测试
# ============================================================================

class BgmDownloadRequest(BaseModel):
    count: int = 1       # 下载几个章节
    book_id: str = ""    # 指定书籍 ID（留空随机）


class BgmMixRequest(BaseModel):
    input_file: str = ""
    volume_offset_db: int = -25
    highpass_freq: int = 150
    fade_duration_ms: int = 3000
    min_volume_db: int = -40
    dyn_vol: bool = True
    spec_shape: bool = True
    stereo_offset: float = 0.0


def _bgm_test_dir() -> str:
    """BGM 测试专用目录（持久保留下载的章节音频）。"""
    from ..settings import settings as app_settings
    return os.path.join(app_settings.output_root, "_bgm_test")


@router.get("/bgm/cache")
def bgm_list_cache():
    """列出 BGM 测试缓存的音频文件 + 音乐池信息。"""
    import glob as _glob
    from ..settings import settings as app_settings

    test_dir = _bgm_test_dir()
    os.makedirs(test_dir, exist_ok=True)

    supported_exts = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac")
    files = []
    for name in sorted(os.listdir(test_dir), reverse=True):
        path = os.path.join(test_dir, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() not in supported_exts:
            continue
        stat = os.stat(path)
        files.append({
            "name": name,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": stat.st_mtime,
        })

    # 音乐池信息
    music_dir = app_settings.music_dir
    music_files_set = set()
    if os.path.isdir(music_dir):
        for ext in ("*.mp3", "*.wav", "*.flac", "*.ogg", "*.m4a", "*.aac", "*.wma"):
            music_files_set.update(_glob.glob(os.path.join(music_dir, ext)))
            music_files_set.update(_glob.glob(os.path.join(music_dir, ext.upper())))

    return {
        "files": files,
        "music_dir": music_dir,
        "music_count": len(music_files_set),
        "test_dir": test_dir,
    }


@router.post("/bgm/download")
def bgm_download(body: BgmDownloadRequest):
    """随机下载章节音频用于 BGM 测试。

    从数据库中随机选取有章节的书，下载指定数量的章节音频到测试目录。
    文件保留在测试目录中，可反复使用，也可手动清理后重新下载。
    """
    from ..database import fetch_one
    from psycopg import sql

    test_dir = _bgm_test_dir()
    os.makedirs(test_dir, exist_ok=True)

    count = max(1, min(body.count, 20))

    # ── 从数据库取一本有章节 URL 的书 ──
    if body.book_id:
        row = fetch_one(
            sql.SQL("SELECT book_id, book_name, book_data FROM public.books WHERE book_id = %s"),
            (body.book_id,),
        )
    else:
        row = fetch_one(
            sql.SQL("""
                SELECT book_id, book_name, book_data
                FROM public.books
                WHERE book_data IS NOT NULL
                  AND book_data::text != 'null'
                  AND book_data::text LIKE %s
                ORDER BY RANDOM()
                LIMIT 1
            """),
            ('%mp3Url%',),
        )

    if not row:
        return {
            "success": False,
            "error": "数据库中无可用书籍（需要有包含 mp3Url 的 book_data）",
            "logs": "",
        }

    book_id = row["book_id"]
    book_name = row.get("book_name", "")
    raw = row.get("book_data")

    try:
        book_data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        return {"success": False, "error": f"book_data JSON 解析失败: {e}", "logs": ""}

    # ── 用 pipeline 的提取函数解析章节列表 ──
    try:
        from pipeline.pipeline import _extract_chapters_from_book_data
        chapters = _extract_chapters_from_book_data(book_data)
    except ImportError:
        # 回退：简单解析 chapters_data
        chapters = []
        for key in ("chapters_data", "tingChapterList", "chapterList", "chapters"):
            val = book_data.get(key) if isinstance(book_data, dict) else None
            if isinstance(val, list) and val:
                chapters = val
                break

    if not chapters:
        return {"success": False, "error": f"书籍「{book_name}」未提取到章节列表", "logs": ""}

    # ── 随机选取章节 ──
    import random as _random
    _random.shuffle(chapters)
    selected = chapters[:count]

    # ── 获取串行锁 ──
    ok, err = _acquire_pipeline_lock()
    if not ok:
        return {"success": False, "error": err, "logs": ""}

    cap = None
    downloaded = []
    try:
        from pipeline.config import apply_runtime_config
        config = _build_test_config()
        apply_runtime_config(config)

        with _capture_logs() as cap:
            from pipeline.audio import download_audio_file
            from pipeline.runtime import sanitize_filename

            print(f"[BGM测试] 从书「{book_name}」({book_id}) 随机选取 {len(selected)} 个章节", flush=True)

            for i, ch in enumerate(selected, 1):
                mp3_url = ch.get("mp3Url", ch.get("playUrl", ch.get("url", "")))
                title = ch.get("title", ch.get("chapterName", ch.get("name", f"chapter_{i:04d}")))

                if not mp3_url:
                    print(f"[BGM测试] 跳过章节 {i}（无 URL）: {title}", flush=True)
                    continue

                safe_title = sanitize_filename(str(title))
                # 文件名: 书名_章节名_bookID前8位.mp3，限制长度
                filename = f"{sanitize_filename(book_name)}_{safe_title}_{str(book_id)[:8]}.mp3"[:120]
                save_path = os.path.join(test_dir, filename)

                if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                    size_mb = round(os.path.getsize(save_path) / (1024 * 1024), 2)
                    print(f"[BGM测试] 复用已存在: {filename} ({size_mb} MB)", flush=True)
                    downloaded.append({
                        "name": filename,
                        "size_mb": size_mb,
                        "title": title,
                        "reused": True,
                    })
                    continue

                print(f"[BGM测试] 下载 {i}/{len(selected)}: {title}", flush=True)
                result = download_audio_file(mp3_url, save_path)

                if result.get("ok"):
                    size_mb = round(os.path.getsize(save_path) / (1024 * 1024), 2)
                    print(f"[BGM测试] ✓ 下载成功: {filename} ({size_mb} MB)", flush=True)
                    downloaded.append({
                        "name": filename,
                        "size_mb": size_mb,
                        "title": title,
                        "reused": False,
                    })
                else:
                    print(f"[BGM测试] ✗ 下载失败: {result.get('error', '')}", flush=True)

        return {
            "success": len(downloaded) > 0,
            "book_name": book_name,
            "book_id": book_id,
            "downloaded": downloaded,
            "logs": _logs_text(cap),
        }
    except ImportError as e:
        return {
            "success": False,
            "error": f"Pipeline 模块导入失败（依赖可能未安装）: {e}",
            "logs": _logs_text(cap),
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": _logs_text(cap),
        }
    finally:
        _release_pipeline_lock()


@router.post("/bgm/clear")
def bgm_clear():
    """清理 BGM 测试缓存目录中的所有音频文件。"""
    test_dir = _bgm_test_dir()

    if not os.path.isdir(test_dir):
        return {"success": True, "deleted": 0, "message": "测试目录不存在"}

    supported_exts = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac")
    deleted = 0
    for name in os.listdir(test_dir):
        path = os.path.join(test_dir, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in supported_exts:
            try:
                os.remove(path)
                deleted += 1
            except Exception:
                pass

    return {"success": True, "deleted": deleted, "message": f"已清理 {deleted} 个测试文件"}


# ============================================================================
# BGM 混音后台任务（异步执行 + 轮询，避免长耗时 HTTP 超时）
# ============================================================================

_bgm_mix_jobs: dict[str, dict] = {}


@router.post("/bgm/mix")
def bgm_mix(body: BgmMixRequest):
    """启动 BGM 混音测试（后台异步执行）。

    BGM 混音涉及 STFT/ISTFT 等高 CPU 操作，单次测试可能耗时数分钟，
    同步 HTTP 请求会因浏览器/代理超时而断开。
    改为后台线程执行 + 前端轮询日志进度，返回 job_id 供前端查询。
    """
    from ..settings import settings as app_settings

    test_dir = _bgm_test_dir()
    input_path = os.path.join(test_dir, body.input_file)

    if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
        return {"success": False, "error": f"输入文件不存在或为空: {body.input_file}", "logs": ""}

    music_dir = app_settings.music_dir
    if not os.path.isdir(music_dir) or not any(os.listdir(music_dir)):
        return {
            "success": False,
            "error": f"音乐目录为空或不存在: {music_dir}，请先上传 BGM 音乐文件",
            "logs": "",
        }

    job_id = uuid.uuid4().hex[:8]
    job = {
        "job_id": job_id,
        "status": "starting",
        "result": None,
        "logs": "",
        "started_at": time.time(),
        "input_file": body.input_file,
        "_cap": None,  # _LogCapture 对象引用（供轮询实时读取日志）
    }
    _bgm_mix_jobs[job_id] = job

    # ── 捕获参数（闭包安全） ──
    _input_path = input_path
    _test_dir = test_dir
    _music_dir = music_dir
    _params = {
        "volume_offset_db": body.volume_offset_db,
        "highpass_freq": body.highpass_freq,
        "fade_duration_ms": body.fade_duration_ms,
        "min_volume_db": body.min_volume_db,
        "dyn_vol": body.dyn_vol,
        "spec_shape": body.spec_shape,
        "stereo_offset": body.stereo_offset,
    }
    _input_name = body.input_file

    def _run_mix_job():
        """后台线程：获取锁 → 应用配置 → 捕获日志 → 执行混音 → 存储结果。"""
        job_ref = _bgm_mix_jobs[job_id]
        cap = None
        try:
            ok, err = _acquire_pipeline_lock()
            if not ok:
                job_ref["status"] = "failed"
                job_ref["result"] = {"success": False, "error": err}
                job_ref["logs"] = err
                return

            from pipeline.config import apply_runtime_config
            config = _build_test_config()
            apply_runtime_config(config)

            with _capture_logs() as cap:
                job_ref["_cap"] = cap  # 供轮询端点实时读取
                print(f"[BGM测试] 混音测试开始: {_input_name}", flush=True)
                print(f"[BGM测试] 音乐目录: {_music_dir}", flush=True)
                print(
                    f"[BGM测试] 参数: vol_offset={_params['volume_offset_db']}dB, "
                    f"hp={_params['highpass_freq']}Hz, "
                    f"fade={_params['fade_duration_ms']}ms, "
                    f"dyn_vol={_params['dyn_vol']}, spec_shape={_params['spec_shape']}",
                    flush=True,
                )
                job_ref["status"] = "running"

                output_name = "bgm_output_" + os.path.splitext(_input_name)[0] + ".mp3"
                output_path = os.path.join(_test_dir, output_name)

                from pipeline.bgm import mix_with_bgm
                t0 = time.time()
                ok_mix = mix_with_bgm(
                    _input_path,
                    output_path,
                    _music_dir,
                    volume_offset_db=_params["volume_offset_db"],
                    highpass_freq=_params["highpass_freq"],
                    fade_duration_ms=_params["fade_duration_ms"],
                    min_volume_db=_params["min_volume_db"],
                    dyn_vol=_params["dyn_vol"],
                    spec_shape=_params["spec_shape"],
                    stereo_offset=_params["stereo_offset"],
                )
                elapsed = time.time() - t0

                if ok_mix and os.path.exists(output_path):
                    size_mb = round(os.path.getsize(output_path) / (1024 * 1024), 2)
                    print(f"[BGM测试] ✓ 混音完成，耗时 {elapsed:.1f}s，输出: {output_name} ({size_mb} MB)", flush=True)
                    job_ref["status"] = "completed"
                    job_ref["result"] = {
                        "success": True,
                        "output_file": output_name,
                        "output_size_mb": size_mb,
                        "elapsed_seconds": round(elapsed, 1),
                        "message": f"✅ 混音成功！耗时 {elapsed:.1f}s，输出文件 {size_mb} MB",
                    }
                else:
                    print(f"[BGM测试] ✗ 混音失败", flush=True)
                    job_ref["status"] = "failed"
                    job_ref["result"] = {
                        "success": False,
                        "error": "混音失败，请查看日志",
                        "elapsed_seconds": round(elapsed, 1),
                    }
                job_ref["logs"] = _logs_text(cap)
        except ImportError as e:
            job_ref["status"] = "failed"
            job_ref["result"] = {"success": False, "error": f"Pipeline 模块导入失败: {e}"}
            job_ref["logs"] = _logs_text(cap) if cap else str(e)
        except Exception as e:
            tb = traceback.format_exc()
            job_ref["status"] = "failed"
            job_ref["result"] = {"success": False, "error": f"{type(e).__name__}: {e}", "traceback": tb}
            job_ref["logs"] = _logs_text(cap) if cap else str(e)
        finally:
            job_ref["_cap"] = None  # 清除引用，后续读取 job["logs"]
            job_ref["logs"] = _logs_text(cap) if cap else job_ref.get("logs", "")
            _release_pipeline_lock()
            # 清理旧任务（保留最近 10 个）
            if len(_bgm_mix_jobs) > 10:
                oldest = sorted(_bgm_mix_jobs.items(), key=lambda x: x[1].get("started_at", 0))
                for k, _ in oldest[:-10]:
                    if k != job_id:
                        _bgm_mix_jobs.pop(k, None)

    thread = threading.Thread(target=_run_mix_job, daemon=True)
    thread.start()

    return {"job_id": job_id, "status": "starting", "message": "BGM 混音测试已启动，正在后台执行..."}


@router.get("/bgm/mix/status")
def bgm_mix_status(job_id: str = ""):
    """轮询 BGM 混音测试进度。

    返回当前状态（starting/running/completed/failed）、实时日志和最终结果。
    日志在运行期间从 _LogCapture 对象实时读取，完成后从 job["logs"] 读取。
    """
    job = _bgm_mix_jobs.get(job_id)
    if not job:
        return {"status": "not_found", "error": f"Job not found: {job_id}", "logs": ""}

    # 运行期间从 _LogCapture 对象实时读取日志
    cap = job.get("_cap")
    if cap and cap.text:
        logs = cap.text[-12000:] if len(cap.text) > 12000 else cap.text
    else:
        logs = job.get("logs", "")

    return {
        "job_id": job_id,
        "status": job["status"],
        "result": job["result"],
        "logs": logs,
        "elapsed_seconds": round(time.time() - job["started_at"], 1),
    }
