"""运行核心：主流程编排。

对应原 runtime_core.py 行 6608-8266 + 行 1291-1424 + 行 2869-3093：
- BookResult
- prepare_book_cover_and_seo
- prepare_standard_book_cover_and_seo_with_state
- build_part_result_record
- sync_result_from_split_state (original, before monkey-patch)
- cleanup_completed_split_state_for_book
- process_split_part
- process_split_book
- skip_and_delete_short_book
- process_book
- finalize_book_result (original, before monkey-patch)
- finalize_successful_book_for_project
- persist_youtube_upload_receipt / load_youtube_upload_receipt
- _normalize_local_path_for_compare / _capture_local_file_signature
- build_ordered_split_video_records / build_split_playlist_description
- sync_split_playlist (original, before monkey-patch)
- reconcile_split_part_upload_states
- save_run_summary
- run_pipeline
"""

from __future__ import annotations

import json
import os
import random
import shutil
import time
import datetime as dt_module
from dataclasses import dataclass, field

# ── 章节列表可能的 JSON 键名（兼容旧项目 tingChapterList 和新格式 chapters_data）──
_CHAPTER_LIST_KEYS = (
    "chapters_data", "tingChapterList", "chapterList", "chapters",
    "list", "tingChapters", "sectionList",
)
# ── 章节字段可能的键名 ──
_CHAPTER_ID_KEYS = ("id", "tingChapterId", "chapterId", "chapter_id", "sectionId")
_CHAPTER_TITLE_KEYS = ("title", "chapterName", "name", "tingChapterName", "sectionName")
_CHAPTER_URL_KEYS = ("mp3Url", "playUrl", "downUrl", "url", "filePath", "mediaUrl", "audioUrl", "tingUrl", "fileUrl", "downloadUrl")
_CHAPTER_DURATION_KEYS = ("duration_seconds", "duration", "durationSeconds", "timeMillisecond")


def _extract_chapters_from_book_data(book_data: dict) -> list[dict]:
    """从 book_data JSON 中提取章节列表，兼容多种字段名和嵌套结构。

    旧项目（掌阅 DuckDB）使用 tingChapterList + tingChapterId + chapterName + playUrl，
    新格式使用 chapters_data + id + title + mp3Url。
    本函数统一提取并标准化为 {id, title, mp3Url, ...} 格式。
    """
    if not isinstance(book_data, dict):
        return []

    # 1. 顶层查找章节列表
    raw_chapters = []
    for key in _CHAPTER_LIST_KEYS:
        val = book_data.get(key)
        if isinstance(val, list) and val:
            raw_chapters = val
            break

    # 2. 嵌套在 bookInfo 中查找（掌阅实际结构）
    if not raw_chapters:
        book_info = book_data.get("bookInfo")
        if isinstance(book_info, dict):
            for key in _CHAPTER_LIST_KEYS:
                val = book_info.get(key)
                if isinstance(val, list) and val:
                    raw_chapters = val
                    break

    if not raw_chapters:
        return []

    # 3. 标准化每个章节的字段名
    normalized = []
    for ch in raw_chapters:
        if not isinstance(ch, dict):
            continue
        item = dict(ch)  # 保留原始字段

        # 标准化 id
        if "id" not in item:
            for k in _CHAPTER_ID_KEYS:
                if ch.get(k) is not None:
                    try:
                        item["id"] = int(ch[k]) if str(ch[k]).isdigit() else ch[k]
                    except (ValueError, TypeError):
                        item["id"] = ch[k]
                    break

        # 标准化 title
        if "title" not in item:
            for k in _CHAPTER_TITLE_KEYS:
                if ch.get(k):
                    item["title"] = str(ch[k])
                    break

        # 标准化 mp3Url
        if "mp3Url" not in item:
            for k in _CHAPTER_URL_KEYS:
                if ch.get(k):
                    item["mp3Url"] = str(ch[k])
                    break

        # 标准化 duration_seconds（掌阅用毫秒）
        if "duration_seconds" not in item:
            for k in _CHAPTER_DURATION_KEYS:
                val = ch.get(k)
                if val is not None:
                    try:
                        ms = float(val)
                        # timeMillisecond 是毫秒，需转换为秒
                        if "Millisecond" in k or ms > 100000:
                            item["duration_seconds"] = int(ms / 1000)
                        else:
                            item["duration_seconds"] = int(ms)
                    except (ValueError, TypeError):
                        pass
                    break

        normalized.append(item)

    return normalized

from . import config as cfg
from .runtime import (
    log,
    runtime_console_print,
    clear_runtime_output_if_needed,
    sanitize_filename,
    normalize_text_items,
    build_supabase_text_update,
    make_json_compatible,
    write_json_file,
    read_json_file,
    format_seconds_hhmmss,
)
from .db import (
    execute_postgres_fetchval,
    execute_postgres_fetchall,
    get_public_table_identifier,

    _fetch_books_page_from_database,
    _update_book_status_in_database,
    _update_book_tags_in_database,
    _delete_book_from_database,
)
from .audio import (
    download_file,
    download_audio_file,
    merge_audio_ffmpeg,
    probe_audio_duration_seconds,
    estimate_chapter_duration_seconds,
    get_explicit_chapter_duration_seconds,
    get_explicit_total_book_duration_seconds,
    generate_youtube_timestamps,
    download_chapter_items,
    build_final_audio_from_chapter_paths,
)
from .deepfilter import denoise_audio_paths_parallel
from .bgm import mix_with_bgm
from .cover import (
    auto_create_youtube_cover,
    CoverGenerationPolicyRejectedError,
    _is_nonempty_local_file,
    _persist_cover_fallback_image,
    build_modelscope_token_pool_bundle,
)
from .seo import auto_create_youtube_seo
from .youtube import (
    generate_video,
    authenticate_youtube_from_supabase,
    MissingYouTubeCredentialsError,
    upload_to_youtube_detailed,
    persist_youtube_upload_receipt,
    load_youtube_upload_receipt,
    _extract_youtube_video_id,
    _wait_for_live_video_rows_with_client,
    _build_channel_video_title_index_with_client,
    _build_existing_video_match_from_row,
    _normalize_youtube_title_key,
    build_youtube_payload,
    sync_youtube_playlist,
    _apply_video_match_to_split_part,
    _reset_split_part_upload_state,
)
from .state import (
    get_book_state_table_name,
    build_split_part_plans,
    build_split_plan_signature,
    build_split_state_ref,
    load_split_processing_state,
    reload_split_processing_state,
    initialize_split_processing_state,
    get_split_part_state,
    get_split_playlist_state,
    get_split_shared_assets,
    save_split_processing_state,
    delete_split_processing_state,
    evaluate_split_completion_state,
    _split_part_is_completed,
    _split_part_has_uploaded_video,
    _is_split_playlist_required,
    _book_has_project_status,
    list_interrupted_book_states,
    cleanup_completed_split_states,
    restore_split_shared_assets_from_state,
    persist_split_shared_assets_to_state,
    build_standard_processing_state,
    MIN_BOOK_DURATION_SECONDS,
)

# ---------------------------------------------------------------------------
# BookResult（原文件行 6608-6651）
# ---------------------------------------------------------------------------
@dataclass
class BookResult:
    """单本书的处理结果。"""

    book_id: str = ""
    book_name: str = ""
    category: str = ""
    chapter_count: int = 0
    success_count: int = 0
    chapter_audio_paths: list = field(default_factory=list)
    merged_audio_path: str = ""
    mixed_audio_path: str = ""
    cover_image_path: str = ""
    video_path: str = ""
    seo_text_path: str = ""
    seo_title: str = ""
    seo_description: str = ""
    seo_tags: str = ""
    youtube_chapters: str = ""
    youtube_url: str = ""
    youtube_urls: list = field(default_factory=list)
    youtube_publish_at: str = ""
    youtube_schedule_reason: str = ""
    playlist_id: str = ""
    playlist_url: str = ""
    playlist_title: str = ""
    part_results: list = field(default_factory=list)
    part_count: int = 1
    completed_part_count: int = 0
    playlist_required: bool = False
    playlist_completed: bool = False
    estimated_total_duration_seconds: int = 0
    split_mode: bool = False
    pending_resume: bool = False
    stop_requested: bool = False
    state_path: str = ""
    audio_ready: bool = False
    video_ready: bool = False
    upload_ready: bool = False
    success: bool = False
    skipped: bool = False
    deleted_from_books: bool = False
    skipped_reason: str = ""
    error: str = ""

    # podcast 追加字段（由 podcast.py monkey-patch 写入）
    show_playlist_id: str = ""
    show_image_source: str = ""
    show_podcast_status: str = ""
    show_last_synced_at: str = ""
    show_last_error: str = ""
    playlist_podcast_status: str = ""
    playlist_podcast_image_status: str = ""
    playlist_podcast_image_source: str = ""
    playlist_podcast_last_synced_at: str = ""
    playlist_podcast_last_error: str = ""


