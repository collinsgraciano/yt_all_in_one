#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 Telegram 音频下载 (多Bot模式) — 命令行版

从数据库中获取已上传章节的 telegram_file_id 和 telegram_bot_id，
用对应的 Bot Token 下载音频文件，验证 file_id 是否能正确下载。

⚠️ 重要: Telegram 的 file_id 与 Bot 绑定。Bot A 上传的文件，
   只能用 Bot A 的 Token 下载。数据库中的 telegram_bot_id 记录了
   上传时使用的 Bot 编号（对应 BOT_TOKENS 数组索引，从0开始）。

用法:
    # 基本测试 (随机抽样10个章节，完整下载+验证)
    python test_download.py \\
        --dsn "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook" \\
        --tokens "111111:AAAxxx,222222:BBByyy,333333:CCCzzz" \\
        --sample 10

    # 只检查 getFile API (不下载文件，速度快)
    python test_download.py \\
        --dsn "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook" \\
        --tokens "111111:AAAxxx,222222:BBByyy" \\
        --sample 50 \\
        --check-only

    # 测试指定 file_id
    python test_download.py \\
        --dsn "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook" \\
        --tokens "111111:AAAxxx,222222:BBByyy" \\
        --file-id "AwACAgUAAxx..." \\
        --bot-id 0

    # 测试所有已上传章节 (不限抽样数量)
    python test_download.py \\
        --dsn "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook" \\
        --tokens "111111:AAAxxx,222222:BBByyy" \\
        --sample 0

依赖:
    pip install psycopg2-binary requests
    # ffprobe 可选 (用于音频完整性验证): apt install ffmpeg
