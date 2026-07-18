#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
有声书数据库管理面板
Flask Web 应用，用于搜索、查看、修改 PostgreSQL 数据库

使用方法:
    pip install -r requirements.txt
    python app.py

    然后浏览器打开 http://localhost:5000

环境变量:
    POSTGRES_DSN  PostgreSQL 连接串 (默认: 本地 Docker)
    PORT          监听端口 (默认: 5000)
"""

import os
import json
import math
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

# ============================================================
# 配置
# ============================================================

DEFAULT_DSN = os.environ.get(
    'POSTGRES_DSN',
    'postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook'
)
PORT = int(os.environ.get('PORT', '5000'))
PAGE_SIZE = 50  # 每页显示条数

BOOKS_TABLE = 'books'
CHAPTERS_TABLE = 'audiobook_chapters'

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'audiobook-admin-2024')


# ============================================================
# 数据库连接
# ============================================================

def get_dsn():
    return DEFAULT_DSN

def db_query(query, params=None, fetch=True):
    """执行查询，返回结果列表"""
    conn = psycopg2.connect(get_dsn())
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            if fetch:
                return cur.fetchall()
            conn.commit()
            return None
    finally:
        conn.close()

def db_execute(query, params=None):
    """执行写入操作，返回受影响行数"""
    conn = psycopg2.connect(get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rowcount = cur.rowcount
            conn.commit()
            return rowcount
    finally:
        conn.close()


# ============================================================
# 工具函数
# ============================================================

def get_stats():
    """获取仪表盘统计"""
    stats = {}
    # 章节统计
    rows = db_query(f'SELECT upload_status, COUNT(*) as cnt FROM {CHAPTERS_TABLE} GROUP BY upload_status')
    ch_stats = {r['upload_status'] or 'null': r['cnt'] for r in rows}
    stats['ch_total']      = sum(ch_stats.values())
    stats['ch_pending']    = ch_stats.get('pending', 0)
    stats['ch_processing'] = ch_stats.get('processing', 0)
    stats['ch_uploaded']   = ch_stats.get('uploaded', 0)
    stats['ch_failed']     = ch_stats.get('failed', 0)

    # 书籍统计
    rows = db_query(f'SELECT book_status, COUNT(*) as cnt FROM {BOOKS_TABLE} GROUP BY book_status')
    bk_stats = {r['book_status'] or 'null': r['cnt'] for r in rows}
    stats['bk_total']    = sum(bk_stats.values())
    stats['bk_pending']  = bk_stats.get('pending', 0)
    stats['bk_success']  = bk_stats.get('success', 0)

    # 有音频URL vs 无URL
    rows = db_query(f"SELECT COUNT(*) as cnt FROM {CHAPTERS_TABLE} WHERE audio_url IS NOT NULL AND audio_url != ''")
    stats['ch_has_url'] = rows[0]['cnt'] if rows else 0
    stats['ch_no_url']  = stats['ch_total'] - stats['ch_has_url']

    # 最近上传
    rows = db_query(
        f"SELECT book_name, chapter_name, uploaded_at FROM {CHAPTERS_TABLE} "
        f"WHERE upload_status = 'uploaded' ORDER BY uploaded_at DESC LIMIT 10"
    )
    stats['recent_uploads'] = rows

    # 最近失败
    rows = db_query(
        f"SELECT book_name, chapter_name, error_message FROM {CHAPTERS_TABLE} "
        f"WHERE upload_status = 'failed' ORDER BY claimed_at DESC LIMIT 10"
    )
    stats['recent_failures'] = rows

    return stats


# ============================================================
# 路由: 仪表盘
# ============================================================

@app.route('/')
def dashboard():
    stats = get_stats()
    return render_template('dashboard.html', stats=stats)


# ============================================================
# 路由: 书籍列表
# ============================================================

@app.route('/books')
def books():
    page    = int(request.args.get('page', 1))
    search  = request.args.get('q', '').strip()
    status  = request.args.get('status', '')
    offset  = (page - 1) * PAGE_SIZE

    where_parts = []
    params = []

    if search:
        where_parts.append("book_name ILIKE %s")
        params.append(f'%{search}%')
    if status:
        where_parts.append("book_status = %s")
        params.append(status)
    category = request.args.get('category', '').strip()
    if category:
        where_parts.append("category = %s")
        params.append(category)

    where_clause = (' WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

    # 总数
    count_q = f'SELECT COUNT(*) FROM {BOOKS_TABLE}{where_clause}'
    total = db_query(count_q, params)[0]['count']

    # 分页数据 (附带章节统计, 使用顶层列 book_name/author/category)
    query = sql.SQL("""
        SELECT b.book_id, b.book_name, b.book_status, b.author, b.category,
               b.total_chapters,
               (SELECT COUNT(*) FROM {ch} WHERE {ch}.book_id = b.book_id) as ch_total,
               (SELECT COUNT(*) FROM {ch} WHERE {ch}.book_id = b.book_id AND {ch}.upload_status = 'uploaded') as ch_uploaded,
               (SELECT COUNT(*) FROM {ch} WHERE {ch}.book_id = b.book_id AND {ch}.upload_status = 'failed') as ch_failed
        FROM {bk} b
        {where}
        ORDER BY b.book_id
        LIMIT %s OFFSET %s
    """).format(
        ch=sql.Identifier(CHAPTERS_TABLE),
        bk=sql.Identifier(BOOKS_TABLE),
        where=sql.SQL(where_clause) if where_clause else sql.SQL('')
    )

    page_params = params + [PAGE_SIZE, offset]
    rows = db_query(query, page_params)

    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    # 获取分类列表 (用于侧边筛选)
    cat_rows = db_query(f"""
        SELECT category, COUNT(*) as cnt FROM {BOOKS_TABLE}
        WHERE category IS NOT NULL AND category != ''
        GROUP BY category ORDER BY cnt DESC LIMIT 30
    """)

    return render_template('books.html',
        books=rows, total=total, page=page, total_pages=total_pages,
        search=search, status=status, category=category, categories=cat_rows
    )


# ============================================================
# 路由: 书籍详情
# ============================================================

@app.route('/books/<book_id>')
def book_detail(book_id):
    # 书籍信息
    bk_query = sql.SQL("SELECT * FROM {} WHERE book_id = %s").format(sql.Identifier(BOOKS_TABLE))
    book = db_query(bk_query, (book_id,))
    if not book:
        abort(404)
    book = book[0]

    # 解析 book_data 中的关键字段
    book_data = book.get('book_data', {})
    if isinstance(book_data, str):
        book_data = json.loads(book_data)

    # 章节列表
    ch_query = sql.SQL("""
        SELECT * FROM {} WHERE book_id = %s ORDER BY chapter_id
    """).format(sql.Identifier(CHAPTERS_TABLE))
    chapters = db_query(ch_query, (book_id,))

    # 章节统计
    ch_stats = {'pending': 0, 'processing': 0, 'uploaded': 0, 'failed': 0}
    for ch in chapters:
        s = ch.get('upload_status', 'pending')
        ch_stats[s] = ch_stats.get(s, 0) + 1

    return render_template('book_detail.html',
        book=book, book_data=book_data, chapters=chapters, ch_stats=ch_stats
    )


# ============================================================
# 路由: 章节列表
# ============================================================

@app.route('/chapters')
def chapters():
    page    = int(request.args.get('page', 1))
    search  = request.args.get('q', '').strip()
    status  = request.args.get('status', '')
    book_id = request.args.get('book_id', '')
    offset  = (page - 1) * PAGE_SIZE

    where_parts = []
    params = []

    if search:
        where_parts.append("(book_name ILIKE %s OR chapter_name ILIKE %s)")
        params.extend([f'%{search}%', f'%{search}%'])
    if status:
        where_parts.append("upload_status = %s")
        params.append(status)
    if book_id:
        where_parts.append("book_id = %s")
        params.append(book_id)

    where_clause = (' WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

    count_q = f'SELECT COUNT(*) FROM {CHAPTERS_TABLE}{where_clause}'
    total = db_query(count_q, params)[0]['count']

    query = sql.SQL("""
        SELECT * FROM {} {}
        ORDER BY book_id, chapter_id
        LIMIT %s OFFSET %s
    """).format(
        sql.Identifier(CHAPTERS_TABLE),
        sql.SQL(where_clause) if where_clause else sql.SQL('')
    )

    page_params = params + [PAGE_SIZE, offset]
    rows = db_query(query, page_params)

    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    return render_template('chapters.html',
        chapters=rows, total=total, page=page, total_pages=total_pages,
        search=search, status=status, book_id=book_id
    )


# ============================================================
# 路由: 章节编辑
# ============================================================

@app.route('/chapters/<book_id>/<chapter_id>/edit', methods=['GET', 'POST'])
def chapter_edit(book_id, chapter_id):
    if request.method == 'POST':
        upload_status   = request.form.get('upload_status', 'pending')
        audio_url       = request.form.get('audio_url', '')
        telegram_file_id = request.form.get('telegram_file_id', '')
        telegram_message_id = request.form.get('telegram_message_id', '') or None
        telegram_bot_id = request.form.get('telegram_bot_id', '') or None
        telegram_bot_user_id = request.form.get('telegram_bot_user_id', '') or None
        error_message   = request.form.get('error_message', '')
        worker_id       = request.form.get('worker_id', '')
        claimed_at      = request.form.get('claimed_at', '')
        uploaded_at     = request.form.get('uploaded_at', '')

        # 重置字段: 如果改回 pending，清空处理信息
        if upload_status == 'pending':
            worker_id = ''
            claimed_at = None
            uploaded_at = None
            error_message = ''
            telegram_bot_id = None
            telegram_bot_user_id = None

        update_q = sql.SQL("""
            UPDATE {} SET
                upload_status = %s,
                audio_url = %s,
                telegram_file_id = %s,
                telegram_message_id = %s,
                telegram_bot_id = %s,
                telegram_bot_user_id = %s,
                error_message = %s,
                worker_id = %s,
                claimed_at = %s,
                uploaded_at = %s
            WHERE book_id = %s AND chapter_id = %s
        """).format(sql.Identifier(CHAPTERS_TABLE))

        db_execute(update_q, (
            upload_status, audio_url or None, telegram_file_id or None,
            int(telegram_message_id) if telegram_message_id else None,
            int(telegram_bot_id) if telegram_bot_id else None,
            int(telegram_bot_user_id) if telegram_bot_user_id else None,
            error_message or None, worker_id or None,
            claimed_at or None, uploaded_at or None,
            book_id, chapter_id
        ))

        flash('章节已更新', 'success')
        return redirect(url_for('chapter_edit', book_id=book_id, chapter_id=chapter_id))

    # GET: 显示编辑表单
    query = sql.SQL("SELECT * FROM {} WHERE book_id = %s AND chapter_id = %s").format(sql.Identifier(CHAPTERS_TABLE))
    chapter = db_query(query, (book_id, chapter_id))
    if not chapter:
        abort(404)
    chapter = chapter[0]

    return render_template('chapter_edit.html', chapter=chapter)


# ============================================================
# 路由: 批量操作
# ============================================================

@app.route('/actions/reset_stuck', methods=['POST'])
def reset_stuck():
    """重置卡住的 processing 章节"""
    timeout_hours = int(request.form.get('hours', 24))
    rowcount = db_execute(
        sql.SQL("UPDATE {} SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL "
                "WHERE upload_status = 'processing' AND claimed_at < NOW() - INTERVAL %s")
        .format(sql.Identifier(CHAPTERS_TABLE)),
        (f'{timeout_hours} hours',)
    )
    flash(f'已重置 {rowcount} 个卡住超过 {timeout_hours} 小时的章节', 'success')
    return redirect(url_for('dashboard'))


@app.route('/actions/reset_failed', methods=['POST'])
def reset_failed():
    """重置所有 failed 章节"""
    rowcount = db_execute(
        sql.SQL("UPDATE {} SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL, "
                "error_message = NULL WHERE upload_status = 'failed'")
        .format(sql.Identifier(CHAPTERS_TABLE))
    )
    flash(f'已重置 {rowcount} 个失败章节', 'success')
    return redirect(url_for('dashboard'))


@app.route('/actions/reset_processing', methods=['POST'])
def reset_processing():
    """重置所有 processing 章节 (不管超时)"""
    rowcount = db_execute(
        sql.SQL("UPDATE {} SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL "
                "WHERE upload_status = 'processing'")
        .format(sql.Identifier(CHAPTERS_TABLE))
    )
    flash(f'已重置 {rowcount} 个处理中章节', 'success')
    return redirect(url_for('dashboard'))


@app.route('/actions/reset_book_chapters', methods=['POST'])
def reset_book_chapters():
    """重置某本书的所有章节为 pending"""
    book_id = request.form.get('book_id')
    if not book_id:
        flash('缺少 book_id', 'danger')
        return redirect(url_for('dashboard'))

    rowcount = db_execute(
        sql.SQL("UPDATE {} SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL, "
                "error_message = NULL WHERE book_id = %s")
        .format(sql.Identifier(CHAPTERS_TABLE)),
        (book_id,)
    )
    # 同时重置书级状态
    db_execute(
        sql.SQL("UPDATE {} SET book_status = 'pending' WHERE book_id = %s").format(sql.Identifier(BOOKS_TABLE)),
        (book_id,)
    )
    flash(f'已重置书 {book_id} 的 {rowcount} 个章节', 'success')
    return redirect(url_for('book_detail', book_id=book_id))


@app.route('/actions/delete_chapter', methods=['POST'])
def delete_chapter():
    """删除单个章节"""
    book_id = request.form.get('book_id')
    chapter_id = request.form.get('chapter_id')
    if not book_id or not chapter_id:
        flash('缺少参数', 'danger')
        return redirect(url_for('chapters'))

    db_execute(
        sql.SQL("DELETE FROM {} WHERE book_id = %s AND chapter_id = %s").format(sql.Identifier(CHAPTERS_TABLE)),
        (book_id, chapter_id)
    )
    flash(f'已删除章节 {book_id}/{chapter_id}', 'success')
    return redirect(url_for('chapters'))


# ============================================================
# API: 搜索建议 (AJAX)
# ============================================================

@app.route('/api/search_books')
def api_search_books():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])

    rows = db_query(
        sql.SQL("SELECT book_id, book_name FROM {} "
                "WHERE book_name ILIKE %s LIMIT 10")
        .format(sql.Identifier(BOOKS_TABLE)),
        (f'%{q}%',)
    )
    return jsonify([{'book_id': r['book_id'], 'book_name': r['book_name']} for r in rows])


# ============================================================
# 错误处理
# ============================================================

@app.errorhandler(404)
def not_found(e):
    return render_template('base.html', error='404 - 页面不存在'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('base.html', error=f'500 - 服务器错误: {e}'), 500


# ============================================================
# Jinja 过滤器
# ============================================================

@app.template_filter('fmt_time')
def fmt_time(value):
    if not value:
        return '-'
    if isinstance(value, str):
        return value
    return value.strftime('%Y-%m-%d %H:%M:%S')

@app.template_filter('fmt_json')
def fmt_json(value):
    if value is None:
        return ''
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)

@app.template_filter('truncate_str')
def truncate_str(value, length=50):
    if not value:
        return ''
    s = str(value)
    return s[:length] + '...' if len(s) > length else s


# ============================================================
# 启动
# ============================================================

if __name__ == '__main__':
    print(f'>>> 有声书数据库管理面板')
    print(f'    数据库: {get_dsn().split("@")[-1]}')
    print(f'    访问:   http://0.0.0.0:{PORT}')
    print()
    app.run(host='0.0.0.0', port=PORT, debug=True)
