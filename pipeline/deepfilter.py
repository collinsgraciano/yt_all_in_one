"""运行核心：DeepFilter 降噪处理。

对应原 runtime_core.py:
- DEEP_FILTER_PATH / DEEP_FILTER_DRIVE（行 877-878）
- setup_deep_filter（行 881-898）
- module-level if ENABLE_DEEPFILTER: setup_deep_filter()（行 901-902）
- split_audio_to_wav（行 905-952）
- _df_process_wav（行 955-958）
- df_and_merge_wav（行 960-978）
- denoise_audio（行 982-999）
- denoise_audio_keep_format（行 1002-1037）
- denoise_audio_paths_parallel（行 1040-1063）
"""

from __future__ import annotations

import concurrent.futures
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydub import AudioSegment
from tqdm.auto import tqdm
from concurrent.futures import ThreadPoolExecutor

from . import config as cfg
from .runtime import log, sanitize_filename
from .audio import clear_folder


# ---------------------------------------------------------------------------
# 常量 — DeepFilter 二进制持久化路径
# ---------------------------------------------------------------------------
_OUTPUT_ROOT = str(getattr(cfg, "OUTPUT_ROOT", "/data/output") or "/data/output").strip()
_DEEPFILTER_DIR = os.path.join(_OUTPUT_ROOT, ".deepfilter")
_DEEPFILTER_BIN = "deep-filter-0.5.6-x86_64-unknown-linux-musl"
DEEP_FILTER_PATH = os.path.join(_DEEPFILTER_DIR, _DEEPFILTER_BIN)
DEEP_FILTER_DRIVE = os.path.join(_DEEPFILTER_DIR, _DEEPFILTER_BIN + ".bak")

# 宿主机持久目录（通过 docker-compose volume 挂载到此）
# 部署脚本下载一次，后续重建镜像不再重复下载
_BAKED_DEEPFILTER_DIR = "/opt/deepfilter"

DEEPFILTER_DOWNLOAD_URL = (
    "https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/"
    "deep-filter-0.5.6-x86_64-unknown-linux-musl"
)


# ---------------------------------------------------------------------------
# setup_deep_filter — 持久化初始化（幂等，优先级：卷 > 宿主机缓存 > 网络下载）
# ---------------------------------------------------------------------------
def setup_deep_filter():
    """确保 DeepFilter 二进制在持久卷上就绪。

    查找顺序：
      1. 持久卷 (/data/output/.deepfilter/) — 已存在则复用
      2. 宿主机缓存 (/opt/deepfilter/ = ./data/deepfilter/) — 部署脚本下载，拷贝到卷
      3. 网络下载 — 最后手段，下载到卷并创建备份
    """
    os.makedirs(_DEEPFILTER_DIR, exist_ok=True)

    # 1. 卷上主文件已存在 → 直接复用
    if os.path.exists(DEEP_FILTER_PATH) and os.path.getsize(DEEP_FILTER_PATH) > 0:
        if not os.access(DEEP_FILTER_PATH, os.X_OK):
            os.chmod(DEEP_FILTER_PATH, 0o755)
        return

    # 2. 卷上备份存在 → 恢复
    if os.path.exists(DEEP_FILTER_DRIVE) and os.path.getsize(DEEP_FILTER_DRIVE) > 0:
        shutil.copy(DEEP_FILTER_DRIVE, DEEP_FILTER_PATH)
        os.chmod(DEEP_FILTER_PATH, 0o755)
        return

    # 3. 宿主机缓存存在 → 拷贝到卷（首次容器启动）
    baked_bin = os.path.join(_BAKED_DEEPFILTER_DIR, _DEEPFILTER_BIN)
    baked_bak = baked_bin + ".bak"
    if os.path.exists(baked_bin) and os.path.getsize(baked_bin) > 0:
        shutil.copy(baked_bin, DEEP_FILTER_PATH)
        os.chmod(DEEP_FILTER_PATH, 0o755)
        shutil.copy(baked_bin, DEEP_FILTER_DRIVE)
        return
    if os.path.exists(baked_bak) and os.path.getsize(baked_bak) > 0:
        shutil.copy(baked_bak, DEEP_FILTER_PATH)
        os.chmod(DEEP_FILTER_PATH, 0o755)
        shutil.copy(baked_bak, DEEP_FILTER_DRIVE)
        return

    # 4. 最后手段 → 网络下载
    subprocess.run(
        ["wget", "--tries=5", "--timeout=30", "--retry-connrefused",
         DEEPFILTER_DOWNLOAD_URL, "-O", DEEP_FILTER_PATH],
        check=True,
    )
    os.chmod(DEEP_FILTER_PATH, 0o755)
    shutil.copy(DEEP_FILTER_PATH, DEEP_FILTER_DRIVE)


# ---------------------------------------------------------------------------
# 模块级：若启用就下载 DeepFilter 二进制（原文件行 901-902）
# ---------------------------------------------------------------------------
from .runtime import runtime_console_print

if bool(getattr(cfg, "ENABLE_DEEPFILTER", True)):
    try:
        setup_deep_filter()
        runtime_console_print("✅ DeepFilter 就绪", level="INFO")
    except Exception as _df_init_err:
        runtime_console_print(
            f"⚠️ DeepFilter 初始化失败，将在首次使用时重试: {_df_init_err}",
            level="WARNING",
        )


