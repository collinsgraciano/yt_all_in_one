#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPS 任务调度器 - 有声书 Serverless 架构 (双核并行版)

工作流程:
  1. 轮询 PostgreSQL, 检查 pending / processing 章节数量
  2. 当有空闲槽位时, 触发 HF Space Worker 填充空闲槽位
  3. 持续监控并填充空闲槽位
  4. 定期重置卡住的任务

特点:
  - 极低 CPU 占用 (仅 PG 查询 + HTTP 请求)
  - 充分利用 HF Space 2 vCPU (双槽位并行)
  - 支持多个 HF Space Worker 并行
  - 自动重试和错误恢复
  - Scheduler 类支持后台线程运行 + 运行时配置修改

环境变量:
  POSTGRES_DSN     - 本机 PostgreSQL 连接串
  HF_SPACE_URLS    - HF Space URL (逗号分隔支持多个)
  MAX_SLOTS        - 每个 Worker 的并行槽位数 (默认 2, = HF CPU 核数)
  CHECK_INTERVAL   - 检查间隔秒数 (默认 15)
  STUCK_TIMEOUT_M  - 卡住超时分钟数 (默认 1440=1天)

⚠️ HF Space URL 格式:
  正确: https://用户名-空间名.hf.space  (如 https://r777r7-t1.hf.space)
  错误: https://huggingface.co/spaces/用户名/空间名  (这是页面地址, 不是 API 地址)