# ---------------------------------------------------------------------------
# 封面 & SEO 准备（原文件行 6654-6749）
# ---------------------------------------------------------------------------
def prepare_book_cover_and_seo(result, book_data, book_dir, safe_name, book_name):
    ai_cover_target_path = os.path.join(book_dir, f"{safe_name}_cover.jpg")
    seo_path_ai = os.path.join(book_dir, f"{safe_name}_seo_description.json")
    ai_cover_ready = bool(
        result.cover_image_path
        and os.path.exists(result.cover_image_path)
        and os.path.getsize(result.cover_image_path) > 0
        and os.path.abspath(result.cover_image_path) == os.path.abspath(ai_cover_target_path)
    )
    seo_ready = bool(
        result.seo_text_path
        and os.path.exists(result.seo_text_path)
        and os.path.getsize(result.seo_text_path) > 0
    )
    cover_ready = _is_nonempty_local_file(result.cover_image_path)
    fallback_cover_path = result.cover_image_path if cover_ready and not ai_cover_ready else ""

    pic_url = book_data.get("picUrl", "")
    if pic_url:
        ext = pic_url.split("?")[0].rsplit(".", 1)[-1] or "jpg"
        cover_path = os.path.join(book_dir, f"cover.{ext}")
        if download_file(pic_url, cover_path):
            fallback_cover_path = cover_path
            if not ai_cover_ready:
                result.cover_image_path = cover_path
                cover_ready = True
            log.info("原始封面已准备完成：%s", os.path.basename(cover_path))

    enable_cover = bool(getattr(cfg, "ENABLE_COVER_GENERATION", True))
    skip_existing = bool(getattr(cfg, "SKIP_EXISTING", True))

    if enable_cover and not ai_cover_ready and skip_existing and os.path.exists(ai_cover_target_path) and os.path.getsize(ai_cover_target_path) > 0:
        result.cover_image_path = ai_cover_target_path
        ai_cover_ready = True
        cover_ready = True
        log.info("[%s] 复用已生成的 AI 封面。", book_name)

    enable_seo = bool(getattr(cfg, "ENABLE_SEO_GENERATION", True))

    if enable_seo and not seo_ready and skip_existing and os.path.exists(seo_path_ai) and os.path.getsize(seo_path_ai) > 0:
        seo_dict = read_json_file(seo_path_ai, default={}) or {}
        if isinstance(seo_dict, dict):
            result.seo_text_path = seo_path_ai
            result.seo_title = seo_dict.get("title", "")
            result.seo_description = seo_dict.get("Description", "")
            result.seo_tags = seo_dict.get("label", "")
            seo_ready = True
            log.info("[%s] 复用已生成的 SEO 文案。", book_name)

    needs_modelscope_token = (enable_cover and not ai_cover_ready) or (enable_seo and not seo_ready)
    token_pool = {}
    if needs_modelscope_token:
        from .db import resolve_modelscope_token

        resolved_modelscope_token = resolve_modelscope_token(str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "")).strip())
        token_pool = build_modelscope_token_pool_bundle(resolved_modelscope_token, shuffle_once=True)
        if not any(token_pool.values()):
            raise RuntimeError("未能解析出可用的 ModelScope Token，无法继续 AI 生成。")

    if enable_cover and not ai_cover_ready:
        book_desc_text = str(book_data.get("keyWord", "")) + " " + str(book_data.get("bookDescription", ""))
        video_res = getattr(cfg, "VIDEO_RESOLUTION", "1080p")
        try:
            ok_cover = auto_create_youtube_cover(book_name, book_desc_text, ai_cover_target_path, token_pool, video_res)
        except CoverGenerationPolicyRejectedError as e:
            if not _is_nonempty_local_file(fallback_cover_path):
                raise RuntimeError(
                    "AI 封面命中提供商审核拒绝，且 books 数据中没有可用封面可回退，停止后续处理。"
                ) from e

            result.cover_image_path = _persist_cover_fallback_image(fallback_cover_path, ai_cover_target_path)
            cover_ready = _is_nonempty_local_file(result.cover_image_path)
            ai_cover_ready = (
                os.path.abspath(result.cover_image_path) == os.path.abspath(ai_cover_target_path) and cover_ready
            )
            log.warning(
                "[%s] AI 封面命中提供商审核拒绝，已停止继续重试并改用 books 数据封面：%s | %s",
                book_name,
                os.path.basename(result.cover_image_path),
                e,
            )
            ok_cover = True
        if not ok_cover:
            raise RuntimeError("AI 封面生成未成功，停止后续处理。")
        if _is_nonempty_local_file(ai_cover_target_path):
            result.cover_image_path = ai_cover_target_path
            ai_cover_ready = True
            cover_ready = True

    if enable_seo and not seo_ready:
        book_desc_text = str(book_data.get("keyWord", "")) + " " + str(book_data.get("bookDescription", ""))
        ok_seo, seo_dict = auto_create_youtube_seo(book_name, book_desc_text, seo_path_ai, token_pool)
        if not ok_seo or not isinstance(seo_dict, dict):
            raise RuntimeError("SEO 文案生成未成功，停止后续处理。")
        result.seo_text_path = seo_path_ai
        result.seo_title = seo_dict.get("title", "")
        result.seo_description = seo_dict.get("Description", "")
        result.seo_tags = seo_dict.get("label", "")
        seo_ready = True

    cover_ready = _is_nonempty_local_file(result.cover_image_path)
    if enable_cover and not cover_ready:
        raise RuntimeError("已开启 AI 封面生成，但封面既未生成成功，也没有可用的 books 封面可回退，停止后续处理。")

    if enable_seo and not seo_ready:
        raise RuntimeError("已开启 SEO 生成，但文案未生成成功，停止后续处理。")

    return result


def prepare_standard_book_cover_and_seo_with_state(result, book_record, book_data, book_dir, safe_name, book_name):
    state = build_standard_processing_state(book_record)
    restore_split_shared_assets_from_state(result, state, book_dir, safe_name, book_name)
    prepare_book_cover_and_seo(result, book_data, book_dir, safe_name, book_name)
    state["last_stage"] = "standard_shared_assets_ready"
    state["last_error"] = ""
    state_ref = persist_split_shared_assets_to_state(book_record, state, result, book_dir, safe_name, book_name)
    result.state_path = state_ref
    return state_ref, state


# ---------------------------------------------------------------------------
# 单本书标准处理（原文件行 7185-7340，original version before monkey-patch）
# ---------------------------------------------------------------------------
def process_standard_book(result, book_record, book_data, chapters_sorted, book_dir, safe_name, book_name, category):
    enable_bgm = bool(getattr(cfg, "ENABLE_BGM_MIX", True))
    skip_existing = bool(getattr(cfg, "SKIP_EXISTING", True))
    merged_path = os.path.join(book_dir, f"{safe_name}.mp3")
    mixed_path = os.path.join(book_dir, f"{safe_name}_mixed.mp3")
    final_path = mixed_path if enable_bgm else merged_path

    reuse_existing_audio = skip_existing and os.path.exists(final_path) and os.path.getsize(final_path) > 0
    if reuse_existing_audio:
        log.info("[%s] 复用现成音频: %s", book_name, os.path.basename(final_path))
        result.merged_audio_path = final_path
        if enable_bgm:
            result.mixed_audio_path = final_path
        result.audio_ready = True

    if not chapters_sorted:
        if reuse_existing_audio:
            log.warning("[%s] chapters_data 为空，跳过章节下载，仅复用已有音频。", book_name)
        else:
            result.error = "chapters_data 为空或无效，且不存在可复用的成品音频"
        return result

    result.chapter_count = len(chapters_sorted)
    result.youtube_chapters = generate_youtube_timestamps(chapters_sorted)
    result.estimated_total_duration_seconds = sum(estimate_chapter_duration_seconds(ch) for ch in chapters_sorted)

    if not reuse_existing_audio:
        chapter_items = [
            {
                "source_index": idx,
                "chapter": chapter,
                "title": chapter.get("title", f"chapter_{idx:04d}"),
            }
            for idx, chapter in enumerate(chapters_sorted, start=1)
        ]
        book_id_str = str(book_record.get("book_id", ""))
        try:
            chapter_paths, tg_cached_indices = download_chapter_items(
                chapter_items, os.path.join(book_dir, "chapters"), book_id=book_id_str
            )
        except RuntimeError as e:
            if "用户手动停止" in str(e):
                result.error = "用户手动停止"
                result.stop_requested = True
                return result
            raise
        result.success_count = len(chapter_paths)

        if result.success_count == 0:
            result.error = "所有章节下载失败"
            return result

        enable_deepfilter = bool(getattr(cfg, "ENABLE_DEEPFILTER", True))
        if enable_deepfilter:
            denoised_dir = os.path.join(book_dir, "denoised_chapters")
            denoised_targets = [os.path.join(denoised_dir, os.path.basename(path)) for path in chapter_paths]

            # TG 缓存章节已降噪，提前复制到 denoised 目录，DeepFilter 会自动跳过已存在文件
            if tg_cached_indices:
                import shutil as _shutil
                for i, item in enumerate(chapter_items):
                    if item["source_index"] in tg_cached_indices and i < len(chapter_paths):
                        src = chapter_paths[i]
                        dst = denoised_targets[i]
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
                            _shutil.copy2(src, dst)
                            log.info("[TG缓存] 已预置降噪文件: %s", os.path.basename(dst))

            try:
                deepfilter_workers = int(getattr(cfg, "DEEPFILTER_WORKERS", 2))
                chapter_paths = denoise_audio_paths_parallel(
                    chapter_paths,
                    output_paths=denoised_targets,
                    max_workers=deepfilter_workers,
                )
            except RuntimeError as e:
                if "用户手动停止" in str(e):
                    result.error = "用户手动停止"
                    result.stop_requested = True
                    return result
                result.error = f"DeepFilter 降噪失败: {e}"
                return result
            except Exception as e:
                result.error = f"DeepFilter 降噪失败: {e}"
                return result

        result.chapter_audio_paths = chapter_paths
        result.youtube_chapters = generate_youtube_timestamps(chapters_sorted, chapter_paths)

        try:
            audio_info = build_final_audio_from_chapter_paths(
                chapter_paths,
                book_dir,
                merged_path,
                mixed_path,
                book_name,
            )
        except Exception as e:
            result.error = str(e)
            return result

        result.merged_audio_path = audio_info["audio_path"]
        result.mixed_audio_path = audio_info["mixed_audio_path"]
        result.audio_ready = True
    else:
        result.success_count = result.chapter_count

    prepare_standard_book_cover_and_seo_with_state(
        result,
        book_record,
        book_data,
        book_dir,
        safe_name,
        book_name,
    )

    enable_video = bool(getattr(cfg, "ENABLE_VIDEO_GENERATION", True))
    if enable_video:
        video_path = os.path.join(book_dir, f"{safe_name}_final.mp4")
        if skip_existing and os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            result.video_path = video_path
            result.video_ready = True
            log.info("[%s] 复用已封装的 MP4 成品。", book_name)
        elif result.merged_audio_path and result.cover_image_path:
            try:
                video_res = getattr(cfg, "VIDEO_RESOLUTION", "1080p")
                ok_vid = generate_video(result.merged_audio_path, result.cover_image_path, video_path, video_res)
                if ok_vid:
                    result.video_path = video_path
                    result.video_ready = True
                else:
                    log.warning("[%s] MP4 封装失败，本次仅保留音频成品。", book_name)
            except Exception as e:
                log.error("[%s] MP4 封装发生异常: %s", book_name, e)
        else:
            log.warning("[%s] 缺少音频或封面，跳过 MP4 封装。", book_name)

    enable_upload = bool(getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True))
    channel = str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()

    if enable_upload and channel:
        if result.video_path and os.path.exists(result.video_path):
            try:
                upload_receipt_path = os.path.join(book_dir, "youtube_upload_receipt.json")
                final_title, final_desc, final_tags = build_youtube_payload(
                    result,
                    book_name,
                    category,
                    youtube_chapters=result.youtube_chapters,
                )
                force_reprocess = bool(getattr(cfg, "FORCE_REPROCESS", False))
                upload_result = {}
                if not force_reprocess:
                    upload_result = load_youtube_upload_receipt(
                        upload_receipt_path,
                        video_path=result.video_path,
                        channel_name=channel,
                    )
                if upload_result:
                    log.info("[%s] 复用本地 YouTube 上传回执，跳过重复上传。", book_name)
                else:
                    privacy = str(getattr(cfg, "YOUTUBE_PRIVACY_STATUS", "schedule"))
                    category_id = str(getattr(cfg, "YOUTUBE_CATEGORY_ID", ""))
                    schedule_hours = int(getattr(cfg, "YOUTUBE_SCHEDULE_AFTER_HOURS", 24) or 24)
                    upload_result = upload_to_youtube_detailed(
                        video_path=result.video_path,
                        title=final_title,
                        description=final_desc,
                        tags=final_tags,
                        cover_path=result.cover_image_path,
                        channel_name=channel,
                        privacy_status=privacy,
                        category_id=category_id,
                        schedule_after_hours=schedule_hours,
                    )
                    if upload_result:
                        persist_youtube_upload_receipt(
                            upload_receipt_path,
                            video_path=result.video_path,
                            upload_result=upload_result,
                            channel_name=channel,
                            title=final_title,
                            privacy_status=privacy,
                            category_id=category_id,
                            schedule_after_hours=schedule_hours,
                        )
                if upload_result:
                    result.youtube_url = upload_result.get("youtube_url", "")
                    result.youtube_urls = [result.youtube_url] if result.youtube_url else []
                    result.youtube_publish_at = upload_result.get("publish_at", "")
                    result.youtube_schedule_reason = upload_result.get("schedule_reason", "")
                    result.upload_ready = bool(result.youtube_url)
            except Exception as e:
                log.error("[%s] YouTube 上传异常: %s", book_name, e)
        else:
            log.warning("[%s] 缺少可上传的 MP4，跳过 YouTube 上传。", book_name)

    return result


