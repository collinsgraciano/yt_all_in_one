"""Telegram 音频缓存模块（多 Bot Token 匹配）。

从旧项目「下载掌阅有声书到tg」整合而来。
旧项目已将章节音频下载→DeepFilter 降噪→上传到 Telegram，
并将 telegram_file_id / telegram_bot_id / telegram_bot_user_id 记录在 audiobook_chapters 表中。

本模块在 pipeline 处理章节时：
  1. 查询 audiobook_chapters 表，判断该章节是否已有 TG 缓存
  2. 若有，根据 telegram_bot_user_id 匹配正确的 Bot Token
  3. 用匹配的 Token 从 Telegram 下载已降噪的音频
  4. 跳过原始 URL 下载和 DeepFilter 降噪

匹配策略（book_id + audio_url），Bot Token 匹配策略（三层双保险）：
  1. 优先 telegram_bot_user_id 匹配（最可靠，不受 Token 顺序/增删影响）
  2. 回退 telegram_bot_id 索引匹配（快速路径）
  3. 保底全量尝试所有 Token

⚠️ 重要: Telegram 的 file_id 与 Bot 绑定。Bot A 上传的文件只能用 Bot A 的 Token 下载。
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

def _get_tg_bot_tokens() -> list[str]:
    """从全局配置读取 Telegram Bot Token 列表。

    TG_BOT_TOKEN 支持逗号分隔的多 Token（多 Bot 轮换下载）。
    每个文件用上传时记录的 telegram_bot_user_id 匹配对应的 Token 下载。
    """
    raw = str(getattr(cfg, "TG_BOT_TOKEN", "") or "").strip()
    if not raw:
        return []
    # 逗号分隔，去除空白和空项
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    return tokens


def _get_tg_bot_token() -> str:
    """获取第一个 Bot Token（向后兼容，单 Token 场景）。"""
    tokens = _get_tg_bot_tokens()
    return tokens[0] if tokens else ""


def _is_tg_cache_enabled() -> bool:
    """检查 TG 音频缓存功能是否启用。"""
    if not bool(getattr(cfg, "ENABLE_TG_AUDIO_CACHE", True)):
        return False
    if not _get_tg_bot_tokens():
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
# Bot Token 工具：从 Token 提取永久 User ID
# ============================================================================

def extract_bot_user_id(token: str) -> int | None:
    """从 Bot Token 中提取 Bot 的永久 Telegram User ID。

    Token 格式: {bot_user_id}:{secret}  例如: 7485554965:AAHxxx...
    这个 user_id 是 Bot 的永久 ID，即使 BOT_TOKENS 列表重新排序、
    增删 Token，也能通过 user_id 找到正确的 Token。
    """
    try:
        return int(token.split(":")[0])
    except (ValueError, IndexError):
        return None


def _build_user_id_to_token_index_map(bot_tokens: list[str]) -> dict[int, int]:
    """构建 {bot_user_id: token_index} 映射表。

    用于根据 DB 中记录的 telegram_bot_user_id 快速找到对应的 Token 索引。
    """
    mapping: dict[int, int] = {}
    for i, token in enumerate(bot_tokens):
        uid = extract_bot_user_id(token)
        if uid is not None:
            mapping[uid] = i
    return mapping


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
# 数据库查询：获取 TG 缓存信息（含 Bot ID 匹配字段）
# ============================================================================

def fetch_tg_cache_map(book_id: str, audio_urls: list[str]) -> dict[str, dict]:
    """查询 audiobook_chapters 表，返回 {audio_url: cache_info} 映射。

    cache_info 结构:
        {
            "file_id": str,           # Telegram file_id
            "bot_id": int | None,     # 上传 Bot 的数组索引（可能因顺序变化失效）
            "bot_user_id": int | None # 上传 Bot 的永久 Telegram User ID（可靠）
        }

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
        result: dict[str, dict] = {}
        batch_size = 500
        for i in range(0, len(valid_urls), batch_size):
            batch = valid_urls[i : i + batch_size]
            placeholders = sql.SQL(", ").join(sql.Placeholder() * len(batch))
            rows = execute_postgres_fetchall(
                sql.SQL(
                    """
                    SELECT audio_url, telegram_file_id, telegram_bot_id, telegram_bot_user_id
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
                    result[url] = {
                        "file_id": file_id,
                        "bot_id": row.get("telegram_bot_id"),
                        "bot_user_id": row.get("telegram_bot_user_id"),
                    }

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


def fetch_tg_cache_for_chapter(book_id: str, audio_url: str) -> dict | None:
    """查询单个章节的 TG 缓存信息。

    返回 cache_info dict 或 None:
        {
            "file_id": str,
            "bot_id": int | None,
            "bot_user_id": int | None
        }
    """
    if not _is_tg_cache_enabled():
        return None
    if not audio_url or not str(audio_url).strip():
        return None

    table_sql = get_public_table_identifier("audiobook_chapters")
    try:
        row = execute_postgres_fetchone(
            sql.SQL(
                """
                SELECT telegram_file_id, telegram_bot_id, telegram_bot_user_id
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
            return {
                "file_id": str(row["telegram_file_id"]),
                "bot_id": row.get("telegram_bot_id"),
                "bot_user_id": row.get("telegram_bot_user_id"),
            }
        return None
    except Exception as e:
        log.warning("[TG缓存] 查询单章节缓存失败: %s", e)
        return None


# ============================================================================
# Bot Token 匹配：根据 DB 记录找到正确的 Token
# ============================================================================

def _find_correct_bot_token(
    file_id: str,
    bot_tokens: list[str],
    known_bot_id: int | None = None,
    known_bot_user_id: int | None = None,
) -> tuple[str | None, int | None]:
    """根据 DB 记录的 bot_id / bot_user_id 匹配正确的 Bot Token。

    匹配策略（按可靠性排序）:
    1. 如果有 known_bot_user_id，通过 user_id 匹配 Token（最可靠，不受顺序影响）
    2. 如果有 known_bot_id 且在范围内，返回对应 Token（快速路径，不验证）
    3. 返回 None（调用方可选择全量尝试）

    返回: (bot_token, token_index) 或 (None, None)
    """
    if not bot_tokens:
        return None, None

    # 策略1: 通过 bot_user_id 匹配（最可靠）
    if known_bot_user_id is not None:
        uid_map = _build_user_id_to_token_index_map(bot_tokens)
        matched_idx = uid_map.get(known_bot_user_id)
        if matched_idx is not None:
            return bot_tokens[matched_idx], matched_idx
        else:
            log.warning(
                "[TG下载] DB记录 bot_user_id=%s 但当前 TG_BOT_TOKEN 中无此 Bot（可能已删除该Token）",
                known_bot_user_id,
            )

    # 策略2: 通过 bot_id 索引匹配（快速路径）
    if known_bot_id is not None and 0 <= known_bot_id < len(bot_tokens):
        return bot_tokens[known_bot_id], known_bot_id
    elif known_bot_id is not None and known_bot_id >= len(bot_tokens):
        log.warning(
            "[TG下载] DB记录 bot_id=%s 超出范围（当前只有 %d 个Token）",
            known_bot_id,
            len(bot_tokens),
        )

    # 策略3: 无匹配信息，返回第一个 Token 作为默认
    # （调用方可在 getFile 失败时全量尝试）
    return bot_tokens[0], 0


def _try_all_tokens_get_file_path(
    file_id: str,
    bot_tokens: list[str],
    skip_indices: set[int] | None = None,
    max_retries: int = 3,
) -> tuple[str | None, str | None, int | None]:
    """全量尝试所有 Token 调用 getFile（保底策略）。

    返回: (file_path, bot_token, token_index) 或 (None, None, None)
    """
    skip = skip_indices or set()
    for i, token in enumerate(bot_tokens):
        if i in skip:
            continue
        file_path = _tg_get_file_path(file_id, token, max_retries=max_retries, suppress_invalid=True)
        if file_path:
            return file_path, token, i
    return None, None, None


# ============================================================================
# Telegram 文件下载
# ============================================================================

def _tg_get_file_path(
    file_id: str,
    bot_token: str,
    max_retries: int = 5,
    suppress_invalid: bool = False,
) -> str | None:
    """调用 Telegram Bot API getFile，返回 file_path（用于下载）。

    参数:
        file_id: Telegram 文件 ID
        bot_token: 使用的 Bot Token
        max_retries: 最大重试次数
        suppress_invalid: 为 True 时，400 错误不抛异常而是返回 None
                         （用于全量尝试场景，避免一个 Token 失败就中断）

    返回值：
      - str: 成功获取 file_path
      - None: 网络/DNS 错误重试耗尽，或 400 错误（suppress_invalid=True 时）

    异常：
      - TgFileIdInvalidError: 400 Bad Request（file_id 无效/不属于当前 bot），
                              当 suppress_invalid=False 时抛出
    """
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

                # 4xx 错误（非 429）：file_id 无效或文件不可访问
                if 400 <= resp.status_code < 500:
                    if suppress_invalid:
                        # 全量尝试模式：静默返回 None，让调用方尝试下一个 Token
                        return None
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


def download_audio_from_telegram(
    file_id: str,
    save_path: str,
    max_retries: int = 3,
    bot_id: int | None = None,
    bot_user_id: int | None = None,
) -> dict:
    """从 Telegram 下载音频文件到本地（支持多 Bot Token 匹配）。

    根据 DB 中记录的 bot_id / bot_user_id 匹配正确的 Bot Token 下载文件。
    Telegram 的 file_id 与上传它的 Bot 绑定，必须用同一个 Bot 的 Token 才能下载。

    参数:
        file_id: Telegram 文件 ID
        save_path: 本地保存路径
        max_retries: 最大重试次数
        bot_id: 上传此文件的 Bot 在 BOT_TOKENS 数组中的索引（可能因顺序变化失效）
        bot_user_id: 上传此文件的 Bot 的永久 Telegram User ID（可靠匹配依据）

    当 TG_SERIAL_DOWNLOAD=True 时，使用全局锁确保同时只有一个线程
    在调用 Telegram API。下载完成后会等待 TG_DOWNLOAD_INTERVAL_SECONDS 秒
    再释放锁，避免下个请求过快。

    支持停止检查：在等待锁期间会定期检查 pipeline 停止标志，
    如果用户点击了停止，会立即放弃下载。

    返回: {"ok": bool, "error": str, "file_size": int}
    """
    bot_tokens = _get_tg_bot_tokens()
    if not bot_tokens:
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
        result = _do_download_from_telegram(
            file_id, save_path, tmp_path, bot_tokens, max_retries,
            bot_id=bot_id, bot_user_id=bot_user_id,
        )
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
    file_id: str,
    save_path: str,
    tmp_path: str,
    bot_tokens: list[str],
    max_retries: int,
    bot_id: int | None = None,
    bot_user_id: int | None = None,
) -> dict:
    """实际执行 TG 下载逻辑（已持有锁）。

    多 Bot Token 匹配策略：
    1. 优先用 bot_user_id 匹配的 Token 调用 getFile
    2. 若失败，用 bot_id 索引对应的 Token 尝试
    3. 若仍失败，全量尝试所有 Token（保底）

    对 400 Bad Request (file_id 无效) 在全量尝试所有 Token 后才判定为不可恢复。
    仅对网络/DNS 错误重试。
    """

    # 第一步：根据 DB 记录匹配 Bot Token
    matched_token, matched_idx = _find_correct_bot_token(
        file_id, bot_tokens, known_bot_id=bot_id, known_bot_user_id=bot_user_id
    )

    if matched_token:
        uid_str = f"uid:{bot_user_id}" if bot_user_id else f"idx:{bot_id}"
        log.info("[TG下载] 匹配 Bot Token %s (%s)", matched_idx, uid_str)

    # 尝试匹配的 Token 下载
    tried_indices: set[int] = set()
    if matched_idx is not None:
        tried_indices.add(matched_idx)
        result = _download_with_single_token(
            file_id, save_path, tmp_path, matched_token, matched_idx, max_retries
        )
        if result.get("ok"):
            return result
        # 如果是 400 不可恢复错误，记录后继续尝试其他 Token
        if result.get("_unrecoverable"):
            log.warning(
                "[TG下载] 匹配的 Bot Token %d getFile 失败(400)，尝试其他 Token...",
                matched_idx,
            )
        else:
            # 网络错误，不继续尝试其他 Token（网络问题换 Token 也没用）
            return result

    # 全量尝试剩余 Token（保底策略）
    remaining = [i for i in range(len(bot_tokens)) if i not in tried_indices]
    if remaining:
        log.info("[TG下载] 全量尝试剩余 %d 个 Bot Token...", len(remaining))

    for attempt in range(1, max_retries + 1):
        # 检查停止标志
        if _check_stop_requested():
            return {"ok": False, "error": "用户手动停止", "file_size": 0}

        # 全量尝试 getFile（跳过已尝试的 Token）
        file_path, found_token, found_idx = _try_all_tokens_get_file_path(
            file_id, bot_tokens, skip_indices=tried_indices, max_retries=2
        )

        if not file_path:
            # 所有 Token 都无法获取 file_path
            if attempt < max_retries:
                base_delay = 5 * attempt
                jitter = random.uniform(0, base_delay * 0.5)
                log.warning(
                    "[TG下载] 全量尝试 getFile 失败 (尝试 %d/%d) | 文件: %s",
                    attempt, max_retries, os.path.basename(save_path),
                )
                time.sleep(base_delay + jitter)
                continue
            return {
                "ok": False,
                "error": f"所有 Bot Token 均无法获取此 file_id (file_id={file_id[:30]}...)",
                "file_size": 0,
            }

        # getFile 成功，用找到的 Token 下载文件
        log.info("[TG下载] getFile 成功，使用 Bot Token %d 下载", found_idx)
        return _download_file_content(
            file_path, save_path, tmp_path, found_token, max_retries
        )

    return {"ok": False, "error": f"超出最大重试次数 ({max_retries})", "file_size": 0}


def _download_with_single_token(
    file_id: str,
    save_path: str,
    tmp_path: str,
    bot_token: str,
    token_idx: int,
    max_retries: int,
) -> dict:
    """用指定的单个 Bot Token 下载文件。

    返回 result dict，含特殊字段 _unrecoverable 标记 400 不可恢复错误。
    """
    for attempt in range(1, max_retries + 1):
        if _check_stop_requested():
            return {"ok": False, "error": "用户手动停止", "file_size": 0}

        try:
            # 步骤 1: 获取 file_path
            file_path = _tg_get_file_path(file_id, bot_token, max_retries=5)

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
            return _download_file_content(
                file_path, save_path, tmp_path, bot_token, max_retries
            )

        except TgFileIdInvalidError as e:
            # 400 Bad Request: file_id 无效，标记为不可恢复
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            log.error(
                "[TG下载] ❌ Bot Token %d getFile 返回 400: %s | file_id=%s...",
                token_idx, e, file_id[:40],
            )
            return {
                "ok": False,
                "error": f"file_id 无效: {e}",
                "file_size": 0,
                "_unrecoverable": True,
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


def _download_file_content(
    file_path: str,
    save_path: str,
    tmp_path: str,
    bot_token: str,
    max_retries: int,
) -> dict:
    """根据 getFile 返回的 file_path 下载文件内容。

    对 400 Bad Request (file_id 无效) 立即返回，不重试。
    仅对网络/DNS 错误重试。
    """
    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

    for attempt in range(1, max_retries + 1):
        if _check_stop_requested():
            return {"ok": False, "error": "用户手动停止", "file_size": 0}

        try:
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
                file_path[:40],
            )
            return {
                "ok": False,
                "error": f"file_id 无效: {e}",
                "file_size": 0,
                "_unrecoverable": True,
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
