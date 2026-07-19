"""测试执行器 — 在 HF Space 上运行 4 类测试实验。

复用项目 pipeline/ 的实际生成/下载/上传逻辑，与本机自跑的 backend/api/tests.py 行为一致：
  1. AI 生成测试（SEO 文案 / 封面图片）
  2. YouTube 上传测试（凭证验证 + 频道信息）
  3. TG 音频下载测试（getFile 验证 + 可选实际下载）
  4. BGM 混音测试（下载章节 → 混音 → 输出）

配置从 VPS 中继拉取（凭证不落地 HF）。
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

logger = logging.getLogger("test_runner")


# ═══════════════════════════════════════════════════════════
# 日志捕获（与 backend/api/tests.py 的 _LogCapture 一致）
# ═══════════════════════════════════════════════════════════

class _LogCapture:
    """捕获 stdout 输出。"""

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
    """同时捕获标准 logging 模块输出。"""

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


def _logs_text(cap) -> str:
    if not cap or not cap.text:
        return ""
    return cap.text[-12000:] if len(cap.text) > 12000 else cap.text


def _ensure_pipeline_importable():
    """确保 pipeline 包可导入。"""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in [app_dir, "/app"]:
        pipeline_path = os.path.join(candidate, "pipeline")
        if os.path.isdir(pipeline_path):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return
    logger.error("未找到 pipeline 包目录")


# ═══════════════════════════════════════════════════════════
# 测试 1：AI 生成测试
# ═══════════════════════════════════════════════════════════

def run_ai_test(params: dict, config: dict) -> dict:
    """AI 生成测试（SEO 文案 / 封面图片）。

    params:
      book_name, book_desc, test_type (seo|cover|both), resolution
    """
    book_name = params.get("book_name", "测试书籍：星光彼岸")
    book_desc = params.get("book_desc", "这是一本关于勇气与冒险的奇幻小说。")
    test_type = params.get("test_type", "seo")
    resolution = params.get("resolution", "1080p")

    cap = None
    try:
        _ensure_pipeline_importable()
        from pipeline.config import apply_runtime_config
        apply_runtime_config(config)

        with _capture_logs() as cap:
            print(f"[测试] AI 生成测试开始（类型: {test_type}，书名: {book_name}）", flush=True)
            results = {}
            errors = []

            # ── SEO 文案测试 ──
            if test_type in ("seo", "both"):
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
                        results["seo"] = {
                            "success": False,
                            "error": "MODELSCOPE_TOKEN 未配置",
                        }
                        errors.append("SEO: MODELSCOPE_TOKEN 未配置")
                    else:
                        print(f"[测试] ✓ Token 池就绪（{len(token_pool)} 个）", flush=True)

                        def _seo_runner(current_token, text_model):
                            client = _create_modelscope_openai_client(current_token)
                            system_prompt = (
                                "你是YouTube运营专家。根据书名和简介返回JSON，"
                                '包含 title(标题)、Description(描述)、label(标签)。'
                                "只返回JSON，不要其他文字。"
                            )
                            user_prompt = f"书名：[{book_name}]\n简介：[{book_desc}]"
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
                            results["seo"] = {"success": True, "content": seo_dict}
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
            if test_type in ("cover", "both"):
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
                        results["cover"] = {
                            "success": False,
                            "error": "MODELSCOPE_TOKEN 未配置",
                        }
                        errors.append("封面: MODELSCOPE_TOKEN 未配置")
                    else:
                        print("[测试] → 生成绘图提示词...", flush=True)
                        draw_prompt, prompt_errors = _dispatch_cover_text(
                            book_name=book_name,
                            book_desc=book_desc,
                            text_token_pool=text_pool,
                            prompt_generation_attempt=1,
                        )
                        if not draw_prompt:
                            err_summary = " | ".join(prompt_errors[-5:]) if prompt_errors else "提示词生成失败"
                            results["cover"] = {"success": False, "error": err_summary}
                            errors.append(f"封面提示词: {err_summary}")
                        else:
                            print(f"[测试] ✓ 提示词生成成功，开始生成图片（{resolution}）...", flush=True)
                            results["cover"] = {"draw_prompt": draw_prompt}

                            cover_path = os.path.join(
                                tempfile.gettempdir(),
                                f"test_cover_{int(time.time())}.jpg",
                            )
                            image_ok, image_errors = _dispatch_cover_image(
                                output_path=cover_path,
                                draw_prompt=draw_prompt,
                                resolution=resolution,
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
                                        img.convert("RGB").save(buf, format="JPEG", quality=70)
                                        preview = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
                                except Exception:
                                    pass
                                results["cover"].update({
                                    "success": True,
                                    "size": size,
                                    "preview": preview,
                                })
                                try:
                                    os.remove(cover_path)
                                except Exception:
                                    pass
                            else:
                                err_summary = " | ".join(image_errors[-5:]) if image_errors else "图片生成失败"
                                results["cover"].update({"success": False, "error": err_summary})
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
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": _logs_text(cap),
        }


# ═══════════════════════════════════════════════════════════
# 测试 2：YouTube 上传测试（通过 VPS 中继验证凭证）
# ═══════════════════════════════════════════════════════════

def run_upload_test(params: dict, config: dict) -> dict:
    """YouTube 上传测试 — 通过 VPS 中继验证频道凭证。

    HF Worker 不持有 OAuth token，调用 VPS 中继 /yt-api/<channel>/info 验证。
    """
    import requests as _requests

    channel_name = params.get("channel_name", "") or config.get("YOUTUBE_CHANNEL_NAME", "")
    if not channel_name:
        return {"success": False, "error": "未指定频道名", "logs": ""}

    vps_relay_url = config.get("YOUTUBE_OAUTH_BASE", "").rstrip("/").replace("/yt-api", "")
    # YOUTUBE_OAUTH_BASE 形如 https://vps/yt-api，需提取 base
    if "/yt-api" in vps_relay_url:
        vps_relay_url = vps_relay_url.split("/yt-api")[0]

    cap = None
    try:
        with _capture_logs() as cap:
            print(f"[测试] YouTube 上传测试开始（频道: {channel_name}）", flush=True)
            print(f"[测试] → 通过 VPS 中继验证凭证: {vps_relay_url}", flush=True)

            oauth_base = config.get("YOUTUBE_OAUTH_BASE", "").rstrip("/")
            if not oauth_base:
                return {
                    "success": False,
                    "error": "YOUTUBE_OAUTH_BASE 未配置（VPS 中继地址）",
                    "logs": _logs_text(cap),
                }

            resp = _requests.get(
                f"{oauth_base}/{channel_name}/info",
                timeout=30,
            )
            data = resp.json()

            if not data.get("success"):
                print(f"[测试] ✗ 凭证验证失败: {data.get('error', '')}", flush=True)
                return {
                    "success": False,
                    "error": f"频道「{channel_name}」凭证无效: {data.get('error', '')}",
                    "logs": _logs_text(cap),
                }

            channel_info = {
                "channel_id": data.get("channel_id", ""),
                "title": data.get("title", ""),
                "channel_name": data.get("channel_name", channel_name),
                "uploads_playlist_id": data.get("uploads_playlist_id", ""),
            }
            print(f"[测试] ✓ 频道: {channel_info['title']}，凭证有效", flush=True)
            print("[测试] YouTube 上传测试完成", flush=True)

        return {
            "success": True,
            "channel_name": channel_name,
            "channel_info": channel_info,
            "message": f"✅ 频道「{channel_name}」凭证有效，上传功能可用。\n频道: {channel_info['title']}",
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


# ═══════════════════════════════════════════════════════════
# 测试 3：TG 音频下载测试（通过 VPS 中继）
# ═══════════════════════════════════════════════════════════

def run_tg_download_test(params: dict, config: dict) -> dict:
    """TG 音频下载测试 — 通过 VPS 中继调用 Telegram API。

    HF 无法直连 api.telegram.org，所有 TG API 调用经 VPS 中继 /tg-api/ 代理。
    """
    import requests as _requests

    file_id = params.get("file_id", "")
    bot_user_id = params.get("bot_user_id")
    bot_id = params.get("bot_id")
    do_download = params.get("do_download", False)

    cap = None
    try:
        _ensure_pipeline_importable()
        from pipeline.config import apply_runtime_config
        apply_runtime_config(config)

        with _capture_logs() as cap:
            if not file_id:
                # 尝试从数据库取样本
                from pipeline.db import execute_postgres_fetchone
                from psycopg import sql
                row = execute_postgres_fetchone(
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
                        "error": "未输入 file_id，已从数据库取到一条样本",
                        "sample": {
                            "file_id": row["telegram_file_id"],
                            "bot_user_id": row.get("telegram_bot_user_id"),
                            "bot_id": row.get("telegram_bot_id"),
                            "book_name": row.get("book_name", ""),
                            "chapter_name": row.get("chapter_name", ""),
                        },
                        "logs": _logs_text(cap),
                    }
                return {
                    "success": False,
                    "error": "未输入 file_id，且数据库中无已上传的 TG 缓存样本",
                    "logs": _logs_text(cap),
                }

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
                return {
                    "success": False,
                    "error": "TG_BOT_TOKEN 未配置",
                    "logs": _logs_text(cap),
                }
            print(f"[测试] ✓ 已加载 {len(bot_tokens)} 个 Bot Token", flush=True)

            matched_token, matched_idx = _find_correct_bot_token(
                file_id, bot_tokens,
                known_bot_id=bot_id,
                known_bot_user_id=bot_user_id,
            )

            file_path = None
            if matched_token:
                print("[测试] → 调用 getFile 验证（经 VPS 中继）...", flush=True)
                file_path = _tg_get_file_path(file_id, matched_token, max_retries=2, suppress_invalid=True)

            used_token_idx = matched_idx if matched_idx is not None else 0

            if not file_path:
                skip = {matched_idx} if matched_idx is not None else None
                print("[测试] → 全量尝试所有 Token...", flush=True)
                file_path, found_token, found_idx = _try_all_tokens_get_file_path(
                    file_id, bot_tokens, skip_indices=skip, max_retries=2
                )
                if found_token:
                    matched_token = found_token
                    used_token_idx = found_idx

            if not file_path:
                return {
                    "success": False,
                    "error": "getFile 失败：所有 Bot Token 均无法获取此 file_id",
                    "file_id": file_id,
                    "token_count": len(bot_tokens),
                    "logs": _logs_text(cap),
                }

            print(f"[测试] ✓ getFile 成功！Token #{used_token_idx}，路径: {file_path}", flush=True)

            result = {
                "success": True,
                "file_id": file_id,
                "tg_file_path": file_path,
                "token_index": used_token_idx,
                "token_count": len(bot_tokens),
                "message": f"✅ getFile 成功！Bot Token #{used_token_idx}（共 {len(bot_tokens)} 个）可访问此文件。\n文件路径: {file_path}",
                "logs": _logs_text(cap),
            }

            # 可选实际下载
            if do_download:
                save_path = os.path.join(tempfile.gettempdir(), f"tg_test_{int(time.time())}.mp3")
                print("[测试] → 开始实际下载文件（经 VPS 中继）...", flush=True)
                dl_result = download_audio_from_telegram(
                    file_id, save_path, max_retries=2,
                    bot_id=bot_id, bot_user_id=bot_user_id,
                )
                result["download"] = {
                    "success": dl_result.get("ok", False),
                    "file_size": dl_result.get("file_size", 0),
                    "error": dl_result.get("error", ""),
                }
                if dl_result.get("ok"):
                    size_kb = dl_result.get("file_size", 0) // 1024
                    print(f"[测试] ✓ 下载成功，文件大小: {size_kb} KB", flush=True)
                    result["message"] += f"\n📥 下载成功，文件大小: {size_kb} KB"
                else:
                    print(f"[测试] ✗ 下载失败: {dl_result.get('error', '')}", flush=True)
                    result["message"] += f"\n❌ 下载失败: {dl_result.get('error', '')}"
                if os.path.exists(save_path):
                    try:
                        os.remove(save_path)
                    except Exception:
                        pass

            print("[测试] TG 音频下载测试完成", flush=True)
            return result
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
            "logs": _logs_text(cap),
        }


# ═══════════════════════════════════════════════════════════
# 测试 4：BGM 混音测试
# ═══════════════════════════════════════════════════════════

def run_bgm_test(params: dict, config: dict) -> dict:
    """BGM 混音测试 — 下载章节音频 → 混音 → 输出。

    params:
      input_file: 已有测试音频文件名（留空则随机下载）
      book_id: 指定书籍 ID（随机下载时使用）
      count: 下载章节数
      volume_offset_db, highpass_freq, ...: 混音参数
    """
    import glob as _glob

    cap = None
    test_dir = os.path.join(config.get("OUTPUT_ROOT", "/tmp/output"), "_bgm_test")
    os.makedirs(test_dir, exist_ok=True)

    try:
        _ensure_pipeline_importable()
        from pipeline.config import apply_runtime_config
        apply_runtime_config(config)

        with _capture_logs() as cap:
            input_file = params.get("input_file", "")
            music_dir = config.get("MUSIC_DIR", "/data/music")

            # 如果有输入文件，直接混音
            if input_file:
                input_path = os.path.join(test_dir, input_file)
                if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
                    return {
                        "success": False,
                        "error": f"输入文件不存在: {input_file}",
                        "logs": _logs_text(cap),
                    }
                if not os.path.isdir(music_dir) or not any(os.listdir(music_dir)):
                    return {
                        "success": False,
                        "error": f"音乐目录为空或不存在: {music_dir}",
                        "logs": _logs_text(cap),
                    }

                print(f"[BGM测试] 混音测试开始: {input_file}", flush=True)
                print(f"[BGM测试] 音乐目录: {music_dir}", flush=True)

                mix_params = {
                    "volume_offset_db": params.get("volume_offset_db", -25),
                    "highpass_freq": params.get("highpass_freq", 150),
                    "fade_duration_ms": params.get("fade_duration_ms", 3000),
                    "min_volume_db": params.get("min_volume_db", -40),
                    "dyn_vol": params.get("dyn_vol", True),
                    "spec_shape": params.get("spec_shape", True),
                    "stereo_offset": params.get("stereo_offset", 0.0),
                }
                print(f"[BGM测试] 参数: {mix_params}", flush=True)

                output_name = "bgm_output_" + os.path.splitext(input_file)[0] + ".mp3"
                output_path = os.path.join(test_dir, output_name)

                from pipeline.bgm import mix_with_bgm
                t0 = time.time()
                ok_mix = mix_with_bgm(input_path, output_path, music_dir, **mix_params)
                elapsed = time.time() - t0

                if ok_mix and os.path.exists(output_path):
                    size_mb = round(os.path.getsize(output_path) / (1024 * 1024), 2)
                    print(f"[BGM测试] ✓ 混音完成，耗时 {elapsed:.1f}s，输出: {output_name} ({size_mb} MB)", flush=True)
                    return {
                        "success": True,
                        "output_file": output_name,
                        "output_size_mb": size_mb,
                        "elapsed_seconds": round(elapsed, 1),
                        "message": f"✅ 混音成功！耗时 {elapsed:.1f}s，输出 {size_mb} MB",
                        "logs": _logs_text(cap),
                    }
                else:
                    return {
                        "success": False,
                        "error": "混音失败，请查看日志",
                        "elapsed_seconds": round(elapsed, 1),
                        "logs": _logs_text(cap),
                    }
            else:
                # 随机下载章节音频
                count = max(1, min(params.get("count", 1), 20))
                book_id = params.get("book_id", "")

                from pipeline.db import execute_postgres_fetchone
                from psycopg import sql

                if book_id:
                    row = execute_postgres_fetchone(
                        sql.SQL("SELECT book_id, book_name, book_data FROM public.books WHERE book_id = %s"),
                        (book_id,),
                    )
                else:
                    row = execute_postgres_fetchone(
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
                        "error": "数据库中无可用书籍",
                        "logs": _logs_text(cap),
                    }

                book_id = row["book_id"]
                book_name = row.get("book_name", "")
                raw = row.get("book_data")
                try:
                    book_data = json.loads(raw) if isinstance(raw, str) else raw
                except Exception as e:
                    return {"success": False, "error": f"book_data JSON 解析失败: {e}", "logs": _logs_text(cap)}

                try:
                    from pipeline.pipeline import _extract_chapters_from_book_data
                    chapters = _extract_chapters_from_book_data(book_data)
                except ImportError:
                    chapters = []
                    for key in ("chapters_data", "tingChapterList", "chapterList", "chapters"):
                        val = book_data.get(key) if isinstance(book_data, dict) else None
                        if isinstance(val, list) and val:
                            chapters = val
                            break

                if not chapters:
                    return {"success": False, "error": f"书籍「{book_name}」未提取到章节列表", "logs": _logs_text(cap)}

                import random as _random
                _random.shuffle(chapters)
                selected = chapters[:count]

                from pipeline.audio import download_audio_file
                from pipeline.runtime import sanitize_filename

                print(f"[BGM测试] 从书「{book_name}」({book_id}) 随机选取 {len(selected)} 个章节", flush=True)

                downloaded = []
                for i, ch in enumerate(selected, 1):
                    mp3_url = ch.get("mp3Url", ch.get("playUrl", ch.get("url", "")))
                    title = ch.get("title", ch.get("chapterName", ch.get("name", f"chapter_{i:04d}")))
                    if not mp3_url:
                        continue
                    safe_title = sanitize_filename(str(title))
                    filename = f"{sanitize_filename(book_name)}_{safe_title}_{str(book_id)[:8]}.mp3"[:120]
                    save_path = os.path.join(test_dir, filename)

                    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                        size_mb = round(os.path.getsize(save_path) / (1024 * 1024), 2)
                        downloaded.append({"name": filename, "size_mb": size_mb, "title": title, "reused": True})
                        continue

                    print(f"[BGM测试] 下载 {i}/{len(selected)}: {title}", flush=True)
                    result = download_audio_file(mp3_url, save_path)
                    if result.get("ok"):
                        size_mb = round(os.path.getsize(save_path) / (1024 * 1024), 2)
                        downloaded.append({"name": filename, "size_mb": size_mb, "title": title, "reused": False})

                return {
                    "success": len(downloaded) > 0,
                    "book_name": book_name,
                    "book_id": book_id,
                    "downloaded": downloaded,
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


# ═══════════════════════════════════════════════════════════
# 列出 BGM 测试缓存
# ═══════════════════════════════════════════════════════════

def list_bgm_cache(config: dict) -> dict:
    """列出 BGM 测试缓存的音频文件 + 音乐池信息。"""
    import glob as _glob
    test_dir = os.path.join(config.get("OUTPUT_ROOT", "/tmp/output"), "_bgm_test")
    os.makedirs(test_dir, exist_ok=True)

    supported_exts = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac")
    test_files = []
    output_files = []
    for name in sorted(os.listdir(test_dir), reverse=True):
        path = os.path.join(test_dir, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() not in supported_exts:
            continue
        stat = os.stat(path)
        entry = {
            "name": name,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": stat.st_mtime,
        }
        if name.startswith("bgm_output_"):
            output_files.append(entry)
        else:
            test_files.append(entry)

    music_dir = config.get("MUSIC_DIR", "/data/music")
    music_count = 0
    if os.path.isdir(music_dir):
        for ext in ("*.mp3", "*.wav", "*.flac", "*.ogg", "*.m4a", "*.aac", "*.wma"):
            music_count += len(_glob.glob(os.path.join(music_dir, ext)))

    return {
        "files": test_files + output_files,
        "test_files": test_files,
        "output_files": output_files,
        "music_dir": music_dir,
        "music_count": music_count,
        "test_dir": test_dir,
    }


# ═══════════════════════════════════════════════════════════
# 测试分发器
# ═══════════════════════════════════════════════════════════

TEST_HANDLERS = {
    "test_ai": run_ai_test,
    "test_upload": run_upload_test,
    "test_tg_download": run_tg_download_test,
    "test_bgm": run_bgm_test,
}


def run_test(job_type: str, params: dict, config: dict) -> dict:
    """根据 job_type 分发到对应的测试函数。"""
    handler = TEST_HANDLERS.get(job_type)
    if not handler:
        return {"success": False, "error": f"未知的测试类型: {job_type}", "logs": ""}
    return handler(params, config)