# ---------------------------------------------------------------------------
# 分片辅助（原文件行 6879-6923 / 7343-7362 / 7365-7427 / 7430-7437）
# ---------------------------------------------------------------------------
def build_part_result_record(part_plan, part_state):
    return {
        "part_index": part_plan["part_index"],
        "chapter_start_index": part_plan["chapter_start_index"],
        "chapter_end_index": part_plan["chapter_end_index"],
        "chapter_count": len(part_plan.get("items", [])),
        "estimated_duration_seconds": part_plan.get("estimated_duration_seconds", 0),
        "actual_duration_seconds": part_state.get("actual_duration_seconds", 0),
        "audio_path": part_state.get("audio_path", ""),
        "video_path": part_state.get("video_path", ""),
        "video_id": part_state.get("video_id", ""),
        "uploaded_at": part_state.get("uploaded_at", ""),
        "publish_at": part_state.get("publish_at", ""),
        "schedule_reason": part_state.get("schedule_reason", ""),
        "youtube_url": part_state.get("youtube_url", ""),
        "youtube_title": part_state.get("youtube_title", ""),
        "playlist_item_id": part_state.get("playlist_item_id", ""),
        "status": part_state.get("status", "pending"),
        "error": part_state.get("error", ""),
    }


def sync_result_from_split_state(result, state, split_plan):
    result.part_count = len(split_plan.get("parts", [])) or 1
    result.part_results = []
    result.youtube_urls = []
    result.youtube_publish_at = ""
    result.youtube_schedule_reason = ""
    result.success_count = 0
    result.chapter_audio_paths = []
    result.youtube_chapters = ""
    playlist_state = get_split_playlist_state(state)
    result.playlist_id = str(playlist_state.get("playlist_id") or "")
    result.playlist_url = str(playlist_state.get("playlist_url") or "")
    result.playlist_title = str(playlist_state.get("title") or "")
    progress = evaluate_split_completion_state(state)
    playlist_required = progress["playlist_required"]
    playlist_completed = progress["playlist_completed"]

    latest_audio_path = ""
    latest_video_path = ""
    latest_youtube_chapters = ""
    latest_publish_at = ""
    latest_schedule_reason = ""
    all_timestamps = []
    completed_part_count = 0

    for part_plan in split_plan.get("parts", []):
        part_state = get_split_part_state(state, part_plan["part_index"]) or {}
        from .state import _reconcile_split_part_state
        _reconcile_split_part_state(part_state)
        result.part_results.append(build_part_result_record(part_plan, part_state))

        if _split_part_is_completed(part_state):
            completed_part_count += 1
            result.success_count += len(part_plan.get("items", []))

        if part_state.get("youtube_url"):
            result.youtube_urls.append(part_state["youtube_url"])

        if part_state.get("audio_path"):
            latest_audio_path = part_state["audio_path"]
        if part_state.get("video_path"):
            latest_video_path = part_state["video_path"]
        if part_state.get("publish_at"):
            latest_publish_at = str(part_state.get("publish_at") or "")
        if part_state.get("schedule_reason"):
            latest_schedule_reason = str(part_state.get("schedule_reason") or "")

        if part_state.get("youtube_title"):
            all_timestamps.append(f"{part_state['youtube_title']}: {part_state.get('youtube_url', '')}".strip())

        if part_state.get("youtube_chapters"):
            latest_youtube_chapters = part_state["youtube_chapters"]

    result.merged_audio_path = latest_audio_path
    result.video_path = latest_video_path
    result.completed_part_count = progress["completed_part_count"]
    result.playlist_required = playlist_required
    result.playlist_completed = playlist_completed
    result.pending_resume = not progress["fully_completed"]
    result.youtube_url = "\n".join(result.youtube_urls)
    result.youtube_publish_at = latest_publish_at
    result.youtube_schedule_reason = latest_schedule_reason
    result.youtube_chapters = latest_youtube_chapters or "\n".join([item for item in all_timestamps if item])
    return result


def cleanup_completed_split_state_for_book(book_record, result, book_name):
    try:
        if delete_split_processing_state(book_record, only_if_completed=False):
            result.state_path = ""
            log.info("[%s] Split upload state deleted.", book_name)
    except Exception as e:
        log.warning("[%s] Failed to delete split upload state: %s", book_name, e)
    return result


def build_ordered_split_video_records(state, split_plan):
    records = []
    for part_plan in split_plan.get("parts", []):
        part_state = get_split_part_state(state, part_plan["part_index"]) or {}
        video_id = str(part_state.get("video_id") or "").strip()
        youtube_url = str(part_state.get("youtube_url") or "").strip()
        if not video_id and youtube_url:
            video_id = _extract_youtube_video_id(youtube_url)
        if not video_id:
            continue

        records.append(
            {
                "part_index": part_plan["part_index"],
                "video_id": video_id,
                "youtube_url": youtube_url or f"https://youtu.be/{video_id}",
                "youtube_title": str(part_state.get("youtube_title") or ""),
                "uploaded_at": str(part_state.get("uploaded_at") or ""),
            }
        )

    def sort_key(item):
        uploaded_at = item.get("uploaded_at", "")
        if uploaded_at:
            return (0, uploaded_at, int(item.get("part_index", 0)))
        return (1, "", int(item.get("part_index", 0)))

    return sorted(records, key=sort_key)


def build_split_playlist_description(result, ordered_video_records):
    base_desc = str(result.seo_description or "").strip()
    link_lines = []
    for record in ordered_video_records:
        title = str(record.get("youtube_title") or "").strip()
        url = str(record.get("youtube_url") or "").strip()
        if not url:
            continue
        link_lines.append(f"{title}: {url}" if title else url)

    if link_lines and base_desc:
        return base_desc + "\n\n分片链接:\n" + "\n".join(link_lines)
    if link_lines:
        return "分片链接:\n" + "\n".join(link_lines)
    return base_desc


