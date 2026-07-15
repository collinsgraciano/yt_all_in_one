"""Telegram 音频缓存模块。

从旧项目「下载掌阅有声书到tg」整合而来。
旧项目已将章节音频下载→DeepFilter 降噪→上传到 Telegram，
并将 telegram_file_id 记录在 audiobook_chapters 表中。

本模块在 pipeline 处理章节时：
  1. 查询 audiobook_chapters 表，判断该章节是否已有 TG 缓存
  2. 若有，直接从 Telegram 下载已降噪的音频
  3. 跳过原始 URL 下载和 DeepFilter 降噪

匹配策略：book_id + audio_url（即原始 mp3Url），与旧项目数据一致。
"""

from __future__ import annotations

import os
import time
import requests

from psycopg import sql

from . import config as cfg
from .runtime import log
from .db import (
    get_public_table_identifier,
    execute_postgres_fetchall,
    execute_postgres_fetchone,
)


# ============================================================================
# 配置读取
# ============================================================================

def _get_tg_bot_token() -> str:
    """从全局配置读取 Telegram Bot Token。"""
    return str(getattr(cfg, "TG_BOT_TOKEN", "") or "").strip()


def _is_tg_cache_enabled() -> bool:
    """检查 TG 音频缓存功能是否启用。"""
    if not bool(getattr(cfg, "ENABLE_TG_AUDIO_CACHE", True)):
        return False
    if not _get_tg_bot_token():
        return False
    return True


# ============================================================================
# 数据库查询：获取 TG 缓存信息
# ============================================================================

def fetch_tg_cache_map(book_id: str, audio_urls: list[str]) -> dict[str, str]:
    """查询 audiobook_chapters 表，返回 {audio_url: telegram_file_id} 映射。

    只返回 upload_status='uploaded' 且 telegram_file_id 非空的记录。
    匹配条件：book_id + audio_url。
    """
    if not _is_tg_cache_enabled():
        return {}

    # 过滤掉空 URL
    valid_urls = [u for u in audio_urls if u and str(u).strip()]
    if not valid_urls:
        return {}

    table_sql = get_public_table_identifier("audiobook_chapters")

    try:
        # 由于 IN 子句参数数量可能很多，分批查询（每批 500 个）
        result: dict[str, str] = {}
        batch_size = 500
        for i in range(0, len(valid_urls), batch_size):
            batch = valid_urls[i : i + batch_size]
            placeholders = sql.SQL(", ").join(sql.Placeholder() * len(batch))
            rows = execute_postgres_fetchall(
                sql.SQL(
                    """
                    SELECT audio_url, telegram_file_id
                    FROM {}
                    WHERE book_id = %s
                      AND audio_url = ANY(%s)
                      AND telegram_file_id IS NOT NULL
                      AND telegram_file_id != ''
                      AND upload_status = %s
                    """
                ).format(table_sql),
                (str(book_id), batch, "uploaded"),
            )
            for row in rows:
                url = row.get("audio_url") or ""
                file_id = row.get("telegram_file_id") or ""
                if url and file_id:
                    result[url] = file_id

        if result:
            log.info(
                "[TG缓存] 书 %s 共 %d 个章节，找到 %d 个 TG 缓存",
                book_id,
                len(valid_urls),
                len(result),
            )
        return result
    except Exception as e:
        log.warning("[TG缓存] 查询 audiobook_chapters 失败（可能表未创建）: %s", e)
        return {}


def fetch_tg_cache_for_chapter(book_id: str, audio_url: str) -> str | None:
    """查询单个章节的 TG 缓存 file_id。"""
    if not _is_tg_cache_enabled():
        return None
    if not audio_url or not str(audio_url).strip():
        return None

    table_sql = get_public_table_identifier("audiobook_chapters")
    try:
        row = execute_postgres_fetchone(
            sql.SQL(
                """
                SELECT telegram_file_id
                FROM {}
                WHERE book_id = %s
                  AND audio_url = %s
                  AND telegram_file_id IS NOT NULL
                  AND telegram_file_id != ''
                  AND upload_status = %s
                LIMIT 1
                """
            ).format(table_sql),
            (str(book_id), str(audio_url), "uploaded"),
        )
        if row and row.get("telegram_file_id"):
            return str(row["telegram_file_id"])
        return None
    except Exception as e:
        log.warning("[TG缓存] 查询单章节缓存失败: %s", e)
        return None


# ============================================================================
# Telegram 文件下载
# ============================================================================

def _tg_get_file_path(file_id: str, max_retries: int = 3) -> str | None:
    """调用 Telegram Bot API getFile，返回 file_path（用于下载）。"""
    bot_token = _get_tg_bot_token()
    api_url = f"https://api.telegram.org/bot{bot_token}/getFile"

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(api_url, params={"file_id": file_id}, timeout=30)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                file_path = data["result"].get("file_path", "")
                if file_path:
                    return file_path
                log.warning("[TG下载] getFile 返回但无 file_path: %s", data)
            else:
                error_desc = data.get("description", "未知错误")
                log.warning(
                    "[TG下载] getFile 失败 (尝试 %d/%d): %s",
                    attempt,
                    max_retries,
                    error_desc,
                )
                if resp.status_code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 5)
                    time.sleep(retry_after)
                    continue
        except Exception as e:
            log.warning("[TG下载] getFile 异常 (尝试 %d/%d): %s", attempt, max_retries, e)

        if attempt < max_retries:
            time.sleep(2 * attempt)

    return None


def download_audio_from_telegram(file_id: str, save_path: str, max_retries: int = 3) -> dict:
    """从 Telegram 下载音频文件到本地。

    流程：
      1. 调用 getFile API 获取 file_path
      2. 从 https://api.telegram.org/file/bot{TOKEN}/{file_path} 下载文件

    返回: {"ok": bool, "error": str, "file_size": int}
    """
    bot_token = _get_tg_bot_token()
    if not bot_token:
        return {"ok": False, "error": "TG_BOT_TOKEN 未配置", "file_size": 0}

    # 如果文件已存在且非空，跳过下载
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        return {"ok": True, "error": "", "file_size": os.path.getsize(save_path)}

    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    tmp_path = save_path + ".tmp"

    for attempt in range(1, max_retries + 1):
        try:
            # 步骤 1: 获取 file_path
            file_path = _tg_get_file_path(file_id, max_retries=3)
            if not file_path:
                if attempt < max_retries:
                    time.sleep(3 * attempt)
                    continue
                return {"ok": False, "error": f"getFile 失败 (file_id={file_id[:30]}...)", "file_size": 0}

            # 步骤 2: 下载文件
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            with requests.get(download_url, stream=True, timeout=120) as resp:
                if resp.status_code != 200:
                    raise Exception(f"下载 HTTP {resp.status_code}")

                downloaded = 0
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                if downloaded == 0:
                    raise Exception("下载文件为空")

            # 校验文件
            actual_size = os.path.getsize(tmp_path)
            if actual_size == 0:
                raise Exception("文件大小为 0")

            # 重命名到最终路径
            import shutil
            shutil.move(tmp_path, save_path)

            log.info("[TG下载] 成功: %s (%dKB)", os.path.basename(save_path), actual_size // 1024)
            return {"ok": True, "error": "", "file_size": actual_size}

        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            log.warning(
                "[TG下载] 失败 (尝试 %d/%d): %s | 文件: %s",
                attempt,
                max_retries,
                e,
                os.path.basename(save_path),
            )
            if attempt < max_retries:
                time.sleep(3 * attempt)

    return {"ok": False, "error": f"超出最大重试次数 ({max_retries})", "file_size": 0}
