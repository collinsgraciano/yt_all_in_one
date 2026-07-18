"""运行核心：音频下载、合并、时长探测、章节时间轴。

对应原 runtime_core.py:
- download_file_with_wget / download_file_with_requests / download_file (行 315-709)
- download_audio_file (行 712-812)
- merge_audio_ffmpeg (行 813-861)
- clear_folder (行 864-874)
- probe_audio_duration_seconds (行 1088-1113)
- estimate_chapter_duration_seconds (行 1116-1132)
- get_explicit_chapter_duration_seconds (行 1135-1151)
- get_explicit_total_book_duration_seconds (行 1154-1164)
- generate_youtube_timestamps (行 4688-4725)
- download_chapter_items (行 7033-7128)
- build_final_audio_from_chapter_paths (行 7131-7182)
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlparse, urlunparse

import requests
from pydub import AudioSegment
from tqdm.auto import tqdm

from . import config as cfg
from .runtime import (
    log,
    parse_duration_to_seconds,
    sanitize_filename,
)


# ============================================================================
# 通用下载（原文件行 315-376）
# ============================================================================

def download_file_with_wget(download_url, output_path, headers=None, retries=3):
    from .runtime import runtime_console_print

    headers = headers or {}
    wget_binary = shutil.which("wget")
    if not wget_binary:
        return False

    for attempt in range(1, retries + 1):
        if os.path.exists(output_path):
            os.remove(output_path)

        cmd = [
            wget_binary,
            "-O",
            output_path,
            "--tries=1",
            "--timeout=30",
            "--read-timeout=30",
            "--retry-connrefused",
            "--waitretry=5",
            download_url,
        ]
        for key, value in headers.items():
            cmd.insert(-1, "--header")
            cmd.insert(-1, f"{key}: {value}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
        except Exception as e:
            runtime_console_print(f"⚠️ wget 下载第 {attempt}/{retries} 次失败: {e}", level="WARNING")

        time.sleep(min(10, attempt * 2))

    return False


def download_file_with_requests(download_url, output_path, headers=None, retries=3):
    from .runtime import runtime_console_print

    headers = headers or {}
    temp_path = output_path + ".tmp"

    for attempt in range(1, retries + 1):
        try:
            with requests.get(download_url, headers=headers, stream=True,
                              timeout=(30, 120), allow_redirects=True) as response:
                response.raise_for_status()
                with open(temp_path, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            handle.write(chunk)

            if os.path.exists(output_path):
                os.remove(output_path)
            shutil.move(temp_path, output_path)
            if os.path.getsize(output_path) > 0:
                return True
        except Exception as e:
            runtime_console_print(
                f"⚠️ requests 下载第 {attempt}/{retries} 次失败: {e}", level="WARNING",
            )
            if os.path.exists(temp_path):
                os.remove(temp_path)
            time.sleep(min(10, attempt * 2))

    return False


# ============================================================================
# 通用文件下载（原文件行 673-709）
# ============================================================================

def download_file(url: str, save_path: str, retries=None) -> bool:
    """
    下载文件到指定路径。
    使用临时文件 + rename 确保原子性，指数退避重试。
    """
    if retries is None:
        retries = int(getattr(cfg, "MAX_RETRIES", 3))
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        return True

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    tmp_path = save_path + ".tmp"
    request_timeout = int(getattr(cfg, "REQUEST_TIMEOUT", 300))

    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=request_timeout, stream=True)
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    f.write(chunk)

            # 验证文件大小
            expected = resp.headers.get("Content-Length")
            actual = os.path.getsize(tmp_path)
            if expected and int(expected) != actual:
                log.warning("文件大小不匹配: 预期=%s 实际=%s", expected, actual)
                os.remove(tmp_path)
                continue

            shutil.move(tmp_path, save_path)
            return True
        except Exception as e:
            wait = 2 ** attempt
            log.warning("下载失败（第%d/%d次，等%ds）: %s", attempt + 1, retries, wait, e)
            time.sleep(wait)

    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    return False


# ============================================================================
# 章节音频专用下载（原文件行 712-812）
# ============================================================================

def download_audio_file(url: str, save_path: str, timeout_seconds: int = 300) -> dict:
    """
    章节音频专用下载：
    - 单次请求拆分为连接超时 + 读超时
    - 失败后按上限重试，避免单个坏链接无限卡死整本书
    - 继续使用临时文件 + rename，避免生成坏文件
    """
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        return {"ok": True, "attempts": 0, "elapsed_seconds": 0.0, "error": ""}

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    tmp_path = save_path + ".tmp"
    attempt = 0
    started_at = time.time()
    last_error = ""

    connect_timeout = max(3, int(getattr(cfg, "AUDIO_DOWNLOAD_CONNECT_TIMEOUT", 20) or 20))
    read_timeout = max(5, int(getattr(cfg, "AUDIO_DOWNLOAD_READ_TIMEOUT", timeout_seconds) or timeout_seconds))
    max_attempts = max(1, int(getattr(cfg, "AUDIO_DOWNLOAD_MAX_RETRY_ATTEMPTS", 12) or 12))
    max_total_seconds = max(read_timeout, int(getattr(cfg, "AUDIO_DOWNLOAD_MAX_TOTAL_SECONDS", 1800) or 1800))

    while attempt < max_attempts:
        attempt += 1
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

            with requests.get(url, timeout=(connect_timeout, read_timeout), stream=True) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)

                expected = resp.headers.get("Content-Length")
            actual = os.path.getsize(tmp_path)
            if expected and int(expected) != actual:
                last_error = f"文件大小不匹配: 预期={expected} 实际={actual}"
                log.warning(
                    "章节音频下载大小不匹配，将继续重试: 预期=%s 实际=%s 文件=%s",
                    expected,
                    actual,
                    os.path.basename(save_path),
                )
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                wait = min(60, max(2, 2 ** min(attempt - 1, 5)))
                if attempt >= max_attempts or (time.time() - started_at + wait) > max_total_seconds:
                    break
                time.sleep(wait)
                continue

            shutil.move(tmp_path, save_path)
            if attempt > 1:
                log.info("章节音频下载重试后成功: %s（第 %d 次）", os.path.basename(save_path), attempt)
            return {
                "ok": True,
                "attempts": attempt,
                "elapsed_seconds": round(time.time() - started_at, 1),
                "error": "",
            }
        except Exception as e:
            last_error = str(e)
            wait = min(60, max(2, 2 ** min(attempt - 1, 5)))
            log.warning(
                "章节音频下载失败，将继续重试（第 %d/%d 次，%ds 后重试）: %s | %s",
                attempt,
                max_attempts,
                wait,
                os.path.basename(save_path),
                e,
            )
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if attempt >= max_attempts or (time.time() - started_at + wait) > max_total_seconds:
                break
            time.sleep(wait)

    elapsed_seconds = round(time.time() - started_at, 1)
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    log.error(
        "章节音频下载达到上限，停止重试: %s | 已尝试 %d 次，耗时 %.1fs | 最后错误: %s",
        os.path.basename(save_path),
        attempt,
        elapsed_seconds,
        last_error or "未知错误",
    )

    return {
        "ok": False,
        "attempts": attempt,
        "elapsed_seconds": elapsed_seconds,
        "error": last_error or "未知错误",
    }


# ============================================================================
# 合并（原文件行 813-861）
# ============================================================================

def merge_audio_ffmpeg(mp3_paths: list, output_path: str) -> bool:
    """
    使用 ffmpeg concat demuxer 合并多个 mp3 文件。
    零内存占用、无损（直接复制流）。
    """
    if not mp3_paths:
        log.warning("没有音频文件可合并")
        return False

    if getattr(cfg, "SKIP_EXISTING", True) and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        log.info("合并文件已存在，跳过: %s", os.path.basename(output_path))
        return True

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    list_file = output_path + ".filelist.txt"

    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in mp3_paths:
                escaped = p.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        log.info("开始合并 %d 个音频...", len(mp3_paths))
        tmp_output = output_path + ".merging.mp3"
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file, "-c", "copy", tmp_output],
            capture_output=True, text=True, timeout=3600,
        )

        if result.returncode != 0:
            log.error("ffmpeg 合并失败: %s", result.stderr[-500:] if result.stderr else "")
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
            return False

        shutil.move(tmp_output, output_path)
        log.info("✅ 合并完成：%s", os.path.basename(output_path))
        return True
    except subprocess.TimeoutExpired:
        log.error("ffmpeg 合并超时")
        return False
    except Exception as e:
        log.error("合并失败: %s", e)
        return False
    finally:
        if os.path.exists(list_file):
            os.remove(list_file)


# ============================================================================
# 目录清理（原文件行 864-874）
# ============================================================================

def clear_folder(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        target = os.path.join(path, name)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
        except Exception as e:
            log.warning("清理目录失败: %s", e)


# ============================================================================
# 时长探测与估算（原文件行 1066-1164）
# ============================================================================

def probe_audio_duration_seconds(audio_path):
    if not audio_path or not os.path.exists(audio_path):
        return None

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return max(0, int(round(float(result.stdout.strip()))))
    except Exception:
        try:
            return max(0, int(round(len(AudioSegment.from_file(audio_path)) / 1000)))
        except Exception:
            return None


def estimate_chapter_duration_seconds(chapter):
    if not isinstance(chapter, dict):
        return 1

    direct_value = chapter.get("duration_seconds")
    if isinstance(direct_value, (int, float)) and direct_value > 0:
        return max(1, int(round(float(direct_value))))

    for key in ("long", "duration", "audioDuration", "audio_duration"):
        value = chapter.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return max(1, int(round(float(value))))
        seconds = parse_duration_to_seconds(value)
        if seconds > 0:
            return seconds

    return 1


def get_explicit_chapter_duration_seconds(chapter):
    if not isinstance(chapter, dict):
        return None

    direct_value = chapter.get("duration_seconds")
    if isinstance(direct_value, (int, float)) and direct_value > 0:
        return max(1, int(round(float(direct_value))))

    for key in ("long", "duration", "audioDuration", "audio_duration"):
        value = chapter.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return max(1, int(round(float(value))))
        seconds = parse_duration_to_seconds(value)
        if seconds > 0:
            return seconds

    return None


def get_explicit_total_book_duration_seconds(chapters_sorted):
    if not chapters_sorted:
        return 0

    total_seconds = 0
    for chapter in chapters_sorted:
        chapter_seconds = get_explicit_chapter_duration_seconds(chapter)
        if chapter_seconds is None:
            return None
        total_seconds += chapter_seconds
    return total_seconds


# ============================================================================
# 章节时间轴生成（原文件行 4688-4725）
# ============================================================================

def generate_youtube_timestamps(chapters_data, chapter_audio_paths=None):
    """
    优先根据实际章节音频时长生成时间轴；若没有音频文件则回退到 chapters_data.long。
    """
    log.info("【⏳ 时间轴计算】正在组装 YouTube 视频分段指针...")
    timestamps = []
    current_time_seconds = 0

    sorted_chapters = sorted(chapters_data, key=lambda x: x.get("id", 0))
    use_audio_durations = bool(chapter_audio_paths) and len(chapter_audio_paths) == len(sorted_chapters)
    if chapter_audio_paths and not use_audio_durations:
        log.warning("时间轴音频数量与章节数量不一致，回退到 long 字段。")
    elif use_audio_durations:
        log.info("时间轴优先使用实际章节音频时长，避免 long 字段漂移。")

    for idx, ch in enumerate(sorted_chapters):
        h = current_time_seconds // 3600
        m = (current_time_seconds % 3600) // 60
        s = current_time_seconds % 60
        if h > 0:
            time_str = f"{h:02d}:{m:02d}:{s:02d}"
        else:
            time_str = f"{m:02d}:{s:02d}"

        title = ch.get("title", f"章节 {ch.get('id', '')}").strip()
        timestamps.append(f"{time_str} {title}")

        duration_sec = None
        if use_audio_durations:
            duration_sec = probe_audio_duration_seconds(chapter_audio_paths[idx])
        if duration_sec is None:
            duration_sec = parse_duration_to_seconds(ch.get("long", "00:00"))

        current_time_seconds += max(0, int(duration_sec or 0))

    final_text = "\n".join(timestamps)
    log.info("🎉 成功排盘 %d 章时间轴，成片总预估时长：%02d:%02d:%02d",
             len(sorted_chapters),
             current_time_seconds // 3600,
             (current_time_seconds % 3600) // 60,
             current_time_seconds % 60)
    return final_text


# ============================================================================
# 批量章节下载（原文件行 7033-7128）
# ============================================================================

def download_chapter_items(chapter_items, chapters_dir, book_id="", tg_cache_map=None):
    """批量下载章节音频，支持 Telegram 缓存回退。

    参数:
        chapter_items: 章节列表，每项含 source_index / chapter / title
        chapters_dir: 下载目录
        book_id: 书籍ID（用于查询 TG 缓存）
        tg_cache_map: 预取的 {audio_url: telegram_file_id} 映射；
                      若为 None 且 book_id 非空，则自动查询

    返回: (chapter_paths, tg_cached_indices)
        chapter_paths: 成功下载的文件路径列表（按 source_index 排序）
        tg_cached_indices: 从 TG 缓存下载的 source_index 集合（已降噪，可跳过 DeepFilter）
    """
    if not chapter_items:
        return [], set()

    os.makedirs(chapters_dir, exist_ok=True)
    stuck_log_interval = max(10, int(getattr(cfg, "AUDIO_DOWNLOAD_STUCK_LOG_INTERVAL_SECONDS", 30) or 30))
    request_delay = float(getattr(cfg, "REQUEST_DELAY", 0.3))

    # 延迟导入避免循环依赖
    from .tg_audio import fetch_tg_cache_map, download_audio_from_telegram

    # 自动查询 TG 缓存（如果未传入且 book_id 有效）
    if tg_cache_map is None and book_id:
        audio_urls = [item["chapter"].get("mp3Url", "") for item in chapter_items]
        tg_cache_map = fetch_tg_cache_map(book_id, audio_urls)
    elif tg_cache_map is None:
        tg_cache_map = {}

    def dl_one(item):
        mp3_url = item["chapter"].get("mp3Url", "")
        title = item.get("title") or f"chapter_{item['source_index']:04d}"
        if not mp3_url:
            return {
                "source_index": item["source_index"],
                "title": title,
                "path": None,
                "attempts": 0,
                "elapsed_seconds": 0.0,
                "error": "章节缺少 mp3Url",
                "from_tg": False,
            }

        path = os.path.join(chapters_dir, f"{item['source_index']:04d}_{sanitize_filename(title)}.mp3")

        # 检查 TG 缓存：如果该章节的 mp3Url 在缓存映射中，从 Telegram 下载已降噪音频
        # tg_cache_map 的值现在是 dict: {file_id, bot_id, bot_user_id}
        # 兼容旧格式（纯 file_id 字符串）
        tg_cache_info = tg_cache_map.get(mp3_url)
        if tg_cache_info:
            if isinstance(tg_cache_info, dict):
                tg_file_id = tg_cache_info.get("file_id", "")
                tg_bot_id = tg_cache_info.get("bot_id")
                tg_bot_user_id = tg_cache_info.get("bot_user_id")
            else:
                # 向后兼容：旧格式直接是 file_id 字符串
                tg_file_id = str(tg_cache_info)
                tg_bot_id = None
                tg_bot_user_id = None

            if tg_file_id:
                log.info("[TG缓存] 章节 %s 命中 TG 缓存，从 Telegram 下载", title)
                # download_audio_from_telegram 内部已处理串行锁和下载间隔，无需再 sleep
                # 传入 bot_id / bot_user_id 以匹配正确的 Bot Token（file_id 与上传 Bot 绑定）
                tg_result = download_audio_from_telegram(
                    tg_file_id, path, max_retries=3,
                    bot_id=tg_bot_id, bot_user_id=tg_bot_user_id,
                )
                return {
                    "source_index": item["source_index"],
                    "title": title,
                    "path": path if tg_result["ok"] else None,
                    "attempts": 1,
                    "elapsed_seconds": 0.0,
                    "error": tg_result["error"],
                    "from_tg": True,
                }

        # 常规下载
        result = download_audio_file(mp3_url, path, timeout_seconds=300)
        time.sleep(request_delay)
        return {
            "source_index": item["source_index"],
            "title": title,
            "path": path if result["ok"] else None,
            "attempts": result["attempts"],
            "elapsed_seconds": result["elapsed_seconds"],
            "error": result["error"],
            "from_tg": False,
        }

    download_workers = int(getattr(cfg, "DOWNLOAD_WORKERS", 4))
    paths_map = {}
    tg_cached_map = {}  # source_index -> bool (是否从 TG 缓存下载)
    failures = {}
    total = len(chapter_items)

    with concurrent.futures.ThreadPoolExecutor(max_workers=download_workers) as exe:
        futures = {
            exe.submit(dl_one, item): {
                "source_index": item["source_index"],
                "title": item.get("title") or f"chapter_{item['source_index']:04d}",
                "submitted_at": time.time(),
            }
            for item in chapter_items
        }
        pending = set(futures.keys())

        # 延迟导入 stop 检查
        try:
            from .pipeline import _check_db_stop_flag
        except ImportError:
            _check_db_stop_flag = None

        with tqdm(total=total, desc="并发下载分片章节", unit="章") as progress:
            while pending:
                # 检查用户是否请求了停止
                if _check_db_stop_flag:
                    try:
                        if _check_db_stop_flag():
                            log.warning("章节下载被用户中止，取消剩余任务...")
                            for f in pending:
                                f.cancel()
                            raise RuntimeError("用户手动停止")
                    except RuntimeError:
                        raise
                    except Exception:
                        pass

                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=stuck_log_interval,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                if done:
                    for future in done:
                        result = future.result()
                        idx = result["source_index"]
                        paths_map[idx] = result["path"]
                        tg_cached_map[idx] = result.get("from_tg", False)
                        if not result["path"]:
                            failures[idx] = result
                        progress.update(1)
                    continue

                pending_samples = []
                now = time.time()
                for future in sorted(pending, key=lambda f: futures[f]["source_index"])[:5]:
                    meta = futures[future]
                    pending_samples.append(
                        f"{meta['source_index']:04d}_{sanitize_filename(meta['title'])}({int(now - meta['submitted_at'])}s)"
                    )

                log.warning(
                    "并发下载仍在等待 %d/%d 个章节完成，可能有线程正在长时间重试或网络静默。当前等待中: %s",
                    len(pending),
                    total,
                    " | ".join(pending_samples) if pending_samples else "无",
                )

    ordered_indexes = [item["source_index"] for item in chapter_items]
    chapter_paths = [paths_map[idx] for idx in ordered_indexes if paths_map.get(idx)]
    tg_cached_indices = {idx for idx in ordered_indexes if tg_cached_map.get(idx)}

    if len(chapter_paths) != len(ordered_indexes):
        missing_details = []
        for idx in ordered_indexes:
            if paths_map.get(idx):
                continue
            failed = failures.get(idx)
            if failed:
                missing_details.append(
                    f"{idx:04d}_{sanitize_filename(failed['title'])}"
                    f"(重试{failed['attempts']}次, 耗时{int(failed['elapsed_seconds'])}s, {failed['error']})"
                )
            else:
                missing_details.append(f"{idx:04d}_未知章节(未返回结果)")
        raise RuntimeError(f"章节下载不完整，失败章节: {'; '.join(missing_details)}")

    if tg_cached_indices:
        log.info("[TG缓存] %d/%d 个章节从 TG 缓存下载（跳过 DeepFilter）", len(tg_cached_indices), total)

    return chapter_paths, tg_cached_indices


# ============================================================================
# 最终音频构建（原文件行 7131-7182）
# ============================================================================

def build_final_audio_from_chapter_paths(chapter_paths, working_dir, merged_path, mixed_path, book_name):
    enable_bgm = bool(getattr(cfg, "ENABLE_BGM_MIX", True))
    music_dir = str(getattr(cfg, "MUSIC_DIR", "") or "").strip()

    if enable_bgm and music_dir and os.path.exists(music_dir):
        from .bgm import mix_with_bgm

        mixed_dir = os.path.join(working_dir, "mixed_chapters")
        os.makedirs(mixed_dir, exist_ok=True)
        mixed_chapters = []

        for i, ch_path in enumerate(chapter_paths, start=1):
            mixed_basename = os.path.splitext(os.path.basename(ch_path))[0] + "_mixed.mp3"
            ch_mixed = os.path.join(mixed_dir, mixed_basename)
            if os.path.exists(ch_mixed) and os.path.getsize(ch_mixed) > 0:
                mixed_chapters.append(ch_mixed)
                continue

            log.info("[%s] 混音章节 %d/%d -> %s", book_name, i, len(chapter_paths), os.path.basename(ch_path))
            ok_mix = mix_with_bgm(
                ch_path,
                ch_mixed,
                music_dir,
                volume_offset_db=int(getattr(cfg, "VOLUME_OFFSET_DB", -25)),
                highpass_freq=int(getattr(cfg, "HIGHPASS_FREQ", 150)),
                fade_duration_ms=int(getattr(cfg, "FADE_DURATION_MS", 3000)),
                min_volume_db=int(getattr(cfg, "MIN_VOLUME_DB", -40)),
                dyn_vol=bool(getattr(cfg, "ENABLE_DYNAMIC_VOLUME", True)),
                spec_shape=bool(getattr(cfg, "ENABLE_SPECTRAL_SHAPING", True)),
                stereo_offset=float(getattr(cfg, "STEREO_OFFSET", 0.0)),
            )
            if not ok_mix:
                raise RuntimeError(f"BGM 混音失败: {os.path.basename(ch_path)}")
            mixed_chapters.append(ch_mixed)

        if not merge_audio_ffmpeg(mixed_chapters, mixed_path):
            raise RuntimeError("长音频分片混音合并失败")

        for temp_path in mixed_chapters:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception as cleanup_error:
                log.warning("清理临时混音文件失败: %s", cleanup_error)

        return {
            "audio_path": mixed_path,
            "mixed_audio_path": mixed_path,
        }

    if not merge_audio_ffmpeg(chapter_paths, merged_path):
        raise RuntimeError("章节音频合并失败")

    return {
        "audio_path": merged_path,
        "mixed_audio_path": "",
    }