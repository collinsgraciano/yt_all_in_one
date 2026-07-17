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
import random
import threading
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
# 自定义异常
# ============================================================================

class TgFileIdInvalidError(Exception):
    """file_id 无效（TG API 返回 400 Bad Request），不可恢复。

    常见原因：file_id 属于另一个 bot，或文件已被删除。
    Telegram 的 file_id 是 bot 专属的，不同 bot 之间不通用。
    """
    pass


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


def _is_serial_download() -> bool:
    """是否启用串行下载模式（一次只下载一个 TG 文件）。"""
    return bool(getattr(cfg, "TG_SERIAL_DOWNLOAD", True))


def _get_download_interval() -> float:
    """获取每次 TG 下载完成后的等待间隔（秒）。"""
    try:
        return max(0.0, float(getattr(cfg, "TG_DOWNLOAD_INTERVAL_SECONDS", 5) or 0))
    except (ValueError, TypeError):
        return 5.0


# ============================================================================
# 停止标志检查
# ============================================================================

def _check_stop_requested() -> bool:
    """检查 pipeline 是否请求了停止任务。

    延迟导入避免循环依赖。
    """
    try:
        from .pipeline import _check_db_stop_flag
        if _check_db_stop_flag:
            return bool(_check_db_stop_flag())
    except Exception:
        pass
    return False


# ============================================================================
# 串行控制：确保同时只有一个线程在调用 Telegram API
# ============================================================================

# 全局锁：当 TG_SERIAL_DOWNLOAD=True 时，确保一次只下载一个 TG 文件
_TG_DOWNLOAD_LOCK = threading.Lock()


# ============================================================================
# DNS / 网络错误检测
# ============================================================================

def _is_dns_error(exc: Exception) -> bool:
    """判断异常是否为 DNS 解析失败或网络连接问题。"""
    exc_str = str(exc).lower()
    dns_keywords = [
        "name resolution",
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname provided",
        "getaddrinfo failed",
        "failed to resolve",
    ]
    conn_keywords = [
        "connection refused",
        "connection reset",
        "connection aborted",
        "connection timed out",
        "max retries exceeded",
        "read timed out",
    ]
    return any(kw in exc_str for kw in dns_keywords + conn_keywords)


def _is_dns_only_error(exc: Exception) -> bool:
    """仅判断 DNS 解析失败（非一般性网络错误）。"""
    exc_str = str(exc).lower()
    dns_keywords = [
        "name resolution",
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname provided",
        "getaddrinfo failed",
        "failed to resolve",
    ]
    return any(kw in exc_str for kw in dns_keywords)


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

    valid_urls = [u for u in audio_urls if u and str(u).strip()]
    if not valid_urls:
        return {}

    table_sql = get_public_table_identifier("audiobook_chapters")

    try:
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

def _tg_get_file_path(file_id: str, max_retries: int = 5) -> str | None:
    """调用 Telegram Bot API getFile，返回 file_path（用于下载）。

    返回值：
      - str: 成功获取 file_path
      - None: 网络/DNS 错误重试耗尽

    异常：
      - TgFileIdInvalidError: 400 Bad Request（file_id 无效/不属于当前 bot），不可恢复
    """
    bot_token = _get_tg_bot_token()
    api_url = f"https://api.telegram.org/bot{bot_token}/getFile"

    dns_failure_count = 0

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

                if resp.status_code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 5)
                    log.warning("[TG下载] 触发 TG 限流 (429)，等待 %d 秒后重试", retry_after)
                    time.sleep(retry_after)
                    continue

                # 4xx 错误（非 429）：file_id 无效或文件不可访问，不可恢复
                # 抛出异常，让上层立即终止不再重试
                if 400 <= resp.status_code < 500:
                    raise TgFileIdInvalidError(
                        f"HTTP {resp.status_code}: {error_desc}"
                    )

                # 5xx 错误：服务器临时故障，可重试
                log.warning(
                    "[TG下载] getFile 失败 (尝试 %d/%d): HTTP %d %s",
                    attempt, max_retries, resp.status_code, error_desc,
                )

        except TgFileIdInvalidError:
            raise  # 直接向上传播，不重试

        except requests.exceptions.ConnectionError as e:
            if _is_dns_only_error(e):
                dns_failure_count += 1
                base_delay = min(10 * (2 ** (dns_failure_count - 1)), 60)
                jitter = random.uniform(0, base_delay * 0.3)
                sleep_time = base_delay + jitter
                log.warning(
                    "[TG下载] getFile DNS 解析失败 (尝试 %d/%d, DNS连续失败 %d 次)，"
                    "等待 %.1f 秒后重试",
                    attempt, max_retries, dns_failure_count, sleep_time,
                )
                if attempt < max_retries:
                    time.sleep(sleep_time)
                continue
            else:
                log.warning("[TG下载] getFile 连接异常 (尝试 %d/%d): %s", attempt, max_retries, e)
        except Exception as e:
            log.warning("[TG下载] getFile 异常 (尝试 %d/%d): %s", attempt, max_retries, e)

        # 通用退避：指数 + 抖动
        if attempt < max_retries:
            base_delay = 3 * attempt
            jitter = random.uniform(0, base_delay * 0.5)
            time.sleep(base_delay + jitter)

    return None