# ---------------------------------------------------------------------------
# 分片处理（原文件行 7440-7849）
# ---------------------------------------------------------------------------
def process_split_part(result, state_ref, state, split_plan, part_plan, book_record, book_dir, safe_name, book_name, category):
    part_index = part_plan["part_index"]
    part_count = len(split_plan.get("parts", []))
    part_state = get_split_part_state(state, part_index)
    part_dir = os.path.join(book_dir, "_split_parts", f"part_{part_index:02d}")
    upload_receipt_path = os.path.join(part_dir, "youtube_upload_receipt.json")
    expected_video_path = os.path.join(part_dir, f"{safe_name}_part_{part_index:02d}_final.mp4")
    os.makedirs(part_dir, exist_ok=True)

    if part_state is None:
        raise RuntimeError(f"未找到分片状态定义: part {part_index}")

    enable_video = bool(getattr(cfg, "ENABLE_VIDEO_GENERATION", True))
    enable_upload = bool(getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True))
    channel = str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    skip_existing = bool(getattr(cfg, "SKIP_EXISTING", True))
    force_reprocess = bool(getattr(cfg, "FORCE_REPROCESS", False))

    if enable_video and _is_nonempty_local_file(expected_video_path):
        part_state["video_path"] = part_state.get("video_path") or expected_video_path

    if enable_upload and channel:
        reused_upload_result = {}
        if not force_reprocess:
            reused_upload_result = load_youtube_upload_receipt(
                upload_receipt_path,
                video_path=part_state.get("video_path") or expected_video_path,
                channel_name=channel,
            )
        if reused_upload_result:
            part_state["video_id"] = str(part_state.get("video_id") or reused_upload_result.get("video_id") or "")
            part_state["youtube_url"] = str(part_state.get("youtube_url") or reused_upload_result.get("youtube_url") or "")
            part_state["uploaded_at"] = str(part_state.get("uploaded_at") or reused_upload_result.get("uploaded_at") or "")
            part_state["publish_at"] = str(part_state.get("publish_at") or reused_upload_result.get("publish_at") or "")
            part_state["schedule_reason"] = str(part_state.get("schedule_reason") or reused_upload_result.get("schedule_reason") or "")
            part_state["youtube_title"] = str(part_state.get("youtube_title") or reused_upload_result.get("title") or "")

    from .state import _reconcile_split_part_state
    if _reconcile_split_part_state(part_state):
        state["last_error"] = ""
        state_ref = save_split_processing_state(book_record, state)
        result.state_path = state_ref

    if part_state.get("status") == "completed":
        log.info("[%s] 分片 %d/%d 已完成，跳过重做。", book_name, part_index, part_count)
        return build_part_result_record(part_plan, part_state)

    precomputed_upload_title = ""
    precomputed_upload_desc = ""
    precomputed_upload_tags = ""
    if enable_upload and channel:
        precomputed_upload_title, precomputed_upload_desc, precomputed_upload_tags = build_youtube_payload(
            result,
            book_name,
            category,
            youtube_chapters="",
            title_prefix=f"{part_index}-" if part_count > 1 else "",
            part_hint="",
            include_youtube_chapters=False,
            include_part_hint=False,
        )
        if not force_reprocess:
            from .youtube import find_existing_channel_video_by_exact_title

            existing_channel_match = find_existing_channel_video_by_exact_title(
                channel,
                precomputed_upload_title,
            )
            if existing_channel_match:
                part_state["youtube_title"] = str(existing_channel_match.get("title") or precomputed_upload_title or "")
                part_state["youtube_url"] = str(existing_channel_match.get("youtube_url") or "")
                part_state["video_id"] = str(existing_channel_match.get("video_id") or "")
                part_state["uploaded_at"] = str(existing_channel_match.get("uploaded_at") or "")
                part_state["publish_at"] = str(existing_channel_match.get("publish_at") or "")
                part_state["schedule_reason"] = str(existing_channel_match.get("schedule_reason") or "existing_title_match")
                part_state["last_stage"] = "existing_title_match"
                state["last_stage"] = f"part_{part_index}_existing_title_match"
                state["last_error"] = ""
                state_ref = save_split_processing_state(book_record, state)
                result.state_path = state_ref
                result.youtube_publish_at = part_state["publish_at"]
                result.youtube_schedule_reason = part_state["schedule_reason"]
                log.info("[%s] 分片 %d/%d 命中频道内同标题视频，直接复用并跳过重复处理。", book_name, part_index, part_count)
                return build_part_result_record(part_plan, part_state)

    chapter_items = part_plan.get("items", [])
    chapters_only = [item["chapter"] for item in chapter_items]
    chapters_dir = os.path.join(part_dir, "chapters")
    denoised_dir = os.path.join(part_dir, "denoised")

    part_state["status"] = "in_progress"
    part_state["started_at"] = part_state.get("started_at") or dt_module.datetime.now().isoformat()
    part_state["last_stage"] = "download"
    part_state["error"] = ""
    state["last_stage"] = f"part_{part_index}_download"
    state["last_error"] = ""
    state["pending_resume"] = True
    state_ref = save_split_processing_state(book_record, state)
    result.state_path = state_ref

    try:
        book_id_str = str(book_record.get("book_id", ""))
        try:
            chapter_paths, tg_cached_indices = download_chapter_items(
                chapter_items, chapters_dir, book_id=book_id_str
            )
        except RuntimeError as e:
            if "用户手动停止" in str(e):
                result.error = "用户手动停止"
                result.stop_requested = True
                return result
            raise

        enable_deepfilter = bool(getattr(cfg, "ENABLE_DEEPFILTER", True))
        if enable_deepfilter:
            part_state["last_stage"] = "denoise"
            state["last_stage"] = f"part_{part_index}_denoise"
            state_ref = save_split_processing_state(book_record, state)
            result.state_path = state_ref

            deepfilter_workers = int(getattr(cfg, "DEEPFILTER_WORKERS", 2))
            denoised_targets = [os.path.join(denoised_dir, os.path.basename(path)) for path in chapter_paths]

            # TG 缓存章节已降噪，提前复制到 denoised 目录，DeepFilter 会自动跳过已存在文件
            if tg_cached_indices:
                import shutil as _shutil
                for i, item in enumerate(chapter_items):
                    if item["source_index"] in tg_cached_indices and i < len(chapter_paths):
                        src = chapter_paths[i]
                        dst = denoised_targets[i]
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
                            _shutil.copy2(src, dst)
                            log.info("[TG缓存] 已预置降噪文件: %s", os.path.basename(dst))

            try:
                chapter_paths = denoise_audio_paths_parallel(
                    chapter_paths,
                    output_paths=denoised_targets,
                    max_workers=deepfilter_workers,
                )
            except RuntimeError as e:
                if "用户手动停止" in str(e):
                    result.error = "用户手动停止"
                    result.stop_requested = True
                    return result
                raise

        youtube_chapters = generate_youtube_timestamps(chapters_only, chapter_paths)
        merged_path = os.path.join(part_dir, f"{safe_name}_part_{part_index:02d}.mp3")
        mixed_path = os.path.join(part_dir, f"{safe_name}_part_{part_index:02d}_mixed.mp3")

        part_state["last_stage"] = "merge_audio"
        state["last_stage"] = f"part_{part_index}_merge_audio"
        state_ref = save_split_processing_state(book_record, state)
        result.state_path = state_ref

        audio_info = build_final_audio_from_chapter_paths(
            chapter_paths,
            part_dir,
            merged_path,
            mixed_path,
            f"{book_name} [part {part_index}]",
        )
        audio_path = audio_info["audio_path"]
        actual_duration_seconds = probe_audio_duration_seconds(audio_path) or part_plan.get("estimated_duration_seconds", 0)

        part_state["audio_path"] = audio_path
        part_state["youtube_chapters"] = youtube_chapters
        part_state["actual_duration_seconds"] = actual_duration_seconds

        video_path = ""
        if enable_video:
            part_state["last_stage"] = "generate_video"
            state["last_stage"] = f"part_{part_index}_generate_video"
            state_ref = save_split_processing_state(book_record, state)
            result.state_path = state_ref

            video_path = expected_video_path
            if skip_existing and os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                log.info("[%s] 分片 %d/%d 复用现有 MP4。", book_name, part_index, part_count)
            else:
                if not result.cover_image_path:
                    raise RuntimeError("缺少封面，无法为分片封装视频")
                video_res = getattr(cfg, "VIDEO_RESOLUTION", "1080p")
                ok_vid = generate_video(audio_path, result.cover_image_path, video_path, video_res)
                if not ok_vid:
                    raise RuntimeError("分片 MP4 封装失败")
            part_state["video_path"] = video_path

        if enable_upload and channel:
            part_state["last_stage"] = "upload_youtube"
            state["last_stage"] = f"part_{part_index}_upload_youtube"
            state_ref = save_split_processing_state(book_record, state)
            result.state_path = state_ref

            if not part_state.get("video_path") or not os.path.exists(part_state["video_path"]):
                raise RuntimeError("缺少可上传的视频分片")

            final_title = precomputed_upload_title
            final_desc = precomputed_upload_desc
            final_tags = precomputed_upload_tags
            if not final_title:
                final_title, final_desc, final_tags = build_youtube_payload(
                    result,
                    book_name,
                    category,
                    youtube_chapters="",
                    title_prefix=f"{part_index}-" if part_count > 1 else "",
                    part_hint="",
                    include_youtube_chapters=False,
                    include_part_hint=False,
                )
            upload_result = {}
            if not force_reprocess:
                upload_result = load_youtube_upload_receipt(
                    upload_receipt_path,
                    video_path=part_state["video_path"],
                    channel_name=channel,
                )
            if upload_result:
                log.info("[%s] Split part %d/%d reuses a saved YouTube upload receipt.", book_name, part_index, part_count)
            else:
                privacy = str(getattr(cfg, "YOUTUBE_PRIVACY_STATUS", "schedule"))
                cat_id = str(getattr(cfg, "YOUTUBE_CATEGORY_ID", ""))
                sched_hours = int(getattr(cfg, "YOUTUBE_SCHEDULE_AFTER_HOURS", 24) or 24)
                upload_result = upload_to_youtube_detailed(
                    video_path=part_state["video_path"],
                    title=final_title,
                    description=final_desc,
                    tags=final_tags,
                    cover_path=result.cover_image_path,
                    channel_name=channel,
                    privacy_status=privacy,
                    category_id=cat_id,
                    schedule_after_hours=sched_hours,
                )
            if not upload_result:
                raise RuntimeError("YouTube upload did not complete")

            persist_youtube_upload_receipt(
                upload_receipt_path,
                video_path=part_state["video_path"],
                upload_result=upload_result,
                channel_name=channel,
                title=final_title,
                privacy_status=privacy if isinstance(privacy, str) else "schedule",
                category_id=cat_id,
                schedule_after_hours=sched_hours,
            )

            part_state["youtube_title"] = final_title
            part_state["youtube_url"] = upload_result.get("youtube_url", "")
            part_state["video_id"] = upload_result.get("video_id", "")
            part_state["uploaded_at"] = upload_result.get("uploaded_at", "")
            part_state["publish_at"] = upload_result.get("publish_at", "")
            part_state["schedule_reason"] = upload_result.get("schedule_reason", "")
            result.youtube_publish_at = part_state["publish_at"]
            result.youtube_schedule_reason = part_state["schedule_reason"]
            part_state["last_stage"] = "upload_persisted"
            state["last_stage"] = f"part_{part_index}_upload_persisted"
            state["last_error"] = ""
            state_ref = save_split_processing_state(book_record, state)
            result.state_path = state_ref

        part_state["status"] = "completed"
        part_state["completed_at"] = dt_module.datetime.now().isoformat()
        part_state["last_stage"] = "completed"
        part_state["error"] = ""
        state["last_stage"] = f"part_{part_index}_completed"
        state["last_error"] = ""
        state_ref = save_split_processing_state(book_record, state)
        result.state_path = state_ref

        return build_part_result_record(part_plan, part_state)
    except Exception as e:
        part_state["status"] = "failed"
        part_state["error"] = str(e)
        state["last_stage"] = f"part_{part_index}_failed"
        state["last_error"] = str(e)
        state_ref = save_split_processing_state(book_record, state)
        result.state_path = state_ref
        raise


