"""运行核心：AI 封面生成（ModelScope 通义万相 / Sensenova 回退）。

对应原 runtime_core.py 行 3602-4686（但不含 _sensenova_* 回退函数，
它们已移到 podcast.py），以及：
- normalize_modelscope_token_pool / build_modelscope_token_pool / clone_*
- _run_qwen_task_with_token_rotation / _run_text_task_with_model_fallback
- _build_youtube_cover_draw_prompt / _request_modelscope_cover_image_url
- _try_generate_cover_with_image_model
- _parse_api_priority_order / _dispatch_cover_text / _dispatch_cover_image
- _2K_IMAGE_SIZES / _map_resolution_to_2k_size
- CoverGenerationPolicyRejectedError
- _is_nonempty_local_file / _persist_cover_fallback_image
- auto_create_youtube_cover
"""

from __future__ import annotations

import json
import os
import random
import time
from io import BytesIO
from PIL import Image
import requests

from . import config as cfg
from .runtime import log
from .audio import download_file


# ============================================================================
# Token 池管理（原文件行 3610-3682）
# ============================================================================

def normalize_modelscope_token_pool(token_value, preserve_list_reference=False):
    if isinstance(token_value, list) and preserve_list_reference:
        raw_items = token_value
    elif isinstance(token_value, (list, tuple, set)):
        raw_items = list(token_value)
    else:
        raw_items = str(token_value or "").split(",")

    normalized = []
    seen = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)

    if isinstance(token_value, list) and preserve_list_reference:
        token_value[:] = normalized
        return token_value
    return normalized


def build_modelscope_token_pool(token_value, shuffle_once=False):
    normalized_tokens = normalize_modelscope_token_pool(token_value)
    if shuffle_once and len(normalized_tokens) > 1:
        random.shuffle(normalized_tokens)
    return normalized_tokens


def clone_modelscope_token_pool(token_value, shuffle_once=False):
    cloned_tokens = normalize_modelscope_token_pool(token_value)
    if shuffle_once and len(cloned_tokens) > 1:
        random.shuffle(cloned_tokens)
    return cloned_tokens


def build_modelscope_token_pool_bundle(token_value, shuffle_once=False):
    base_tokens = build_modelscope_token_pool(token_value, shuffle_once=shuffle_once)
    return {
        "text": list(base_tokens),
        "image": list(base_tokens),
    }


def _get_modelscope_active_tokens(token_pool):
    if isinstance(token_pool, list):
        return normalize_modelscope_token_pool(token_pool, preserve_list_reference=True)
    return normalize_modelscope_token_pool(token_pool)


def _get_modelscope_usage_token_pool(token_source, usage):
    if isinstance(token_source, dict):
        token_pool = token_source.get(usage)
        if isinstance(token_pool, list):
            return normalize_modelscope_token_pool(token_pool, preserve_list_reference=True)
        return normalize_modelscope_token_pool(token_pool)
    return _get_modelscope_active_tokens(token_source)


def _remove_modelscope_token_from_pool(token_pool, token_text):
    if not isinstance(token_pool, list):
        return False
    normalized_pool = normalize_modelscope_token_pool(token_pool, preserve_list_reference=True)
    token_value = str(token_text or "").strip()
    if not token_value:
        return False
    removed = False
    while token_value in normalized_pool:
        normalized_pool.remove(token_value)
        removed = True
    return removed


# ============================================================================
# 错误识别（原文件行 3684-3780）
# ============================================================================

def is_modelscope_daily_quota_exceeded_error(error):
    text = str(error or "")
    lowered = text.lower()
    return (
        "you have exceeded today's quota" in lowered
        or ("try again tomorrow" in lowered and "quota" in lowered)
        or ("error code: 429" in lowered and "quota" in lowered)
    )


