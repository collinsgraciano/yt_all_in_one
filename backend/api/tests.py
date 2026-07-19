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

    try:
        from pipeline.config import apply_runtime_config
        config = _build_test_config()
        apply_runtime_config(config)

        with _capture_logs() as cap:
            results = {}
            errors = []

            # ── SEO 文案测试 ──
            if body.test_type in ("seo", "both"):
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
                        results["seo"] = {
                            "success": False,
                            "error": "MODELSCOPE_TOKEN 未配置，无法生成 SEO 文案",
                        }
                        errors.append("SEO: MODELSCOPE_TOKEN 未配置")
                    else:

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
                            results["seo"] = {
                                "success": True,
                                "content": seo_dict,
                            }
                        else:
                            err_summary = " | ".join(gen_errors[-5:]) if gen_errors else "未知错误"
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
                        results["cover"] = {
                            "success": False,
                            "error": "MODELSCOPE_TOKEN 未配置，无法生成封面",
                        }
                        errors.append("封面: MODELSCOPE_TOKEN 未配置")
                    else:
                        # 1. 生成绘图提示词
                        draw_prompt, prompt_errors = _dispatch_cover_text(
                            book_name=body.book_name,
                            book_desc=body.book_desc,
                            text_token_pool=text_pool,
                            prompt_generation_attempt=1,
                        )
                        if not draw_prompt:
                            err_summary = (
                                " | ".join(prompt_errors[-5:])
                                if prompt_errors
                                else "提示词生成失败"
                            )
                            results["cover"] = {"success": False, "error": err_summary}
                            errors.append(f"封面提示词: {err_summary}")
                        else:
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

        return {
            "success": len(errors) == 0,
            "results": results,
            "logs": cap.text[-8000:] if len(cap.text) > 8000 else cap.text,
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
            "logs": "",
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": "",
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

    try:
        from pipeline.config import apply_runtime_config
        config = _build_test_config(channel_name)
        apply_runtime_config(config)

        with _capture_logs() as cap:
            from pipeline.youtube import (
                authenticate_youtube_from_supabase,
                MissingYouTubeCredentialsError,
            )

            youtube = authenticate_youtube_from_supabase(channel_name)
            if not youtube:
                return {
                    "success": False,
                    "error": f"无法初始化 YouTube 客户端（频道「{channel_name}」凭证无效或缺失）。"
                             f"请先在频道管理中完成 OAuth 授权。",
                    "logs": cap.text,
                }

            # 获取频道信息
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
            except Exception as e:
                return {
                    "success": False,
                    "error": f"获取频道信息失败: {type(e).__name__}: {e}",
                    "logs": cap.text,
                }

            # 获取最近上传的视频（验证 uploads 列表可读）
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
            except Exception as e:
                logger.warning("读取上传列表失败（非致命）: %s", e)

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
            "logs": cap.text[-8000:] if len(cap.text) > 8000 else cap.text,
        }
    except MissingYouTubeCredentialsError as e:
        return {"success": False, "error": str(e), "logs": ""}
    except ImportError as e:
        return {
            "success": False,
            "error": f"Pipeline 模块导入失败（依赖可能未安装）: {e}",
            "logs": "",
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": "",
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
    try:
        from pipeline.config import apply_runtime_config
        config = _build_test_config()
        apply_runtime_config(config)

        with _capture_logs() as cap:
            from pipeline.tg_audio import (
                _get_tg_bot_tokens,
                _find_correct_bot_token,
                _tg_get_file_path,
                _try_all_tokens_get_file_path,
                download_audio_from_telegram,
            )

            bot_tokens = _get_tg_bot_tokens()
            if not bot_tokens:
                return {
                    "success": False,
                    "error": "全局 TG_BOT_TOKEN 未配置，请在「相关设置」中配置",
                    "logs": cap.text,
                }

            # 用 pipeline 的匹配逻辑找到正确的 token（与正式下载逻辑一致）
            matched_token, matched_idx = _find_correct_bot_token(
                file_id,
                bot_tokens,
                known_bot_id=body.bot_id,
                known_bot_user_id=body.bot_user_id,
            )

            # 第一步：getFile 验证（先试匹配到的 token）
            file_path = None
            if matched_token:
                file_path = _tg_get_file_path(
                    file_id, matched_token, max_retries=2, suppress_invalid=True
                )

            used_token_idx = matched_idx if matched_idx is not None else 0

            if not file_path:
                # 全量尝试所有 token（跳过已试过的）
                skip = {matched_idx} if matched_idx is not None else None
                file_path, found_token, found_idx = _try_all_tokens_get_file_path(
                    file_id, bot_tokens, skip_indices=skip, max_retries=2
                )
                if found_token:
                    matched_token = found_token
                    used_token_idx = found_idx

            if not file_path:
                return {
                    "success": False,
                    "error": (
                        "getFile 失败：所有 Bot Token 均无法获取此 file_id。"
                        "可能原因：file_id 无效、文件已被删除、或 file_id 属于其他 Bot。"
                    ),
                    "file_id": file_id,
                    "token_count": len(bot_tokens),
                    "logs": cap.text[-8000:] if len(cap.text) > 8000 else cap.text,
                }

            result = {
                "success": True,
                "file_id": file_id,
                "tg_file_path": file_path,
                "download_url": f"https://api.telegram.org/file/bot{matched_token}/{file_path}",
                "token_index": used_token_idx,
                "token_count": len(bot_tokens),
                "message": f"✅ getFile 成功！Bot Token #{used_token_idx}（共 {len(bot_tokens)} 个）可访问此文件。\n文件路径: {file_path}",
                "logs": cap.text[-8000:] if len(cap.text) > 8000 else cap.text,
            }

            # 第二步：可选实际下载
            if body.do_download:
                save_path = os.path.join(
                    tempfile.gettempdir(), f"tg_test_{int(time.time())}.mp3"
                )
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
                    result["message"] += f"\n📥 下载成功，文件大小: {size_kb} KB（测试文件已自动清理）"
                else:
                    result["message"] += f"\n❌ 下载失败: {dl_result.get('error', '')}"

            return result
    except ImportError as e:
        return {
            "success": False,
            "error": f"Pipeline 模块导入失败（依赖可能未安装）: {e}",
            "logs": "",
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": "",
        }
    finally:
        # 清理临时下载文件（测试只需验证，不需保留文件）
        if save_path and os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception:
                pass
        _release_pipeline_lock()