"""

import os
import sys
import time
import threading
import argparse
from collections import deque
from datetime import datetime

# ============================================================
# 常量
# ============================================================

BOOKS_TABLE = 'books'
CHAPTERS_TABLE = 'audiobook_chapters'

DEFAULT_DSN = os.environ.get(
    'POSTGRES_DSN',
    'postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook'
)
DEFAULT_HF_URLS = os.environ.get(
    'HF_SPACE_URLS',
    'https://YOUR_USERNAME-audiobook-worker.hf.space'
)
DEFAULT_MAX_SLOTS = int(os.environ.get('MAX_SLOTS', '2'))
DEFAULT_CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', '15'))
DEFAULT_STUCK_TIMEOUT_M = int(os.environ.get('STUCK_TIMEOUT_M', '1440'))


# ============================================================
# 模块级日志 (独立运行时使用)
# ============================================================

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


# ============================================================
# PostgreSQL 操作 (无状态函数)
# ============================================================

def safe_pg_execute(dsn, query, params=None, fetch=False, retries=3):
    import psycopg2
    for i in range(retries):
        conn = None
        try:
            conn = psycopg2.connect(dsn)
            with conn.cursor() as cur:
                cur.execute(query, params)
                result = cur.fetchall() if fetch else None
                conn.commit()
                return result
        except Exception as e:
            log(f'  [PG 重试 {i+1}/{retries}] {e}')
            time.sleep(1)
        finally:
            if conn:
                conn.close()
    return None if fetch else False


def get_stats(dsn):
    stats = {}
    query = f'SELECT COUNT(*) FROM {CHAPTERS_TABLE} WHERE upload_status = %s'
    for status in ['pending', 'processing', 'uploaded', 'failed']:
        r = safe_pg_execute(dsn, query, (status,), fetch=True)
        stats[status] = r[0][0] if r else 0
    return stats


def reset_stuck_jobs(dsn, timeout_minutes):
    query = (
        f'UPDATE {CHAPTERS_TABLE} SET upload_status = %s, worker_id = NULL, claimed_at = NULL '
        f'WHERE upload_status = %s AND claimed_at < NOW() - INTERVAL %s'
    )
    r = safe_pg_execute(dsn, query, ('pending', 'processing', f'{timeout_minutes} minutes'))
    return r


# ============================================================
# HF Space 触发 / 健康检查 (无状态函数)
# ============================================================

def trigger_worker(hf_url, timeout=15):
    """发送 POST /process 触发 HF Space, 返回 (成功?, 响应信息, 空闲槽位数)"""
    import requests
    url = hf_url.rstrip('/') + '/process'
    try:
        resp = requests.post(url, timeout=timeout)
        data = resp.json()

        if resp.status_code == 202:
            free = data.get('free_slots', '?')
            total = data.get('total_slots', '?')
            slot = data.get('slot', '?')
            return True, f'已触发 (槽位{slot}, 剩余空闲{free}/{total})', free
        elif resp.status_code == 409:
            return True, f'Worker 满 ({data.get("message", "busy")})', 0
        else:
            return False, f'HTTP {resp.status_code}: {data.get("message", str(data))}', 0
    except requests.exceptions.Timeout:
        return True, '请求超时 (可能冷启动中)', 0
    except requests.exceptions.ConnectionError as e:
        return False, f'连接失败: {str(e)[:100]}', 0
    except Exception as e:
        return False, f'异常: {str(e)[:100]}', 0


def check_worker_health(hf_url, timeout=10):
    """检查 HF Space 是否在线, 返回 (在线?, 空闲槽位, 总槽位, worker_id)"""
    import requests
    url = hf_url.rstrip('/') + '/health'
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return True, data.get('free_slots', 0), data.get('total_slots', 0), data.get('worker_id', '?')
        return False, 0, 0, '?'
    except Exception:
        return False, 0, 0, '?'


# ============================================================
# Scheduler 类 (支持后台线程 + 运行时配置修改)
# ============================================================

class Scheduler:
    """任务调度器

    用法:
      # 方式 1: 被 web_app.py 导入
      sched = Scheduler(dsn=..., hf_urls=[...], max_slots=2, ...)
      sched.start()  # 后台线程运行

      # 方式 2: 独立运行
      python scheduler.py --hf-urls https://...
    """

    def __init__(self, dsn, hf_urls, max_slots=2, check_interval=15, stuck_timeout=1440,
                 chat_id='', bot_tokens='', telegram_api_base='',
                 cleanup_interval=600, cleanup_reset_failed=True, cleanup_auto_enabled=True):
        self._config_lock = threading.Lock()
        self.config = {
            'dsn': dsn,
            'hf_urls': list(hf_urls) if hf_urls else [],
            'max_slots': int(max_slots),
            'check_interval': int(check_interval),
            'stuck_timeout': int(stuck_timeout),
            'chat_id': chat_id,
            'bot_tokens': bot_tokens,
            'telegram_api_base': telegram_api_base,
            'cleanup_interval': int(cleanup_interval),
            'cleanup_reset_failed': bool(cleanup_reset_failed),
            'cleanup_auto_enabled': bool(cleanup_auto_enabled),
        }

        self.running = False
        self._thread = None
        self._stop_event = threading.Event()

        # 运行时状态 (供 Web 面板读取)
        self.logs = deque(maxlen=500)
        self.stats = {'pending': 0, 'processing': 0, 'uploaded': 0, 'failed': 0}
        self.worker_status = []
        self.total_triggered = 0
        self.last_trigger_time = None
        self.start_time = None
        self.status_text = '已停止'
        self.last_error = None

    # ---- 日志 ----

    def _log(self, msg):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = f'[{ts}] {msg}'
        self.logs.append(line)
        print(line, flush=True)

    # ---- 配置管理 ----

    def get_config(self):
        """返回当前配置 (hf_urls 以逗号分隔字符串返回, 便于前端展示)"""
        with self._config_lock:
            cfg = dict(self.config)
        cfg['hf_urls'] = ','.join(cfg['hf_urls'])
        return cfg

    def get_tg_config(self):
        """返回 Telegram 配置 (供 Worker 通过 API 获取)"""
        with self._config_lock:
            return {
                'chat_id': self.config.get('chat_id', ''),
                'bot_tokens': self.config.get('bot_tokens', ''),
                'telegram_api_base': self.config.get('telegram_api_base', ''),
            }

    def update_config(self, **kwargs):
        """运行时更新配置, 立即生效 (下一次循环读取)"""
        changes = []
        with self._config_lock:
            if 'hf_urls' in kwargs:
                raw = kwargs.pop('hf_urls')
                if isinstance(raw, str):
                    new_urls = [u.strip() for u in raw.split(',') if u.strip()]
                elif isinstance(raw, list):
                    new_urls = raw
                else:
                    new_urls = []
                if new_urls != self.config['hf_urls']:
                    self.config['hf_urls'] = new_urls
                    changes.append(f'hf_urls={new_urls}')

            if 'dsn' in kwargs:
                new_dsn = kwargs.pop('dsn')
                if new_dsn and new_dsn != self.config['dsn']:
                    self.config['dsn'] = new_dsn
                    changes.append('dsn=***')

            for k in ('max_slots', 'check_interval', 'stuck_timeout'):
                if k in kwargs and kwargs[k] is not None:
                    try:
                        new_val = int(kwargs[k])
                        if new_val != self.config[k]:
                            self.config[k] = new_val
                            changes.append(f'{k}={new_val}')
                    except (ValueError, TypeError):
                        pass

            # Telegram 配置 (多Bot模式)
            for k in ('chat_id', 'bot_tokens', 'telegram_api_base'):
                if k in kwargs and kwargs[k] is not None:
                    new_val = str(kwargs[k]).strip()
                    if new_val != self.config.get(k, ''):
                        self.config[k] = new_val
                        if k == 'bot_tokens':
                            changes.append(f'{k}=***({len(new_val.split(","))} tokens)')
                        else:
                            changes.append(f'{k}={new_val[:60]}')

            # 清理配置
            if 'cleanup_interval' in kwargs and kwargs['cleanup_interval'] is not None:
                try:
                    new_val = int(kwargs['cleanup_interval'])
                    if new_val != self.config['cleanup_interval']:
                        self.config['cleanup_interval'] = new_val
                        changes.append(f'cleanup_interval={new_val}')
                except (ValueError, TypeError):
                    pass

            for k in ('cleanup_reset_failed', 'cleanup_auto_enabled'):
                if k in kwargs and kwargs[k] is not None:
                    raw_val = kwargs[k]
                    if isinstance(raw_val, bool):
                        new_val = raw_val
                    else:
                        new_val = str(raw_val).strip().lower() in ('true', '1', 'yes', 'on')
                    if new_val != self.config[k]:
                        self.config[k] = new_val
                        changes.append(f'{k}={new_val}')

        if changes:
            self._log(f'[配置更新] {", ".join(changes)}')
            self.check_workers()
        else:
            self._log('[配置更新] 无变化')

    # ---- 状态查询 ----

    def get_status(self):
        """返回完整状态 (供 Web API 使用)"""
        with self._config_lock:
            cfg = dict(self.config)
        total_capacity = len(cfg['hf_urls']) * cfg['max_slots']

        total_free = sum(w['free_slots'] for w in self.worker_status if w['online'])

        uptime = None
        if self.start_time and self.running:
            uptime = int(time.time() - self.start_time)

        return {
            'running': self.running,
            'status_text': self.status_text,
            'stats': dict(self.stats),
            'workers': list(self.worker_status),
            'total_triggered': self.total_triggered,
            'total_capacity': total_capacity,
            'total_free_slots': total_free,
            'last_trigger_time': self.last_trigger_time,
            'uptime': uptime,
            'last_error': self.last_error,
            'num_workers': len(cfg['hf_urls']),
            'max_slots': cfg['max_slots'],
            'check_interval': cfg['check_interval'],
            'stuck_timeout': cfg['stuck_timeout'],
            'cleanup_interval': cfg['cleanup_interval'],
            'cleanup_reset_failed': cfg['cleanup_reset_failed'],
            'cleanup_auto_enabled': cfg['cleanup_auto_enabled'],
        }

    def get_logs(self, count=200):
        """返回最近 N 条日志"""
        items = list(self.logs)
        return items[-count:] if count < len(items) else items

    # ---- 生命周期控制 ----

    def start(self):
        if self.running:
            self._log('[警告] 调度器已在运行中')
            return
        self._stop_event.clear()
        self.running = True
        self.start_time = time.time()
        self.status_text = '启动中...'
        self.last_error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._log('>>> 调度器已启动 (后台线程)')

    def stop(self):
        if not self.running:
            self._log('[提示] 调度器未在运行')
            return
        self._stop_event.set()
        self.running = False
        self.status_text = '已停止'
        self._log('>>> 调度器已停止')

    # ---- 手动操作 ----

    def check_workers(self):
        """检查所有 Worker 健康状态"""
        with self._config_lock:
            cfg = dict(self.config)
        results = []
        for url in cfg['hf_urls']:
            online, free, total, wid = check_worker_health(url)
            results.append({
                'url': url,
                'online': online,
                'free_slots': free,
                'total_slots': total,
                'worker_id': wid,
            })
        self.worker_status = results
        return results

    def trigger_worker_now(self, worker_index=0):
        """手动触发指定 Worker"""
        with self._config_lock:
            cfg = dict(self.config)
        if not cfg['hf_urls']:
            return False, '未配置 HF Space URL'
        if worker_index < 0 or worker_index >= len(cfg['hf_urls']):
            worker_index = 0
        url = cfg['hf_urls'][worker_index]
        self._log(f'>>> 手动触发 Worker [{worker_index + 1}]: {url}')
        ok, msg, free = trigger_worker(url)
        self._log(f'    结果: {msg}')
        if ok:
            self.total_triggered += 1
            self.last_trigger_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return ok, msg

    def reset_stuck_now(self):
        """手动重置卡住的任务 (仅 processing 超时)"""
        with self._config_lock:
            cfg = dict(self.config)
        self._log('>>> 手动重置卡住任务...')
        ok = reset_stuck_jobs(cfg['dsn'], cfg['stuck_timeout'])
        if ok:
            self._log('    [OK] 卡住任务已重置为 pending')
        else:
            self._log('    [失败] 重置失败')
        return ok

    def run_cleanup_now(self):
        """手动运行完整清理 (整合 cleanup.py 功能)"""
        with self._config_lock:
            cfg = dict(self.config)

        import psycopg2
        from psycopg2 import sql as pg_sql

        self._log('>>> 开始手动清理...')
        self._log(f'    超时: {cfg["stuck_timeout"]} 分钟')
        self._log(f'    重置 failed: {"是" if cfg["cleanup_reset_failed"] else "否"}')

        result = {'ok': False, 'before': {}, 'after': {}, 'reset_processing': 0, 'reset_failed': 0}
        conn = None
        try:
            conn = psycopg2.connect(cfg['dsn'])
            with conn.cursor() as cur:
                cur.execute(
                    pg_sql.SQL('SELECT upload_status, COUNT(*) FROM {} GROUP BY upload_status')
                    .format(pg_sql.Identifier(CHAPTERS_TABLE))
                )
                before = dict(cur.fetchall())
                result['before'] = before
                self._log(f'    清理前: {before}')

                cur.execute(
                    pg_sql.SQL(
                        'UPDATE {} SET upload_status = %s, worker_id = NULL, claimed_at = NULL '
                        'WHERE upload_status = %s AND claimed_at < NOW() - INTERVAL %s'
                    ).format(pg_sql.Identifier(CHAPTERS_TABLE)),
                    ('pending', 'processing', f'{cfg["stuck_timeout"]} minutes')
                )
                reset_processing = cur.rowcount
                conn.commit()
                self._log(f'    [OK] 重置了 {reset_processing} 个卡住的 processing 章节')
                result['reset_processing'] = reset_processing

                reset_failed = 0
                if cfg['cleanup_reset_failed']:
                    cur.execute(
                        pg_sql.SQL(
                            'UPDATE {} SET upload_status = %s, worker_id = NULL, claimed_at = NULL, '
                            'error_message = NULL WHERE upload_status = %s'
                        ).format(pg_sql.Identifier(CHAPTERS_TABLE)),
                        ('pending', 'failed')
                    )
                    reset_failed = cur.rowcount
                    conn.commit()
                    self._log(f'    [OK] 重置了 {reset_failed} 个 failed 章节')
                    result['reset_failed'] = reset_failed

                cur.execute(
                    pg_sql.SQL('SELECT upload_status, COUNT(*) FROM {} GROUP BY upload_status')
                    .format(pg_sql.Identifier(CHAPTERS_TABLE))
                )
                after = dict(cur.fetchall())
                result['after'] = after
                self._log(f'    清理后: {after}')

            result['ok'] = True
            self._log('>>> 清理完成')

        except Exception as e:
            self._log(f'    [错误] 清理失败: {e}')
            if conn:
                conn.rollback()
            result['error'] = str(e)
        finally:
            if conn:
                conn.close()

        try:
            self.stats = get_stats(cfg['dsn'])
        except Exception:
            pass

        return result

    # ---- 主循环 (在后台线程中运行) ----

    def _run(self):
        try:
            self._main_loop()
        except Exception as e:
            self.last_error = str(e)
            self.status_text = f'错误: {e}'
            self._log(f'[错误] 调度器异常退出: {e}')
            import traceback
            self._log(traceback.format_exc())
        finally:
            self.running = False

    def _main_loop(self):
        with self._config_lock:
            cfg = dict(self.config)

        if not cfg['hf_urls'] or any(u.startswith('https://YOUR_') for u in cfg['hf_urls']):
            self._log('[错误] 请配置 HF_SPACE_URLS!')
            self._log('  正确格式: https://用户名-空间名.hf.space')
            self._log('  例如:     https://r777r7-t1.hf.space')
            self._log('  ❌ 不要用: https://huggingface.co/spaces/用户名/空间名')
            self.status_text = '配置错误: 未设置 HF Space URL'
            return

        for url in cfg['hf_urls']:
            if 'huggingface.co/spaces/' in url:
                self._log(f'[警告] URL 格式可能不正确: {url}')
                self._log(f'  应使用 API 地址: https://用户名-空间名.hf.space')

        total_capacity = len(cfg['hf_urls']) * cfg['max_slots']

        sep = '=' * 60
        self._log(sep)
        self._log('  有声书 Serverless 调度器 (双核并行)')
        self._log(sep)
        self._log(f'  HF Space URL:  {cfg["hf_urls"][0]}')
        self._log(f'  Worker 数量:   {len(cfg["hf_urls"])}')
        if len(cfg['hf_urls']) > 1:
            for i, u in enumerate(cfg['hf_urls']):
                self._log(f'    [{i + 1}] {u}')
        self._log(f'  每Worker槽位:  {cfg["max_slots"]} (vCPU)')
        self._log(f'  总并行能力:    {total_capacity} 个任务同时处理')
        self._log(f'  检查间隔:      {cfg["check_interval"]}s')
        self._log(f'  卡住超时:      {cfg["stuck_timeout"]} 分钟')
        self._log(sep)

        self._log('>>> 测试 PostgreSQL 连接...')
        try:
            self.stats = get_stats(cfg['dsn'])
            self._log('[OK] PG 连接成功')
            self._log(f'     pending={self.stats["pending"]}  processing={self.stats["processing"]}  '
                      f'uploaded={self.stats["uploaded"]}  failed={self.stats["failed"]}')
        except Exception as e:
            self._log(f'[错误] PG 连接失败: {e}')
            self.status_text = 'PG 连接失败'
            self.last_error = str(e)
            return

        self._log('>>> 检查 HF Space 健康...')
        self.check_workers()
        for i, w in enumerate(self.worker_status):
            if w['online']:
                self._log(f'  Worker [{i + 1}] ✅ 在线  空闲: {w["free_slots"]}/{w["total_slots"]}  '
                          f'ID: {w["worker_id"]}')
            else:
                self._log(f'  Worker [{i + 1}] ❌ 离线 (冷启动?)  {w["url"]}')

        self._log(sep)
        self._log('>>> 调度器启动, 开始监控...')
        self._log(f'    策略: 有空闲槽位时触发, 填充空闲槽位')
        self.status_text = '运行中'

        last_reset_time = 0
        last_stats_log = 0
        last_health_check = 0

        while not self._stop_event.is_set():
            try:
                with self._config_lock:
                    cfg = dict(self.config)
                total_capacity = len(cfg['hf_urls']) * cfg['max_slots']

                now = time.time()

                # 定期清理
                if cfg['cleanup_auto_enabled'] and now - last_reset_time > cfg['cleanup_interval']:
                    self._log('>>> 自动清理...')
                    self.run_cleanup_now()
                    last_reset_time = now

                # 获取数据库状态
                self.stats = get_stats(cfg['dsn'])
                pending = self.stats['pending']
                processing = self.stats['processing']
                uploaded = self.stats['uploaded']
                failed = self.stats['failed']

                if now - last_stats_log > 60:
                    self._log(f'  [状态] pending={pending}  processing={processing}  '
                              f'uploaded={uploaded}  failed={failed}  '
                              f'(容量: {total_capacity})')
                    last_stats_log = now

                # 检查 Worker 健康
                if now - last_health_check > 10:
                    self.check_workers()
                    last_health_check = now

                total_free = sum(w['free_slots'] for w in self.worker_status if w['online'])
                online_workers = [w for w in self.worker_status if w['online'] and w['free_slots'] > 0]

                if pending == 0:
                    self.status_text = f'运行中 (空闲, DB processing={processing})'
                    self._stop_event.wait(timeout=cfg['check_interval'])
                    continue

                if not self.worker_status or not any(w['online'] for w in self.worker_status):
                    self.status_text = f'运行中 (Worker 离线, 等待...)'
                    self._log(f'  [警告] 所有 Worker 离线, 30s 后重试')
                    self._stop_event.wait(timeout=30)
                    continue

                if total_free == 0:
                    self.status_text = f'运行中 (Worker 满载, DB processing={processing})'
                    self._stop_event.wait(timeout=5)
                    continue

                self.status_text = f'运行中 (空闲槽位={total_free}, pending={pending})'

                for w in online_workers:
                    if pending <= 0:
                        break

                    self._log(f'>>> 触发 Worker: {w["url"]}')
                    self._log(f'    pending={pending}  Worker空闲={w["free_slots"]}/{w["total_slots"]}  '
                              f'(DB processing={processing}, 与本调度器无关)')

                    ok, msg, free_after = trigger_worker(w['url'])
                    self._log(f'    结果: {msg}')

                    if ok:
                        self.total_triggered += 1
                        self.last_trigger_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        self._stop_event.wait(timeout=3)
                        break
                    elif '满' in msg or 'busy' in msg:
                        self._stop_event.wait(timeout=2)
                        continue
                    else:
                        self._log(f'    [警告] 触发失败, {cfg["check_interval"]}s 后重试')
                        self._stop_event.wait(timeout=cfg['check_interval'])
                        break

                self._stop_event.wait(timeout=5)

            except Exception as e:
                self._log(f'[错误] 调度器循环异常: {e}')
                import traceback
                self._log(traceback.format_exc())
                self.last_error = str(e)
                self._stop_event.wait(timeout=30)

        self._log(sep)
        self._log('>>> 调度器停止')
        self._log(f'    总触发次数:  {self.total_triggered}')
        self._log(sep)


# ============================================================
# 独立运行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='有声书 Serverless 调度器 (VPS 端, 双核并行)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--dsn', default=DEFAULT_DSN, help='PostgreSQL 连接串')
    parser.add_argument('--hf-urls', default=DEFAULT_HF_URLS,
                        help='HF Space URL (逗号分隔多个)')
    parser.add_argument('--max-slots', type=int, default=DEFAULT_MAX_SLOTS,
                        help=f'每个 Worker 的并行槽位数 (默认 {DEFAULT_MAX_SLOTS})')
    parser.add_argument('--check-interval', type=int, default=DEFAULT_CHECK_INTERVAL,
                        help=f'检查间隔秒数 (默认 {DEFAULT_CHECK_INTERVAL})')
    parser.add_argument('--stuck-timeout', type=int, default=DEFAULT_STUCK_TIMEOUT_M,
                        help=f'卡住超时分钟数 (默认 {DEFAULT_STUCK_TIMEOUT_M})')

    args = parser.parse_args()

    hf_urls = [u.strip() for u in args.hf_urls.split(',') if u.strip()]

    scheduler = Scheduler(
        dsn=args.dsn,
        hf_urls=hf_urls,
        max_slots=args.max_slots,
        check_interval=args.check_interval,
        stuck_timeout=args.stuck_timeout,
    )
    scheduler.start()

    try:
        while scheduler.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        scheduler.stop()


if __name__ == '__main__':
    main()
