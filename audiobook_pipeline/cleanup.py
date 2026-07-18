#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPS 定时清理脚本: 重置卡住的 processing 章节

默认行为:
  - 只重置卡住超过超时时间的 processing 章节 (→ pending)
  - 不动 failed 章节 (需手动用 --reset-failed 重置)

用法:
    python3 cleanup.py                        # 仅重置卡住的 processing
    python3 cleanup.py --timeout 24           # 指定超时 24 小时
    python3 cleanup.py --reset-failed         # 同时重置所有 failed
    # crontab: 0 3 * * * cd /path && python3 cleanup.py >> /tmp/cleanup.log 2>&1
"""

import os
import sys
import argparse
from datetime import datetime

DEFAULT_DSN = os.environ.get('POSTGRES_DSN', 'postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook')
CHAPTERS_TABLE = 'audiobook_chapters'
DEFAULT_TIMEOUT_HOURS = 24

def main():
    parser = argparse.ArgumentParser(description='清理卡住的 processing 章节 (默认不动 failed)')
    parser.add_argument('--dsn', default=DEFAULT_DSN, help='PostgreSQL 连接串')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_HOURS, help=f'超时小时数 (默认 {DEFAULT_TIMEOUT_HOURS})')
    parser.add_argument('--reset-failed', action='store_true', help='同时重置所有 failed 章节 (默认不重置)')
    args = parser.parse_args()

    import psycopg2
    from psycopg2 import sql

    print(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] 开始清理...')
    print(f'  超时: {args.timeout} 小时')
    print(f'  重置 failed: {"是" if args.reset_failed else "否 (仅手动 --reset-failed)"}')

    conn = None
    try:
        conn = psycopg2.connect(args.dsn)
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL('SELECT upload_status, COUNT(*) FROM {} GROUP BY upload_status')
                .format(sql.Identifier(CHAPTERS_TABLE))
            )
            before = dict(cur.fetchall())
            print(f'  清理前状态: {before}')

            # 1. 重置卡住的 processing 章节
            cur.execute(
                sql.SQL(
                    'UPDATE {} SET upload_status = %s, worker_id = NULL, claimed_at = NULL '
                    'WHERE upload_status = %s AND claimed_at < NOW() - INTERVAL %s'
                ).format(sql.Identifier(CHAPTERS_TABLE)),
                ('pending', 'processing', f'{args.timeout} hours')
            )
            reset_count = cur.rowcount
            conn.commit()
            print(f'  [OK] 重置了 {reset_count} 个卡住的 processing 章节')

            # 2. 重置 failed 章节 (仅在 --reset-failed 时)
            failed_count = 0
            if args.reset_failed:
                cur.execute(
                    sql.SQL(
                        'UPDATE {} SET upload_status = %s, worker_id = NULL, claimed_at = NULL, '
                        'error_message = NULL WHERE upload_status = %s'
                    ).format(sql.Identifier(CHAPTERS_TABLE)),
                    ('pending', 'failed')
                )
                failed_count = cur.rowcount
                conn.commit()
                print(f'  [OK] 重置了 {failed_count} 个 failed 章节')
            else:
                failed_total = before.get('failed', 0)
                if failed_total > 0:
                    print(f'  [跳过] {failed_total} 个 failed 章节 (加 --reset-failed 可重置)')

            cur.execute(
                sql.SQL('SELECT upload_status, COUNT(*) FROM {} GROUP BY upload_status')
                .format(sql.Identifier(CHAPTERS_TABLE))
            )
            after = dict(cur.fetchall())
            print(f'  清理后状态: {after}')

        print(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] 清理完成')

    except Exception as e:
        print(f'[错误] {e}')
        if conn:
            conn.rollback()
        sys.exit(1)
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    main()
