#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HF Space Worker Flask 服务器 — 多Bot轮换模式

Worker 直接上传 Telegram, 使用多个 Bot Token 轮换以分散限流。

端点:
  GET  /                       - 状态面板 (含Bot池状态)
  GET  /health                 - 健康检查 (空闲槽位)
  GET  /status                 - JSON 状态
  POST /process                - 触发处理一个任务
  GET  /bots                   - Bot池详细状态
  GET  /test-telegram          - 测试Telegram连通性
  POST /refresh-tg-config      - 一键从VPS获取最新TG配置
  POST /process-batch          - 启动批量处理 (多线程并发)
  GET  /batch-status           - 获取批量处理进度
  POST /batch-stop             - 停止批量处理
"""

import os
import threading
import time
import logging
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string

import worker

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

NUM_SLOTS = int(os.environ.get('NUM_SLOTS', '2'))


class Slot:
    def __init__(self, slot_id):
        self.slot_id = slot_id
        self.status = 'idle'
        self.thread = None
        self.last_result = None
        self.last_update = None
        self.started_at = None
        self.current_task = None

    def is_free(self):
        return self.status == 'idle'

    def to_dict(self):
        return {
            'slot_id': self.slot_id, 'status': self.status,
            'started_at': self.started_at, 'last_result': self.last_result,
            'last_update': self.last_update, 'current_task': self.current_task,
        }

_slots = [Slot(i) for i in range(NUM_SLOTS)]
_lock = threading.Lock()

def _find_and_claim_slot():
    with _lock:
        for s in _slots:
            if s.is_free():
                s.status = 'processing'
                s.started_at = datetime.now().isoformat()
                s.last_update = datetime.now().isoformat()
                return s
    return None

def _free_slot_count():
    with _lock:
        return sum(1 for s in _slots if s.is_free())

def _process_in_background(slot):
    print(f'[槽位{slot.slot_id}] 开始处理...')
    try:
        result = worker.run_one(slot)
        slot.last_result = result
        status = result.get('status', 'unknown')
        book = result.get('book_name', '')
        chapter = result.get('chapter_name', '')
        dur = result.get('duration', 0)
        err = result.get('error', '')
        bot_id = result.get('bot_id', '?')
        if status == 'uploaded':
            print(f'[槽位{slot.slot_id} ✅] {book} - {chapter} ({dur:.1f}s, Bot{bot_id})')
        elif status == 'no_task':
            print(f'[槽位{slot.slot_id}] 无待处理任务')
        else:
            print(f'[槽位{slot.slot_id} ❌] {book} - {chapter} ({dur:.1f}s) {err}')
    except Exception as e:
        slot.last_result = {'status': 'error', 'error': str(e)}
        print(f'[槽位{slot.slot_id} ❌异常] {e}')
    finally:
        with _lock:
            slot.status = 'idle'
            slot.current_task = None
            slot.last_update = datetime.now().isoformat()


@app.route('/')
def index():
    with _lock:
        slots_data = [s.to_dict() for s in _slots]
    try:
        stats = worker.get_db_stats()
    except Exception as e:
        stats = {'error': str(e)}

    df_status = 'enabled' if worker._use_df else 'disabled'
    free_count = sum(1 for s in slots_data if s['status'] == 'idle')
    overall_status = 'processing' if free_count < NUM_SLOTS else 'idle'

    bot_status = worker.bot_pool.status() if worker.bot_pool else []
    bot_summary = worker.bot_pool.summary() if worker.bot_pool else {'bot_count': 0, 'total_uploads': 0, 'total_429': 0, 'total_errors': 0}

    slots_html = ''
    for s in slots_data:
        slot_status = s['status']
        spinner = '<span class="spinner"></span>' if slot_status == 'processing' else ''
        result = s.get('last_result')
        current = s.get('current_task')
        if slot_status == 'processing' and current:
            c_book = current.get('book_name', '')
            c_chapter = current.get('chapter_name', '')
            c_step = current.get('current_step', '处理中...')
            c_started = s.get('started_at', '')
            elapsed_str = ''
            if c_started:
                try:
                    elapsed = (datetime.now() - datetime.fromisoformat(c_started)).total_seconds()
                    elapsed_str = f' (已运行 {elapsed:.0f}s)'
                except:
                    pass
            result_html = f'<div class="result-card" style="background:#fff3cd;"><strong>{c_book}</strong> - {c_chapter}<br><small class="text-muted">{c_step}{elapsed_str}</small></div>'
        elif result:
            r_status = result.get('status', '?')
            r_color = {'uploaded': 'success', 'failed': 'danger', 'no_task': 'secondary', 'error': 'danger'}.get(r_status, 'secondary')
            r_book = result.get('book_name', '')
            r_chapter = result.get('chapter_name', '')
            r_err = result.get('error', '')
            r_dur = result.get('duration', 0)
            r_bot = result.get('bot_id', '?')
            r_bot_uid = result.get('bot_user_id', '')
            r_fid = result.get('file_id', '')
            err_html = f'<br><small class="text-danger">{r_err}</small>' if r_err else ''
            fid_html = f'<br><small class="text-success">file_id: {r_fid[:30]}...</small>' if r_fid else ''
            bot_uid_html = f' (uid:{r_bot_uid})' if r_bot_uid else ''
            bot_html = f'<br><small class="text-info">Bot{r_bot}{bot_uid_html}</small>' if r_bot != '?' else ''
            result_html = f'<div class="result-card"><span class="badge bg-{r_color}">{r_status}</span> <strong>{r_book}</strong> - {r_chapter}<br><small class="text-muted">耗时: {r_dur:.1f}s</small>{bot_html}{err_html}{fid_html}</div>'
        else:
            result_html = '<p class="text-muted small">暂无</p>'
        slots_html += f'<div class="col-md-6"><div class="card p-3 slot-card"><span class="status-badge status-{slot_status}">{spinner} 槽位 {s["slot_id"]+1}: {slot_status.upper()}</span>{result_html}<small class="text-muted">更新: {s.get("last_update", "-")}</small></div></div>'

    bots_html = ''
    for b in bot_status:
        avail_class = 'success' if b['available'] else 'danger'
        avail_text = '✅ 可用' if b['available'] else f'⏸ 冷却 {b["cooldown_remaining"]}s'
        bots_html += f'''
        <div class="col-md-4 mb-2">
            <div class="bot-card" data-bot-id="{b['id']}">
                <div class="d-flex justify-content-between">
                    <strong>Bot{b['id']}</strong>
                    <span class="badge bg-{avail_class} badge-sm">{avail_text}</span>
                </div>
                <small class="text-muted">@{b['username']}</small>
                {'<small class="text-success d-block">uid:' + str(b['user_id']) + '</small>' if b.get('user_id') else ''}
                <div class="bot-stats mt-1">
                    <span class="badge bg-success badge-sm">上传 {b['total_uploads']}</span>
                    <span class="badge bg-warning badge-sm">429: {b['total_429']}</span>
                    <span class="badge bg-danger badge-sm">错误 {b['total_errors']}</span>
                </div>
            </div>
        </div>'''

    batch_status = worker.get_batch_status()
    return render_template_string(HTML_TEMPLATE,
        overall_status=overall_status, free_count=free_count, num_slots=NUM_SLOTS,
        worker_id=worker.WORKER_ID, stats=stats, df_status=df_status, slots_html=slots_html,
        bot_count=len(bot_status), bot_summary=bot_summary, bots_html=bots_html,
        pg_set='✅' if worker.POSTGRES_DSN else '❌',
        chat_set='✅' if worker.CHAT_ID else '❌',
        tg_route=worker.TELEGRAM_API_BASE if worker.TELEGRAM_API_BASE != 'https://api.telegram.org' else '直连',
        min_interval=worker.BOT_MIN_INTERVAL, max_retries=worker.MAX_RETRIES,
        tg_config_source=worker.TG_CONFIG_SOURCE,
        vps_scheduler_url=worker.VPS_SCHEDULER_URL or '(未设置)',
        max_chapters_desc=f'{worker.MAX_CHAPTERS} 个' if worker.MAX_CHAPTERS > 0 else '不限 (0)',
        num_workers=worker.NUM_WORKERS,
        batch_running=batch_status.get('running', False),
    )


@app.route('/health')
def health():
    return jsonify({'ok': True, 'worker_id': worker.WORKER_ID, 'free_slots': _free_slot_count(), 'total_slots': NUM_SLOTS})

@app.route('/status')
def status():
    with _lock:
        slots_data = [s.to_dict() for s in _slots]
    try:
        stats = worker.get_db_stats()
    except Exception as e:
        stats = {'error': str(e)}
    free = sum(1 for s in slots_data if s['status'] == 'idle')
    return jsonify({
        'overall_status': 'processing' if free < NUM_SLOTS else 'idle',
        'free_slots': free, 'total_slots': NUM_SLOTS,
        'worker_id': worker.WORKER_ID, 'slots': slots_data, 'db_stats': stats,
        'bot_pool': worker.bot_pool.status() if worker.bot_pool else [],
        'bot_summary': worker.bot_pool.summary() if worker.bot_pool else {},
    })

@app.route('/bots')
def bots():
    if worker.bot_pool:
        return jsonify({'ok': True, 'bots': worker.bot_pool.status(), 'summary': worker.bot_pool.summary()})
    return jsonify({'ok': False, 'error': 'Bot池未初始化'}), 500

@app.route('/process', methods=['POST'])
def process():
    slot = _find_and_claim_slot()
    if slot is None:
        return jsonify({'status': 'busy', 'message': f'所有 {NUM_SLOTS} 槽位满', 'free_slots': 0}), 409
    thread = threading.Thread(target=_process_in_background, args=(slot,), daemon=True)
    slot.thread = thread
    thread.start()
    return jsonify({'status': 'started', 'slot': slot.slot_id, 'free_slots': _free_slot_count()}), 202

@app.route('/test-telegram')
def test_telegram():
    import requests
    if not worker.BOT_TOKENS:
        return jsonify({'reachable': False, 'message': '无Bot Token配置'})
    try:
        url = f'{worker.TELEGRAM_API_BASE}/bot{worker.BOT_TOKENS[0]}/getMe'
        resp = requests.get(url, timeout=(15, 60))
        data = resp.json()
        if resp.status_code == 200 and data.get('ok'):
            bot_info = data.get('result', {})
            route = '中继' if worker.TELEGRAM_API_BASE != 'https://api.telegram.org' else '直连'
            return jsonify({'reachable': True, 'message': f'Bot @{bot_info.get("username", "?")} 连接正常 ({route})'})
        return jsonify({'reachable': False, 'message': f'HTTP {resp.status_code}: {data.get("description", "?")}'})
    except Exception as e:
        return jsonify({'reachable': False, 'message': f'{type(e).__name__}: {str(e)[:200]}'})


@app.route('/refresh-tg-config', methods=['POST'])
def refresh_tg_config():
    """一键从 VPS 调度器获取最新 Telegram 配置, 更新后立即生效"""
    try:
        result = worker.refresh_tg_config()
        return jsonify(result)
    except Exception as e:
        return jsonify({'ok': False, 'message': f'刷新异常: {type(e).__name__}: {str(e)[:200]}'}), 500


@app.route('/process-batch', methods=['POST'])
def process_batch():
    """启动批量处理 (多线程并发: 下载 → 降噪 → 上传)

    可选参数 (JSON body 或 query string):
      max_chapters: 最多处理章节数 (0=不限, 默认用环境变量)
      num_workers:  并发线程数 (默认用环境变量)
    """
    if worker._batch_state.get('running'):
        return jsonify({'ok': False, 'message': '批量处理已在运行中'}), 409

    data = request.get_json(silent=True) or {}
    max_chapters = data.get('max_chapters')
    if max_chapters is None:
        max_chapters = request.args.get('max_chapters', type=int)
    num_workers = data.get('num_workers')
    if num_workers is None:
        num_workers = request.args.get('num_workers', type=int)

    # 在后台线程运行, 不阻塞 HTTP 响应
    def _run_batch_bg():
        try:
            worker.run_batch(max_chapters=max_chapters, num_workers=num_workers)
        except Exception as e:
            print(f'[批量处理异常] {type(e).__name__}: {e}')

    thread = threading.Thread(target=_run_batch_bg, daemon=True)
    thread.start()

    return jsonify({
        'ok': True,
        'message': '批量处理已启动',
        'max_chapters': max_chapters if max_chapters is not None else worker.MAX_CHAPTERS,
        'num_workers': num_workers if num_workers is not None else worker.NUM_WORKERS,
    }), 202


@app.route('/batch-status')
def batch_status():
    """获取批量处理进度"""
    return jsonify(worker.get_batch_status())


@app.route('/batch-stop', methods=['POST'])
def batch_stop():
    """停止正在运行的批量处理"""
    ok, msg = worker.stop_batch()
    return jsonify({'ok': ok, 'message': msg})


HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Worker (多Bot轮换) - {{ worker_id }}</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
body{background:#f0f2f5;font-family:system-ui,sans-serif}.container{max-width:1000px}
.card{border:none;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.08);margin-bottom:16px}
.slot-card{min-height:160px}.status-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-weight:600;font-size:.85rem}
.status-processing{background:#fff3cd;color:#856404;animation:p 2s infinite}.status-idle{background:#d4edda;color:#155724}
@keyframes p{0%,100%{opacity:1}50%{opacity:.6}}
.stat-box{text-align:center;padding:10px;border-radius:8px;background:#f8f9fa}.stat-number{font-size:1.5rem;font-weight:700}
.result-card{padding:8px;border-radius:6px;background:#f8f9fa;margin-top:6px;font-size:.85rem}
.config-item{display:flex;justify-content:space-between;padding:3px 0}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #ddd;border-top:2px solid #667eea;border-radius:50%;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.bot-card{padding:10px;border-radius:8px;background:#f8f9fa;border:1px solid #e9ecef}
.bot-stats{display:flex;gap:4px;flex-wrap:wrap}
.badge-sm{font-size:0.70rem}
</style>
</head>
<body>
<div class="container py-4">
    <div class="text-center mb-4">
        <h2><i class="bi bi-robot"></i> 有声书 Worker</h2>
        <p class="text-muted"><span class="badge bg-info">多Bot轮换模式</span> 下载→降噪→多Bot直传Telegram</p>
    </div>

    <div class="card p-4">
        <div class="d-flex justify-content-between align-items-center">
            <div>
                <span class="status-badge status-{{ overall_status }}">{% if overall_status == 'processing' %}<span class="spinner"></span>{% endif %} {{ overall_status | upper }}</span>
                <span class="ms-2">空闲: <strong>{{ free_count }}</strong>/{{ num_slots }}</span>
            </div>
            <div class="text-end"><small class="text-muted">Worker ID</small><br><code>{{ worker_id }}</code></div>
        </div>
    </div>

    <div class="card p-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h5 class="mb-0"><i class="bi bi-people-fill"></i> Bot 池 ({{ bot_count }} 个Bot)</h5>
            <div>
                <span class="badge bg-success badge-sm">总上传 {{ bot_summary.total_uploads }}</span>
                <span class="badge bg-warning badge-sm">总429 {{ bot_summary.total_429 }}</span>
                <span class="badge bg-danger badge-sm">总错误 {{ bot_summary.total_errors }}</span>
            </div>
        </div>
        <div class="row">{{ bots_html | safe }}</div>
    </div>

    <div class="card p-4">
        <h5>📊 数据库状态</h5>
        {% if stats and stats.error %}<div class="alert alert-danger">{{ stats.error }}</div>{% else %}
        <div class="row g-2 mt-1">
            <div class="col"><div class="stat-box"><div class="stat-number text-secondary">{{ stats.pending or 0 }}</div><small>待处理</small></div></div>
            <div class="col"><div class="stat-box"><div class="stat-number text-primary">{{ stats.processing or 0 }}</div><small>处理中</small></div></div>
            <div class="col"><div class="stat-box"><div class="stat-number text-success">{{ stats.uploaded or 0 }}</div><small>已上传</small></div></div>
            <div class="col"><div class="stat-box"><div class="stat-number text-danger">{{ stats.failed or 0 }}</div><small>已失败</small></div></div>
            <div class="col"><div class="stat-box"><div class="stat-number text-info">{{ stats.books_success or 0 }}/{{ stats.books_total or 0 }}</div><small>完成书籍</small></div></div>
        </div>{% endif %}
    </div>

    <div class="card p-4">
        <h5>🖥️ 处理槽位 ({{ num_slots }} vCPU)</h5>
        <div class="row g-3 mt-1">{{ slots_html | safe }}</div>
    </div>

    <div class="card p-4">
        <h5>⚙️ 配置</h5>
        <div class="config-item"><span>POSTGRES_DSN</span><span>{{ pg_set }}</span></div>
        <div class="config-item"><span>CHAT_ID</span><span>{{ chat_set }}</span></div>
        <div class="config-item"><span>Bot数量</span><span>{{ bot_count }}</span></div>
        <div class="config-item"><span>Bot最小间隔</span><span>{{ min_interval }}s</span></div>
        <div class="config-item"><span>最大重试</span><span>{{ max_retries }}</span></div>
        <div class="config-item"><span>DeepFilter</span><span>{{ df_status }}</span></div>
        <div class="config-item"><span>TG路由</span><span>{{ tg_route }}</span></div>
        <div class="config-item"><span>TG配置来源</span><span>{% if tg_config_source == 'vps' %}<span class="badge bg-info">VPS调度器</span>{% else %}<span class="badge bg-secondary">本地环境变量</span>{% endif %}</span></div>
        <div class="config-item"><span>VPS调度器</span><span><code>{{ vps_scheduler_url }}</code></span></div>
    </div>

    <div class="card p-4">
        <h5>🔧 API & 操作</h5>
        <div class="mt-2">
            <button class="btn btn-primary btn-sm" onclick="triggerProcess()"><i class="bi bi-play-fill"></i> 手动触发</button>
            <button class="btn btn-info btn-sm" onclick="testTelegram()"><i class="bi bi-wifi"></i> 测试Telegram</button>
            <button class="btn btn-success btn-sm" onclick="refreshTgConfig()"><i class="bi bi-arrow-clockwise"></i> 一键获取TG配置更新</button>
            <span id="trigger-result" class="ms-2"></span>
            <span id="test-result" class="ms-2"></span>
        </div>
        <div id="tg-config-result" class="mt-3" style="display:none;"></div>
    </div>

    <div class="card p-4">
        <h5>🚀 批量处理 (多线程并发: 下载→降噪→上传)</h5>
        <div class="mt-2 mb-2">
            <span class="text-muted" style="font-size:.85rem;">DeepFilter: 
                <span class="badge {% if df_status == 'enabled' %}bg-success{% else %}bg-secondary{% endif %}">{{ df_status }}</span>
                | MAX_CHAPTERS (默认): <code>{{ max_chapters_desc }}</code>
                | NUM_WORKERS (默认): <code>{{ num_workers }}</code>
            </span>
        </div>
        <div class="mt-2 d-flex align-items-center flex-wrap gap-2">
            <label class="form-label mb-0 me-1" style="font-size:.85rem;">章节数:</label>
            <input type="number" id="batch-max-chapters" class="form-control form-control-sm" style="width:100px;" value="0" min="0" title="0=不限">
            <label class="form-label mb-0 me-1" style="font-size:.85rem;">线程数:</label>
            <input type="number" id="batch-num-workers" class="form-control form-control-sm" style="width:80px;" value="{{ num_workers }}" min="1" max="16">
            <button class="btn btn-primary btn-sm" onclick="startBatch()"><i class="bi bi-play-fill"></i> 开始批量处理</button>
            <button class="btn btn-danger btn-sm" onclick="stopBatch()"><i class="bi bi-stop-fill"></i> 停止</button>
            <button class="btn btn-outline-secondary btn-sm" onclick="refreshBatchStatus()"><i class="bi bi-arrow-clockwise"></i> 刷新进度</button>
        </div>
        <div id="batch-result" class="mt-3" style="display:none;"></div>
    </div>
</div>
{% if overall_status == 'processing' and not batch_running %}<script>setTimeout(()=>location.reload(),5000);</script>{% endif %}
{% if batch_running %}<script>setTimeout(()=>refreshBatchStatus(),3000);</script>{% endif %}
<script>
async function triggerProcess(){
    const el=document.getElementById('trigger-result');
    el.innerHTML='<span class="spinner"></span> 触发中...';
    try{
        const r=await fetch('/process',{method:'POST'});
        const d=await r.json();
        if(r.status===202){el.innerHTML='<span class="text-success">✅ '+d.message+'</span>';}
        else{el.innerHTML='<span class="text-warning">⚠️ '+(d.message||'忙')+'</span>';}
        setTimeout(()=>location.reload(),3000);
    }catch(e){el.innerHTML='<span class="text-danger">❌ '+e+'</span>';}
}
async function testTelegram(){
    const el=document.getElementById('test-result');
    el.innerHTML='<span class="spinner"></span> 测试中...';
    try{
        const r=await fetch('/test-telegram');
        const d=await r.json();
        el.innerHTML=d.reachable?'<span class="text-success">✅ '+d.message+'</span>':'<span class="text-danger">❌ '+d.message+'</span>';
    }catch(e){el.innerHTML='<span class="text-danger">❌ '+e+'</span>';}
}
async function refreshTgConfig(){
    const el=document.getElementById('tg-config-result');
    el.style.display='block';
    el.innerHTML='<span class="spinner"></span> 正在从 VPS 调度器获取最新 Telegram 配置...';
    try{
        const r=await fetch('/refresh-tg-config',{method:'POST'});
        const d=await r.json();
        if(d.ok){
            const raw=d.raw||{};
            const previews=(raw.bot_tokens_preview||[]).map(t=>'<code>'+t+'</code>').join('<br>')||'<span class="text-muted">无</span>';
            const changedBadge=d.changed?'<span class="badge bg-warning">配置已变更</span>':'<span class="badge bg-secondary">无变化</span>';
            const rebuiltBadge=d.bot_pool_rebuilt?'<span class="badge bg-success">Bot池已重建</span>':'';
            let html='<div class="alert alert-success">';
            html+='<div class="d-flex justify-content-between align-items-center mb-2">';
            html+='<strong>✅ '+d.message+'</strong>';
            html+='<span>'+changedBadge+' '+rebuiltBadge+'</span>';
            html+='</div>';
            html+='<table class="table table-sm table-bordered mb-0" style="font-size:.85rem;">';
            html+='<tr><th style="width:160px;">VPS 调度器</th><td><code>'+d.vps_url+'</code></td></tr>';
            html+='<tr><th>Chat ID</th><td><code>'+d.chat_id+'</code></td></tr>';
            html+='<tr><th>Bot 数量</th><td>'+d.bot_count+' 个</td></tr>';
            html+='<tr><th>API Base</th><td><code>'+d.api_base+'</code></td></tr>';
            html+='<tr><th>配置来源</th><td><span class="badge bg-info">'+d.config_source+'</span></td></tr>';
            html+='<tr><th>Bot Tokens (预览)</th><td>'+previews+'</td></tr>';
            html+='</table>';
            html+='</div>';
            el.innerHTML=html;
            setTimeout(()=>location.reload(),4000);
        }else{
            el.innerHTML='<div class="alert alert-danger">❌ '+(d.message||'获取失败')+'</div>';
        }
    }catch(e){
        el.innerHTML='<div class="alert alert-danger">❌ '+e+'</div>';
    }
}
async function startBatch(){
    const el=document.getElementById('batch-result');
    const mc=document.getElementById('batch-max-chapters').value;
    const nw=document.getElementById('batch-num-workers').value;
    el.style.display='block';
    el.innerHTML='<span class=\'spinner\'></span> 正在启动批量处理...';
    try{
        const body={max_chapters:parseInt(mc)||0, num_workers:parseInt(nw)||1};
        const r=await fetch('/process-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        const d=await r.json();
        if(d.ok){
            el.innerHTML='<div class=\'alert alert-success\'>✅ '+d.message+' (最多 '+(d.max_chapters>0?d.max_chapters+' 个':'不限')+', '+d.num_workers+' 线程)</div>';
            setTimeout(()=>refreshBatchStatus(),2000);
        }else{
            el.innerHTML='<div class=\'alert alert-warning\'>⚠️ '+(d.message||'启动失败')+'</div>';
        }
    }catch(e){el.innerHTML='<div class=\'alert alert-danger\'>❌ '+e+'</div>';}
}
async function stopBatch(){
    const el=document.getElementById('batch-result');
    el.style.display='block';
    el.innerHTML='<span class=\'spinner\'></span> 正在停止...';
    try{
        const r=await fetch('/batch-stop',{method:'POST'});
        const d=await r.json();
        el.innerHTML='<div class=\'alert '+(d.ok?\'alert-success\':\'alert-warning\')+\'\'>⚠️ '+d.message+'</div>';
        setTimeout(()=>refreshBatchStatus(),2000);
    }catch(e){el.innerHTML='<div class=\'alert alert-danger\'>❌ '+e+'</div>';}
}
async function refreshBatchStatus(){
    const el=document.getElementById('batch-result');
    el.style.display='block';
    try{
        const r=await fetch('/batch-status');
        const d=await r.json();
        if(!d.running && !d.finished_at){
            el.innerHTML='<div class=\'alert alert-secondary\'>📋 未启动批量处理</div>';
            return;
        }
        let html='<div class=\'alert '+(d.running?\'alert-info\':(d.df_failed>0?\'alert-warning\':\'alert-success\'))+\'\'>';
        const statusBadge=d.running?'<span class=\'badge bg-primary\'>运行中</span>':(d.df_failed>0?'<span class=\'badge bg-warning text-dark\'>有降噪失败</span>':'<span class=\'badge bg-success\'>已完成</span>');
        html+='<div class=\'d-flex justify-content-between align-items-center mb-2\'>';
        html+='<strong>'+statusBadge+' '+d.message+'</strong>';
        if(d.running){html+='<span class=\'spinner\'></span>';}
        html+='</div>';
        html+='<table class=\'table table-sm table-bordered mb-0\' style=\'font-size:.85rem;\'>';
        html+='<tr><th style=\'width:120px;\'>已处理</th><td><strong>'+d.processed+'</strong>'+(d.max_chapters>0?' / '+d.max_chapters:'')+'</td></tr>';
        html+='<tr><th>✅ 成功上传</th><td><span class=\'text-success\'>'+d.uploaded+'</span></td></tr>';
        html+='<tr><th>❌ 失败</th><td><span class=\'text-danger\'>'+d.failed+'</span></td></tr>';
        html+='<tr><th>无URL</th><td>'+d.no_url+'</td></tr>';
        html+='<tr><th>⚠️ 降噪失败</th><td>'+(d.df_failed>0?'<span class=\'text-warning fw-bold\'>'+d.df_failed+'</span>':'0')+'</td></tr>';
        html+='<tr><th>整书完成</th><td>'+d.books_completed+'</td></tr>';
        html+='<tr><th>DeepFilter</th><td>'+(d.use_df?'<span class=\'badge bg-success\'>启用</span>':'<span class=\'badge bg-secondary\'>禁用</span>')+'</td></tr>';
        html+='<tr><th>线程数</th><td>'+d.num_workers+'</td></tr>';
        if(d.started_at){html+='<tr><th>开始时间</th><td>'+d.started_at+'</td></tr>';}
        if(d.finished_at){html+='<tr><th>结束时间</th><td>'+d.finished_at+'</td></tr>';}
        html+='</table>';
        html+='</div>';
        el.innerHTML=html;
        if(d.running){setTimeout(()=>refreshBatchStatus(),3000);}
    }catch(e){el.innerHTML='<div class=\'alert alert-danger\'>❌ '+e+'</div>';}
}
window.addEventListener('load',function(){
    const el=document.getElementById('batch-result');
    if(el){refreshBatchStatus();}
});
</script>
</body></html>
'''


if __name__ == '__main__':
    worker.init_worker()
    port = int(os.environ.get('PORT', 7860))
    print(f'>>> Worker 启动: port={port}, slots={NUM_SLOTS}')
    print(f'>>> Bots: {len(worker.BOT_TOKENS)}, MinInterval: {worker.BOT_MIN_INTERVAL}s')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