# ---------------------------------------------------------------------------
# 分片同步（原文件行 7680-7849 / 6926-6996）
# ---------------------------------------------------------------------------
def process_split_book(result, book_record, book_data, chapters_sorted, book_dir, safe_name, book_name, category, run_started_at=None):
    split_plan = build_split_part_plans(chapters_sorted)
    result.split_mode = True
    result.part_count = len(split_plan.get("parts", [])) or 1
    result.chapter_count = len(chapters_sorted)
    result.estimated_total_duration_seconds = split_plan.get("estimated_total_seconds", 0)
    result.success_count = 0

    state_ref, state = initialize_split_processing_state(book_record, book_dir, chapters_sorted, split_plan)
    result.state_path = state_ref

    log.info(
        "[%s] 预估总时长 %s，触发长音频分片模式，计划拆成 %d 个视频上传。",
        book_name,
        format_seconds_hhmmss(result.estimated_total_duration_seconds),
        result.part_count,
    )

    restore_split_shared_assets_from_state(result, state, book_dir, safe_name, book_name)
    prepare_book_cover_and_seo(result, book_data, book_dir, safe_name, book_name)
    state_ref = persist_split_shared_assets_to_state(book_record, state, result, book_dir, safe_name, book_name)
    result.state_path = state_ref

    reconcile_summary = reconcile_split_part_upload_states(result, state, split_plan, book_name, category)
    if reconcile_summary.get("changed"):
        recovered_parts = [str(item[0]) for item in reconcile_summary.get("recovered", [])]
        reset_parts = [str(item[0]) for item in reconcile_summary.get("reset", [])]
        if reset_parts:
            playlist_state = get_split_playlist_state(state)
            playlist_state["status"] = "pending"
            playlist_state["last_error"] = "Waiting for split parts to be re-uploaded after stale video recovery."
            playlist_state["video_ids"] = []
        state["last_stage"] = "resume_reconciled"
        if reset_parts:
            state["last_error"] = "Reset stale uploaded YouTube references for split parts: " + ",".join(reset_parts)
        else:
            state["last_error"] = ""
        state_ref = save_split_processing_state(book_record, state)
        result.state_path = state_ref
        log.info(
            "[%s] Resume reconciliation finished. recovered_parts=%s reset_parts=%s state=%s",
            book_name,
            ",".join(recovered_parts) if recovered_parts else "<none>",
            ",".join(reset_parts) if reset_parts else "<none>",
            state_ref,
        )

    channel = str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    enable_upload = bool(getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True))
    playlist_required = bool(result.part_count > 1 and enable_upload and channel)
    playlist_state = get_split_playlist_state(state)
    playlist_completed = bool(playlist_state.get("playlist_id")) and str(playlist_state.get("status") or "").strip().lower() == "completed"

    if state.get("status") == "completed" and (not playlist_required or playlist_completed):
        sync_result_from_split_state(result, state, split_plan)
        result.pending_resume = False
        return result
    elif state.get("status") == "completed" and playlist_required and not playlist_completed:
        log.info("[%s] 检测到分片上传已完成但播放列表尚未补齐，将继续恢复 playlist。", book_name)

    for part_plan in split_plan.get("parts", []):
        part_state = get_split_part_state(state, part_plan["part_index"]) or {}
        if part_state.get("status") == "completed":
            continue

        try:
            process_split_part(
                result,
                state_ref,
                state,
                split_plan,
                part_plan,
                book_record,
                book_dir,
                safe_name,
                book_name,
                category,
            )
        except Exception as e:
            result.error = str(e)
            break

    state = reload_split_processing_state(book_record, fallback_state=state, book_name=book_name)
    sync_result_from_split_state(result, state, split_plan)

    if result.completed_part_count >= result.part_count:
        if playlist_required:
            try:
                playlist_state = get_split_playlist_state(state)
                playlist_state["status"] = "syncing"
                state["pending_resume"] = True
                state["last_stage"] = "playlist_pending"
                state_ref = save_split_processing_state(book_record, state)
                result.state_path = state_ref

                sync_split_playlist(result, state, split_plan, book_record, book_name)
                state = reload_split_processing_state(book_record, fallback_state=state, book_name=book_name)
                sync_result_from_split_state(result, state, split_plan)
                if not bool(getattr(result, "playlist_completed", False)):
                    playlist_state = get_split_playlist_state(state)
                    incomplete_error = (
                        "Playlist sync returned without completion: "
                        f"playlist_id={str(playlist_state.get('playlist_id') or '')} "
                        f"playlist_status={str(playlist_state.get('status') or '')} "
                        f"playlist_url={str(playlist_state.get('playlist_url') or '')} "
                        f"ordered_video_ids={[item.get('video_id') for item in build_ordered_split_video_records(state, split_plan)]}"
                    )
                    playlist_state["status"] = "failed"
                    playlist_state["last_error"] = incomplete_error
                    state["pending_resume"] = True
                    state["last_stage"] = "playlist_failed"
                    state["last_error"] = incomplete_error
                    state_ref = save_split_processing_state(book_record, state)
                    result.state_path = state_ref
                    result.pending_resume = True
                    result.error = incomplete_error
                    return result
            except Exception as e:
                playlist_state = get_split_playlist_state(state)
                playlist_state["status"] = "failed"
                playlist_state["last_error"] = str(e)
                state["pending_resume"] = True
                state["last_stage"] = "playlist_failed"
                state["last_error"] = str(e)
                state_ref = save_split_processing_state(book_record, state)
                result.state_path = state_ref
                result.pending_resume = True
                result.error = str(e)
                return result

        state["pending_resume"] = False
        state["last_error"] = ""
        state["last_stage"] = "all_parts_completed"
        state_ref = save_split_processing_state(book_record, state)
        result.state_path = state_ref
        state = reload_split_processing_state(book_record, fallback_state=state, book_name=book_name)
        sync_result_from_split_state(result, state, split_plan)
        result.pending_resume = not (
            result.completed_part_count >= result.part_count
            and (not playlist_required or bool(getattr(result, "playlist_completed", False)))
        )
        if not result.pending_resume:
            result.error = ""
        elif not result.error:
            playlist_state = get_split_playlist_state(state)
            result.error = (
                "Split book reached final checkpoint but is still incomplete: "
                f"playlist_id={str(playlist_state.get('playlist_id') or '')} "
                f"playlist_status={str(playlist_state.get('status') or '')} "
                f"playlist_url={str(playlist_state.get('playlist_url') or '')} "
                f"completed_part_count={result.completed_part_count}/{result.part_count}"
            )
    elif not result.error:
        result.error = "长音频分片处理中断，已记录进度，等待下次续跑"

    return result


def sync_split_playlist(result, state, split_plan, book_record, book_name):
    playlist_state = get_split_playlist_state(state)
    ordered_video_records = build_ordered_split_video_records(state, split_plan)
    expected_count = len(split_plan.get("parts", []))

    if len(ordered_video_records) != expected_count:
        raise RuntimeError("分片视频尚未全部上传成功，暂不能创建播放列表")

    ordered_video_ids = [item["video_id"] for item in ordered_video_records]
    shared = get_split_shared_assets(state)
    playlist_title = str(shared.get("shared_title_without_prefix") or result.seo_title or book_name or "").strip()
    playlist_description = build_split_playlist_description(result, ordered_video_records)

    playlist_state["title"] = playlist_title
    playlist_state["description"] = playlist_description
    playlist_state["privacy_status"] = "public"
    playlist_state["status"] = "syncing"
    playlist_state["video_ids"] = ordered_video_ids
    playlist_state["last_error"] = ""
    state["last_stage"] = "playlist_syncing"
    save_split_processing_state(book_record, state)

    channel = str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    sync_result = sync_youtube_playlist(
        channel_name=channel,
        playlist_id=str(playlist_state.get("playlist_id") or ""),
        title=playlist_title,
        description=playlist_description,
        ordered_video_ids=ordered_video_ids,
        privacy_status="public",
    )
    if isinstance(sync_result, dict) and sync_result.get("playlist_id"):
        playlist_state["playlist_id"] = sync_result.get("playlist_id", "")
        playlist_state["playlist_url"] = sync_result.get("playlist_url", "")
        playlist_state["title"] = sync_result.get("title", playlist_title)
        playlist_state["description"] = sync_result.get("description", playlist_description)
        playlist_state["privacy_status"] = sync_result.get("privacy_status", "public")
        save_split_processing_state(book_record, state)

    if not sync_result or not sync_result.get("success", False):
        sync_error = ""
        if isinstance(sync_result, dict):
            sync_error = str(sync_result.get("error") or "").strip()
        raise RuntimeError(sync_error or "YouTube 播放列表同步失败")

    playlist_state["playlist_id"] = sync_result.get("playlist_id", "")
    playlist_state["playlist_url"] = sync_result.get("playlist_url", "")
    playlist_state["title"] = sync_result.get("title", playlist_title)
    playlist_state["description"] = sync_result.get("description", playlist_description)
    playlist_state["privacy_status"] = sync_result.get("privacy_status", "public")
    playlist_state["video_ids"] = ordered_video_ids
    playlist_state["status"] = "completed"
    playlist_state["last_error"] = ""
    playlist_state["last_synced_at"] = dt_module.datetime.now().isoformat()
    state["last_stage"] = "playlist_completed"
    state["last_error"] = ""

    playlist_item_map = sync_result.get("playlist_item_map", {}) if isinstance(sync_result, dict) else {}
    for part in state.get("parts", []):
        video_id = str(part.get("video_id") or "").strip()
        if video_id and video_id in playlist_item_map:
            part["playlist_item_id"] = playlist_item_map[video_id]

    save_split_processing_state(book_record, state)
    result.playlist_id = playlist_state["playlist_id"]
    result.playlist_url = playlist_state.get("playlist_url", "")
    result.playlist_title = playlist_state.get("title", "")
    result.playlist_required = True
    result.playlist_completed = True
    result.pending_resume = False
    result.error = ""
    return result


# ---------------------------------------------------------------------------
# 上传状态协调（原文件行 2245-2367）+ 辅助
# ---------------------------------------------------------------------------
def _build_expected_split_upload_title(result, book_name, category, part_index, part_count):
    title, _, _ = build_youtube_payload(
        result,
        book_name,
        category,
        youtube_chapters="",
        title_prefix=f"{part_index}-" if int(part_count or 0) > 1 else "",
        part_hint="",
        include_youtube_chapters=False,
        include_part_hint=False,
    )
    return str(title or "").strip()[:100]