"""

import os
import sys
import json
import time
import argparse
import subprocess
import tempfile
from datetime import datetime


# ============================================================
# Bot Token User ID 提取 (关键: 不受 Token 顺序/增删影响)
# ============================================================
def extract_bot_user_id(token):
    """从 Bot Token 中提取 Bot 的永久 Telegram User ID

    Token 格式: {bot_user_id}:{secret}  例如: 7485554965:AAHxxx...
    这个 user_id 是 Bot 的永久 ID, 即使 BOT_TOKENS 列表重新排序、
    增删 Token, 也能通过 user_id 找到正确的 Token。
    """
    try:
        return int(token.split(':')[0])
    except (ValueError, IndexError):
        return None


def build_user_id_to_token_map(bot_tokens):
    """构建 {user_id: token_index} 映射表"""
    mapping = {}
    for i, token in enumerate(bot_tokens):
        uid = extract_bot_user_id(token)
        if uid is not None:
            mapping[uid] = i
    return mapping

# ============================================================
# 颜色输出
# ============================================================
class C:
    OK    = '\033[92m'  # 绿
    FAIL  = '\033[91m'  # 红
    WARN  = '\033[93m'  # 黄
    CYAN  = '\033[96m'  # 青
    BOLD  = '\033[1m'
    END   = '\033[0m'

def ok(msg):   print(f"{C.OK}✅ {msg}{C.END}")
def fail(msg): print(f"{C.FAIL}❌ {msg}{C.END}")
def warn(msg): print(f"{C.WARN}⚠️  {msg}{C.END}")
def info(msg): print(f"{C.CYAN}ℹ️  {msg}{C.END}")
def header(msg): print(f"\n{C.BOLD}{'='*60}\n  {msg}\n{'='*60}{C.END}")


# ============================================================
# 数据库操作
# ============================================================
def db_query(dsn, query, params=None):
    """查询数据库，返回字典列表"""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()
    finally:
        conn.close()


def test_db_connection(dsn):
    """测试数据库连接"""
    try:
        rows = db_query(dsn, 'SELECT 1 AS test')
        return True, '数据库连接成功'
    except Exception as e:
        return False, str(e)


def get_uploaded_stats(dsn):
    """获取已上传章节统计"""
    return db_query(dsn, """
        SELECT
            COUNT(*) AS total_uploaded,
            COUNT(telegram_file_id) AS has_file_id,
            COUNT(telegram_message_id) AS has_message_id,
            COUNT(telegram_bot_id) AS has_bot_id,
            COUNT(telegram_bot_user_id) AS has_bot_user_id,
            COUNT(*) FILTER (WHERE telegram_bot_id IS NULL) AS no_bot_id,
            COUNT(*) FILTER (WHERE telegram_bot_user_id IS NULL) AS no_bot_user_id
        FROM audiobook_chapters
        WHERE upload_status = 'uploaded'
    """)[0]


def get_sample_chapters(dsn, sample_size):
    """随机抽样已上传章节"""
    if sample_size and sample_size > 0:
        return db_query(dsn, """
            SELECT book_id, chapter_id, book_name, chapter_name,
                   telegram_file_id, telegram_message_id, telegram_bot_id,
                   telegram_bot_user_id, uploaded_at
            FROM audiobook_chapters
            WHERE upload_status = 'uploaded'
              AND telegram_file_id IS NOT NULL
            ORDER BY RANDOM()
            LIMIT %s
        """, (sample_size,))
    else:
        return db_query(dsn, """
            SELECT book_id, chapter_id, book_name, chapter_name,
                   telegram_file_id, telegram_message_id, telegram_bot_id,
                   telegram_bot_user_id, uploaded_at
            FROM audiobook_chapters
            WHERE upload_status = 'uploaded'
              AND telegram_file_id IS NOT NULL
            ORDER BY book_id, chapter_id
        """)


# ============================================================
# Telegram API 操作
# ============================================================
def tg_get_file(file_id, bot_token, api_base=None):
    """调用 Telegram Bot API getFile
    返回: (成功?, 文件信息或错误描述)
    """
    import requests
    base = api_base or 'https://api.telegram.org'
    url = f'{base}/bot{bot_token}/getFile'
    try:
        resp = requests.get(url, params={'file_id': file_id}, timeout=30)
        data = resp.json()
        if resp.status_code == 200 and data.get('ok'):
            return True, data['result']
        else:
            return False, data.get('description', f'HTTP {resp.status_code}')
    except requests.exceptions.Timeout:
        return False, '请求超时'
    except Exception as e:
        return False, str(e)


def tg_download_file(file_path, save_path, bot_token, api_base=None):
    """从 Telegram 下载文件
    返回: (成功?, 文件大小(bytes)或错误描述)
    """
    import requests
    base = api_base or 'https://api.telegram.org'
    url = f'{base}/file/bot{bot_token}/{file_path}'
    try:
        resp = requests.get(url, stream=True, timeout=120)
        if resp.status_code != 200:
            return False, f'HTTP {resp.status_code}'

        downloaded = 0
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)

        if downloaded == 0:
            return False, '下载文件为空 (0 bytes)'

        return True, downloaded
    except Exception as e:
        return False, str(e)


def verify_audio(file_path):
    """用 ffprobe 验证音频文件完整性
    返回: (成功?, 音频信息或错误描述)
    """
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_format', '-show_streams',
             '-of', 'json', file_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return False, f'ffprobe 失败: {result.stderr.strip()[:200]}'

        info = json.loads(result.stdout)
        streams = info.get('streams', [])
        if not any(s.get('codec_type') == 'audio' for s in streams):
            return False, '文件无音频流'

        fmt = info.get('format', {})
        duration = fmt.get('duration')
        if duration and float(duration) < 0.1:
            return False, f'音频时长异常: {duration}s'

        audio_stream = next(s for s in streams if s.get('codec_type') == 'audio')
        size_kb = os.path.getsize(file_path) // 1024
        return True, {
            'codec': audio_stream.get('codec_name', '?'),
            'duration': f'{float(duration):.1f}s' if duration else '?',
            'sample_rate': f'{audio_stream.get("sample_rate", "?")}Hz',
            'bit_rate': f'{int(fmt.get("bit_rate", 0)) // 1000}kbps',
            'size_kb': size_kb,
        }
    except FileNotFoundError:
        return False, 'ffprobe 未安装 (apt install ffmpeg)'
    except Exception as e:
        return False, f'验证异常: {e}'


def find_correct_bot(file_id, bot_tokens, api_base=None, known_bot_id=None, known_bot_user_id=None):
    """尝试用正确的 Bot Token 获取文件信息

    策略 (按可靠性排序):
    1. 如果有 known_bot_user_id, 通过 user_id 匹配 Token (最可靠, 不受顺序影响)
    2. 如果有 known_bot_id 且在范围内, 先用对应 Token 尝试 (快速路径)
    3. 尝试所有 Token (保底)

    返回: (成功?, 文件信息或错误, 实际 bot_id)
    """
    tried_indices = set()

    # 策略1: 通过 bot_user_id 匹配 (最可靠)
    if known_bot_user_id is not None:
        uid_map = build_user_id_to_token_map(bot_tokens)
        matched_idx = uid_map.get(known_bot_user_id)
        if matched_idx is not None:
            ok_flag, info = tg_get_file(file_id, bot_tokens[matched_idx], api_base)
            if ok_flag:
                return True, info, matched_idx
            tried_indices.add(matched_idx)
        else:
            warn(f'  DB记录 bot_user_id={known_bot_user_id} 但当前 BOT_TOKENS 中无此 Bot (可能已删除该Token)')

    # 策略2: 通过 bot_id 索引尝试 (快速路径)
    if known_bot_id is not None and 0 <= known_bot_id < len(bot_tokens) and known_bot_id not in tried_indices:
        ok_flag, info = tg_get_file(file_id, bot_tokens[known_bot_id], api_base)
        if ok_flag:
            return True, info, known_bot_id
        tried_indices.add(known_bot_id)
    elif known_bot_id is not None and known_bot_id >= len(bot_tokens):
        warn(f'  DB记录 bot_id={known_bot_id} 超出范围 (只有 {len(bot_tokens)} 个Token)，尝试所有Token')

    # 策略3: 尝试所有 Token (保底)
    for i, token in enumerate(bot_tokens):
        if i in tried_indices:
            continue  # 已尝试过
        ok_flag, info = tg_get_file(file_id, token, api_base)
        if ok_flag:
            return True, info, i

    return False, '所有 Token 均无法获取此 file_id', None


# ============================================================
# 主测试逻辑
# ============================================================
def run_test(dsn, bot_tokens, sample_size, check_only=False,
             api_base=None, download_dir=None):
    """运行完整测试"""
    header('测试 Telegram 音频下载 (多Bot模式)')

    print(f'\n  数据库:    {dsn.split("@")[-1] if "@" in dsn else dsn}')
    print(f'  Bot 数量:  {len(bot_tokens)}')
    for i, t in enumerate(bot_tokens):
        print(f'    Bot{i}: {t[:15]}...{t[-6:]}')
    print(f'  模式:      {"仅检查 getFile" if check_only else "完整下载+验证"}')
    print(f'  抽样数量:  {sample_size if sample_size > 0 else "全部"}')

    # --- 1. 测试数据库连接 ---
    print()
    info('步骤 1/4: 测试数据库连接...')
    ok_flag, msg = test_db_connection(dsn)
    if not ok_flag:
        fail(f'数据库连接失败: {msg}')
        return 1
    ok('数据库连接成功')

    # --- 2. 查询统计 ---
    info('步骤 2/4: 查询已上传章节统计...')
    stats = get_uploaded_stats(dsn)

    print(f'\n  已上传章节总数:       {stats["total_uploaded"]}')
    print(f'  有 telegram_file_id:  {stats["has_file_id"]}')
    print(f'  有 message_id:        {stats["has_message_id"]}')
    print(f'  有 telegram_bot_id:   {stats["has_bot_id"]}')
    print(f'  有 bot_user_id:       {stats["has_bot_user_id"]}')
    print(f'  无 bot_id (旧数据):   {stats["no_bot_id"]}')
    print(f'  无 bot_user_id:       {stats["no_bot_user_id"]}')

    if stats['has_file_id'] == 0:
        warn('没有找到已上传的章节! 请先运行上传 Worker')
        return 1

    # --- 3. 抽样 ---
    info('步骤 3/4: 抽样已上传章节...')
    chapters = get_sample_chapters(dsn, sample_size)
    print(f'\n  抽取了 {len(chapters)} 个章节进行测试\n')

    # --- 4. 逐个测试 ---
    info('步骤 4/4: 开始下载测试...\n')

    results = []
    use_temp_dir = download_dir is None
    if use_temp_dir:
        download_dir = tempfile.mkdtemp(prefix='tg_test_')

    for idx, ch in enumerate(chapters, 1):
        book_name = ch['book_name'] or '(未知)'
        chapter_name = ch['chapter_name'] or '(未知)'
        file_id = ch['telegram_file_id']
        bot_id = ch['telegram_bot_id']
        bot_user_id = ch['telegram_bot_user_id']

        file_id_display = file_id[:30] + '...' if file_id and len(file_id) > 30 else file_id
        bot_display = f'Bot{bot_id}' if bot_id is not None else 'Bot?(NULL)'
        uid_display = f'uid:{bot_user_id}' if bot_user_id else 'uid:NULL'

        print(f'  [{idx}/{len(chapters)}] {book_name} - {chapter_name}')
        print(f'         file_id: {file_id_display}')
        print(f'         bot_id:  {bot_display}  {uid_display}')

        result = {
            'index': idx,
            'book_name': book_name,
            'chapter_name': chapter_name,
            'book_id': ch['book_id'],
            'chapter_id': ch['chapter_id'],
            'file_id': file_id,
            'db_bot_id': bot_id,
            'db_bot_user_id': bot_user_id,
            'actual_bot_id': None,
            'getfile_ok': False,
            'getfile_error': '',
            'file_size_expected': 0,
            'download_ok': False,
            'download_size': 0,
            'download_error': '',
            'audio_ok': False,
            'audio_info': {},
            'audio_error': '',
        }

        # --- 步骤 A: getFile API ---
        ok_flag, info_data, actual_bot_id = find_correct_bot(
            file_id, bot_tokens, api_base,
            known_bot_id=bot_id, known_bot_user_id=bot_user_id
        )

        if not ok_flag:
            result['getfile_error'] = str(info_data)
            fail(f'    [1/3] getFile  ❌ {info_data}')
            if bot_user_id:
                uid_map = build_user_id_to_token_map(bot_tokens)
                if bot_user_id not in uid_map:
                    fail(f'           DB记录 bot_user_id={bot_user_id} 但当前 BOT_TOKENS 中无此 Bot')
                    fail(f'           可能原因: 该 Bot Token 已被删除')
            elif bot_id is not None and bot_id < len(bot_tokens):
                fail(f'           DB记录 Bot{bot_id}，但该Token无法获取此文件')
                fail(f'           可能原因: BOT_TOKENS 顺序与上传时不一致')
            results.append(result)
            print()
            time.sleep(0.3)
            continue

        result['getfile_ok'] = True
        result['actual_bot_id'] = actual_bot_id
        result['file_size_expected'] = info_data.get('file_size', 0)
        file_path = info_data.get('file_path', '')
        expected_kb = result['file_size_expected'] // 1024

        # 判断匹配方式
        actual_uid = extract_bot_user_id(bot_tokens[actual_bot_id])
        if bot_user_id and actual_uid == bot_user_id:
            ok(f'    [1/3] getFile  ✅ size={expected_kb}KB  Bot{actual_bot_id} ✅user_id匹配(uid:{actual_uid})')
        elif actual_bot_id == bot_id:
            ok(f'    [1/3] getFile  ✅ size={expected_kb}KB  Bot{actual_bot_id} ✅索引匹配')
        elif bot_id is None and bot_user_id is None:
            warn(f'    [1/3] getFile  ✅ size={expected_kb}KB  Bot{actual_bot_id} (DB无记录,自动匹配)')
        else:
            warn(f'    [1/3] getFile  ✅ size={expected_kb}KB  Bot{actual_bot_id} ⚠️DB记录Bot{bot_id}/uid:{bot_user_id}不匹配')

        if check_only:
            results.append(result)
            print()
            time.sleep(0.3)
            continue

        # --- 步骤 B: 下载文件 ---
        download_token = bot_tokens[actual_bot_id]
        save_name = f'test_{idx}_{ch["chapter_id"]}.mp3'
        save_path = os.path.join(download_dir, save_name)

        dl_ok, dl_info = tg_download_file(file_path, save_path, download_token, api_base)
        if not dl_ok:
            result['download_error'] = str(dl_info)
            fail(f'    [2/3] 下载     ❌ {dl_info}')
            fail(f'    [3/3] ffprobe  ⏭️  跳过')
            results.append(result)
            print()
            time.sleep(0.3)
            continue

        result['download_ok'] = True
        result['download_size'] = dl_info
        dl_kb = dl_info // 1024
        ok(f'    [2/3] 下载     ✅ {dl_kb}KB')

        # --- 步骤 C: ffprobe 验证 ---
        au_ok, au_info = verify_audio(save_path)
        if au_ok:
            result['audio_ok'] = True
            result['audio_info'] = au_info
            ok(f'    [3/3] ffprobe  ✅ {au_info["codec"]} {au_info["duration"]} '
               f'{au_info["sample_rate"]} {au_info["bit_rate"]}')
        else:
            result['audio_error'] = str(au_info)
            fail(f'    [3/3] ffprobe  ❌ {au_info}')

        # 清理下载文件
        if os.path.exists(save_path):
            os.remove(save_path)

        results.append(result)
        print()
        time.sleep(0.3)

    # --- 汇总 ---
    print_summary(results, bot_tokens)

    # 清理临时目录
    if use_temp_dir and os.path.exists(download_dir):
        import shutil
        shutil.rmtree(download_dir)

    # 返回码: 全部通过=0, 部分失败=1, 全部失败=2
    total = len(results)
    pass_count = sum(1 for r in results if r['getfile_ok'] and (check_only or r['audio_ok']))
    if pass_count == total:
        return 0
    elif pass_count == 0:
        return 2
    else:
        return 1


def print_summary(results, bot_tokens):
    """打印测试结果汇总"""
    header('测试结果汇总')

    total = len(results)
    getfile_pass = sum(1 for r in results if r['getfile_ok'])
    download_pass = sum(1 for r in results if r['download_ok'])
    audio_pass = sum(1 for r in results if r['audio_ok'])
    bot_mismatch = sum(1 for r in results
                       if r['getfile_ok']
                       and r['db_bot_id'] is not None
                       and r['actual_bot_id'] != r['db_bot_id'])
    bot_null = sum(1 for r in results if r['db_bot_id'] is None)
    uid_null = sum(1 for r in results if r.get('db_bot_user_id') is None)
    uid_match = sum(1 for r in results
                     if r['getfile_ok']
                     and r.get('db_bot_user_id')
                     and r['actual_bot_id'] is not None
                     and extract_bot_user_id(bot_tokens[r['actual_bot_id']]) == r.get('db_bot_user_id'))

    print(f'\n  测试章节总数:    {total}')
    print(f'  getFile 有效:    {getfile_pass}/{total}  ({getfile_pass*100//total if total else 0}%)')
    print(f'  下载成功:        {download_pass}/{total}  ({download_pass*100//total if total else 0}%)')
    print(f'  音频验证通过:    {audio_pass}/{total}  ({audio_pass*100//total if total else 0}%)')
    if uid_match > 0:
        print(f'  user_id 匹配:    {uid_match}/{total}  (可靠匹配, 不受Token顺序影响)')
    if bot_mismatch > 0:
        warn(f'  索引不匹配:      {bot_mismatch} (DB记录的bot_id与实际不符, 但user_id匹配则无碍)')
    if bot_null > 0:
        warn(f'  无 bot_id (旧数据): {bot_null}')
    if uid_null > 0:
        warn(f'  无 bot_user_id:     {uid_null} (旧数据, 建议重新上传)')

    print()
    if audio_pass == total:
        ok('全部测试通过! Telegram 中的音频文件可正常下载和播放。\n')
    elif audio_pass > 0 or getfile_pass > 0:
        warn('部分测试失败，请检查下方详细结果。\n')
    else:
        fail('全部测试失败! 请检查 Bot Token 列表和 file_id 是否正确。\n')

    # --- 详细表格 ---
    print(f'  {"#":>3}  {"书名":<20}  {"章节":<16}  {"DB Bot":<8}  {"实际Bot":<8}  {"DB uid":<12}  {"getFile":<8}  {"下载":<8}  {"ffprobe":<8}')
    print(f'  {"-"*3}  {"-"*20}  {"-"*16}  {"-"*8}  {"-"*8}  {"-"*12}  {"-"*8}  {"-"*8}  {"-"*8}')

    for r in results:
        gf = '✅' if r['getfile_ok'] else '❌'
        dl = '✅' if r['download_ok'] else ('❌' if r.get('download_error') else '-')
        au = '✅' if r['audio_ok'] else ('❌' if r.get('audio_error') else '-')
        db_bot = f'Bot{r["db_bot_id"]}' if r['db_bot_id'] is not None else 'NULL'
        act_bot = f'Bot{r["actual_bot_id"]}' if r['actual_bot_id'] is not None else '?'
        uid = f'{r.get("db_bot_user_id", "?")}' if r.get('db_bot_user_id') else '-'

        book_short = (r['book_name'] or '')[:20]
        ch_short = (r['chapter_name'] or '')[:16]
        print(f'  {r["index"]:3d}  {book_short:<20}  {ch_short:<16}  {db_bot:<8}  {act_bot:<8}  uid:{uid:<12}  {gf:<8}  {dl:<8}  {au:<8}')

    # --- 失败详情 ---
    failed = [r for r in results if not r['getfile_ok'] or not r.get('audio_ok')]
    if failed:
        print(f'\n  --- 失败详情 ---\n')
        for r in failed:
            print(f'  [{r["index"]}] {r["book_name"]} - {r["chapter_name"]}')
            print(f'       book_id:        {r["book_id"]}')
            print(f'       chapter_id:     {r["chapter_id"]}')
            print(f'       file_id:        {r["file_id"][:50]}...' if r['file_id'] and len(r['file_id']) > 50 else f'       file_id:        {r["file_id"]}')
            print(f'       DB bot_id:      {r["db_bot_id"]}')
            print(f'       DB bot_user_id: {r.get("db_bot_user_id")}')
            print(f'       实际 bot_id:     {r["actual_bot_id"]}')
            if not r['getfile_ok']:
                fail(f'       getFile失败: {r["getfile_error"]}')
                if r.get('db_bot_user_id'):
                    uid_map = build_user_id_to_token_map(bot_tokens)
                    if r['db_bot_user_id'] not in uid_map:
                        print(f'       💡 该 Bot Token 已从 BOT_TOKENS 中删除, 无法下载')
                    else:
                        print(f'       💡 Bot Token 存在但下载失败, 可能 file_id 已失效')
                else:
                    print(f'       💡 可能原因: BOT_TOKENS 顺序与上传时不一致, 或 file_id 已失效')
            elif not r.get('download_ok') and r.get('download_error'):
                fail(f'       下载失败: {r["download_error"]}')
            elif not r.get('audio_ok') and r.get('audio_error'):
                fail(f'       ffprobe失败: {r["audio_error"]}')
                print(f'       下载大小: {r.get("download_size", 0)//1024}KB')
            print()


def test_single_file_id(file_id, bot_id, bot_tokens, api_base=None):
    """测试指定的 file_id"""
    header(f'测试指定 file_id')

    print(f'\n  file_id: {file_id[:50]}...' if len(file_id) > 50 else f'\n  file_id: {file_id}')
    print(f'  指定 bot_id: {bot_id}')
    print(f'  Token 数量: {len(bot_tokens)}')

    if bot_id is not None and 0 <= bot_id < len(bot_tokens):
        print(f'  使用 Token: Bot{bot_id} = {bot_tokens[bot_id][:15]}...{bot_tokens[bot_id][-6:]}')
    else:
        warn(f'  bot_id={bot_id} 超出范围 (0~{len(bot_tokens)-1})，将尝试所有Token')

    # getFile
    print()
    ok_flag, info_data, actual_bot_id = find_correct_bot(
        file_id, bot_tokens, api_base, known_bot_id=bot_id
    )

    if not ok_flag:
        fail(f'getFile 失败: {info_data}')
        fail('所有 Token 都无法获取此 file_id')
        return 2

    file_path = info_data.get('file_path', '')
    file_size = info_data.get('file_size', 0)
    match_str = '✅匹配' if actual_bot_id == bot_id else f'⚠️实际Bot{actual_bot_id}'
    ok(f'getFile 成功! size={file_size//1024}KB  Bot{actual_bot_id} {match_str}')
    print(f'  file_path: {file_path}')

    # 下载
    download_token = bot_tokens[actual_bot_id]
    save_path = os.path.join(tempfile.gettempdir(), 'tg_manual_test.mp3')

    dl_ok, dl_info = tg_download_file(file_path, save_path, download_token, api_base)
    if not dl_ok:
        fail(f'下载失败: {dl_info}')
        return 2

    ok(f'下载成功! {dl_info//1024}KB')

    # ffprobe
    au_ok, au_info = verify_audio(save_path)
    if au_ok:
        ok(f'ffprobe 通过: {au_info}')
        if os.path.exists(save_path):
            os.remove(save_path)
        return 0
    else:
        fail(f'ffprobe 失败: {au_info}')
        if os.path.exists(save_path):
            os.remove(save_path)
        return 1


# ============================================================
# 命令行入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='测试 Telegram 音频下载 (多Bot模式)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 随机抽样10个章节测试
  %(prog)s --dsn "postgresql://user:pass@host:5432/audiobook" \\
           --tokens "token1,token2,token3" \\
           --sample 10

  # 只检查 getFile API (不下载)
  %(prog)s --dsn "..." --tokens "token1,token2" --sample 50 --check-only

  # 测试指定 file_id
  %(prog)s --dsn "..." --tokens "token1,token2" \\
           --file-id "AwACAgUAAxx..." --bot-id 0

  # 测试全部已上传章节
  %(prog)s --dsn "..." --tokens "token1,token2" --sample 0
        """)
    parser.add_argument('--dsn', required=True,
                        help='PostgreSQL 连接串 (postgresql://user:pass@host:port/db)')
    parser.add_argument('--tokens', required=True,
                        help='Bot Token 列表，逗号分隔 (顺序必须与上传时一致!)')
    parser.add_argument('--sample', type=int, default=10,
                        help='随机抽样数量 (0=全部, 默认10)')
    parser.add_argument('--check-only', action='store_true',
                        help='只检查 getFile API, 不下载文件')
    parser.add_argument('--file-id', default=None,
                        help='测试指定的 file_id (不查数据库)')
    parser.add_argument('--bot-id', type=int, default=None,
                        help='指定 file_id 的 bot_id (配合 --file-id 使用)')
    parser.add_argument('--api-base', default=None,
                        help='Telegram API 中继地址 (默认: https://api.telegram.org)')
    parser.add_argument('--download-dir', default=None,
                        help='下载文件保存目录 (默认: 临时目录, 测试后自动清理)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='显示详细日志')

    args = parser.parse_args()

    # 解析 Token 列表
    bot_tokens = [t.strip() for t in args.tokens.split(',') if t.strip()]
    if not bot_tokens:
        fail('没有提供有效的 Bot Token')
        return 1

    # 显示配置
    header('配置信息')
    print(f'  时间:      {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'  数据库:    {args.dsn.split("@")[-1] if "@" in args.dsn else args.dsn}')
    print(f'  Bot 数量:  {len(bot_tokens)}')
    for i, t in enumerate(bot_tokens):
        print(f'    Bot{i}: {t[:15]}...{t[-6:]}')
    if args.api_base:
        print(f'  API 中继:  {args.api_base}')
    if args.file_id:
        print(f'  指定测试:  file_id={args.file_id[:30]}...  bot_id={args.bot_id}')
    elif args.sample == 0:
        print(f'  抽样:      全部已上传章节')
    else:
        print(f'  抽样:      随机 {args.sample} 个')
    print(f'  模式:      {"仅 getFile" if args.check_only else "完整下载+ffprobe验证"}')

    # 测试指定 file_id
    if args.file_id:
        return test_single_file_id(
            args.file_id, args.bot_id, bot_tokens, args.api_base
        )

    # 完整测试
    return run_test(
        dsn=args.dsn,
        bot_tokens=bot_tokens,
        sample_size=args.sample,
        check_only=args.check_only,
        api_base=args.api_base,
        download_dir=args.download_dir,
    )


if __name__ == '__main__':
    sys.exit(main())