def download_audio_from_telegram(file_id: str, save_path: str, max_retries: int = 3) -> dict:
    """从 Telegram 下载音频文件到本地。

    当 TG_SERIAL_DOWNLOAD=True 时，使用全局锁确保同时只有一个线程
    在调用 Telegram API。下载完成后会等待 TG_DOWNLOAD_INTERVAL_SECONDS 秒
    再释放锁，避免下个请求过快。

    支持停止检查：在等待锁期间会定期检查 pipeline 停止标志，
    如果用户点击了停止，会立即放弃下载。

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

    # 串行模式：获取全局锁，确保同时只有一个 TG 下载
    use_lock = _is_serial_download()
    if use_lock:
        # 用 timeout 轮询获取锁，以便在用户停止时能及时退出
        while not _TG_DOWNLOAD_LOCK.acquire(timeout=2):
            if _check_stop_requested():
                log.info("[TG下载] 检测到停止请求，放弃等待下载锁: %s", os.path.basename(save_path))
                return {"ok": False, "error": "用户手动停止", "file_size": 0}

    try:
        result = _do_download_from_telegram(file_id, save_path, tmp_path, bot_token, max_retries)
    finally:
        if use_lock:
            # 仅在下载成功/网络错误时等待间隔；400 或停止不等待
            if result.get("ok"):
                interval = _get_download_interval()
                if interval > 0:
                    log.info("[TG下载] 等待 %.0f 秒后继续下一个章节...", interval)
                    time.sleep(interval)
            _TG_DOWNLOAD_LOCK.release()

    return result


def _do_download_from_telegram(
    file_id: str, save_path: str, tmp_path: str, bot_token: str, max_retries: int
) -> dict:
    """实际执行 TG 下载逻辑（已持有锁）。

    对 400 Bad Request (file_id 无效) 立即返回，不重试。
    仅对网络/DNS 错误重试。
    """

    for attempt in range(1, max_retries + 1):
        # 检查停止标志
        if _check_stop_requested():
            return {"ok": False, "error": "用户手动停止", "file_size": 0}

        try:
            # 步骤 1: 获取 file_path
            # TgFileIdInvalidError 会被外层 try 捕获，立即返回不重试
            file_path = _tg_get_file_path(file_id, max_retries=5)

            if not file_path:
                # getFile 返回 None = 网络/DNS 重试耗尽
                if attempt < max_retries:
                    base_delay = 5 * attempt
                    jitter = random.uniform(0, base_delay * 0.5)
                    log.warning(
                        "[TG下载] getFile 网络重试耗尽，整体重试 %d/%d | 文件: %s",
                        attempt, max_retries, os.path.basename(save_path),
                    )
                    time.sleep(base_delay + jitter)
                    continue
                return {
                    "ok": False,
                    "error": f"getFile 网络重试耗尽 (file_id={file_id[:30]}...)",
                    "file_size": 0,
                }

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

        except TgFileIdInvalidError as e:
            # 400 Bad Request: file_id 无效，不可恢复，立即返回
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            log.error(
                "[TG下载] ❌ file_id 无效，跳过（不可恢复）: %s | 文件: %s | file_id=%s...",
                e,
                os.path.basename(save_path),
                file_id[:40],
            )
            return {
                "ok": False,
                "error": f"file_id 无效: {e}",
                "file_size": 0,
            }

        except requests.exceptions.ConnectionError as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if _is_dns_error(e):
                base_delay = 10 * attempt
                jitter = random.uniform(0, base_delay * 0.5)
                log.warning(
                    "[TG下载] DNS/网络异常 (尝试 %d/%d): %s | 文件: %s | 等待 %.1f 秒",
                    attempt, max_retries, e, os.path.basename(save_path), base_delay + jitter,
                )
            else:
                base_delay = 5 * attempt
                jitter = random.uniform(0, base_delay * 0.5)
                log.warning(
                    "[TG下载] 连接异常 (尝试 %d/%d): %s | 文件: %s",
                    attempt, max_retries, e, os.path.basename(save_path),
                )
            if attempt < max_retries:
                time.sleep(base_delay + jitter)

        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            log.warning(
                "[TG下载] 失败 (尝试 %d/%d): %s | 文件: %s",
                attempt, max_retries, e, os.path.basename(save_path),
            )
            if attempt < max_retries:
                base_delay = 5 * attempt
                jitter = random.uniform(0, base_delay * 0.5)
                time.sleep(base_delay + jitter)

    return {"ok": False, "error": f"超出最大重试次数 ({max_retries})", "file_size": 0}