def reconcile_split_part_upload_states(result, state, split_plan, book_name, category):
    channel_name = str(getattr(cfg, "YOUTUBE_CHANNEL_NAME", "") or "").strip()
    if not bool(getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True)) or not channel_name:
        return {"changed": False, "recovered": [], "reset": []}

    part_count = len(split_plan.get("parts", [])) or 1
    candidates = []
    candidate_video_ids = []
    changed = False

    for part_plan in split_plan.get("parts", []):
        part_state = get_split_part_state(state, part_plan["part_index"])
        if not isinstance(part_state, dict):
            continue

        current_status = str(part_state.get("status") or "").strip().lower()
        candidate_video_id = _extract_youtube_video_id(part_state.get("video_id")) or _extract_youtube_video_id(part_state.get("youtube_url"))
        has_upload_state = bool(
            candidate_video_id
            or str(part_state.get("youtube_url") or "").strip()
            or str(part_state.get("uploaded_at") or "").strip()
            or current_status == "completed"
        )
        if not has_upload_state:
            continue

        expected_title = str(part_state.get("youtube_title") or "").strip()
        if not expected_title:
            expected_title = _build_expected_split_upload_title(
                result,
                book_name,
                category,
                part_plan["part_index"],
                part_count,
            )
            if expected_title and str(part_state.get("youtube_title") or "").strip() != expected_title:
                part_state["youtube_title"] = expected_title
                changed = True

        candidates.append(
            {
                "part_plan": part_plan,
                "part_state": part_state,
                "candidate_video_id": candidate_video_id,
                "expected_title": expected_title,
            }
        )
        if candidate_video_id:
            candidate_video_ids.append(candidate_video_id)

    if not candidates:
        return {"changed": changed, "recovered": [], "reset": []}

    youtube = authenticate_youtube_from_supabase(channel_name)
    if not youtube:
        return {"changed": changed, "recovered": [], "reset": []}

    live_rows_by_id, _ = _wait_for_live_video_rows_with_client(
        youtube,
        candidate_video_ids,
        max_attempts=2,
        context_label=book_name,
    )

    title_index = None
    recovered = []
    reset = []

    for candidate in candidates:
        part_plan = candidate["part_plan"]
        part_state = candidate["part_state"]
        part_index = int(part_plan["part_index"])
        candidate_video_id = candidate["candidate_video_id"]
        expected_title = candidate["expected_title"]

        if candidate_video_id and candidate_video_id in live_rows_by_id:
            match = _build_existing_video_match_from_row(live_rows_by_id[candidate_video_id])
            if expected_title and not str(match.get("title") or "").strip():
                match["title"] = expected_title
            if _apply_video_match_to_split_part(part_state, match):
                changed = True
            continue

        recovered_match = {}
        if expected_title:
            if title_index is None:
                title_index = _build_channel_video_title_index_with_client(youtube)
            recovered_match = dict(title_index.get(_normalize_youtube_title_key(expected_title), {})) or {}

        if recovered_match:
            old_video_id = candidate_video_id
            if _apply_video_match_to_split_part(part_state, recovered_match):
                changed = True
            new_video_id = str(recovered_match.get("video_id") or "").strip()
            recovered.append((part_index, old_video_id, new_video_id, expected_title))
            log.warning(
                "[%s] Split part %d/%d recovered a stale YouTube upload reference by exact title match. old_video_id=%s new_video_id=%s title=%s",
                book_name,
                part_index,
                part_count,
                old_video_id or "<empty>",
                new_video_id or "<empty>",
                expected_title or "<empty>",
            )
            continue

        missing_reason = (
            f"Missing uploaded YouTube video for split part {part_index}/{part_count}: "
            f"video_id={candidate_video_id or '<empty>'} title={expected_title or '<empty>'}"
        )
        if _reset_split_part_upload_state(part_state, reason=missing_reason):
            changed = True
        reset.append((part_index, candidate_video_id, expected_title))
        log.warning(
            "[%s] Split part %d/%d references a missing YouTube video and will resume from local artifacts before re-upload. video_id=%s title=%s",
            book_name,
            part_index,
            part_count,
            candidate_video_id or "<empty>",
            expected_title or "<empty>",
        )

    return {"changed": changed, "recovered": recovered, "reset": reset}


# ---------------------------------------------------------------------------
# 跳过长音频 & 最终化（原文件行 7852-7877 / 2870-2971 / 8012-8044）
# ---------------------------------------------------------------------------
def skip_and_delete_short_book(book_record, result, book_name):
    duration_text = format_seconds_hhmmss(getattr(result, "estimated_total_duration_seconds", 0))
    short_reason = (
        f"预估总时长 {duration_text} 小于 {format_seconds_hhmmss(MIN_BOOK_DURATION_SECONDS)}，"
        "已跳过处理并从 books 表删除。"
    )
    try:
        _delete_book_from_database(book_record["book_id"])
        try:
            if delete_split_processing_state(book_record, only_if_completed=False):
                result.state_path = ""
        except Exception as state_error:
            log.warning("[%s] books 记录已删除，但清理 book_processing_states 失败: %s", book_name, state_error)
    except Exception as e:
        result.error = (
            f"预估总时长 {duration_text} 小于 {format_seconds_hhmmss(MIN_BOOK_DURATION_SECONDS)}，"
            f"但删除 books 记录失败: {e}"
        )
        return result

    result.skipped = True
    result.deleted_from_books = True
    result.skipped_reason = short_reason
    result.error = short_reason
    log.info("[%s] %s", book_name, short_reason)
    return result


# ---------------------------------------------------------------------------
# 成功后清理中间文件（释放磁盘空间）
# ---------------------------------------------------------------------------
# 保留的白名单文件（小文件，用于审计 / 防重复上传双保险）
_BOOK_DIR_KEEP_FILES = frozenset({"book_result.json", "youtube_upload_receipt.json"})


def cleanup_book_dir_intermediate_files(book_dir, result, book_name=""):
    """任务结束后清理 book_dir 中的中间文件（无论成功/失败/中断/跳过）。

    删除章节音频、降噪音频、混音临时文件、合并/混音成品、MP4 视频、封面、SEO 等，
    仅保留 ``book_result.json``（结果报告）和 ``youtube_upload_receipt.json``（上传回执）。

    断点续跑信息已存储在数据库 ``book_processing_states`` 表中，无需依赖中间文件。
    无论任务成功、失败、中断还是跳过，都清理中间文件以释放磁盘空间。
    可通过配置 ``CLEANUP_INTERMEDIATE_FILES_AFTER_SUCCESS`` 关闭此行为。
    """
    if not bool(getattr(cfg, "CLEANUP_INTERMEDIATE_FILES_AFTER_SUCCESS", True)):
        return
    if not book_dir or not os.path.isdir(book_dir):
        return

    removed_count = 0
    freed_bytes = 0
    for name in os.listdir(book_dir):
        if name in _BOOK_DIR_KEEP_FILES:
            continue
        target = os.path.join(book_dir, name)
        try:
            if os.path.isdir(target):
                # 统计目录大小
                for root, _dirs, files in os.walk(target):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            freed_bytes += os.path.getsize(fp)
                        except Exception:
                            pass
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    freed_bytes += os.path.getsize(target)
                except Exception:
                    pass
                os.remove(target)
            removed_count += 1
        except Exception as e:
            log.warning("[%s] 清理中间文件失败: %s -> %s", book_name, name, e)

    if removed_count > 0:
        freed_mb = freed_bytes / (1024 * 1024)
        if getattr(result, "success", False):
            status_label = "成功"
        elif getattr(result, "skipped", False):
            status_label = "跳过"
        elif getattr(result, "pending_resume", False):
            status_label = "中断"
        else:
            status_label = "失败"
        log.info(
            "[%s] 🧹 任务%s，已清理 %d 项中间文件，释放 %.1f MB 磁盘空间",
            book_name, status_label, removed_count, freed_mb,
        )


def finalize_book_result(result, book_dir, book_record=None):
    if bool(getattr(result, "skipped", False)):
        result.audio_ready = False
        result.video_ready = False
        result.upload_ready = False
        result.pending_resume = False
        result.success = False
        cleanup_book_dir_intermediate_files(book_dir, result, result.book_name)
        return result

    part_count = max(1, int(getattr(result, "part_count", 1) or 1))
    completed_part_count = max(0, int(getattr(result, "completed_part_count", 0) or 0))
    enable_video = bool(getattr(cfg, "ENABLE_VIDEO_GENERATION", True))
    enable_upload = bool(getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True))

    if getattr(result, "split_mode", False) or part_count > 1:
        playlist_required = bool(getattr(result, "playlist_required", False))
        playlist_completed = not playlist_required or bool(getattr(result, "playlist_completed", False))
        all_parts_completed = completed_part_count >= part_count

        result.audio_ready = all_parts_completed
        result.video_ready = all_parts_completed if enable_video else result.audio_ready
        result.upload_ready = (
            all_parts_completed and (not playlist_required or playlist_completed)
            if enable_upload
            else result.video_ready
        )
        computed_pending_resume = (not all_parts_completed) or (playlist_required and not playlist_completed)
        stale_pending_resume = bool(getattr(result, "pending_resume", False)) and not computed_pending_resume
        result.pending_resume = computed_pending_resume
        required_stages = [result.audio_ready]
        if enable_video:
            required_stages.append(result.video_ready)
        if enable_upload:
            required_stages.append(result.upload_ready)
        result.success = all(required_stages) and all_parts_completed and playlist_completed and not result.pending_resume
        if stale_pending_resume:
            log.warning(
                "[%s] Clearing stale pending_resume during final split evaluation. completed=%d/%d playlist_required=%s playlist_completed=%s state=%s",
                result.book_name,
                completed_part_count,
                part_count,
                playlist_required,
                playlist_completed,
                getattr(result, "state_path", ""),
            )
    else:
        result.audio_ready = bool(result.merged_audio_path and os.path.exists(result.merged_audio_path))
        result.video_ready = bool(result.video_path and os.path.exists(result.video_path))
        result.upload_ready = bool(result.youtube_url)

        required_stages = [result.audio_ready]
        if enable_video:
            required_stages.append(result.video_ready)
        if enable_upload:
            required_stages.append(result.upload_ready)

        result.success = all(required_stages)

    if not result.success and not result.error:
        if bool(getattr(result, "pending_resume", False)):
            result.error = "长音频分片处理中断，已记录进度，等待下次续跑"
        elif not result.audio_ready:
            result.error = "音频成品未准备完成"
        elif enable_video and not result.video_ready:
            result.error = "MP4 成品未准备完成"
        elif enable_upload and not result.upload_ready:
            result.error = "YouTube 上传未完成"

    if getattr(result, "split_mode", False) and not result.success:
        log.error(
            "[%s] Split finalization failed: completed_part_count=%d part_count=%d pending_resume=%s playlist_required=%s playlist_completed=%s audio_ready=%s video_ready=%s upload_ready=%s state=%s error=%s",
            result.book_name,
            completed_part_count,
            part_count,
            bool(getattr(result, "pending_resume", False)),
            bool(getattr(result, "playlist_required", False)),
            bool(getattr(result, "playlist_completed", False)),
            bool(getattr(result, "audio_ready", False)),
            bool(getattr(result, "video_ready", False)),
            bool(getattr(result, "upload_ready", False)),
            getattr(result, "state_path", ""),
            str(getattr(result, "error", "") or ""),
        )

    report = {
        "generated_at": dt_module.datetime.now().isoformat(),
        "book_dir": book_dir,
        "result": dict(result.__dict__),
    }
    if book_record is not None:
        report["source"] = {
            "book_id": book_record.get("book_id"),
            "book_name": book_record.get("book_name"),
            "category": book_record.get("category"),
        }

    report_path = os.path.join(book_dir, "book_result.json")
    try:
        write_json_file(report_path, report)
    except Exception as e:
        log.warning("单书结果写入失败: %s", e)

    log.info("🏆 本书《%s》全程线走完。状态：%s", result.book_name, "✅" if result.success else "❌")

    # 任务结束后清理中间文件（无论成功/失败/中断，释放磁盘空间）
    cleanup_book_dir_intermediate_files(book_dir, result, result.book_name)

    return result


