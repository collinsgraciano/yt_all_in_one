#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPS 调度器 - Web 管理面板

功能:
  1. 实时监控: 任务统计、Worker 健康、运行日志
  2. 配置管理: 运行时修改 HF Space URL、槽位数、检查间隔等
  3. 手动控制: 启停调度器、手动触发 Worker、重置卡住任务
  4. Telegram API 中继: HF Space Worker 通过 VPS 中继访问 api.telegram.org
  5. Telegram 配置分发: Worker 通过 /api/tg-config 获取 Chat ID / Bot Tokens

启动:
  python web_app.py
  # 或通过 Docker: docker compose up -d --build

端口: 38080 (可通过 WEB_PORT 环境变量修改)
认证: 可选, 设置 WEB_PASSWORD 环境变量启用
"""

import os
import functools

from flask import Flask, request, jsonify, Response

from scheduler import Scheduler

app = Flask(__name__)

# ============================================================
# 配置
# ============================================================

WEB_PASSWORD = os.environ.get('WEB_PASSWORD', '')

# 创建调度器实例 (全局单例)
scheduler = Scheduler(
    dsn=os.environ.get('POSTGRES_DSN', 'postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook'),
    hf_urls=[u.strip() for u in os.environ.get('HF_SPACE_URLS', '').split(',') if u.strip()],
    max_slots=int(os.environ.get('MAX_SLOTS', '2')),
    check_interval=int(os.environ.get('CHECK_INTERVAL', '15')),
    stuck_timeout=int(os.environ.get('STUCK_TIMEOUT_M', '1440')),
    chat_id=os.environ.get('TG_CHAT_ID', ''),
    bot_tokens=os.environ.get('TG_BOT_TOKENS', ''),
    telegram_api_base=os.environ.get('TELEGRAM_API_BASE', ''),
    cleanup_interval=int(os.environ.get('CLEANUP_INTERVAL', '600')),
    cleanup_reset_failed=os.environ.get('CLEANUP_RESET_FAILED', 'false').strip().lower() in ('true', '1', 'yes', 'on'),
    cleanup_auto_enabled=os.environ.get('CLEANUP_AUTO_ENABLED', 'true').strip().lower() in ('true', '1', 'yes', 'on'),
)

# 自动启动调度器
if scheduler.config['hf_urls'] and not any(u.startswith('https://YOUR_') for u in scheduler.config['hf_urls']):
    scheduler.start()


# ============================================================
# 认证
# ============================================================

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if WEB_PASSWORD:
            auth = request.authorization
            if not auth or auth.password != WEB_PASSWORD:
                return Response(
                    '需要认证\n',
                    401,
                    {'WWW-Authenticate': 'Basic realm="Scheduler"'}
                )
        return f(*args, **kwargs)
    return decorated


# ============================================================
# 页面路由
# ============================================================

@app.route('/')
@require_auth
def index():
    return HTML_PAGE


# ============================================================
# API 路由
# ============================================================

@app.route('/api/status')
@require_auth
def api_status():
    return jsonify(scheduler.get_status())


@app.route('/api/config', methods=['GET'])
@require_auth
def api_get_config():
    return jsonify(scheduler.get_config())


@app.route('/api/config', methods=['POST'])
@require_auth
def api_update_config():
    data = request.get_json(silent=True) or {}
    scheduler.update_config(**data)
    return jsonify({'ok': True, 'message': '配置已更新'})


@app.route('/api/logs')
@require_auth
def api_logs():
    count = request.args.get('count', 200, type=int)
    return jsonify({'logs': scheduler.get_logs(count)})


@app.route('/api/scheduler/start', methods=['POST'])
@require_auth
def api_start():
    scheduler.start()
    return jsonify({'ok': True, 'message': '调度器已启动'})


@app.route('/api/scheduler/stop', methods=['POST'])
@require_auth
def api_stop():
    scheduler.stop()
    return jsonify({'ok': True, 'message': '调度器已停止'})


@app.route('/api/trigger', methods=['POST'])
@require_auth
def api_trigger():
    data = request.get_json(silent=True) or {}
    worker_index = data.get('worker_index', 0)
    ok, msg = scheduler.trigger_worker_now(worker_index)
    return jsonify({'ok': ok, 'message': msg})


@app.route('/api/reset-stuck', methods=['POST'])
@require_auth
def api_reset_stuck():
    ok = scheduler.reset_stuck_now()
    return jsonify({'ok': ok, 'message': '卡住任务已重置' if ok else '重置失败'})


@app.route('/api/cleanup/run', methods=['POST'])
@require_auth
def api_run_cleanup():
    result = scheduler.run_cleanup_now()
    if result['ok']:
        msg = (f"清理完成: 重置processing {result['reset_processing']}个"
               f", 重置failed {result['reset_failed']}个")
    else:
        msg = result.get('error', '清理失败')
    return jsonify({'ok': result['ok'], 'message': msg, 'result': result})


@app.route('/api/check-workers', methods=['POST'])
@require_auth
def api_check_workers():
    results = scheduler.check_workers()
    return jsonify({'ok': True, 'workers': results})


# ============================================================
# Telegram API 中继 (供 HF Space Worker 使用)
# ============================================================

@app.route('/tg-api/<path:tg_path>', methods=['GET', 'POST'])
def tg_api_proxy(tg_path):
    """Telegram Bot API 中继

    用法 (HF Space 端):
      原始: https://api.telegram.org/bot<TOKEN>/sendAudio
      中继: http://<VPS_IP>:38080/tg-api/bot<TOKEN>/sendAudio

    支持 multipart 文件上传 (sendAudio 等)。
    不需要认证 (Bot Token 本身就是认证)。
    """
    import requests as req

    target_url = f'https://api.telegram.org/{tg_path}'

    if request.query_string:
        target_url += '?' + request.query_string.decode()

    try:
        if request.method == 'POST':
            files = {}
            for key, file_storage in request.files.items():
                files[key] = (
                    file_storage.filename,
                    file_storage.stream.read(),
                    file_storage.content_type,
                )
            data = {k: v for k, v in request.form.items()}
            resp = req.post(target_url, data=data, files=files, timeout=(30, 600))
        else:
            resp = req.get(target_url, timeout=(10, 60))

        resp_headers = {'Content-Type': resp.headers.get('Content-Type', 'application/json')}
        return resp.content, resp.status_code, resp_headers

    except req.exceptions.ConnectTimeout:
        return jsonify({'ok': False, 'error': 'VPS->Telegram 连接超时 (ConnectTimeout)'}), 504
    except req.exceptions.ReadTimeout:
        return jsonify({'ok': False, 'error': 'VPS->Telegram 读取超时 (ReadTimeout, 600s)'}), 504
    except req.exceptions.ConnectionError as e:
        return jsonify({'ok': False, 'error': f'VPS->Telegram 连接失败: {str(e)[:200]}'}), 502
    except Exception as e:
        return jsonify({'ok': False, 'error': f'中继异常: {str(e)[:200]}'}), 500


# ============================================================
# Telegram 配置接口 (供 HF Space Worker 获取)
# ============================================================

@app.route('/api/tg-config')
def api_tg_config():
    """返回 Telegram 配置 (无需认证, 供 Worker 获取)

    返回: {chat_id, bot_tokens, telegram_api_base}
    如果都为空则返回 404 (Worker 会使用本地环境变量)
    """
    cfg = scheduler.get_tg_config()
    if not cfg['chat_id'] and not cfg['bot_tokens'] and not cfg['telegram_api_base']:
        return jsonify({'ok': False, 'message': 'Telegram 配置未设置, 请在管理面板配置'}), 404
    return jsonify({'ok': True, **cfg})


# ============================================================
# HTML 页面
# ============================================================

HTML_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>有声书调度器 - 管理面板</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #0d1117;
    color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    min-height: 100vh;
    padding: 20px;
}
.container { max-width: 1200px; margin: 0 auto; }
.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    padding: 20px 24px;
    background: linear-gradient(135deg, #161b22 0%, #1c2333 100%);
    border-radius: 16px;
    border: 1px solid #30363d;
}
.header h1 {
    font-size: 1.4rem;
    background: linear-gradient(135deg, #58a6ff, #bc8cff);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
.header-right { display: flex; align-items: center; gap: 12px; }
.status-badge {
    padding: 6px 16px; border-radius: 20px;
    font-weight: 600; font-size: 0.8rem; border: 1px solid;
}
.s-running { background: rgba(35,134,54,0.15); color: #3fb950; border-color: #238636; }
.s-stopped { background: rgba(248,81,73,0.15); color: #f85149; border-color: #da3633; }
.s-error { background: rgba(210,153,34,0.15); color: #d29922; border-color: #9e6a03; }
.info-bar { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 16px; font-size: 0.8125rem; color: #8b949e; }
.info-bar strong { color: #c9d1d9; }
.stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 16px; }
@media (max-width: 768px) { .stats-grid { grid-template-columns: repeat(2, 1fr); } }
.stat-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 12px;
    padding: 20px; text-align: center; transition: transform 0.15s, box-shadow 0.15s;
}
.stat-card:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
.stat-number { font-size: 2rem; font-weight: 700; line-height: 1.2; }
.stat-label { color: #8b949e; font-size: 0.8125rem; margin-top: 4px; }
.stat-pending .stat-number { color: #58a6ff; }
.stat-processing .stat-number { color: #d29922; }
.stat-uploaded .stat-number { color: #3fb950; }
.stat-failed .stat-number { color: #f85149; }
.card {
    background: #161b22; border: 1px solid #30363d; border-radius: 12px;
    padding: 20px; margin-bottom: 16px;
}
.card h2 {
    font-size: 1.05rem; margin-bottom: 16px; display: flex;
    align-items: center; gap: 8px; color: #e6edf3;
}
.worker-card {
    background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
    padding: 14px 16px; margin-bottom: 8px; display: flex;
    justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;
}
.worker-online { border-color: rgba(35,134,54,0.5); }
.worker-offline { border-color: rgba(248,81,73,0.5); }
.worker-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.dot-on { background: #3fb950; box-shadow: 0 0 6px rgba(63,185,80,0.6); }
.dot-off { background: #f85149; }
.worker-url { font-family: 'SF Mono', 'Consolas', monospace; font-size: 0.75rem; color: #8b949e; margin-left: 4px; word-break: break-all; }
.worker-slots { font-size: 0.8125rem; font-weight: 600; }
.config-form { display: grid; gap: 14px; }
.config-row { display: grid; grid-template-columns: 160px 1fr; align-items: start; gap: 12px; }
@media (max-width: 600px) { .config-row { grid-template-columns: 1fr; } }
.config-row label { color: #8b949e; font-size: 0.8125rem; padding-top: 8px; }
.config-row input, .config-row select {
    background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    padding: 8px 12px; color: #c9d1d9; font-size: 0.8125rem;
    font-family: 'SF Mono', 'Consolas', monospace; width: 100%;
}
.config-row input:focus { outline: none; border-color: #58a6ff; box-shadow: 0 0 0 3px rgba(88,166,255,0.1); }
.url-hint { color: #d29922; font-size: 0.7rem; margin-top: 4px; line-height: 1.4; }
.url-hint code { color: #58a6ff; }
.btn {
    padding: 8px 18px; border-radius: 6px; border: 1px solid transparent;
    font-size: 0.8125rem; font-weight: 600; cursor: pointer; transition: all 0.15s;
    display: inline-flex; align-items: center; gap: 4px;
}
.btn:active { transform: scale(0.97); }
.btn-primary { background: #238636; color: #fff; border-color: #238636; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; color: #fff; border-color: #da3633; }
.btn-danger:hover { background: #f85149; }
.btn-warning { background: #9e6a03; color: #fff; border-color: #9e6a03; }
.btn-warning:hover { background: #bb8009; }
.btn-secondary { background: #21262d; color: #c9d1d9; border-color: #30363d; }
.btn-secondary:hover { background: #30363d; border-color: #484f58; }
.btn-sm { padding: 4px 12px; font-size: 0.7rem; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; }
.logs-box {
    background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px; max-height: 420px; overflow-y: auto;
    font-family: 'SF Mono', 'Consolas', 'Monaco', monospace; font-size: 0.75rem; line-height: 1.7;
}
.logs-box::-webkit-scrollbar { width: 6px; }
.logs-box::-webkit-scrollbar-track { background: #0d1117; }
.logs-box::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
.logs-box::-webkit-scrollbar-thumb:hover { background: #484f58; }
.log-line { white-space: pre-wrap; word-break: break-word; }
.log-error { color: #f85149; }
.log-warning { color: #d29922; }
.log-ok { color: #3fb950; }
.log-info { color: #58a6ff; }
.log-trigger { color: #bc8cff; }
.toast {
    position: fixed; top: 20px; right: 20px; padding: 10px 20px;
    border-radius: 8px; color: #fff; font-weight: 600; font-size: 0.8125rem;
    z-index: 9999; animation: slideIn 0.25s ease; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}
.toast-success { background: #238636; }
.toast-error { background: #da3633; }
@keyframes slideIn { from { transform: translateX(120%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
.pulse-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #3fb950; margin-right: 6px; animation: pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(0.85); } }
</style>
</head>
<body>
<div class="container">
<div class="header">
    <h1>🎧 有声书调度器</h1>
    <div class="header-right">
        <span id="status-badge" class="status-badge s-stopped">已停止</span>
    </div>
</div>
<div class="info-bar">
    <span>⏱ 运行时间: <strong id="uptime">-</strong></span>
    <span>📦 总触发: <strong id="total-triggered">0</strong></span>
    <span>🔧 总容量: <strong id="total-capacity">0</strong></span>
    <span>🟢 Worker空闲: <strong id="total-free">0</strong>/<strong id="total-capacity-slots">0</strong></span>
    <span>🕐 最后触发: <strong id="last-trigger">-</strong></span>
</div>
<div class="stats-grid">
    <div class="stat-card stat-pending"><div class="stat-number" id="stat-pending">0</div><div class="stat-label">待处理</div></div>
    <div class="stat-card stat-processing"><div class="stat-number" id="stat-processing">0</div><div class="stat-label">处理中</div></div>
    <div class="stat-card stat-uploaded"><div class="stat-number" id="stat-uploaded">0</div><div class="stat-label">已上传</div></div>
    <div class="stat-card stat-failed"><div class="stat-number" id="stat-failed">0</div><div class="stat-label">已失败</div></div>
</div>
<div class="card">
    <h2>🖥️ Worker 状态</h2>
    <div id="workers"><p style="color:#8b949e">加载中...</p></div>
</div>
<div class="card">
    <h2>⚙️ 配置管理 <span style="font-size:0.75rem;color:#8b949e;font-weight:400">(保存后立即生效, 无需重启)</span></h2>
    <form class="config-form" id="config-form" onsubmit="saveConfig(event)">
        <div class="config-row"><label>HF Space URLs</label><div>
            <input type="text" id="cfg-hf-urls" placeholder="https://用户名-空间名.hf.space">
            <div class="url-hint">⚠️ 正确格式: <code>https://用户名-空间名.hf.space</code><br>例如: <code>https://r777r7-t1.hf.space</code><br>❌ 不要用页面地址: <code>https://huggingface.co/spaces/...</code><br>多个 Worker 用逗号分隔</div>
        </div></div>
        <div class="config-row"><label>每Worker槽位数</label><input type="number" id="cfg-max-slots" value="2" min="1" max="4"></div>
        <div class="config-row"><label>检查间隔 (秒)</label><input type="number" id="cfg-check-interval" value="15" min="5" max="300"></div>
        <div class="config-row"><label>卡住超时 (分钟)</label><input type="number" id="cfg-stuck-timeout" value="1440" min="5" max="1440"></div>
        <div class="config-row"><label>PostgreSQL DSN</label><div>
            <input type="password" id="cfg-dsn" placeholder="postgresql://user:pass@host:5432/db">
            <div class="url-hint">留空则不修改当前值</div>
        </div></div>
        <div class="config-row"><label></label><button type="submit" class="btn btn-primary">💾 保存配置</button></div>
    </form>
</div>
<div class="card">
    <h2>🤖 Telegram 配置 <span style="font-size:0.75rem;color:#8b949e;font-weight:400">(多Bot模式, 供 Worker 通过 API 获取)</span></h2>
    <form class="config-form" id="tg-config-form" onsubmit="saveTgConfig(event)">
        <div class="config-row"><label>Chat ID</label><div>
            <input type="text" id="cfg-tg-chat-id" placeholder="例如: -1001234567890">
            <div class="url-hint">目标 Telegram 群/频道 ID</div>
        </div></div>
        <div class="config-row"><label>Bot Tokens</label><div>
            <input type="password" id="cfg-tg-bot-tokens" placeholder="token1,token2,token3">
            <div class="url-hint">多个 Bot Token 用逗号分隔<br>留空则不修改当前值</div>
        </div></div>
        <div class="config-row"><label>API Base</label><div>
            <input type="text" id="cfg-tg-api-base" placeholder="https://api.telegram.org">
            <div class="url-hint">Telegram API 地址, 默认 <code>https://api.telegram.org</code><br>可填 VPS 中继: <code>http://VPS_IP:38080/tg-api</code></div>
        </div></div>
        <div class="config-row"><label></label><button type="submit" class="btn btn-primary">💾 保存 Telegram 配置</button></div>
    </form>
</div>
<div class="card">
    <h2>🧹 清理配置 <span style="font-size:0.75rem;color:#8b949e;font-weight:400">(重置卡住和失败章节)</span></h2>
    <form class="config-form" id="cleanup-config-form" onsubmit="saveCleanupConfig(event)">
        <div class="config-row"><label>自动清理</label><div>
            <select id="cfg-cleanup-auto"><option value="true">启用 (调度器自动定期清理)</option><option value="false">禁用 (仅手动清理)</option></select>
        </div></div>
        <div class="config-row"><label>清理间隔 (秒)</label><div>
            <input type="number" id="cfg-cleanup-interval" value="600" min="60" max="86400">
            <div class="url-hint">自动清理的间隔时间 (默认 600 秒 = 10 分钟)</div>
        </div></div>
        <div class="config-row"><label>卡住超时 (分钟)</label><div>
            <input type="number" id="cfg-cleanup-stuck-timeout" value="1440" min="5" max="10080">
            <div class="url-hint">processing 超过此时间则重置为 pending (默认 1440 分钟 = 24 小时)</div>
        </div></div>
        <div class="config-row"><label>重置 failed 章节</label><div>
            <select id="cfg-cleanup-reset-failed"><option value="true">是 (清理时同时重置 failed 为 pending)</option><option value="false">否 (仅重置卡住的 processing)</option></select>
        </div></div>
        <div class="config-row"><label></label>
            <button type="submit" class="btn btn-primary">💾 保存清理配置</button>
            <button type="button" class="btn btn-danger" style="margin-left:8px" onclick="runCleanupNow()">🧹 立刻运行清理</button>
        </div>
    </form>
</div>
<div class="card">
    <h2>🎮 控制台</h2>
    <div class="controls">
        <button class="btn btn-primary" onclick="startScheduler()">▶️ 启动调度器</button>
        <button class="btn btn-danger" onclick="stopScheduler()">⏹️ 停止调度器</button>
        <button class="btn btn-warning" onclick="triggerWorker()">🔥 手动触发</button>
        <button class="btn btn-secondary" onclick="resetStuck()">♻️ 重置卡住任务</button>
        <button class="btn btn-secondary" onclick="checkWorkersNow()">🔍 检查Worker</button>
    </div>
</div>
<div class="card">
    <h2>📋 实时日志 <span style="font-size:0.75rem;color:#8b949e;font-weight:400">(最近 500 条, 每 3 秒刷新)</span></h2>
    <div class="logs-box" id="logs"><p style="color:#8b949e">等待日志...</p></div>
</div>
</div>
<script>
const API='';
function escapeHtml(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML;}
function showToast(msg,ok){const t=document.createElement('div');t.className='toast toast-'+(ok?'success':'error');t.textContent=msg;document.body.appendChild(t);setTimeout(()=>{t.style.opacity='0';t.style.transform='translateX(120%)';t.style.transition='all 0.3s';},2500);setTimeout(()=>t.remove(),3000);}
async function fetchStatus(){try{const r=await fetch(API+'/api/status');const d=await r.json();updateUI(d);}catch(e){console.error('fetchStatus:',e);}}
function updateUI(d){
    const badge=document.getElementById('status-badge');
    let cls='s-stopped';if(d.running)cls='s-running';else if(d.status_text&&(d.status_text.includes('错误')||d.status_text.includes('失败')))cls='s-error';
    badge.className='status-badge '+cls;let icon='';if(d.running)icon='<span class="pulse-dot"></span>';badge.innerHTML=icon+escapeHtml(d.status_text||'未知');
    const s=d.stats||{};document.getElementById('stat-pending').textContent=s.pending||0;document.getElementById('stat-processing').textContent=s.processing||0;document.getElementById('stat-uploaded').textContent=s.uploaded||0;document.getElementById('stat-failed').textContent=s.failed||0;
    document.getElementById('total-triggered').textContent=d.total_triggered||0;document.getElementById('total-capacity').textContent=(d.total_capacity||0)+' 个任务';document.getElementById('total-free').textContent=d.total_free_slots||0;document.getElementById('total-capacity-slots').textContent=d.total_capacity||0;document.getElementById('last-trigger').textContent=d.last_trigger_time||'-';
    if(d.uptime!=null){const h=Math.floor(d.uptime/3600);const m=Math.floor((d.uptime%3600)/60);const sec=d.uptime%60;document.getElementById('uptime').textContent=(h>0?h+'h ':'')+m+'m '+sec+'s';}else{document.getElementById('uptime').textContent='-';}
    const wDiv=document.getElementById('workers');
    if(d.workers&&d.workers.length>0){wDiv.innerHTML=d.workers.map((w,i)=>{const st=w.online?'online':'offline';const dot=w.online?'dot-on':'dot-off';const slots=w.online?(w.free_slots>0?'<span style="color:#3fb950">空闲 '+w.free_slots+'/'+w.total_slots+'</span>':'<span style="color:#d29922">忙碌 '+w.total_slots+'/'+w.total_slots+'</span>'):'<span style="color:#f85149">离线</span>';const wid=w.worker_id&&w.worker_id!=='?'?' <span style="color:#8b949e;font-size:0.7rem">'+escapeHtml(w.worker_id)+'</span>':'';return '<div class="worker-card worker-'+st+'"><div><span class="worker-dot '+dot+'"></span><strong>Worker '+(i+1)+'</strong><span class="worker-url">'+escapeHtml(w.url)+'</span></div><div><span class="worker-slots">'+slots+'</span>'+wid+' <button class="btn btn-secondary btn-sm" style="margin-left:8px" onclick="triggerWorker('+i+')">触发</button></div></div>';}).join('');}else{wDiv.innerHTML='<p style="color:#d29922">⚠️ 未配置任何 HF Space URL, 请在下方配置中添加</p>';}
}
async function fetchConfig(){try{const r=await fetch(API+'/api/config');const d=await r.json();document.getElementById('cfg-hf-urls').value=d.hf_urls||'';document.getElementById('cfg-max-slots').value=d.max_slots||2;document.getElementById('cfg-check-interval').value=d.check_interval||15;document.getElementById('cfg-stuck-timeout').value=d.stuck_timeout||30;document.getElementById('cfg-tg-chat-id').value=d.chat_id||'';document.getElementById('cfg-tg-api-base').value=d.telegram_api_base||'';if(d.bot_tokens){const count=d.bot_tokens.split(',').filter(t=>t.trim()).length;document.getElementById('cfg-tg-bot-tokens').placeholder=`已配置 ${count} 个 Token (留空不修改)`;}document.getElementById('cfg-cleanup-interval').value=d.cleanup_interval||600;document.getElementById('cfg-cleanup-stuck-timeout').value=d.stuck_timeout||1440;document.getElementById('cfg-cleanup-auto').value=d.cleanup_auto_enabled?'true':'false';document.getElementById('cfg-cleanup-reset-failed').value=d.cleanup_reset_failed?'true':'false';}catch(e){console.error('fetchConfig:',e);}}
async function fetchLogs(){try{const r=await fetch(API+'/api/logs');const d=await r.json();const box=document.getElementById('logs');const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<60;if(d.logs&&d.logs.length>0){box.innerHTML=d.logs.map(line=>{let cls='log-line';const l=line.toLowerCase();if(l.includes('[错误]')||l.includes('[失败]')||l.includes('异常'))cls+=' log-error';else if(l.includes('[警告]')||l.includes('[ok]'))cls+=' log-warning';else if(l.includes('✅')||l.includes('成功'))cls+=' log-ok';else if(l.includes('>>>')||l.includes('[状态]')||l.includes('[配置'))cls+=' log-info';else if(l.includes('触发')||l.includes('结果'))cls+=' log-trigger';return '<div class="'+cls+'">'+escapeHtml(line)+'</div>';}).join('');if(atBottom)box.scrollTop=box.scrollHeight;}else{box.innerHTML='<p style="color:#8b949e">暂无日志</p>';}}catch(e){console.error('fetchLogs:',e);}}
async function saveConfig(e){e.preventDefault();const cfg={hf_urls:document.getElementById('cfg-hf-urls').value,max_slots:document.getElementById('cfg-max-slots').value,check_interval:document.getElementById('cfg-check-interval').value,stuck_timeout:document.getElementById('cfg-stuck-timeout').value};const dsn=document.getElementById('cfg-dsn').value;if(dsn)cfg.dsn=dsn;try{const r=await fetch(API+'/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});const d=await r.json();showToast(d.message||'配置已保存',d.ok);if(d.ok){document.getElementById('cfg-dsn').value='';fetchStatus();}}catch(e){showToast('保存失败: '+e,false);}}
async function saveTgConfig(e){e.preventDefault();const cfg={};const chatId=document.getElementById('cfg-tg-chat-id').value;const apiBase=document.getElementById('cfg-tg-api-base').value;if(chatId!=='')cfg.chat_id=chatId;if(apiBase!=='')cfg.telegram_api_base=apiBase;const botTokens=document.getElementById('cfg-tg-bot-tokens').value;if(botTokens)cfg.bot_tokens=botTokens;try{const r=await fetch(API+'/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});const d=await r.json();showToast(d.message||'Telegram 配置已保存',d.ok);if(d.ok){document.getElementById('cfg-tg-bot-tokens').value='';fetchConfig();}}catch(e){showToast('保存失败: '+e,false);}}
async function startScheduler(){try{const r=await fetch(API+'/api/scheduler/start',{method:'POST'});const d=await r.json();showToast(d.message||'已启动',d.ok);fetchStatus();}catch(e){showToast('启动失败: '+e,false);}}
async function stopScheduler(){try{const r=await fetch(API+'/api/scheduler/stop',{method:'POST'});const d=await r.json();showToast(d.message||'已停止',d.ok);fetchStatus();}catch(e){showToast('停止失败: '+e,false);}}
async function triggerWorker(idx){const wi=(typeof idx==='number')?idx:0;try{const r=await fetch(API+'/api/trigger',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({worker_index:wi})});const d=await r.json();showToast(d.message||(d.ok?'已触发':'触发失败'),d.ok);fetchStatus();}catch(e){showToast('触发失败: '+e,false);}}
async function resetStuck(){try{const r=await fetch(API+'/api/reset-stuck',{method:'POST'});const d=await r.json();showToast(d.message||'已重置',d.ok);fetchStatus();}catch(e){showToast('重置失败: '+e,false);}}
async function checkWorkersNow(){try{const r=await fetch(API+'/api/check-workers',{method:'POST'});const d=await r.json();showToast('Worker 状态已刷新',true);fetchStatus();}catch(e){showToast('检查失败: '+e,false);}}
async function saveCleanupConfig(e){e.preventDefault();const cfg={cleanup_interval:document.getElementById('cfg-cleanup-interval').value,stuck_timeout:document.getElementById('cfg-cleanup-stuck-timeout').value,cleanup_auto_enabled:document.getElementById('cfg-cleanup-auto').value,cleanup_reset_failed:document.getElementById('cfg-cleanup-reset-failed').value};try{const r=await fetch(API+'/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});const d=await r.json();showToast(d.message||'清理配置已保存',d.ok);if(d.ok)fetchStatus();}catch(e){showToast('保存失败: '+e,false);}}
async function runCleanupNow(){if(!confirm('确定立刻运行清理吗？这将重置卡住和失败的章节。'))return;try{showToast('清理运行中...',true);const r=await fetch(API+'/api/cleanup/run',{method:'POST'});const d=await r.json();if(d.ok){const res=d.result||{};showToast(`清理完成: processing ${res.reset_processing||0}个, failed ${res.reset_failed||0}个`,true);}else{showToast(d.message||'清理失败',false);}fetchStatus();fetchLogs();}catch(e){showToast('清理失败: '+e,false);}}
fetchConfig();fetchStatus();fetchLogs();setInterval(fetchStatus,3000);setInterval(fetchLogs,3000);
</script>
</body>
</html>'''


# ============================================================
# 启动
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('WEB_PORT', '38080'))

    print('=' * 60)
    print('  VPS 调度器 - Web 管理面板')
    print('=' * 60)
    print(f'  监听端口:    {port}')
    print(f'  认证:        {"已启用 (WEB_PASSWORD)" if WEB_PASSWORD else "未启用 (设置 WEB_PASSWORD 环境变量启用)"}')
    print(f'  HF Space URL: {scheduler.config["hf_urls"][0] if scheduler.config["hf_urls"] else "(未配置)"}')
    print(f'  调度器状态:   {"运行中" if scheduler.running else "已停止 (需在面板手动启动)"}')
    print('=' * 60)
    print(f'  访问地址:    http://localhost:{port}')
    print('=' * 60)

    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
