#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HF Space Worker — 多Bot轮换模式

流程:
  认领 → 下载 → 降噪 → 上传Telegram(多Bot轮换) → 更新DB

不需要 Upload Service, 多个 Bot Token 轮换上传, 分散限流压力。
每个 Bot 独立追踪 429 状态, 某个 Bot 被限流时自动切换到下一个。

环境变量:
  POSTGRES_DSN        - PostgreSQL 连接串
  BOT_TOKENS          - 逗号分隔的多个 Bot Token (如 token1,token2,token3)
                        (向后兼容: 也支持单个 BOT_TOKEN)
                        ⚠️ 如果为空, 会从 VPS_SCHEDULER_URL 获取
  CHAT_ID             - Telegram Chat ID
                        ⚠️ 如果为空, 会从 VPS_SCHEDULER_URL 获取
  TELEGRAM_API_BASE   - Telegram API 基地址 (HF Space 需中继)
                        默认: https://api.telegram.org
                        中继: https://your-worker.workers.dev/tg-api
                        ⚠️ 如果为空, 会从 VPS_SCHEDULER_URL 获取
  VPS_SCHEDULER_URL   - VPS 调度器地址 (如 http://1.2.3.4:8080)
                        当 BOT_TOKENS/CHAT_ID/TELEGRAM_API_BASE 为空时,
                        Worker 会从此地址的 /api/tg-config 接口获取配置
  BOT_MIN_INTERVAL    - 单个Bot两次上传最小间隔 (默认 3秒)
  MAX_RETRIES         - 最大重试次数 (默认 5, 因为多Bot所以可以多试)
"""

import os
import sys
import time
import uuid
import json
import random
import shutil
import subprocess
import traceback
import threading
from pathlib import Path
from datetime import datetime

# ============================================================
# 配置
# ============================================================

POSTGRES_DSN = os.environ.get('POSTGRES_DSN', '')
CHAT_ID = os.environ.get('CHAT_ID', '')

# VPS 调度器地址 (用于获取 TG 配置)
VPS_SCHEDULER_URL = os.environ.get('VPS_SCHEDULER_URL', '').rstrip('/')

# 多 Bot Token: 优先 BOT_TOKENS (逗号分隔), 回退到单个 BOT_TOKEN
_raw_tokens = os.environ.get('BOT_TOKENS', '').strip()
if not _raw_tokens:
    _single = os.environ.get('BOT_TOKEN', '').strip()
    if _single:
        _raw_tokens = _single
BOT_TOKENS = [t.strip() for t in _raw_tokens.split(',') if t.strip()] if _raw_tokens else []

TELEGRAM_API_BASE = os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org').rstrip('/')
BOT_MIN_INTERVAL = float(os.environ.get('BOT_MIN_INTERVAL', '3'))
MAX_RETRIES = int(os.environ.get('MAX_RETRIES', '5'))

# 批量处理控制
MAX_CHAPTERS = int(os.environ.get('MAX_CHAPTERS', '0'))  # 0 = 不限, >0 = 最多处理 N 个章节
NUM_WORKERS = int(os.environ.get('NUM_WORKERS', str(os.cpu_count() or 1)))  # 默认=CPU核数

# 记录配置来源 (local / vps / mixed)
TG_CONFIG_SOURCE = 'local'  # 默认本地环境变量

BOOKS_TABLE = 'books'
CHAPTERS_TABLE = 'audiobook_chapters'


def extract_bot_user_id(token):
    """从 Bot Token 中提取 Bot 的永久 Telegram User ID

    Token 格式: {bot_user_id}:{secret}  例如: 7485554965:AAHxxx...
    bot_user_id 是 Bot 的永久 ID, 不随 Token 顺序/增删变化。
    """
    try:
        return int(token.split(':')[0])
    except (ValueError, IndexError):
        return None
TEMP_DIR = os.environ.get('TEMP_DIR', '/tmp/audiobook_temp')
DEEP_FILTER_BIN = os.environ.get('DEEP_FILTER_BIN', '/opt/deep-filter')
DEEP_FILTER_URL = 'https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl'

WORKER_ID = f'hf_{uuid.uuid4().hex[:8]}'

_use_df = False
_init_done = False


# ============================================================
# Bot 池 — 多Bot轮换核心
# ============================================================

class BotPool:
    """管理多个 Telegram Bot, 轮换上传以分散限流"""

    def __init__(self, tokens, chat_id, api_base, min_interval=3.0):
        self.bots = []
        for i, token in enumerate(tokens):
            self.bots.append({
                'id': i,
                'token': token,
                'username': None,
                'user_id': extract_bot_user_id(token),
                'last_upload_time': 0.0,
                'cooldown_until': 0.0,
                'consecutive_429': 0,
                'consecutive_ok': 0,
                'total_uploads': 0,
                'total_429': 0,
                'total_errors': 0,
                'available': True,
            })
        self._lock = threading.Lock()
        self.chat_id = chat_id
        self.api_base = api_base
        self.min_interval = min_interval

    def get_bot(self):
        """随机获取一个可用 Bot (不在冷却中且满足最小间隔)

        随机选择而非轮换, 避免多个 Worker 实例同时启动时都选同一个 Bot。
        """
        with self._lock:
            now = time.time()
            # 筛选所有可用的 Bot
            available = [b for b in self.bots
                         if now >= b['cooldown_until']
                         and (now - b['last_upload_time']) >= self.min_interval]
            if available:
                return random.choice(available)
            # 所有 Bot 都在冷却或间隔不足, 返回最快可用的
            best = min(self.bots, key=lambda b: max(b['cooldown_until'], b['last_upload_time'] + self.min_interval))
            return best

    def wait_for_bot(self, bot):
        """等待 Bot 变为可用 (冷却 + 最小间隔)"""
        now = time.time()
        cooldown_end = max(bot['cooldown_until'], bot['last_upload_time'] + self.min_interval)
        if now < cooldown_end:
            wait_s = cooldown_end - now
            print(f'  [Bot{bot["id"]}] 等待 {wait_s:.0f}s 后可用...')
            time.sleep(min(wait_s, 120))

    def on_success(self, bot):
        with self._lock:
            bot['consecutive_429'] = 0
            bot['consecutive_ok'] += 1
            bot['total_uploads'] += 1
            bot['last_upload_time'] = time.time()
            bot['available'] = True

    def on_429(self, bot, retry_after):
        with self._lock:
            bot['consecutive_429'] += 1
            bot['consecutive_ok'] = 0
            bot['total_429'] += 1
            bot['cooldown_until'] = time.time() + retry_after
            bot['last_upload_time'] = time.time()
            bot['available'] = False

    def on_error(self, bot):
        with self._lock:
            bot['consecutive_ok'] = 0
            bot['total_errors'] += 1
            bot['last_upload_time'] = time.time()

    def status(self):
        with self._lock:
            now = time.time()
            return [{
                'id': b['id'],
                'username': b['username'] or f'bot_{b["id"]}',
                'user_id': b['user_id'],
                'total_uploads': b['total_uploads'],
                'total_429': b['total_429'],
                'total_errors': b['total_errors'],
                'consecutive_429': b['consecutive_429'],
                'consecutive_ok': b['consecutive_ok'],
                'cooldown_remaining': max(0, round(b['cooldown_until'] - now)),
                'available': now >= b['cooldown_until'],
            } for b in self.bots]

    def init_bots(self):
        """初始化 Bot 池 (不做网络验证, 直接使用)"""
        for bot in self.bots:
            bot['username'] = f'bot_{bot["id"]}'
        print(f'>>> Bot 池就绪: {len(self.bots)} 个 Bot (不验证, 直接使用)')
        return len(self.bots)

    def summary(self):
        with self._lock:
            total_up = sum(b['total_uploads'] for b in self.bots)
            total_429 = sum(b['total_429'] for b in self.bots)
            total_err = sum(b['total_errors'] for b in self.bots)
            return {
                'bot_count': len(self.bots),
                'total_uploads': total_up,
                'total_429': total_429,
                'total_errors': total_err,
            }


# 全局 Bot 池 (在 init_worker 中创建)
bot_pool = None
_bot_pool_lock = threading.Lock()  # 保护 bot_pool 的重建操作


# ============================================================
# 从 VPS 调度器获取 Telegram 配置
# ============================================================

def fetch_tg_config_from_vps(force=False):
    """从 VPS 调度器的 /api/tg-config 接口获取 Telegram 配置

    当 force=False (默认): 仅在本地为空时才用 VPS 的值 (向后兼容)
    当 force=True: 用 VPS 的值覆盖本地 (用于一键刷新)

    返回: (success, message, raw_data)
      raw_data 为 VPS 返回的原始配置 dict (失败时为 {})
    """
    global CHAT_ID, BOT_TOKENS, TELEGRAM_API_BASE, TG_CONFIG_SOURCE

    if not VPS_SCHEDULER_URL:
        return False, 'VPS_SCHEDULER_URL 未设置', {}

    import requests
    url = f'{VPS_SCHEDULER_URL}/api/tg-config'
    print(f'>>> 从 VPS 调度器获取 Telegram 配置: {url} (force={force})')
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            return False, 'VPS 调度器未配置 Telegram 信息 (请在管理面板配置)', {}
        if resp.status_code != 200:
            return False, f'VPS 返回 HTTP {resp.status_code}', {}

        data = resp.json()
        if not data.get('ok'):
            return False, data.get('message', '未知错误'), {}

        fetched_any = False
        sources = []

        # CHAT_ID
        vps_chat_id = data.get('chat_id', '').strip()
        if vps_chat_id:
            if force or not CHAT_ID:
                if vps_chat_id != CHAT_ID:
                    CHAT_ID = vps_chat_id
                    fetched_any = True
                    sources.append('chat_id')
                elif force:
                    sources.append('chat_id(未变)')

        # BOT_TOKENS
        vps_tokens_str = data.get('bot_tokens', '').strip()
        if vps_tokens_str:
            vps_tokens = [t.strip() for t in vps_tokens_str.split(',') if t.strip()]
            if vps_tokens:
                if force or not BOT_TOKENS:
                    if vps_tokens != BOT_TOKENS:
                        BOT_TOKENS = vps_tokens
                        fetched_any = True
                        sources.append(f'bot_tokens({len(vps_tokens)})')
                    elif force:
                        sources.append(f'bot_tokens({len(vps_tokens)},未变)')

        # TELEGRAM_API_BASE
        vps_api_base = data.get('telegram_api_base', '').strip().rstrip('/')
        if vps_api_base:
            if force or TELEGRAM_API_BASE == 'https://api.telegram.org':
                if vps_api_base != TELEGRAM_API_BASE:
                    TELEGRAM_API_BASE = vps_api_base
                    fetched_any = True
                    sources.append('api_base')
                elif force:
                    sources.append('api_base(未变)')

        if fetched_any:
            TG_CONFIG_SOURCE = 'vps'
            return True, f'已从 VPS 获取: {", ".join(sources)}', data
        elif force:
            return True, f'VPS 配置已同步 (无变化): {", ".join(sources) or "无字段"}', data
        else:
            return False, 'VPS 返回的配置为空或本地已配置', data

    except requests.exceptions.ConnectionError as e:
        return False, f'连接 VPS 失败: {str(e)[:100]}', {}
    except Exception as e:
        return False, f'获取配置异常: {type(e).__name__}: {str(e)[:100]}', {}


def refresh_tg_config():
    """一键从 VPS 调度器强制刷新 Telegram 配置, 并重建 Bot 池

    用于 Web 面板按钮触发: 无论本地是否已有配置, 都用 VPS 最新值覆盖。
    如果 Bot Token 列表发生变化, 会重建 bot_pool 使新配置立即生效。

    返回: dict (供 API 返回给前端显示)
    """
    global bot_pool, TG_CONFIG_SOURCE

    old_bot_count = len(BOT_TOKENS)
    old_chat_id = CHAT_ID
    old_api_base = TELEGRAM_API_BASE

    ok, msg, raw_data = fetch_tg_config_from_vps(force=True)

    result = {
        'ok': ok,
        'message': msg,
        'vps_url': VPS_SCHEDULER_URL or '(未设置)',
        'raw': {},
        'changed': False,
        'bot_count': len(BOT_TOKENS),
        'chat_id': CHAT_ID,
        'api_base': TELEGRAM_API_BASE,
        'config_source': TG_CONFIG_SOURCE,
    }

    # 脱敏后展示 VPS 原始返回
    if raw_data:
        vps_tokens_str = raw_data.get('bot_tokens', '').strip()
        vps_token_list = [t.strip() for t in vps_tokens_str.split(',') if t.strip()] if vps_tokens_str else []
        result['raw'] = {
            'chat_id': raw_data.get('chat_id', ''),
            'bot_tokens_count': len(vps_token_list),
            'bot_tokens_preview': [f'{t[:10]}...{t[-4:]}' for t in vps_token_list] if vps_token_list else [],
            'telegram_api_base': raw_data.get('telegram_api_base', ''),
        }

    # 判断是否有变化
    config_changed = (
        len(BOT_TOKENS) != old_bot_count or
        CHAT_ID != old_chat_id or
        TELEGRAM_API_BASE != old_api_base
    )
    result['changed'] = config_changed

    # 如果 Bot Token 或 Chat ID 变化, 重建 bot_pool
    if ok and config_changed:
        if BOT_TOKENS and CHAT_ID:
            with _bot_pool_lock:
                bot_pool = BotPool(BOT_TOKENS, CHAT_ID, TELEGRAM_API_BASE, BOT_MIN_INTERVAL)
            print(f'[刷新] Bot 池已重建: {len(BOT_TOKENS)} 个 Bot (之前 {old_bot_count} 个)')
            result['bot_pool_rebuilt'] = True
        else:
            result['bot_pool_rebuilt'] = False
            if not BOT_TOKENS:
                result['message'] += ' (警告: Bot Tokens 仍为空)'
            if not CHAT_ID:
                result['message'] += ' (警告: Chat ID 仍为空)'
    else:
        result['bot_pool_rebuilt'] = False

    return result


# ============================================================
# 初始化
# ============================================================

def init_worker():
    global _use_df, _init_done, bot_pool, TG_CONFIG_SOURCE
    if _init_done:
        return

    os.makedirs(TEMP_DIR, exist_ok=True)

    # ffmpeg
    ffmpeg_ok = False
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        if result.returncode == 0:
            ffmpeg_ok = True
            print('[OK] ffmpeg 可用')
    except Exception:
        pass
    if not ffmpeg_ok:
        print('[警告] ffmpeg 不可用!')

    # DeepFilter
    if not os.path.exists(DEEP_FILTER_BIN):
        print('>>> 下载 DeepFilter...')
        import requests
        try:
            resp = requests.get(DEEP_FILTER_URL, stream=True, timeout=120)
            resp.raise_for_status()
            with open(DEEP_FILTER_BIN, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            try:
                os.chmod(DEEP_FILTER_BIN, 0o755)
            except PermissionError:
                pass
            print('[OK] DeepFilter 下载完成')
        except Exception as e:
            print(f'[警告] DeepFilter 下载失败: {e}')
    else:
        try:
            os.chmod(DEEP_FILTER_BIN, 0o755)
        except PermissionError:
            pass

    try:
        result = subprocess.run([DEEP_FILTER_BIN, '--help'], capture_output=True, timeout=10)
        if result.returncode in (0, 2):
            _use_df = True
            print('[OK] DeepFilter 验证通过')
        else:
            print('[警告] DeepFilter 验证失败')
    except Exception as e:
        print(f'[警告] DeepFilter 不可用: {e}')

    if _use_df and not ffmpeg_ok:
        _use_df = False
    if not _use_df:
        print('[INFO] 跳过 DeepFilter 降噪')

    # 如果本地未配置 TG 信息, 尝试从 VPS 调度器获取
    if (not CHAT_ID or not BOT_TOKENS) and VPS_SCHEDULER_URL:
        ok, msg, _ = fetch_tg_config_from_vps()
        if ok:
            print(f'[OK] {msg}')
        else:
            print(f'[警告] 从 VPS 获取配置失败: {msg}')
    elif not CHAT_ID or not BOT_TOKENS:
        if not VPS_SCHEDULER_URL:
            print('[提示] CHAT_ID/BOT_TOKENS 为空且 VPS_SCHEDULER_URL 未设置')
            print('       请在 HF Space 环境变量中配置, 或设置 VPS_SCHEDULER_URL 从调度器获取')

    # 验证配置
    if not POSTGRES_DSN:
        print('[错误] POSTGRES_DSN 未设置!')
    if not CHAT_ID:
        print('[错误] CHAT_ID 未设置! (本地环境变量或 VPS 调度器均未提供)')
    if not BOT_TOKENS:
        print('[错误] BOT_TOKENS 未设置! (本地环境变量或 VPS 调度器均未提供)')
    else:
        # 创建 Bot 池
        bot_pool = BotPool(BOT_TOKENS, CHAT_ID, TELEGRAM_API_BASE, BOT_MIN_INTERVAL)
        for bot in bot_pool.bots:
            bot['username'] = f'bot_{bot["id"]}'
        print(f'>>> Bot 池就绪: {len(BOT_TOKENS)} 个 Bot (直接使用, 不验证)')

    if TELEGRAM_API_BASE != 'https://api.telegram.org':
        print(f'[路由] Telegram API 通过中继: {TELEGRAM_API_BASE}')
    else:
        print('[路由] Telegram API 直连')

    print(f'[Worker] ID: {WORKER_ID}, Bots: {len(BOT_TOKENS)}, MinInterval: {BOT_MIN_INTERVAL}s')
    print(f'[配置来源] TG配置: {TG_CONFIG_SOURCE}')
    _init_done = True


# ============================================================
# PostgreSQL
# ============================================================

def safe_pg_execute(query, params=None, fetch=False, retries=3):
    import psycopg2
    for i in range(retries):
        conn = None
        try:
            conn = psycopg2.connect(POSTGRES_DSN)
            with conn.cursor() as cur:
                cur.execute(query, params)
                result = cur.fetchall() if fetch else None
                conn.commit()
                return result
        except Exception as e:
            print(f'  [PG 重试 {i+1}/{retries}] {e}')
            time.sleep(1)
        finally:
            if conn:
                conn.close()
    return None if fetch else False


def claim_next_chapter():
    """原子认领一个 pending 章节"""
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(POSTGRES_DSN)
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE {CHAPTERS_TABLE} SET upload_status = %s, worker_id = %s, claimed_at = NOW() '
                f'WHERE ctid IN ('
                f'    SELECT ctid FROM {CHAPTERS_TABLE} '
                f'    WHERE upload_status = %s '
                f'    ORDER BY book_id, chapter_id '
                f'    LIMIT 1 '
                f'    FOR UPDATE SKIP LOCKED'
                f') '
                f'RETURNING book_id, chapter_id, book_name, chapter_name, audio_url',
                ('processing', WORKER_ID, 'pending')
            )
            row = cur.fetchone()
            conn.commit()
            return row
    except Exception as e:
        if conn:
            conn.rollback()
        print(f'  [认领错误] {e}')
        return None
    finally:
        if conn:
            conn.close()


def record_upload(book_id, chapter_id, file_id, message_id, status, error_message=None, bot_id=None, bot_user_id=None):
    """记录上传结果到 DB (含上传 Bot 编号 + Bot 永久 User ID)"""
    now = datetime.now() if status == 'uploaded' else None
    safe_pg_execute(
        f'UPDATE {CHAPTERS_TABLE} SET telegram_file_id = %s, telegram_message_id = %s, '
        f'telegram_bot_id = %s, telegram_bot_user_id = %s, upload_status = %s, uploaded_at = %s, error_message = %s '
        f'WHERE book_id = %s AND chapter_id = %s',
        (file_id, message_id, bot_id, bot_user_id, status, now, error_message, str(book_id), str(chapter_id))
    )


def check_and_mark_book_complete(book_id):
    """检查并标记书完成"""
    r = safe_pg_execute(
        f'SELECT COUNT(*) FROM {CHAPTERS_TABLE} WHERE book_id = %s AND upload_status IN (%s, %s)',
        (str(book_id), 'pending', 'processing'), fetch=True
    )
    remaining = r[0][0] if r else 0
    if remaining == 0:
        safe_pg_execute(
            f'UPDATE {BOOKS_TABLE} SET book_status = %s WHERE book_id = %s AND book_status != %s',
            ('success', str(book_id), 'success')
        )
        return True
    return False


def get_db_stats():
    stats = {}
    for status in ['pending', 'processing', 'uploaded', 'failed']:
        r = safe_pg_execute(
            f'SELECT COUNT(*) FROM {CHAPTERS_TABLE} WHERE upload_status = %s',
            (status,), fetch=True
        )
        stats[status] = r[0][0] if r else 0
    r = safe_pg_execute(f'SELECT COUNT(*) FROM {CHAPTERS_TABLE}', fetch=True)
    stats['total'] = r[0][0] if r else 0
    r = safe_pg_execute(f'SELECT COUNT(*) FROM {BOOKS_TABLE}', fetch=True)
    stats['books_total'] = r[0][0] if r else 0
    r = safe_pg_execute(
        f'SELECT COUNT(*) FROM {BOOKS_TABLE} WHERE book_status = %s',
        ('success',), fetch=True
    )
    stats['books_success'] = r[0][0] if r else 0
    return stats


# ============================================================
# 音频处理
# ============================================================

def verify_audio_file(file_path):
    """用 ffprobe 验证音频文件是否完整有效，返回 (有效?, 原因)

    参考旧代码 download_df_aduio_to_my_pg.ipynb:
      - Content-Length 大小校验在调用方 download_audio_file 中完成
      - 本函数负责 ffprobe 三层校验: 进程返回码 / 音频流存在 / 时长正常
      - 明确区分 ffprobe 超时与其他异常
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', file_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return False, f'ffprobe 失败: {result.stderr.strip()[:200]}'
        info = json.loads(result.stdout)
        # 检查是否有音频流
        streams = info.get('streams', [])
        if not any(s.get('codec_type') == 'audio' for s in streams):
            return False, '文件无音频流'
        # 检查是否有时长信息
        fmt = info.get('format', {})
        duration = fmt.get('duration')
        if duration and float(duration) < 0.1:
            return False, f'音频时长异常: {duration}s'
        return True, f'OK ({float(duration):.1f}s)' if duration else 'OK'
    except subprocess.TimeoutExpired:
        return False, 'ffprobe 超时'
    except Exception as e:
        return False, f'验证异常: {e}'


def download_audio_file(url, save_path, timeout=360, max_retries=3):
    import requests
    errors = []
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, stream=True, timeout=timeout)
            resp.raise_for_status()
            expected_size = int(resp.headers.get('content-length', 0))
            with open(save_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            actual_size = os.path.getsize(save_path)
            if expected_size > 0 and actual_size != expected_size:
                os.remove(save_path)
                raise Exception(f'文件不完整: {actual_size}/{expected_size}')
            if actual_size == 0:
                os.remove(save_path)
                raise Exception('下载文件为空')
            valid, msg = verify_audio_file(save_path)
            if not valid:
                os.remove(save_path)
                raise Exception(f'音频验证失败: {msg}')
            return True, actual_size
        except Exception as e:
            errors.append(f'[尝试{attempt}] {type(e).__name__}: {str(e)[:150]}')
            print(f'  [下载重试 {attempt}/{max_retries}] {type(e).__name__}: {str(e)[:100]}')
            if os.path.exists(save_path):
                os.remove(save_path)
            if attempt < max_retries:
                time.sleep(5 * attempt)
    return False, f'下载失败 ({max_retries}次): {"; ".join(errors)}'


def convert_to_wav(mp3_path, wav_path):
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', mp3_path, '-ac', '1', '-ar', '16000', wav_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise Exception(f'ffmpeg: {r.stderr.strip()[:200]}')


def run_deepfilter(wav_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    cmd = [DEEP_FILTER_BIN, wav_path, '--output-dir', output_dir]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise Exception(f'DeepFilter: {r.stderr.strip()[:200]}')
    files = list(Path(output_dir).glob('*.wav'))
    if not files:
        raise Exception('DeepFilter 未生成输出')
    return str(files[0])


def convert_to_mp3(wav_path, mp3_path):
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', wav_path, '-codec:a', 'libmp3lame', '-qscale:a', '2', mp3_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise Exception(f'ffmpeg MP3: {r.stderr.strip()[:200]}')


def process_audio(input_mp3, output_mp3, task_id, use_df=True):
    if not use_df:
        if input_mp3 != output_mp3:
            shutil.copy(input_mp3, output_mp3)
        return True, output_mp3
    work_dir = os.path.join(TEMP_DIR, f'df_{task_id}')
    os.makedirs(work_dir, exist_ok=True)
    wav_input = os.path.join(work_dir, 'input.wav')
    df_output_dir = os.path.join(work_dir, 'df_output')
    try:
        convert_to_wav(input_mp3, wav_input)
        denoised = run_deepfilter(wav_input, df_output_dir)
        convert_to_mp3(denoised, output_mp3)
        return True, output_mp3
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)


# ============================================================
# 多Bot上传 Telegram
# ============================================================

def upload_to_telegram_multi_bot(file_path, book_name, chapter_name, max_retries=None):
    """使用多Bot轮换上传音频到 Telegram

    策略:
    1. 从Bot池获取下一个可用Bot
    2. 如果Bot在冷却中, 等待或切换到下一个
    3. 上传成功 → 标记成功, 返回
    4. 收到429 → 标记该Bot冷却, 切换到下一个Bot重试
    5. 其他错误 → 标记错误, 切换到下一个Bot重试
    """
    import requests

    if max_retries is None:
        max_retries = MAX_RETRIES

    if bot_pool is None:
        return {'success': False, 'file_id': None, 'message_id': None,
                'error': 'Bot池未初始化', 'bot_id': -1}

    filename = os.path.basename(file_path)
    caption = f'{book_name} - {chapter_name}'
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    route = '中继' if TELEGRAM_API_BASE != 'https://api.telegram.org' else '直连'

    print(f'  [上传] {filename} ({file_size // 1024}KB) 路由: {route}')

    errors = []
    for attempt in range(1, max_retries + 1):
        bot = bot_pool.get_bot()
        bot_pool.wait_for_bot(bot)

        api_url = f'{TELEGRAM_API_BASE}/bot{bot["token"]}/sendAudio'
        bot_tag = f'Bot{bot["id"]}@{bot["username"]}'

        t0 = time.time()
        try:
            with open(file_path, 'rb') as audio_file:
                files = {'audio': (filename, audio_file, 'audio/mpeg')}
                data = {
                    'chat_id': CHAT_ID,
                    'caption': caption[:200],
                    'title': chapter_name[:60],
                    'performer': book_name[:60],
                }
                print(f'  [{bot_tag}] 上传尝试 {attempt}/{max_retries}...')
                resp = requests.post(api_url, data=data, files=files, timeout=(30, 600))

            elapsed = time.time() - t0
            result = resp.json()

            if resp.status_code == 200 and result.get('ok'):
                msg = result['result']
                file_id = msg.get('audio', {}).get('file_id', '')
                message_id = msg.get('message_id', 0)
                bot_pool.on_success(bot)
                print(f'  [{bot_tag}] ✅ 成功 ({elapsed:.1f}s) file_id={file_id[:30]}...')
                return {
                    'success': True,
                    'file_id': file_id,
                    'message_id': message_id,
                    'error': None,
                    'bot_id': bot['id'],
                    'bot_user_id': bot['user_id'],
                    'bot_username': bot['username'],
                }

            if resp.status_code == 429:
                retry_after = result.get('parameters', {}).get('retry_after', 60)
                print(f'  [{bot_tag}] 429! retry_after={retry_after}s, 切换Bot...')
                bot_pool.on_429(bot, retry_after)
                errors.append(f'[{bot_tag}] 429 (retry_after={retry_after}s)')
                continue  # 不消耗重试次数, 切换到下一个Bot

            error_desc = result.get('description', '未知错误')
            error_code = result.get('error_code', '?')
            print(f'  [{bot_tag}] HTTP {resp.status_code} code={error_code}: {error_desc}')
            bot_pool.on_error(bot)
            errors.append(f'[{bot_tag}] HTTP {resp.status_code}: {error_desc}')
            time.sleep(2 * attempt)

        except requests.exceptions.ReadTimeout:
            elapsed = time.time() - t0
            print(f'  [{bot_tag}] ReadTimeout ({elapsed:.0f}s)')
            bot_pool.on_error(bot)
            errors.append(f'[{bot_tag}] ReadTimeout ({elapsed:.0f}s)')
            time.sleep(3 * attempt)
        except requests.exceptions.ConnectTimeout:
            print(f'  [{bot_tag}] ConnectTimeout')
            bot_pool.on_error(bot)
            errors.append(f'[{bot_tag}] ConnectTimeout')
            time.sleep(3 * attempt)
        except Exception as e:
            elapsed = time.time() - t0
            print(f'  [{bot_tag}] {type(e).__name__}: {str(e)[:150]} ({elapsed:.0f}s)')
            bot_pool.on_error(bot)
            errors.append(f'[{bot_tag}] {type(e).__name__}: {str(e)[:100]}')
            time.sleep(2 * attempt)

    detail = '; '.join(errors)
    return {
        'success': False,
        'file_id': None,
        'message_id': None,
        'error': f'所有Bot重试失败 ({max_retries}次): {detail}',
        'bot_id': -1,
    }


# ============================================================
# 主入口
# ============================================================

def run_one(slot=None):
    """认领并处理一个章节:
    下载 → 降噪 → 多Bot轮换上传Telegram → 更新DB
    """
    init_worker()

    def _update_slot(step):
        if slot is not None and slot.current_task is not None:
            slot.current_task['current_step'] = step

    # 1. 认领任务
    chapter = claim_next_chapter()
    if chapter is None:
        return {'status': 'no_task', 'success': False, 'message': '没有待处理的章节'}

    book_id, chapter_id, book_name, chapter_name, audio_url = chapter
    task_id = f'{book_id}_{chapter_id}_{uuid.uuid4().hex[:6]}'
    t0 = time.time()

    result = {
        'status': 'processing',
        'worker_id': WORKER_ID,
        'book_id': str(book_id),
        'chapter_id': str(chapter_id),
        'book_name': book_name,
        'chapter_name': chapter_name,
        'audio_url': audio_url,
        'started_at': datetime.now().isoformat(),
    }

    if slot is not None:
        slot.current_task = {
            'book_name': book_name,
            'chapter_name': chapter_name,
            'current_step': '已认领, 准备下载...',
        }

    print(f'>>> [{WORKER_ID}] 认领: {book_name} - {chapter_name}')

    # 2. 无 URL
    if not audio_url:
        err = f'无音频URL (book_id={book_id}, chapter_id={chapter_id})'
        record_upload(book_id, chapter_id, None, None, 'failed', err)
        check_and_mark_book_complete(book_id)
        result.update(status='failed', error=err, duration=time.time() - t0)
        return result

    # 3. 下载音频
    raw_mp3 = os.path.join(TEMP_DIR, f'{task_id}_raw.mp3')
    print(f'  [步骤1/3] 下载音频...')
    _update_slot('下载音频中...')
    dl_ok, dl_info = download_audio_file(audio_url, raw_mp3)

    if not dl_ok:
        err = f'下载失败: {dl_info}'
        record_upload(book_id, chapter_id, None, None, 'failed', err)
        check_and_mark_book_complete(book_id)
        result.update(status='failed', error=err, duration=time.time() - t0)
        print(f'  [FAIL] {err}')
        return result

    dl_kb = dl_info // 1024
    print(f'  [下载完成] {dl_kb}KB')
    result['download_kb'] = dl_kb

    # 4. DeepFilter 降噪
    final_mp3 = raw_mp3
    if _use_df:
        print(f'  [步骤2/3] DeepFilter 降噪...')
        _update_slot(f'DeepFilter 降噪中... ({dl_kb}KB)')
        processed_mp3 = os.path.join(TEMP_DIR, f'{task_id}_denoised.mp3')
        df_ok, df_result = process_audio(raw_mp3, processed_mp3, task_id, use_df=True)
        if df_ok:
            final_mp3 = processed_mp3
            df_kb = os.path.getsize(processed_mp3) // 1024
            print(f'  [降噪完成] {df_kb}KB')
            result['denoised_kb'] = df_kb
            if os.path.exists(raw_mp3):
                os.remove(raw_mp3)
        else:
            err = f'降噪失败: {df_result}'
            record_upload(book_id, chapter_id, None, None, 'failed', err)
            check_and_mark_book_complete(book_id)
            if os.path.exists(raw_mp3):
                os.remove(raw_mp3)
            result.update(status='failed', error=err, duration=time.time() - t0)
            return result
    else:
        print(f'  [步骤2/3] 跳过降噪')

    # 5. 多Bot轮换上传 Telegram
    final_size = os.path.getsize(final_mp3) if os.path.exists(final_mp3) else 0
    print(f'  [步骤3/3] 多Bot上传 Telegram ({final_size // 1024}KB)...')
    _update_slot(f'上传Telegram (多Bot) ({final_size // 1024}KB)...')

    up_result = upload_to_telegram_multi_bot(final_mp3, book_name, chapter_name)

    if up_result['success']:
        record_upload(book_id, chapter_id, up_result['file_id'], up_result['message_id'], 'uploaded',
                      bot_id=up_result.get('bot_id'), bot_user_id=up_result.get('bot_user_id'))
        book_done = check_and_mark_book_complete(book_id)
        bot_tag = f'Bot{up_result.get("bot_id", "?")}'
        if up_result.get('bot_user_id'):
            bot_tag += f'(uid:{up_result["bot_user_id"]})'
        print(f'  [OK] {bot_tag} file_id={up_result["file_id"][:30]}... msg_id={up_result["message_id"]}')
        if book_done:
            print(f'  *** 书 {book_id} 全部完成! ***')
        result.update(
            status='uploaded',
            success=True,
            file_id=up_result['file_id'],
            message_id=up_result['message_id'],
            bot_id=up_result.get('bot_id'),
            bot_user_id=up_result.get('bot_user_id'),
            book_completed=book_done,
            duration=time.time() - t0,
        )
    else:
        err = up_result['error']
        print(f'  [FAIL] {err}')
        record_upload(book_id, chapter_id, None, None, 'failed', err)
        check_and_mark_book_complete(book_id)
        result.update(status='failed', error=err, duration=time.time() - t0)

    # 6. 清理
    for f in [raw_mp3, final_mp3]:
        if f and os.path.exists(f):
            os.remove(f)

    elapsed = time.time() - t0
    print(f'  [完成] 总耗时 {elapsed:.1f}s')
    return result


# ============================================================
# 批量处理 — 多线程并发: 下载 → 降噪 → 上传
# ============================================================

# 批量处理全局状态
_batch_state = {
    'running': False,
    'stop_event': None,       # threading.Event
    'lock': None,             # threading.Lock
    'shared': {},             # 统计数据
    'started_at': None,
    'finished_at': None,
    'max_chapters': 0,
    'num_workers': 1,
    'message': '',
}


def get_batch_status():
    """获取当前批量处理状态"""
    if not _batch_state['running'] and not _batch_state['finished_at']:
        return {'running': False, 'message': '未启动批量处理'}

    shared = _batch_state['shared'] or {}
    lock = _batch_state['lock']
    if lock:
        with lock:
            shared = dict(shared)
    else:
        shared = {}

    return {
        'running': _batch_state['running'],
        'started_at': _batch_state['started_at'],
        'finished_at': _batch_state['finished_at'],
        'max_chapters': _batch_state['max_chapters'],
        'num_workers': _batch_state['num_workers'],
        'processed': shared.get('processed', 0),
        'uploaded': shared.get('uploaded', 0),
        'failed': shared.get('failed', 0),
        'no_url': shared.get('no_url', 0),
        'books_completed': shared.get('books_completed', 0),
        'df_failed': shared.get('df_failed', False),
        'message': _batch_state['message'],
        'use_df': _use_df,
    }


def stop_batch():
    """停止正在运行的批量处理"""
    if not _batch_state['running']:
        return False, '批量处理未在运行'
    if _batch_state['stop_event']:
        _batch_state['stop_event'].set()
    _batch_state['message'] = '用户请求停止...'
    return True, '停止信号已发送'


def run_batch(max_chapters=None, num_workers=None):
    """批量处理多个章节 (多线程并发)

    使用 ThreadPoolExecutor 并发处理: 下载 → DeepFilter 降噪 → 多Bot上传
    每个线程独立认领章节 (FOR UPDATE SKIP LOCKED), 互不冲突。

    失败处理策略 (与旧代码不同, 按用户要求调整):
      - 下载失败 / 降噪失败 / 上传失败 / 无 URL  → 标记 'failed' 写入数据库
      - 降噪失败 **不再停止所有线程**, 仅累加 df_failed 计数, 继续处理下一个章节
      - 其他线程可继续工作, 直到达到 MAX_CHAPTERS 或无待处理章节

    参数:
      max_chapters: 最多处理章节数 (0=不限, None=用环境变量 MAX_CHAPTERS)
      num_workers:  并发线程数 (None=用环境变量 NUM_WORKERS)

    返回: dict 批量处理结果统计
    """
    global _batch_state

    init_worker()

    if _batch_state['running']:
        return {'ok': False, 'message': '批量处理已在运行中'}

    if max_chapters is None:
        max_chapters = MAX_CHAPTERS
    if num_workers is None:
        num_workers = NUM_WORKERS

    if not BOT_TOKENS or not CHAT_ID:
        return {'ok': False, 'message': 'Bot Token 或 Chat ID 未配置'}

    stop_event = threading.Event()
    lock = threading.Lock()
    shared = {'uploaded': 0, 'failed': 0, 'no_url': 0, 'books_completed': 0, 'processed': 0, 'df_failed': 0}

    _batch_state.update({
        'running': True,
        'stop_event': stop_event,
        'lock': lock,
        'shared': shared,
        'started_at': datetime.now().isoformat(),
        'finished_at': None,
        'max_chapters': max_chapters,
        'num_workers': num_workers,
        'message': '处理中...',
    })

    limit_desc = f'{max_chapters} 个' if max_chapters > 0 else '不限'
    print(f'\n{"=" * 60}')
    print(f'>>> 批量处理开始 (最多 {limit_desc} 章节, {num_workers} 线程)')
    print(f'    DeepFilter: {"启用" if _use_df else "禁用"}')
    print(f'    Bot 数量:   {len(BOT_TOKENS)}')
    print(f'    失败策略:   标记 failed 并继续 (不停止其他线程)')
    print(f'{"=" * 60}\n')

    def _batch_worker_loop():
        """工作线程主循环: 持续认领并处理章节

        失败处理: 下载/降噪/上传失败均标记 failed, 继续处理下一个章节。
        降噪失败不再触发全局停止 (与旧代码不同)。
        """
        while not stop_event.is_set():
            # 检查 MAX_CHAPTERS 限制
            with lock:
                if max_chapters > 0 and shared['processed'] >= max_chapters:
                    return  # 达到上限, 退出

            # 认领并处理一个章节
            result = run_one()
            status = result.get('status', '?')

            if status == 'no_task':
                return  # 没有待处理章节了

            with lock:
                shared['processed'] += 1
                if status == 'uploaded':
                    shared['uploaded'] += 1
                elif status == 'failed':
                    shared['failed'] += 1
                    # 区分无 URL 失败
                    if not result.get('audio_url'):
                        shared['no_url'] += 1
                    # 区分降噪失败 (仅计数, 不停止其他线程)
                    if '降噪失败' in result.get('error', ''):
                        shared['df_failed'] += 1
                if result.get('book_completed'):
                    shared['books_completed'] += 1

            # 降噪失败: 仅打印警告, 继续处理下一个章节 (不调用 stop_event.set())
            if status == 'failed' and '降噪失败' in result.get('error', ''):
                print(f'  [警告] 降噪失败已标记为 failed, 继续处理下一个章节: '
                      f'{result.get("book_name")} - {result.get("chapter_name")}')

            # 每处理 10 个, 打印进度
            with lock:
                num = shared['processed']
            if num % 10 == 0:
                bot_s = bot_pool.summary() if bot_pool else {}
                print(f'\n  --- 批量进度: {num} 已处理, '
                      f'{shared["uploaded"]} 成功, {shared["failed"]} 失败 ---')
                print(f'  --- Bot池: 上传 {bot_s.get("total_uploads", 0)}, '
                      f'429 {bot_s.get("total_429", 0)} ---\n')

    try:
        if num_workers <= 1:
            # 单线程模式
            _batch_worker_loop()
        else:
            # 多线程并发模式
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix='BatchW') as executor:
                futures = [executor.submit(_batch_worker_loop) for _ in range(num_workers)]
                for f in futures:
                    f.result()
    except Exception as e:
        print(f'[批量处理异常] {type(e).__name__}: {e}')
        _batch_state['message'] = f'异常: {e}'
    finally:
        _batch_state['running'] = False
        _batch_state['finished_at'] = datetime.now().isoformat()

        if shared.get('df_failed', 0) > 0:
            _batch_state['message'] = f'完成 (其中 {shared["df_failed"]} 个降噪失败已标记 failed)'
        elif stop_event.is_set():
            _batch_state['message'] = '已停止' if shared['processed'] > 0 else '已停止 (未处理)'
        else:
            _batch_state['message'] = '完成'

    # 最终报告
    print(f'\n{"=" * 60}')
    print(f'>>> 批量处理完成!')
    print(f'    已处理:     {shared["processed"]}')
    print(f'    成功上传:   {shared["uploaded"]}')
    print(f'    失败:       {shared["failed"]}')
    print(f'    无URL:      {shared["no_url"]}')
    print(f'    降噪失败:   {shared["df_failed"]}')
    print(f'    整书完成:   {shared["books_completed"]}')
    if bot_pool:
        s = bot_pool.summary()
        print(f'    Bot池统计:  上传 {s["total_uploads"]}, 429 {s["total_429"]}, 错误 {s["total_errors"]}')
    print(f'{"=" * 60}\n')

    return {
        'ok': True,
        'message': _batch_state['message'],
        'processed': shared['processed'],
        'uploaded': shared['uploaded'],
        'failed': shared['failed'],
        'no_url': shared['no_url'],
        'books_completed': shared['books_completed'],
        'df_failed': shared['df_failed'],
    }