def finalize_successful_book_for_project(book_record, result, book_name, flag):
    new_status = build_supabase_text_update(book_record.get("status"), [flag] if flag else [], prefer="string")

    try:
        _update_book_status_in_database(book_record["book_id"], new_status)
        book_record["status"] = new_status
        log.info("Completed and marked status='%s'", new_status)
    except Exception as e:
        log.error("[%s] Failed to update books.status: %s", book_name, e)
        result.success = False
        if getattr(result, "split_mode", False):
            result.pending_resume = True
            result.error = f"Split upload finished, but updating books.status failed: {e}"
        else:
            result.error = f"Output finished, but updating books.status failed: {e}"
        return False

    if getattr(result, "split_mode", False) or str(getattr(result, "state_path", "") or "").strip():
        try:
            if delete_split_processing_state(book_record, only_if_completed=False):
                result.state_path = ""
                if getattr(result, "split_mode", False):
                    log.info("[%s] Split upload finalized and book_processing_states deleted.", book_name)
                else:
                    log.info("[%s] Standard upload finalized and book_processing_states deleted.", book_name)
        except Exception as e:
            log.error(
                "[%s] books.status updated, but deleting book_processing_states failed; startup cleanup will retry: %s",
                book_name,
                e,
            )

    return True


# ---------------------------------------------------------------------------
# 单书入口（原文件行 7880-7968）
# ---------------------------------------------------------------------------
def process_book(book_record: dict, run_started_at=None) -> BookResult:
    """
    单书处理入口：
    1. 普通长度书籍沿用原有流程。
    2. 预估总时长超过 LONG_AUDIO_SPLIT_TRIGGER_HOURS 时切换到分片模式。
    3. 分片模式只处理当前分片所需章节，并把状态写入数据库的 BOOK_STATE_TABLE。
    """

    # 检查用户是否请求了停止（在开始处理前检查）
    if _check_db_stop_flag():
        result = BookResult(
            book_id=str(book_record.get("book_id", "")),
            book_name=book_record.get("book_name", "unknown"),
            category=book_record.get("category", ""),
            error="用户手动停止",
        )
        result.stop_requested = True
        return result

    book_id = str(book_record["book_id"])
    book_name = book_record.get("book_name") or f"book_{book_id}"
    category = book_record.get("category", "未分类")

    safe_name = sanitize_filename(book_name)
    safe_cat = sanitize_filename(category)
    output_root = str(getattr(cfg, "OUTPUT_ROOT", "/data/output") or "/data/output").strip()
    book_dir = os.path.join(output_root, safe_cat, f"{safe_name}_{book_id}")
    os.makedirs(book_dir, exist_ok=True)

    result = BookResult(book_id=book_id, book_name=book_name, category=category)

    def finish():
        return finalize_book_result(result, book_dir, book_record=book_record)

    raw = book_record.get("book_data", {})
    try:
        book_data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        result.error = f"book_data JSON 解析失败: {e}"
        return finish()

    if not isinstance(book_data, dict):
        result.error = "book_data 不是有效字典"
        return finish()

    chapters = _extract_chapters_from_book_data(book_data)
    chapters_sorted = sorted(chapters, key=lambda c: c.get("id", 0))
    result.chapter_count = len(chapters_sorted)
    explicit_total_duration_seconds = get_explicit_total_book_duration_seconds(chapters_sorted)
    enable_bgm = bool(getattr(cfg, "ENABLE_BGM_MIX", True))
    skip_existing = bool(getattr(cfg, "SKIP_EXISTING", True))

    if not chapters_sorted:
        final_path = os.path.join(book_dir, f"{safe_name}_mixed.mp3" if enable_bgm else f"{safe_name}.mp3")
        if skip_existing and os.path.exists(final_path) and os.path.getsize(final_path) > 0:
            result.merged_audio_path = final_path
            result.audio_ready = True
            prepare_standard_book_cover_and_seo_with_state(
                result,
                book_record,
                book_data,
                book_dir,
                safe_name,
                book_name,
            )
        else:
            result.error = "chapters_data 为空或无效，且不存在可复用的成品音频"
        return finish()

    split_plan = build_split_part_plans(chapters_sorted)
    result.estimated_total_duration_seconds = split_plan.get("estimated_total_seconds", 0)
    if explicit_total_duration_seconds is not None:
        result.estimated_total_duration_seconds = explicit_total_duration_seconds

    if explicit_total_duration_seconds is not None and 0 < int(explicit_total_duration_seconds or 0) < MIN_BOOK_DURATION_SECONDS:
        skip_and_delete_short_book(book_record, result, book_name)
        return finish()

    if split_plan.get("split_mode"):
        process_split_book(
            result,
            book_record,
            book_data,
            chapters_sorted,
            book_dir,
            safe_name,
            book_name,
            category,
            run_started_at=run_started_at,
        )
    else:
        process_standard_book(
            result,
            book_record,
            book_data,
            chapters_sorted,
            book_dir,
            safe_name,
            book_name,
            category,
        )

    return finish()


# ---------------------------------------------------------------------------
# 运行汇总 & 时长控制（原文件行 3095-3117 / 3022-3092）
# ---------------------------------------------------------------------------


def _check_db_stop_flag():
    """检查数据库中的 stop_requested 标志（替代 Redis）。

    使用 pipeline.db 的连接池，避免每次检查都新建 TCP 连接。
    """
    import os as _os
    # 优先从配置读取（由 apply_runtime_config 注入，线程安全），
    # 回退到环境变量（兼容旧代码）
    pipeline_task_id = str(getattr(cfg, "PIPELINE_TASK_ID", "") or "").strip()
    if not pipeline_task_id:
        pipeline_task_id = _os.environ.get("PIPELINE_TASK_ID", "").strip()
    if not pipeline_task_id:
        return False
    try:
        from .db import execute_postgres_fetchone
        row = execute_postgres_fetchone(
            "SELECT stop_requested FROM public.run_tasks WHERE task_id = %s",
            (pipeline_task_id,),
            optional=True,
        )
        if row and row.get("stop_requested"):
            return True
    except Exception as e:
        log.warning("检查 stop_requested 标志失败 (task_id=%s): %s", pipeline_task_id, e)
    return False




def save_run_summary(output_root, results, archive=True, extra=None):
    from .config import collect_runtime_config_snapshot

    report_dir = os.path.join(output_root, "_run_reports")
    timestamp = dt_module.datetime.now().strftime("%Y%m%d_%H%M%S")
    success_items = [r for r in results if r.success]
    partial_items = [r for r in results if getattr(r, "pending_resume", False)]
    skipped_items = [r for r in results if getattr(r, "skipped", False)]
    failed_items = [
        r for r in results if not r.success and not getattr(r, "pending_resume", False) and not getattr(r, "skipped", False)
    ]
    summary = {
        "generated_at": dt_module.datetime.now().isoformat(),
        "config": collect_runtime_config_snapshot(),
        "total": len(results),
        "success": len(success_items),
        "partial": len(partial_items),
        "skipped": len(skipped_items),
        "failed": len(failed_items),
        "success_items": [
            {
                "book_id": r.book_id,
                "book_name": r.book_name,
                "youtube_url": r.youtube_url,
                "publish_at": getattr(r, "youtube_publish_at", ""),
                "schedule_reason": getattr(r, "youtube_schedule_reason", ""),
                "video_path": r.video_path,
            }
            for r in success_items
        ],
        "partial_items": [
            {
                "book_id": r.book_id,
                "book_name": r.book_name,
                "error": r.error,
                "state_ref": getattr(r, "state_path", ""),
                "completed_part_count": getattr(r, "completed_part_count", 0),
                "part_count": getattr(r, "part_count", 1),
            }
            for r in partial_items
        ],
        "skipped_items": [
            {
                "book_id": r.book_id,
                "book_name": r.book_name,
                "reason": getattr(r, "skipped_reason", "") or r.error,
                "deleted_from_books": bool(getattr(r, "deleted_from_books", False)),
            }
            for r in skipped_items
        ],
        "failed_items": [
            {
                "book_id": r.book_id,
                "book_name": r.book_name,
                "error": r.error,
            }
            for r in failed_items
        ],
        "items": [dict(r.__dict__) for r in results],
    }
    if extra:
        summary["runtime"] = extra

    latest_path = os.path.join(report_dir, "latest_run_summary.json")
    write_json_file(latest_path, summary)
    if archive:
        archive_path = os.path.join(report_dir, f"run_summary_{timestamp}.json")
        write_json_file(archive_path, summary)
        log.info("🧾 运行汇总已写入: %s", archive_path)
        return archive_path

    log.info("🧾 运行进度已更新: %s", latest_path)
    return latest_path