def is_modelscope_http_429_error(error):
    text = str(error or "")
    lowered = text.lower()
    return (
        is_modelscope_daily_quota_exceeded_error(error)
        or "429 client error" in lowered
        or "too many requests" in lowered
        or "status code 429" in lowered
        or "error code: 429" in lowered
        or "'code': 429" in lowered
        or '"code":429' in lowered
        or '"code": 429' in lowered
    )


class CoverGenerationPolicyRejectedError(RuntimeError):
    """Raised when the provider rejects image generation input and we should fallback."""


def _extract_http_error_details(error):
    response = getattr(error, "response", None)
    request = getattr(error, "request", None)
    status_code = getattr(response, "status_code", None)
    request_url = str(getattr(request, "url", "") or getattr(response, "url", "") or "")
    response_text = ""
    if response is not None:
        try:
            response_text = str(response.text or "")
        except Exception:
            response_text = ""
    return status_code, request_url, response_text


def is_modelscope_http_401_error(error):
    status_code, request_url, response_text = _extract_http_error_details(error)
    merged_text = "\n".join(part for part in [str(error or ""), response_text, request_url] if part).lower()
    return (
        status_code == 401
        or "401 client error" in merged_text
        or "status code 401" in merged_text
        or "error code: 401" in merged_text
        or "'code': 401" in merged_text
        or '"code":401' in merged_text
        or '"code": 401' in merged_text
        or "unauthorized" in merged_text
    )


def _log_modelscope_token_401(task_label, current_token, error, token_index=None, total_tokens=None, model_name=None):
    status_code, request_url, response_text = _extract_http_error_details(error)
    token_position = ""
    if token_index is not None and total_tokens is not None:
        token_position = f"，token={token_index}/{total_tokens}"
    model_text = f"，model={model_name}" if model_name else ""
    log.error(
        "❌ %s 命中 401，当前 token 疑似无效%s%s。token=%s | request_url=%s | response=%s | 原始错误：%s",
        task_label,
        model_text,
        token_position,
        current_token,
        request_url or "无",
        response_text or f"status_code={status_code}",
        error,
    )


def is_modelscope_image_review_rejection_error(error):
    status_code, request_url, response_text = _extract_http_error_details(error)
    merged_text = "\n".join(part for part in [str(error or ""), response_text] if part).lower()
    request_url_lower = (request_url or "").lower()
    review_keywords = (
        "敏感",
        "审核",
        "review",
        "sensitive",
        "moderation",
        "unsafe",
        "violation",
        "违规",
    )
    if any(keyword in merged_text for keyword in review_keywords):
        return "images/generations" in (merged_text + "\n" + request_url_lower)
    return status_code == 400 and "api-inference.modelscope.cn/v1/images/generations" in request_url_lower


# ============================================================================
# 文件/图片辅助（原文件行 3783-3804）
# ============================================================================

def _is_nonempty_local_file(path):
    return bool(path and os.path.exists(path) and os.path.getsize(path) > 0)


def _persist_cover_fallback_image(source_path, target_path):
    if not _is_nonempty_local_file(source_path):
        return ""

    if os.path.abspath(source_path) == os.path.abspath(target_path):
        return target_path

    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with Image.open(source_path) as img:
            img.convert("RGB").save(target_path, format="JPEG", quality=95)
        if _is_nonempty_local_file(target_path):
            return target_path
    except Exception as e:
        log.warning("原始封面转存为标准 JPEG 失败，将继续直接使用原文件：%s", e)

    return source_path


# ============================================================================
# 超时与轮换（原文件行 3806-3832）
# ============================================================================

def _read_positive_int_runtime_config(name, default_value):
    try:
        value = int(getattr(cfg, name, default_value) or default_value)
    except Exception:
        value = default_value
    return max(1, value)


def _get_modelscope_image_request_timeout():
    return (
        _read_positive_int_runtime_config("MODELSCOPE_IMAGE_CONNECT_TIMEOUT", 300),
        _read_positive_int_runtime_config("MODELSCOPE_IMAGE_READ_TIMEOUT", 300),
    )


