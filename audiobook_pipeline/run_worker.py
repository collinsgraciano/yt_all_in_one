#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPS 直接运行 Worker — 多Bot轮换模式

在 VPS 上直接运行, 无需 HF Space 或 Colab。
支持多线程并行处理, 自动重置卡住任务。

使用方法:
    # 设置环境变量 (或通过命令行参数)
    export POSTGRES_DSN="postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook"
    export BOT_TOKENS="token1,token2,token3"
    export CHAT_ID="7485554965"

    # 单线程运行
    python3 run_worker.py

    # 多线程运行
    python3 run_worker.py --workers 2

    # 跳过降噪
    python3 run_worker.py --no-df

    # 限制处理数量
    python3 run_worker.py --max-chapters 100
"""

import os
import sys
import time
import uuid
import argparse

# 将当前目录加入 path, 以便 import worker
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import worker


def main():
    parser = argparse.ArgumentParser(
        description='有声书 Worker (多Bot轮换, VPS直接运行)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--dsn', default=None, help='PostgreSQL 连接串')
    parser.add_argument('--bot-tokens', default=None, help='多Bot Token (逗号分隔)')
    parser.add_argument('--chat-id', default=None, help='Telegram Chat ID')
    parser.add_argument('--api-base', default=None, help='Telegram API 基地址')
    parser.add_argument('--no-df', action='store_true', help='跳过 DeepFilter 降噪')
    parser.add_argument('--workers', type=int, default=1, help='并发线程数 (默认1)')
    parser.add_argument('--max-chapters', type=int, default=0, help='最多处理章节数 (0=不限)')
    parser.add_argument('--upload-delay', type=float, default=1.0, help='上传间隔秒数')
    parser.add_argument('--stuck-timeout', type=int, default=24, help='卡住超时小时数')
    args = parser.parse_args()

    # 命令行参数覆盖环境变量
    if args.dsn:
        worker.POSTGRES_DSN = args.dsn
    if args.bot_tokens:
        worker.BOT_TOKENS = [t.strip() for t in args.bot_tokens.split(',') if t.strip()]
    if args.chat_id:
        worker.CHAT_ID = args.chat_id
    if args.api_base:
        worker.TELEGRAM_API_BASE = args.api_base.rstrip('/')
    if args.no_df:
        worker._use_df = False
        worker._init_done = True  # 跳过 DF 初始化

    # 初始化 Worker
    worker.init_worker()

    if not worker.POSTGRES_DSN:
        print('[错误] POSTGRES_DSN 未设置!')
        sys.exit(1)
    if not worker.BOT_TOKENS:
        print('[错误] BOT_TOKENS 未设置!')
        sys.exit(1)
    if not worker.CHAT_ID:
        print('[错误] CHAT_ID 未设置!')
        sys.exit(1)

    sep = '=' * 60
    print(sep)
    print('  有声书 Worker (多Bot轮换)')
    print(sep)
    print(f'  Worker ID:     {worker.WORKER_ID}')
    print(f'  Bot 数量:      {len(worker.BOT_TOKENS)}')
    print(f'  DeepFilter:    {"禁用" if args.no_df else "启用" if worker._use_df else "不可用"}')
    print(f'  线程数:        {args.workers}')
    print(f'  卡住超时:      {args.stuck_timeout} 小时')
    if args.max_chapters > 0:
        print(f'  最多处理:      {args.max_chapters} 章节')
    print(sep)

    # 重置卡住任务
    from worker import safe_pg_execute, CHAPTERS_TABLE, BOOKS_TABLE
    print('>>> 重置卡住任务...')
    safe_pg_execute(
        f'UPDATE {CHAPTERS_TABLE} SET upload_status = %s, worker_id = NULL, claimed_at = NULL '
        f'WHERE upload_status = %s AND claimed_at < NOW() - INTERVAL %s',
        ('pending', 'processing', f'{args.stuck_timeout} hours')
    )

    # 查看状态
    stats = worker.get_db_stats()
    print(f'\n=== 数据库状态 ===')
    print(f'  待处理:     {stats["pending"]}')
    print(f'  处理中:     {stats["processing"]}')
    print(f'  已上传:     {stats["uploaded"]}')
    print(f'  已失败:     {stats["failed"]}')
    print(f'  书已完成:   {stats["books_success"]}/{stats["books_total"]}')
    print()

    if stats['pending'] == 0:
        print('[OK] 没有待处理的章节')
        return

    # 主处理循环
    print(f'>>> 开始处理 (待处理: {stats["pending"]})\n')

    import threading
    from concurrent.futures import ThreadPoolExecutor

    processed = {'count': 0}
    lock = threading.Lock()

    def worker_loop():
        while True:
            with lock:
                if args.max_chapters > 0 and processed['count'] >= args.max_chapters:
                    return
            result = worker.run_one()
            status = result.get('status', '?')
            if status == 'no_task':
                return
            with lock:
                processed['count'] += 1
            time.sleep(args.upload_delay)

    try:
        if args.workers <= 1:
            worker_loop()
        else:
            with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix='W') as executor:
                futures = [executor.submit(worker_loop) for _ in range(args.workers)]
                for f in futures:
                    f.result()
    except KeyboardInterrupt:
        print('\n\n[INFO] 用户中断 (Ctrl+C)')

    # 最终报告
    stats = worker.get_db_stats()
    print(f'\n{sep}')
    print(f'>>> 处理完成!')
    print(f'  待处理:     {stats["pending"]}')
    print(f'  处理中:     {stats["processing"]}')
    print(f'  已上传:     {stats["uploaded"]}')
    print(f'  已失败:     {stats["failed"]}')
    print(f'  书已完成:   {stats["books_success"]}/{stats["books_total"]}')
    print(sep)


if __name__ == '__main__':
    main()