# ---------------------------------------------------------------------------
# 主入口（原文件行 8047-8266）
# ---------------------------------------------------------------------------
def run_pipeline(runtime_config: dict | None = None):
    cfg.apply_runtime_config(runtime_config)
    from .config import validate_runtime_config

    validate_runtime_config()

    execute_postgres_fetchval("SELECT 1 AS ok")
    log.info("PostgreSQL connected")

    output_root = str(getattr(cfg, "OUTPUT_ROOT", "/data/output") or "/data/output").strip()
    os.makedirs(output_root, exist_ok=True)

    cat_label = str(getattr(cfg, "TARGET_CATEGORY", "") or "").strip() or "all"
    log.info("Fetching books... category=%s", cat_label)

    all_books = []
    all_books_by_id = {}
    page_size = 100
    offset = 0
    target_category = getattr(cfg, "TARGET_CATEGORY", "")

    while True:
        rows = _fetch_books_page_from_database(offset, page_size, target_category)
        if not rows:
            break

        for row in rows:
            book_id = str(row.get("book_id") or "").strip()
            if book_id:
                all_books_by_id[book_id] = row

            tags_list = set(normalize_text_items(row.get("tags")))
            if "bad" not in tags_list:
                all_books.append(row)

        if len(rows) < page_size:
            break
        offset += page_size

    flag = str(getattr(cfg, "PROJECT_FLAG", "") or "").strip()
    interrupted_states = list_interrupted_book_states(all_books_by_id) if all_books_by_id else {}

    force_reprocess = bool(getattr(cfg, "FORCE_REPROCESS", False))
    if not force_reprocess and flag:
        filtered_books = []
        for book in all_books:
            existing_flags = set(normalize_text_items(book.get("status")))
            if flag not in existing_flags:
                filtered_books.append(book)
        all_books = filtered_books

    log.info("Books remaining after status filter: %d", len(all_books))

    # ── 仅TG缓存完整书过滤：只保留所有章节均已DF降噪并上传到TG的书籍 ──
    only_tg_cached = bool(getattr(cfg, "ONLY_TG_CACHED_BOOKS", False))
    if only_tg_cached and all_books:
        # 检查 TG 缓存功能是否真正可用（需要 TG_BOT_TOKEN，支持逗号分隔多个 Token）
        tg_bot_token_raw = str(getattr(cfg, "TG_BOT_TOKEN", "") or "").strip()
        tg_bot_tokens = [t.strip() for t in tg_bot_token_raw.split(",") if t.strip()] if tg_bot_token_raw else []
        if not tg_bot_tokens:
            log.warning(
                "[TG缓存] ⚠️ ONLY_TG_CACHED_BOOKS 已启用，但 TG_BOT_TOKEN 未配置！"
                "将无法从 TG 下载已降噪音频，会回退到常规下载+DeepFilter 处理。"
                "请在「全局设置」中配置 TG_BOT_TOKEN。"
            )
        else:
            log.info("[TG缓存] TG_BOT_TOKEN 已配置 %d 个 Bot，TG 缓存功能可用。", len(tg_bot_tokens))
        from psycopg import sql as _sql_mod
        tg_table = get_public_table_identifier("audiobook_chapters")
        try:
            tg_rows = execute_postgres_fetchall(
                _sql_mod.SQL(
                    """
                    SELECT book_id
                    FROM {}
                    GROUP BY book_id
                    HAVING COUNT(*) = COUNT(
                        CASE WHEN upload_status = 'uploaded'
                             AND telegram_file_id IS NOT NULL
                             AND telegram_file_id != ''
                        THEN 1 END
                    )
                    """
                ).format(tg_table),
            )
            fully_cached_ids = {str(r.get("book_id", "")).strip() for r in (tg_rows or []) if r.get("book_id")}
            before = len(all_books)
            all_books = [b for b in all_books if str(b.get("book_id", "")).strip() in fully_cached_ids]
            log.info("[TG缓存过滤] 仅有 %d/%d 本书的所有章节均已上传到TG", len(all_books), before)
        except Exception as e:
            log.warning("[TG缓存过滤] 查询 audiobook_chapters 失败（表可能不存在）: %s", e)

    if all_books:
        interrupted_books = [book for book in all_books if str(book.get("book_id")) in interrupted_states]
        fresh_books = [book for book in all_books if str(book.get("book_id")) not in interrupted_states]
        random.shuffle(fresh_books)

        prioritize = bool(getattr(cfg, "PRIORITIZE_INTERRUPTED_BOOKS", True))
        if prioritize and interrupted_books:
            interrupted_books.sort(
                key=lambda item: interrupted_states[str(item.get("book_id"))].get("updated_at", ""),
                reverse=True,
            )
            all_books = interrupted_books + fresh_books
            log.info("Prioritizing %d interrupted books with saved processing state.", len(interrupted_books))
        else:
            all_books = fresh_books + interrupted_books
            random.shuffle(all_books)
            log.info("Shuffled processing order for %d books.", len(all_books))

    try:
        success_target_count = max(0, int(getattr(cfg, "MAX_PROCESS_COUNT", 0) or 0))
    except Exception:
        success_target_count = 0

    if success_target_count > 0:
        log.info("This run will stop after %d successful uploads.", success_target_count)

    if not all_books:
        runtime_console_print("No books to process.", level="INFO")
        return {
            "success": True,
            "results": [],
            "summary_path": "",
            "stop_reason": "",
            "successful_upload_count": 0,
        }

    all_results = []
    run_started_at = time.time()
    stop_reason = ""
    successful_upload_count = 0
    enable_upload = bool(getattr(cfg, "ENABLE_YOUTUBE_UPLOAD", True))

    def counts_towards_max_process(result):
        if not getattr(result, "success", False):
            return False
        if enable_upload:
            return bool(getattr(result, "upload_ready", False))
        return True

    for i, book in enumerate(all_books, start=1):
        if success_target_count > 0 and successful_upload_count >= success_target_count:
            stop_reason = f"Reached upload target for this run: {success_target_count}"
            log.info(stop_reason)
            break

        # 先检查用户是否请求了停止（数据库 stop_requested）
        if _check_db_stop_flag():
            stop_reason = "用户手动停止"
            log.warning(stop_reason)
            break

        should_stop = _check_db_stop_flag()
        if should_stop:
            stop_reason = "用户手动停止"
            log.warning(stop_reason)
            break

        if i > 1:
            clear_runtime_output_if_needed()

        name = book.get("book_name", "unknown")
        cat = book.get("category", "uncategorized")
        runtime_console_print(f"\n{'=' * 50}", level="INFO")
        log.info("[%d/%d] Book: %s | %s", i, len(all_books), name, cat)

        should_break_after_summary = False
        try:
            result = process_book(book, run_started_at=run_started_at)
        except MissingYouTubeCredentialsError as e:
            stop_reason = f"YouTube credential initialization failed: {e}"
            log.error("[%s] %s", name, stop_reason)
            result = BookResult(book_id=str(book.get("book_id", "")), book_name=name, category=cat, error=str(e))
            should_break_after_summary = True
        except Exception as e:
            log.error("[%s] Uncaught exception while processing book: %s", name, e)
            result = BookResult(book_id=str(book.get("book_id", "")), book_name=name, category=cat, error=f"Uncaught exception: {e}")
            # 未捕获异常时也清理中间文件，避免磁盘被残留文件占满
            try:
                _safe_name = sanitize_filename(name)
                _safe_cat = sanitize_filename(cat)
                _output_root = str(getattr(cfg, "OUTPUT_ROOT", "/data/output") or "/data/output").strip()
                _book_dir = os.path.join(_output_root, _safe_cat, f"{_safe_name}_{book.get('book_id', '')}")
                cleanup_book_dir_intermediate_files(_book_dir, result, name)
            except Exception:
                pass

        all_results.append(result)

        # 检查是否因用户停止而提前退出
        if getattr(result, "stop_requested", False):
            stop_reason = "用户手动停止"
            log.warning("[%s] %s", name, stop_reason)
            should_break_after_summary = True

        if result.success:
            finalize_successful_book_for_project(book, result, name, flag)

        if result.success:
            if counts_towards_max_process(result):
                successful_upload_count += 1
                if success_target_count > 0:
                    log.info("Upload counter progress: %d/%d", successful_upload_count, success_target_count)

            log.info(
                "chapters=%d merged=%s mixed=%s",
                result.success_count,
                os.path.basename(result.merged_audio_path) if result.merged_audio_path else "none",
                os.path.basename(result.mixed_audio_path) if result.mixed_audio_path else "none",
            )
        elif result.skipped:
            log.info("Skipped: %s", getattr(result, "skipped_reason", "") or result.error)
        elif result.pending_resume:
            log.warning("Resume state saved: %s", result.error)
            if result.stop_requested:
                stop_reason = result.error
                should_break_after_summary = True
        else:
            log.error("Failure: %s", result.error)

            if "chapters_data" in result.error:
                existing_tags = normalize_text_items(book.get("tags"))
                if "bad" not in existing_tags:
                    new_tags = build_supabase_text_update(book.get("tags"), ["bad"], prefer="array")
                    try:
                        _update_book_tags_in_database(book["book_id"], new_tags)
                        book["tags"] = new_tags
                        log.info("Marked book tags with 'bad'.")
                    except Exception as e:
                        log.error("Failed to update tags: %s", e)

        try:
            save_run_summary(
                output_root,
                all_results,
                archive=False,
                extra={
                    "run_started_at": dt_module.datetime.fromtimestamp(run_started_at).isoformat(),
                    "elapsed_seconds": round(time.time() - run_started_at, 1),
                    "stop_reason": stop_reason,
                },
            )
        except Exception as e:
            log.warning("Failed to write incremental run summary: %s", e)

        if should_break_after_summary:
            break

    success = sum(1 for r in all_results if r.success)
    partial = sum(1 for r in all_results if getattr(r, "pending_resume", False))
    skipped = sum(1 for r in all_results if getattr(r, "skipped", False))
    failed = len(all_results) - success - partial - skipped
    runtime_console_print("\n" + "=" * 42, level="INFO")
    runtime_console_print("  Run Complete", level="INFO")
    runtime_console_print(
        f"  Total: {len(all_results)}  Success: {success}  Resume: {partial}  Skipped: {skipped}  Failed: {failed}",
        level="INFO",
    )
    if success_target_count > 0:
        runtime_console_print(f"  Upload Counter: {successful_upload_count}/{success_target_count}", level="INFO")
    runtime_console_print(f"  Output Dir: {output_root}", level="INFO")
    summary_path = save_run_summary(
        output_root,
        all_results,
        archive=True,
        extra={
            "run_started_at": dt_module.datetime.fromtimestamp(run_started_at).isoformat(),
            "elapsed_seconds": round(time.time() - run_started_at, 1),
            "stop_reason": stop_reason,
        },
    )
    runtime_console_print(f"  Summary: {summary_path}", level="INFO")
    runtime_console_print("=" * 42, level="INFO")

    return {
        "success": failed == 0 and partial == 0,
        "results": all_results,
        "summary_path": summary_path,
        "stop_reason": stop_reason,
        "successful_upload_count": successful_upload_count,
    }