def _get_modelscope_image_poll_timeout():
    return (
        _read_positive_int_runtime_config("MODELSCOPE_IMAGE_POLL_CONNECT_TIMEOUT", 300),
        _read_positive_int_runtime_config("MODELSCOPE_IMAGE_POLL_READ_TIMEOUT", 300),
    )


def _sleep_before_next_modelscope_token():
    delay_seconds = _read_positive_int_runtime_config("MODELSCOPE_TOKEN_SWITCH_DELAY_SECONDS", 30)
    log.info("⏳ 不同 token 之间等待 %d 秒，随后继续切换下一个 token...", delay_seconds)
    time.sleep(delay_seconds)


# ============================================================================
# 文本生成（原文件行 3834-4014）
# ============================================================================

def _get_modelscope_text_model_sequence():
    return [
        "Qwen/Qwen3.5-397B-A17B",
        "deepseek-ai/DeepSeek-V4-Pro",
    ]


def _create_modelscope_openai_client(current_token):
    from openai import OpenAI

    return OpenAI(
        base_url="https://api-inference.modelscope.cn/v1",
        api_key=current_token,
    )


def _extract_modelscope_chat_content(response):
    choices = getattr(response, "choices", None) or []
    first_choice = choices[0] if choices else None
    if not first_choice:
        return ""

    message = getattr(first_choice, "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        merged_parts = []
        for item in content:
            if isinstance(item, dict):
                merged_parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                merged_parts.append(str(getattr(item, "text", "") or getattr(item, "content", "") or ""))
        content = "".join(merged_parts)
    return str(content or "").strip()


def _strip_markdown_code_fences(text):
    cleaned_text = str(text or "").strip()
    if cleaned_text.startswith("```json"):
        cleaned_text = cleaned_text[7:]
    if cleaned_text.startswith("```"):
        cleaned_text = cleaned_text[3:]
    if cleaned_text.endswith("```"):
        cleaned_text = cleaned_text[:-3]
    return cleaned_text.strip()


def _run_qwen_task_with_token_rotation(
    task_label,
    token_pool,
    attempt,
    runner,
    max_quota_rounds=2,
    model_name="Qwen/Qwen3.5-397B-A17B",
    invalid_token_pool=None,
):
    active_tokens = _get_modelscope_active_tokens(token_pool)
    if not active_tokens:
        raise ValueError(f"{task_label} 未提供可用的 ModelScope Token。")

    collected_errors = []
    last_quota_error = None

    for quota_round in range(1, max_quota_rounds + 1):
        active_tokens = _get_modelscope_active_tokens(token_pool)
        if not active_tokens:
            break
        quota_hit_this_round = False
        round_tokens = list(active_tokens)
        total_tokens = len(round_tokens)

        for token_index, current_token in enumerate(round_tokens, start=1):
            try:
                return runner(current_token), collected_errors
            except Exception as e:
                if is_modelscope_http_401_error(e):
                    _remove_modelscope_token_from_pool(token_pool, current_token)
                    _remove_modelscope_token_from_pool(invalid_token_pool, current_token)
                    has_next_token = token_index < total_tokens
                    collected_errors.append(f"401 token={current_token} error={e}")
                    _log_modelscope_token_401(
                        task_label=task_label,
                        current_token=current_token,
                        error=e,
                        token_index=token_index,
                        total_tokens=total_tokens,
                        model_name=model_name,
                    )
                    if has_next_token:
                        _sleep_before_next_modelscope_token()
                    continue

                if is_modelscope_http_429_error(e):
                    _remove_modelscope_token_from_pool(token_pool, current_token)
                    has_next_token = token_index < total_tokens
                    quota_hit_this_round = True
                    last_quota_error = e
                    log.warning(
                        "⚠️ %s 第 %d 次失败：当前 token 触发 %s 配额限制，切换下一个 token。"
                        "轮次=%d/%d，token=%d/%d | 原始错误：%s",
                        task_label,
                        attempt,
                        model_name,
                        quota_round,
                        max_quota_rounds,
                        token_index,
                        total_tokens,
                        e,
                    )
                    if has_next_token:
                        _sleep_before_next_modelscope_token()
                    continue

                collected_errors.append(str(e))
                has_next_token = token_index < len(round_tokens)
                log.warning(
                    "⚠️ %s 第 %d 次失败：%s；准备切换下一个 token。",
                    task_label,
                    attempt,
                    e,
                )
                if has_next_token:
                    _sleep_before_next_modelscope_token()

        if not quota_hit_this_round:
            return None, collected_errors
        if not _get_modelscope_active_tokens(token_pool):
            break
        if quota_round < max_quota_rounds:
            _sleep_before_next_modelscope_token()

    raise RuntimeError(
        f"{task_label} 在连续 {max_quota_rounds} 轮切换全部 token 后，仍然触发 "
        f"{model_name} 配额限制，停止运行。最后错误：{last_quota_error}"
    ) from last_quota_error


def _run_text_task_with_model_fallback(task_label, token_pool, attempt, runner, model_sequence=None):
    base_tokens = _get_modelscope_active_tokens(token_pool)
    if not base_tokens:
        raise ValueError(f"{task_label} 未提供可用的 ModelScope Token。")

    resolved_model_sequence = list(model_sequence or _get_modelscope_text_model_sequence())
    collected_errors = []

    for model_index, model_name in enumerate(resolved_model_sequence, start=1):
        model_token_pool = clone_modelscope_token_pool(base_tokens)
        try:
            result, model_errors = _run_qwen_task_with_token_rotation(
                task_label=task_label,
                token_pool=model_token_pool,
                attempt=attempt,
                runner=lambda current_token, current_model=model_name: runner(current_token, current_model),
                model_name=model_name,
                invalid_token_pool=token_pool,
            )
        except RuntimeError as e:
            collected_errors.append(f"{model_name}: {e}")
            if model_index < len(resolved_model_sequence):
                next_model_name = resolved_model_sequence[model_index]
                log.warning(
                    "⚠️ %s 在当前全部可用 token 上触发 %s 配额限制，"
                    "开始自动切换到 %s 再完整重试一轮。",
                    task_label,
                    model_name,
                    next_model_name,
                )
                continue
            raise

        if model_errors:
            collected_errors.extend([f"{model_name}: {msg}" for msg in model_errors])
        if result is not None:
            return result, collected_errors

        if model_index < len(resolved_model_sequence):
            next_model_name = resolved_model_sequence[model_index]
            log.warning(
                "⚠️ %s 在当前全部可用 token 上都生成失败，"
                "开始自动切换到 %s 再完整重试一轮。",
                task_label,
                next_model_name,
            )

    return None, collected_errors


# ============================================================================
# ModelScope 封面绘图提示词（原文件行 4017-4047）
# ============================================================================

def _build_youtube_cover_draw_prompt(book_name, book_desc, current_token, attempt, text_model):
    client = _create_modelscope_openai_client(current_token)

    system_prompt = """角色设定：你是一位顶级 YouTube 封面设计师和 AI 绘图提示词专家。
你的任务是根据我提供的书名和简介，输出一段可直接用于高质量文生图模型的英文提示词。

设计原则：
1. 主体必须直接体现书的内容和情绪，适合 YouTube thumbnail 的高点击构图。
2. 书名对应的中文大字必须作为画面的核心视觉元素，要求醒目、可读、对比强烈。
3. 允许补充一个极短的中文副标题增强点击欲。
4. 输出必须强调高对比、高饱和、戏剧光影、电影感和 16:9 横版构图。

最后约束：
1. 只输出一段英文 prompt，不要输出解释、分析、列表或前缀。
2. 必须包含 --ar 16:9。
3. 画面风格要偏 YouTube thumbnail，而不是普通海报。"""

    user_prompt = f"书名：[{book_name}]\n简介：[{book_desc}]"

    response = client.chat.completions.create(
        model=text_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    draw_prompt = _extract_modelscope_chat_content(response)
    if not draw_prompt:
        raise ValueError("封面提示词接口未返回有效文本内容。")

    log.info("🎨 第 %d 次绘画请求 | 文字模型=%s\n%s", attempt, text_model, draw_prompt)
    return draw_prompt


# ============================================================================
# ModelScope 生图（原文件行 4051-4193）
# ============================================================================

def _request_modelscope_cover_image_url(image_model, current_token, draw_prompt, img_size):
    base_url = "https://api-inference.modelscope.cn/"
    common_headers = {
        "Authorization": f"Bearer {current_token}",
        "Content-Type": "application/json",
    }
    request_timeout = _get_modelscope_image_request_timeout()
    poll_timeout = _get_modelscope_image_poll_timeout()

    log.info("🌅 正在将渲染任务下派给云端高能图层服务器 (X-ModelScope-Async-Mode)... 模型=%s", image_model)
    req_res = requests.post(
        f"{base_url}v1/images/generations",
        headers={**common_headers, "X-ModelScope-Async-Mode": "true"},
        data=json.dumps(
            {
                "model": image_model,
                "size": img_size,
                "prompt": draw_prompt,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        timeout=request_timeout,
    )
    req_res.raise_for_status()
    task_id = req_res.json().get("task_id")
    if not task_id:
        raise ValueError("云端未返回 task_id。")

    log.info("📡 接收到远端任务队列牌号: %s，系统正原地静默巡检直到图块完工...", task_id)

    polls = 0
    poll_interval = 5
    max_polls = 50
    while polls < max_polls:
        polls += 1
        poll_res = requests.get(
            f"{base_url}v1/tasks/{task_id}",
            headers={**common_headers, "X-ModelScope-Task-Type": "image_generation"},
            timeout=poll_timeout,
        )
        poll_res.raise_for_status()
        data = poll_res.json()

        status = data.get("task_status")
        if status == "SUCCEED":
            output_images = data.get("output_images") or []
            if not output_images:
                raise ValueError(f"{image_model} 已成功完成，但返回结果中缺少 output_images。")
            img_url = output_images[0]
            log.info("🖼️ 远端结算完毕，获取到高速下载热链: %s", img_url)
            return img_url
        if status == "FAILED":
            raise ValueError(f"{image_model} 远端画图任务返回 FAILED。")

        time.sleep(poll_interval)

    raise ValueError(f"由于排队压力，远端在 {max_polls * poll_interval} 秒内仍未完成绘图。")


def _try_generate_cover_with_image_model(output_path, draw_prompt, img_size, image_model, token_candidates, invalid_token_pool=None):
    active_tokens = _get_modelscope_active_tokens(token_candidates)
    if not active_tokens:
        return {
            "success": False,
            "errors": ["当前已没有可用的 ModelScope Token。"],
            "failure_count": 0,
            "all_failures_are_429": True,
        }
    failure_messages = []
    failure_count = 0
    http_429_count = 0

    round_tokens = list(active_tokens)
    for token_index, current_token in enumerate(round_tokens, start=1):
        try:
            img_url = _request_modelscope_cover_image_url(image_model, current_token, draw_prompt, img_size)
            if download_file(img_url, output_path):
                log.info(
                    "🎉 %s 已成功生成 YouTube %s 超清海报图并刻录在案: %s",
                    image_model,
                    img_size,
                    os.path.basename(output_path),
                )
                return {
                    "success": True,
                    "errors": [],
                    "failure_count": 0,
                    "all_failures_are_429": False,
                }

            raise ValueError("URL 下载到本地图盘时文件被截断了。")
        except Exception as e:
            if is_modelscope_image_review_rejection_error(e):
                raise CoverGenerationPolicyRejectedError(
                    f"{image_model} 生图请求疑似触发提供商审核拒绝，不再继续重试：{e}"
                ) from e

            failure_count += 1
            failure_messages.append(str(e))
            if is_modelscope_http_401_error(e):
                _remove_modelscope_token_from_pool(token_candidates, current_token)
                _remove_modelscope_token_from_pool(invalid_token_pool, current_token)
                has_next_token = token_index < len(round_tokens)
                _log_modelscope_token_401(
                    task_label=f"{image_model} 生图",
                    current_token=current_token,
                    error=e,
                    token_index=token_index,
                    total_tokens=len(round_tokens),
                    model_name=image_model,
                )
                if has_next_token:
                    _sleep_before_next_modelscope_token()
                continue

            if is_modelscope_http_429_error(e):
                _remove_modelscope_token_from_pool(token_candidates, current_token)
                active_tokens = _get_modelscope_active_tokens(token_candidates)
                has_next_token = bool(active_tokens)
                http_429_count += 1
                log.warning(
                    "⚠️ %s 第 %d 个 token 生图失败：命中 429/限流，准备切换下一个 token。原始错误：%s",
                    image_model,
                    token_index,
                    e,
                )
                if has_next_token:
                    _sleep_before_next_modelscope_token()
                continue

            has_next_token = token_index < len(round_tokens)
            log.warning(
                "⚠️ %s 第 %d 个 token 生图失败：%s；准备切换下一个 token。",
                image_model,
                token_index,
                e,
            )
            if has_next_token:
                _sleep_before_next_modelscope_token()

    return {
        "success": False,
        "errors": failure_messages,
        "failure_count": failure_count,
        "all_failures_are_429": failure_count > 0 and http_429_count == failure_count,
    }


# ============================================================================
# API 优先级调度（原文件行 4203-4260）
# ============================================================================

def _parse_api_priority_order():
    """解析 API_PRIORITY_ORDER 配置项，返回按优先级排列的 API 名称列表"""
    raw = str(getattr(cfg, "API_PRIORITY_ORDER", "modelscope,sensenova") or "modelscope,sensenova").strip()
    api_list = [part.strip().lower() for part in raw.split(",") if part.strip()]
    # 去重但保留顺序
    seen = set()
    result = []
    for api in api_list:
        if api not in seen and api in frozenset({"modelscope", "sensenova"}):
            seen.add(api)
            result.append(api)
    if not result:
        result = ["modelscope", "sensenova"]
    return result


def _dispatch_cover_text(book_name, book_desc, text_token_pool, prompt_generation_attempt):
    """按 API_PRIORITY_ORDER 优先级依次尝试生成封面绘图提示词。

    返回 (draw_prompt, errors) 元组。draw_prompt 为空表示全部失败。
    """
    priority_list = _parse_api_priority_order()
    all_errors = []

    for api_name in priority_list:
        if api_name == "modelscope":
            draw_prompt, model_errors = _run_text_task_with_model_fallback(
                task_label="封面提示词生成",
                token_pool=text_token_pool,
                attempt=prompt_generation_attempt,
                runner=lambda current_token, current_model: _build_youtube_cover_draw_prompt(
                    book_name,
                    book_desc,
                    current_token,
                    prompt_generation_attempt,
                    current_model,
                ),
                model_sequence=_get_modelscope_text_model_sequence(),
            )
            if model_errors:
                all_errors.extend([f"modelscope: {msg}" for msg in model_errors])
            if draw_prompt:
                log.info("✅ [API 优先级] ModelScope 文本生成封面绘图提示词成功。")
                return draw_prompt, all_errors
            next_idx = priority_list.index(api_name) + 1
            log.warning("⚠️ [API 优先级] ModelScope 文本生成失败，检查下一优先级 %s ...",
                        priority_list[next_idx] if next_idx < len(priority_list) else "无")

        elif api_name == "sensenova":
            from .podcast import _call_sensenova_for_draw_prompt

            log.info("🔄 [API 优先级] 切换到 Sensenova (Podcast AI) 生成封面绘图提示词...")
            sensenova_prompt = _call_sensenova_for_draw_prompt(book_name, book_desc)
            if sensenova_prompt:
                log.info("✅ [API 优先级] Sensenova 文本生成封面绘图提示词成功。")
                return sensenova_prompt, all_errors
            error_msg = "sensenova: Sensenova 文本生成全部重试失败"
            all_errors.append(error_msg)
            log.warning("⚠️ [API 优先级] Sensenova 文本生成失败。")

    return "", all_errors


def _dispatch_cover_image(output_path, draw_prompt, resolution, image_token_pool):
    """按 API_PRIORITY_ORDER 优先级依次尝试生成封面图片。

    返回 True 表示生成成功，False 表示全部失败。
    """
    priority_list = _parse_api_priority_order()
    all_image_failures_are_429 = True
    total_image_failures = 0
    all_errors = []

    for api_name in priority_list:
        if api_name == "modelscope":
            res_to_size = {"720p": "1280x720", "1080p": "1920x1080", "1440p": "2560x1440", "4k": "3840x2160"}
            img_size = res_to_size.get(str(resolution).lower(), "1920x1080")
            image_model_sequence = [
                ("qwen/Qwen-Image-2512", "主生图模型"),
                ("Tongyi-MAI/Z-Image-Turbo", "回退生图模型"),
            ]
            any_model_success = False
            for model_index, (image_model, model_label) in enumerate(image_model_sequence, start=1):
                model_result = _try_generate_cover_with_image_model(
                    output_path=output_path,
                    draw_prompt=draw_prompt,
                    img_size=img_size,
                    image_model=image_model,
                    token_candidates=clone_modelscope_token_pool(image_token_pool),
                    invalid_token_pool=image_token_pool,
                )
                if model_result["success"]:
                    return True, []

                if model_result["errors"]:
                    all_errors.extend([f"modelscope/{image_model}: {msg}" for msg in model_result["errors"]])
                total_image_failures += int(model_result["failure_count"] or 0)
                if not model_result["all_failures_are_429"]:
                    all_image_failures_are_429 = False

                if model_index < len(image_model_sequence):
                    log.warning(
                        "⚠️ [API 优先级] %s 在全部 token 上失败，切换到 %s",
                        image_model,
                        image_model_sequence[model_index][0],
                    )

            if any_model_success:
                return True, []

            if total_image_failures > 0 and all_image_failures_are_429:
                next_idx = priority_list.index(api_name) + 1
                log.warning(
                    "⚠️ [API 优先级] ModelScope 图片所有 token 均触发 429，检查下一优先级 %s ...",
                    priority_list[next_idx] if next_idx < len(priority_list) else "无",
                )
            else:
                next_idx = priority_list.index(api_name) + 1
                log.warning(
                    "⚠️ [API 优先级] ModelScope 图片生成失败（非全 429），检查下一优先级 %s ...",
                    priority_list[next_idx] if next_idx < len(priority_list) else "无",
                )

        elif api_name == "sensenova":
            from .podcast import _sensenova_generate_cover_fallback

            log.info("🔄 [API 优先级] 切换到 Sensenova (Podcast AI) 生成封面图片...")
            sensenova_ok = _sensenova_generate_cover_fallback(
                output_path=output_path,
                draw_prompt=draw_prompt,
                resolution=resolution,
            )
            if sensenova_ok:
                log.info("✅ [API 优先级] Sensenova 封面图片生成成功。")
                return True, []
            error_msg = "sensenova: Sensenova 图片生成全部重试失败"
            all_errors.append(error_msg)
            log.warning("⚠️ [API 优先级] Sensenova 图片生成失败。")

    return False, all_errors


# ============================================================================
# 2K 分辨率常量（原文件行 4338-4361）
# ============================================================================

_2K_IMAGE_SIZES = {
    "2:3": (1664, 2496),
    "3:2": (2496, 1664),
    "3:4": (1760, 2368),
    "4:3": (2368, 1760),
    "4:5": (1824, 2272),
    "5:4": (2272, 1824),
    "1:1": (2048, 2048),
    "16:9": (2752, 1536),
    "9:16": (1536, 2752),
    "21:9": (3072, 1376),
    "9:21": (1344, 3136),
}


def _map_resolution_to_2k_size(resolution="1080p"):
    """将标准分辨率映射到最接近的 2K 尺寸（宽x高）"""
    res_to_ratio = {"720p": "4:3", "1080p": "16:9", "1440p": "16:9", "4k": "16:9"}
    ratio = res_to_ratio.get(str(resolution).lower(), "16:9")
    return _2K_IMAGE_SIZES.get(ratio, (2752, 1536))


# ============================================================================
# 顶层封面生成入口（原文件行 4545-4622）
# ============================================================================

def auto_create_youtube_cover(book_name, book_desc, output_path, token, resolution="1080p"):
    """使用 API_PRIORITY_ORDER 配置的优先级链生成 YouTube 封面图。

    支持按优先级顺序尝试 modelscope、sensenova 等 API 服务，
    高优先级服务不可用时自动降级到次优先级。
    """
    priority_list = _parse_api_priority_order()
    needs_modelscope_text = "modelscope" in priority_list
    needs_modelscope_image = "modelscope" in priority_list

    text_token_pool = None
    image_token_pool = None
    # 按需验证 Token 可用性，避免不必要的报错
    if needs_modelscope_text or needs_modelscope_image:
        text_token_pool = _get_modelscope_usage_token_pool(token, "text")
        image_token_pool = _get_modelscope_usage_token_pool(token, "image")

    res_to_size = {"720p": "1280x720", "1080p": "1920x1080", "1440p": "2560x1440", "4k": "3840x2160"}
    img_size = res_to_size.get(str(resolution).lower(), "1920x1080")

    log.info(
        "【🖼️ AI绘图】[%s] 分析有声书意境提取并生成高宽容度爆款 YouTube 封面 (%s)... API 优先级: %s",
        book_name,
        img_size,
        " → ".join(priority_list),
    )

    attempt = 0
    prompt_generation_attempt = 0
    cached_draw_prompt = ""

    while True:
        attempt += 1
        current_cycle_errors = []
        draw_prompt = cached_draw_prompt
        if not draw_prompt:
            prompt_generation_attempt += 1

            # 使用优先级调度获取封面绘图提示词
            draw_prompt, prompt_errors = _dispatch_cover_text(
                book_name=book_name,
                book_desc=book_desc,
                text_token_pool=text_token_pool if needs_modelscope_text else None,
                prompt_generation_attempt=prompt_generation_attempt,
            )
            if prompt_errors:
                current_cycle_errors.extend(prompt_errors)

            if draw_prompt:
                cached_draw_prompt = draw_prompt
            else:
                log.warning(
                    "⚠️ 封面生成模块第 %d 次失败：所有 API 优先级均未能生成有效提示词。错误摘要：%s；系统将持续重试，直到成功为止。",
                    attempt,
                    " | ".join(current_cycle_errors[-5:]) if current_cycle_errors else "无",
                )
                time.sleep(min(30, 5 + attempt))
                continue

        else:
            log.info("🧠 第 %d 次封面重试将复用上一次成功生成的生图提示词，不再重新生成提示词。", attempt)

        # 使用优先级调度生成封面图片
        image_ok, image_errors = _dispatch_cover_image(
            output_path=output_path,
            draw_prompt=draw_prompt,
            resolution=resolution,
            image_token_pool=image_token_pool if needs_modelscope_image else None,
        )
        if image_ok:
            return True

        if image_errors:
            current_cycle_errors.extend(image_errors)

        log.warning(
            "⚠️ 封面生成模块第 %d 次失败：所有 API 优先级均未能生成封面图片。错误摘要：%s；系统将持续复用当前提示词重试，直到成功为止。",
            attempt,
            " | ".join(current_cycle_errors[-6:]) if current_cycle_errors else "无",
        )
        time.sleep(min(30, 5 + attempt))