# ---------------------------------------------------------------------------
# 音频分片（原文件行 905-952）
# ---------------------------------------------------------------------------
def split_audio_to_wav(input_file, output_dir, seg_minutes=60, sr=16000):
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            input_file,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    total = float(r.stdout.strip())
    seg_sec = seg_minutes * 60
    n = math.ceil(total / seg_sec)
    os.makedirs(output_dir, exist_ok=True)
    for i in range(n):
        start = i * seg_sec
        dur = min(seg_sec, total - start)
        out = os.path.join(output_dir, f"segment_{i + 1:03d}.wav")
        subprocess.run(
            [
                "ffmpeg",
                "-ss",
                str(start),
                "-t",
                str(dur),
                "-i",
                input_file,
                "-vn",
                "-ar",
                str(sr),
                "-ac",
                "2",
                "-sample_fmt",
                "s16",
                "-acodec",
                "pcm_s16le",
                "-y",
                out,
            ],
            capture_output=True,
            check=True,
        )


def _df_process_wav(wav_file, output_dir):
    subprocess.run([DEEP_FILTER_PATH, wav_file, "--output-dir", output_dir], check=True)
    return os.path.join(output_dir, os.path.basename(wav_file))


def df_and_merge_wav(input_dir, output_dir, final_output, max_workers=1):
    os.makedirs(output_dir, exist_ok=True)
    wavs = sorted(
        [os.path.join(input_dir, f) for f in os.listdir(input_dir) if f.endswith(".wav")],
        key=os.path.getmtime,
    )
    renamed = []
    for idx, f in enumerate(wavs, 1):
        np_ = os.path.join(input_dir, f"{idx}.wav")
        os.rename(f, np_)
        renamed.append(np_)
    worker_count = max(1, min(int(max_workers or 1), len(renamed) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        processed = list(ex.map(lambda f: _df_process_wav(f, output_dir), renamed))
    processed.sort(key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    combined = AudioSegment.empty()
    for f in processed:
        combined += AudioSegment.from_wav(f)
    combined.export(final_output, format="wav")
    log.info("降噪合并完成: %s", final_output)


def denoise_audio(audio_path, segment_workers=1):
    source = Path(audio_path)
    job_dir = Path(tempfile.mkdtemp(prefix="deepfilter_job_"))
    split_dir = job_dir / "segments"
    df_dir = job_dir / "df"
    denoised = job_dir / f"denoised_{sanitize_filename(source.stem)}.wav"
    log.info("🔧 开始降噪: %s", source.name)

    try:
        clear_folder(str(split_dir))
        segment_duration_minutes = int(getattr(cfg, "segment_duration_minutes", 60))
        split_audio_to_wav(audio_path, str(split_dir), segment_duration_minutes)
        clear_folder(str(df_dir))
        df_and_merge_wav(str(split_dir), str(df_dir), str(denoised), max_workers=segment_workers)
        log.info("✅ 降噪完成: %s", source.name)
        return str(denoised), str(job_dir)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def denoise_audio_keep_format(audio_path: str, output_path: str = "", segment_workers=1) -> str:
    if not bool(getattr(cfg, "ENABLE_DEEPFILTER", True)):
        return audio_path

    # 确保二进制存在（import 时可能下载失败，这里重试）
    if not os.path.exists(DEEP_FILTER_PATH):
        try:
            setup_deep_filter()
        except Exception as e:
            raise RuntimeError(f"DeepFilter 二进制不可用且下载失败: {e}")

    source = Path(audio_path)
    suffix = source.suffix.lower() or ".wav"
    target = Path(output_path) if output_path else source.with_name(f"{source.stem}_denoised{suffix}")

    if bool(getattr(cfg, "SKIP_EXISTING", True)) and target.exists() and target.stat().st_size > 0:
        log.info("复用已降噪音频: %s", target.name)
        return str(target)

    temp_wav, job_dir = denoise_audio(audio_path, segment_workers=segment_workers)
    os.makedirs(target.parent, exist_ok=True)

    try:
        if target.suffix.lower() == ".wav":
            if target.exists():
                target.unlink()
            shutil.move(temp_wav, str(target))
        else:
            cmd = ["ffmpeg", "-y", "-i", temp_wav]
            if target.suffix.lower() == ".mp3":
                cmd += ["-codec:a", "libmp3lame", "-b:a", "192k"]
            elif target.suffix.lower() in {".m4a", ".aac"}:
                cmd += ["-codec:a", "aac", "-b:a", "192k"]
            elif target.suffix.lower() == ".flac":
                cmd += ["-codec:a", "flac"]
            elif target.suffix.lower() == ".ogg":
                cmd += ["-codec:a", "libvorbis", "-qscale:a", "5"]
            cmd.append(str(target))
            subprocess.run(cmd, capture_output=True, check=True)
        log.info("✅ 降噪音频已写回: %s", target.name)
        return str(target)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def denoise_audio_paths_parallel(audio_paths, output_paths=None, max_workers=2):
    if not audio_paths:
        return []

    total = len(audio_paths)
    worker_count = max(1, min(int(max_workers or 1), total))
    results = {}

    if output_paths is not None and len(output_paths) != total:
        raise ValueError("output_paths length must match audio_paths length")

    def _run(item):
        idx, path = item
        log.info("  DeepFilter %d/%d -> %s", idx + 1, total, os.path.basename(path))
        output_path = output_paths[idx] if output_paths is not None else ""
        return idx, denoise_audio_keep_format(path, output_path=output_path, segment_workers=1)

    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        futures = {ex.submit(_run, item): item[0] for item in enumerate(audio_paths)}
        for future in tqdm(
            concurrent.futures.as_completed(futures),
            total=total,
            desc="DeepFilter双线程降噪",
            unit="轨",
        ):
            idx, out_path = future.result()
            results[idx] = out_path

    return [results[i] for i in range(total)]