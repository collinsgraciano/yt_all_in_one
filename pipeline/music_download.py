"""运行核心：版权音乐下载（Hugging Face Datasets / Buckets）。

对应原 runtime_core.py 行 275-493：
- build_hf_download_headers / normalize_hf_dataset_download_url / safe_music_output_path
- download_music_from_dataset_urls / download_music_from_buckets
- extract_audio_files_from_zip
- sync_music_library_if_enabled
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
import time
import zipfile
from urllib.parse import urlparse, urlunparse

import requests

from . import config as cfg
from .runtime import log, parse_text_list_config



# ============================================================================
# HF 下载工具（原文件行 275-312）
# ============================================================================

def build_hf_download_headers():
    token = str(getattr(cfg, "HF_TOKEN", "") or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def normalize_hf_dataset_download_url(url):
    raw = str(url or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    path = parsed.path
    if "/blob/" in path:
        path = path.replace("/blob/", "/resolve/", 1)
    elif "/resolve/" not in path and parsed.netloc.endswith("huggingface.co"):
        path = path.rstrip("/") + "/resolve/main"

    query = parsed.query
    if "download=" not in query.lower():
        query = f"{query}&download=true" if query else "download=true"

    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, parsed.fragment))


def safe_music_output_path(target_dir, original_name):
    base_name = os.path.basename(original_name or "").strip()
    if not base_name:
        base_name = "music.mp3"

    stem, ext = os.path.splitext(base_name)
    candidate = os.path.join(target_dir, base_name)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(target_dir, f"{stem}_{counter}{ext}")
        counter += 1
    return candidate


# ============================================================================
# ZIP 解压（原文件行 379-397）
# ============================================================================

def extract_audio_files_from_zip(zip_path, output_dir, allowed_exts=None):
    if allowed_exts is None:
        allowed_exts = cfg.SUPPORTED_AUDIO_EXTENSIONS

    extracted_paths = []
    os.makedirs(output_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue

            ext = os.path.splitext(member.filename)[1].lower()
            if ext not in allowed_exts:
                continue

            output_path = safe_music_output_path(output_dir, member.filename)
            with archive.open(member, "r") as source, open(output_path, "wb") as target:
                shutil.copyfileobj(source, target)
            extracted_paths.append(output_path)

    return extracted_paths


# ============================================================================
# Datasets ZIP 下载（原文件行 400-439）
# ============================================================================

def download_music_from_dataset_urls():
    from .audio import download_file_with_wget, download_file_with_requests
    from .runtime import runtime_console_print

    url_candidates = parse_text_list_config(getattr(cfg, "HF_DATASET_ZIP_URLS", ""))
    if not url_candidates:
        runtime_console_print("⚠️ 未配置有效的 HF_DATASET_ZIP_URLS，跳过下载。", level="WARNING")
        return False

    selected_input_url = random.choice(url_candidates)
    selected_download_url = normalize_hf_dataset_download_url(selected_input_url)
    headers = build_hf_download_headers()

    local_music_dir = str(getattr(cfg, "LOCAL_MUSIC_DIR", "/content/music") or "/content/music").strip()
    os.makedirs(local_music_dir, exist_ok=True)
    cfg.set_config("MUSIC_DIR", local_music_dir)

    temp_dir = tempfile.mkdtemp(prefix="hf_music_zip_")
    archive_name = os.path.basename(urlparse(selected_download_url).path) or "music_bundle.zip"
    archive_path = os.path.join(temp_dir, archive_name)

    runtime_console_print(f"🎲 已随机选择 Datasets 音乐包: {selected_input_url}", level="INFO")
    runtime_console_print(f"⬇️ 准备下载 ZIP: {selected_download_url}", level="INFO")

    try:
        ok = download_file_with_wget(selected_download_url, archive_path, headers=headers)
        if not ok:
            runtime_console_print("⚠️ wget 下载未成功，切换到 requests 流式下载...", level="WARNING")
            ok = download_file_with_requests(selected_download_url, archive_path, headers=headers)

        if not ok:
            raise RuntimeError("ZIP 下载失败，已尝试 wget 与 requests 两种方式")

        extracted = extract_audio_files_from_zip(archive_path, local_music_dir)
        if not extracted:
            raise RuntimeError("ZIP 下载成功，但解压后未找到任何支持的音频文件")

        runtime_console_print(
            f"✅ Datasets ZIP 下载并解压完成，共导入 {len(extracted)} 个音频文件到 {local_music_dir}",
            level="INFO",
        )
        return True
    except Exception as e:
        runtime_console_print(f"❌ Datasets ZIP 下载失败: {e}", level="ERROR")
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ============================================================================
# Buckets 下载（原文件行 442-480）
# ============================================================================

def download_music_from_buckets():
    from huggingface_hub import list_bucket_tree, download_bucket_files, login
    from .runtime import runtime_console_print

    bucket_ids = getattr(cfg, "BUCKET_IDS", "")
    bucket_list = [b.strip() for b in str(bucket_ids or "").split(",") if b.strip()]
    if not bucket_list or bucket_list[0].startswith("username/my-bucket"):
        runtime_console_print("⚠️ 未配置有效的 BUCKET_IDS，跳过下载。", level="WARNING")
        return False

    selected_bucket = random.choice(bucket_list)
    runtime_console_print(f"🎲 已随机选择 Bucket: {selected_bucket}", level="INFO")

    hf_token = str(getattr(cfg, "HF_TOKEN", "") or "").strip()
    if hf_token:
        runtime_console_print("🔑 正在使用 Token 登录 Hugging Face...", level="INFO")
        login(token=hf_token)

    local_music_dir = str(getattr(cfg, "LOCAL_MUSIC_DIR", "/content/music") or "/content/music").strip()
    os.makedirs(local_music_dir, exist_ok=True)
    cfg.set_config("MUSIC_DIR", local_music_dir)

    try:
        runtime_console_print(f"🔍 正在检索 Bucket {selected_bucket} 中的音频文件...", level="INFO")
        music_files = [
            item
            for item in list_bucket_tree(selected_bucket, recursive=True)
            if item.type == "file" and item.path.lower().endswith(cfg.SUPPORTED_AUDIO_EXTENSIONS)
        ]

        if not music_files:
            runtime_console_print(f"⚠️ 在 Bucket '{selected_bucket}' 中未找到任何音频文件。", level="WARNING")
            return False

        runtime_console_print(
            f"⬇️ 发现 {len(music_files)} 首音乐，开始下载到 {local_music_dir}...",
            level="INFO",
        )
        download_bucket_files(
            selected_bucket,
            files=[(f, safe_music_output_path(local_music_dir, f.path)) for f in music_files],
        )
        runtime_console_print("✅ Hugging Face Buckets 版权音乐同步完成！", level="INFO")
        return True
    except Exception as e:
        runtime_console_print(f"❌ Buckets 下载失败，请检查 Bucket 名称、路径或 Token: {e}", level="ERROR")
        return False


# ============================================================================
# 顶层音乐同步入口（原文件行 483-493）
# ============================================================================

def _count_audio_files_in_dir(directory):
    """统计目录中支持的音频文件数量。"""
    if not os.path.isdir(directory):
        return 0
    exts = cfg.SUPPORTED_AUDIO_EXTENSIONS
    count = 0
    for f in os.listdir(directory):
        if f.lower().endswith(exts):
            count += 1
    return count


def sync_music_library_if_enabled():
    from .runtime import runtime_console_print

    download_from_buckets = bool(getattr(cfg, "DOWNLOAD_FROM_BUCKETS", True))
    if not download_from_buckets:
        runtime_console_print("⏭️ 已关闭版权音乐自动同步。", level="INFO")
        return False

    local_music_dir = str(getattr(cfg, "LOCAL_MUSIC_DIR", "/data/music") or "/data/music").strip()
    existing_count = _count_audio_files_in_dir(local_music_dir)

    # 如果本地已有音乐文件且未强制重新处理，跳过下载（永久化）
    skip_existing = bool(getattr(cfg, "SKIP_EXISTING", True))
    if skip_existing and existing_count > 0:
        runtime_console_print(
            f"🎵 本地音乐库已有 {existing_count} 个音频文件，跳过下载（SKIP_EXISTING=true）。",
            level="INFO",
        )
        cfg.set_config("MUSIC_DIR", local_music_dir)
        return True

    selected_method = str(getattr(cfg, "HF_MUSIC_DOWNLOAD_METHOD", "datasets_zip_urls") or "datasets_zip_urls").strip().lower()
    if selected_method == "buckets":
        return download_music_from_buckets()
    return download_music_from_dataset_urls()