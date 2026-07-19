# HF 免费 Docker 空间外包处理计划书

> 项目：有声书 YouTube 频道管理系统
> 目标：将「仅 TG 缓存完整书处理 + 上传」这一算力密集型流程外包给多个 HF（Hugging Face）免费 Docker 空间执行，VPS 仅负责轻量调度。
> 编制日期：2026-07-19

---

## 一、项目背景与目标

### 1.1 痛点

当前「TG 缓存完整书处理 + 上传」流程包含三个算力密集步骤：

1. **下载音频**：从掌阅源下载章节 MP3（网络 + 磁盘 IO）
2. **DeepFilter 降噪**：CPU 密集型，单章节耗时 10-60 秒（`pipeline/deepfilter.py`）
3. **上传 Telegram**：网络 IO，多 Bot 轮换上传（`pipeline/tg_audio.py`）

这些步骤如果全部跑在 VPS 上，会带来：
- **CPU 占用高**：DeepFilter 降噪吃满 CPU，影响 backend FastAPI 服务和 YouTube pipeline 的正常运行
- **内存压力大**：低配 VPS（1-2 核 / 1-2G 内存）并发处理时容易 OOM
- **带宽抢占**：下载 + 上传同时进行，抢占 YouTube 上传带宽

### 1.2 目标

| 目标 | 说明 |
|------|------|
| **算力外包** | 把下载 → 降噪 → 上传 TG 三步全部迁移到 HF 免费 Docker 空间执行，VPS 几乎零算力负担 |
| **多空间随机待机** | 部署 N 个 HF Space（推荐 3-5 个），随机待机，谁空闲谁接活 |
| **排队处理** | 任务通过 PostgreSQL 共享队列下发，多 Worker 并行不冲突 |
| **结果回报** | 每完成一个章节，自动写回数据库（成功/失败 + 错误信息），VPS 实时可见 |
| **零运维** | HF Space 免费版会自动休眠/冷启动，调度器需自动容错，无需人工干预 |

### 1.3 现状结论（重要）

经过代码审计，**项目现有的 `audiobook_pipeline` 子系统已经完整实现了上述目标的核心能力**：

- `audiobook_pipeline/hf_space/` — HF Space Worker（Flask 服务 + 多Bot轮换上传 + 批量处理）
- `audiobook_pipeline/vps_scheduler/` — VPS 调度器（轮询 PG + 触发 Worker + Web 管理面板 + TG API 中继）

因此本计划书的重点**不是重新开发，而是**：
1. 梳理现有架构，明确各组件职责
2. 给出多 Worker 随机待机的完整部署方案
3. 补充优化点（结果回调通知、监控告警等）

---

## 二、现有架构梳理

### 2.1 整体架构

```
                         ┌─────────────────────────────┐
                         │     VPS (低配, 仅调度)       │
                         │  ┌───────────────────────┐  │
                         │  │  vps_scheduler        │  │
                         │  │  (Flask Web + 后台线程)│  │
                         │  │  - 轮询 PostgreSQL     │  │
                         │  │  - 触发 HF Worker      │  │
                         │  │  - TG API 中继         │  │
                         │  │  - 配置分发            │  │
                         │  └───────────┬───────────┘  │
                         │              │              │
                         │  ┌───────────▼───────────┐  │
                         │  │     PostgreSQL         │  │
                         │  │  books                 │  │
                         │  │  audiobook_chapters    │  │  ← 共享任务队列
                         │  │  (upload_status 字段)   │  │
                         │  └───────────┬───────────┘  │
                         └──────────────┼──────────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
              ┌─────▼─────┐      ┌─────▼─────┐      ┌─────▼─────┐
              │ HF Space  │      │ HF Space  │      │ HF Space  │
              │ Worker #1 │      │ Worker #2 │      │ Worker #N │
              │ (2 vCPU)  │      │ (2 vCPU)  │      │ (2 vCPU)  │
              │ 2 槽位    │      │ 2 槽位    │      │ 2 槽位    │
              └─────┬─────┘      └─────┬─────┘      └─────┬─────┘
                    │                   │                   │
                    │  下载 → 降噪 → 上传 TG (多Bot轮换)    │
                    │                   │                   │
                    └───────────────────┼───────────────────┘
                                        │
                                 ┌──────▼──────┐
                                 │  Telegram   │
                                 │ (多Bot存储)  │
                                 └─────────────┘
```

### 2.2 核心组件

#### 组件 1：HF Space Worker（`audiobook_pipeline/hf_space/`）

| 文件 | 职责 |
|------|------|
| `app.py` | Flask 服务，提供状态面板 + `/process`（触发）+ `/health`（健康检查）+ `/process-batch`（批量）+ `/batch-status`（进度）等 API |
| `worker.py` | Worker 核心：认领任务 → 下载音频 → DeepFilter 降噪 → 多Bot轮换上传 TG → 更新 DB |
| `Dockerfile` | 基于 `python:3.11-slim`，内置 ffmpeg + DeepFilter 二进制 |
| `requirements.txt` | flask + psycopg2-binary + requests |

**单章节处理流程**（`worker.py::run_one`）：

```
1. claim_next_chapter()        — 原子认领一个 pending 章节 (FOR UPDATE SKIP LOCKED)
2. download_audio_file()       — 下载 MP3 + ffprobe 校验
3. process_audio()             — DeepFilter 降噪 (可选, _use_df)
4. upload_to_telegram_multi_bot() — 多Bot轮换上传, 自动切换 429 冷却的 Bot
5. record_upload()             — 写回 telegram_file_id / upload_status / error_message
6. check_and_mark_book_complete() — 整书完成则标记 books.book_status='success'
```

**关键能力**：
- ✅ 多槽位并行（`NUM_SLOTS`，默认 2 = HF vCPU 数）
- ✅ 批量处理（`/process-batch`，ThreadPoolExecutor 多线程并发）
- ✅ 多 Bot 轮换（`BotPool` 类，429 自动冷却切换）
- ✅ TG 配置从 VPS 获取（`/refresh-tg-config`，无需在 HF 硬编码密钥）
- ✅ 结果回报（直接写 PostgreSQL，`upload_status` + `error_message`）

#### 组件 2：VPS 调度器（`audiobook_pipeline/vps_scheduler/`）

| 文件 | 职责 |
|------|------|
| `scheduler.py` | `Scheduler` 类：后台线程轮询 PG，有空闲槽位时触发 Worker；支持运行时配置修改 |
| `web_app.py` | Flask Web 面板（端口 38080）：实时监控 + 配置管理 + 手动控制 + **TG API 中继** + **TG 配置分发** |
| `docker-compose.yml` | 一键部署 |
| `Dockerfile` | 极轻量（python:3.12-slim + flask + psycopg2 + requests） |

**调度器主循环**（`scheduler.py::_main_loop`）：

```
每 15 秒:
  1. get_stats()           — 查询 pending / processing / uploaded / failed 数量
  2. check_workers()       — 对所有 HF Space 调用 /health, 获取空闲槽位
  3. 若 pending > 0 且有空闲 Worker:
       trigger_worker(url) — POST /process 触发该 Worker 处理一个章节
  4. 若所有 Worker 满载: 等待 5s 重试
  5. 若所有 Worker 离线: 等待 30s 重试 (可能是冷启动)
  6. 定期自动清理卡住任务 (默认 600s 一次)
```

**关键能力**：
- ✅ 多 Worker 支持（`HF_SPACE_URLS` 逗号分隔多个 URL）
- ✅ 随机待机触发（检查所有 Worker 健康状态，优先触发有空闲槽位的）
- ✅ 离线容错（Worker 冷启动/休眠时自动跳过，恢复后自动纳入）
- ✅ TG API 中继（HF Space 无法直连 `api.telegram.org`，通过 VPS 中转）
- ✅ TG 配置分发（`/api/tg-config`，Worker 启动时自动拉取 Bot Token / Chat ID）
- ✅ Web 管理面板（实时监控、配置修改、手动触发、日志查看）

### 2.3 数据库支撑

`audiobook_chapters` 表（`docker/init-db.sql`）是任务队列的核心：

| 列名 | 作用 |
|------|------|
| `upload_status` | 任务状态：`pending` / `processing` / `uploaded` / `failed` |
| `worker_id` | 认领该章节的 Worker ID（多 Worker 并行互不冲突） |
| `claimed_at` | 认领时间（用于检测卡住任务） |
| `telegram_file_id` | 上传成功后的 TG 文件 ID（pipeline 后续用此从 TG 下载） |
| `telegram_bot_user_id` | 上传 Bot 的永久 ID（下载时匹配正确 Bot Token） |
| `error_message` | 失败原因记录 |

**原子认领机制**（`worker.py::claim_next_chapter`）：

```sql
UPDATE audiobook_chapters SET upload_status = 'processing', worker_id = %s, claimed_at = NOW()
WHERE ctid IN (
    SELECT ctid FROM audiobook_chapters
    WHERE upload_status = 'pending'
    ORDER BY book_id, chapter_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED   -- ← 关键: 跳过已被其他 Worker 锁定的行
)
RETURNING book_id, chapter_id, book_name, chapter_name, audio_url
```

`FOR UPDATE SKIP LOCKED` 保证多个 Worker 并发认领时不会拿到同一行，天然实现了排队。

---

## 三、多 Worker 随机待机方案

### 3.1 方案概述

部署 N 个 HF Space（推荐 3-5 个），每个 Space 运行一个 Worker 实例（2 槽位）。所有 Worker 共享同一个 PostgreSQL，通过 `FOR UPDATE SKIP LOCKED` 实现无冲突并行。

VPS 调度器作为「中枢」，持续监控所有 Worker 的健康状态，谁有空闲槽位就把任务派给谁。

### 3.2 Worker 池管理

调度器维护一个 Worker 状态表（`scheduler.py::check_workers`）：

```python
worker_status = [
    {
        'url': 'https://user-worker1.hf.space',
        'online': True,           # /health 是否可达
        'free_slots': 2,          # 空闲槽位数
        'total_slots': 2,         # 总槽位数
        'worker_id': 'hf_a1b2c3d4',
    },
    {
        'url': 'https://user-worker2.hf.space',
        'online': False,          # 冷启动中/休眠
        'free_slots': 0,
        'total_slots': 0,
        'worker_id': '?',
    },
    ...
]
```

**触发策略**（`scheduler.py::_main_loop` 第 617-639 行）：

```python
# 筛选在线且有空闲槽位的 Worker
online_workers = [w for w in worker_status if w['online'] and w['free_slots'] > 0]

for w in online_workers:
    if pending <= 0:
        break
    # 触发该 Worker 处理一个章节
    ok, msg, free_after = trigger_worker(w['url'])
    if ok:
        total_triggered += 1
        break  # 一次循环只触发一个, 下次循环再触发下一个
```

这实现了「随机待机接受任务下发」——调度器不是轮询派发，而是**检测到谁空闲就派给谁**。

### 3.3 冷启动与休眠容错

HF Space 免费版特性：
- **无流量 15 分钟后自动休眠**
- **休眠后首次请求需冷启动**（约 30-60 秒，需下载 DeepFilter 二进制）

容错策略（已实现）：

| 场景 | 调度器行为 | Worker 行为 |
|------|-----------|-------------|
| Worker 休眠 | `/health` 超时 → 标记 `online=False` → 跳过 | — |
| Worker 冷启动中 | `/health` 超时 → 等待 30s 重试 | 启动后 `/health` 恢复可达 |
| Worker 在线但满载 | `free_slots=0` → 等待 5s | 处理完成后槽位释放 |
| Worker 触发超时 | `trigger_worker` 返回超时提示 → 视为可能冷启动中 | — |
| Worker 彻底不可达 | 连续失败 → 跳过, 尝试其他 Worker | — |

### 3.4 推荐部署规模

| HF Space 数量 | 总并行槽位 | 适用场景 |
|:---:|:---:|------|
| 1 个 | 2 | 测试 / 小批量（< 100 章节） |
| 3 个 | 6 | **推荐**：日常处理，单 Space 休眠不影响整体 |
| 5 个 | 10 | 大批量迁移（> 1000 章节），快速消化 |
| 8 个+ | 16+ | 极端场景，注意 TG Bot 限流（建议 ≥ 5 个 Bot Token） |

> ⚠️ 注意：并行度越高，Telegram Bot 限流压力越大。建议 Bot Token 数量 ≥ Worker 总槽位数 / 2。

---

## 四、任务下发与排队机制

### 4.1 任务生命周期

```
                  ┌─────────┐
   迁移脚本写入 ──→│ pending │ ← 初始状态
                  └────┬────┘
                       │ claim_next_chapter() [FOR UPDATE SKIP LOCKED]
                       ▼
                  ┌────────────┐
                  │ processing │ ← worker_id 记录, claimed_at 记录
                  └─────┬──────┘
                        │
              ┌─────────┴─────────┐
              │                   │
         上传成功              上传失败
              │                   │
              ▼                   ▼
         ┌──────────┐      ┌────────┐
         │ uploaded │      │ failed │ ← error_message 记录原因
         └──────────┘      └────┬───┘
                                │ 清理脚本重置 (cleanup.py)
                                ▼
                           ┌─────────┐
                           │ pending │ ← 重新排队
                           └─────────┘
```

### 4.2 排队顺序

认领查询使用 `ORDER BY book_id, chapter_id`，保证**同一本书的章节集中处理**，利于尽早触发 `check_and_mark_book_complete()` 标记整书完成。

### 4.3 卡住任务恢复

调度器定期（默认 600 秒）执行清理（`scheduler.py::run_cleanup_now`）：

```sql
-- 重置超时的 processing (默认 1440 分钟 = 24 小时)
UPDATE audiobook_chapters
SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL
WHERE upload_status = 'processing'
  AND claimed_at < NOW() - INTERVAL '1440 minutes';

-- 可选: 重置 failed (CLEANUP_RESET_FAILED=true 时)
UPDATE audiobook_chapters
SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL, error_message = NULL
WHERE upload_status = 'failed';
```

这保证了 Worker 崩溃 / HF Space 重启导致的卡住任务能自动恢复。

---

## 五、结果回报机制

### 5.1 当前机制（已实现）

Worker 每处理完一个章节，立即写回 PostgreSQL：

```python
# worker.py::record_upload()
UPDATE audiobook_chapters SET
    telegram_file_id = %s,
    telegram_message_id = %s,
    telegram_bot_id = %s,
    telegram_bot_user_id = %s,
    upload_status = %s,        -- 'uploaded' 或 'failed'
    uploaded_at = %s,
    error_message = %s         -- 失败时记录原因
WHERE book_id = %s AND chapter_id = %s
```

VPS 端通过以下途径感知结果：

| 途径 | 说明 |
|------|------|
| 调度器 Web 面板 | 每 3 秒刷新 `pending/processing/uploaded/failed` 统计 |
| 调度器日志 | 实时记录触发事件 + DB 状态变化 |
| Worker Web 面板 | 每个 HF Space 自己的面板显示槽位状态 + 最近结果 |
| `/status` API | Worker 返回 JSON 格式的槽位 + 最近处理结果 |

### 5.2 结果回调通知（优化项，待实现）

当前结果通过 DB 共享感知，**非实时推送**。可增加主动回调：

**方案**：Worker 处理完一个章节后，主动 POST 通知 VPS 调度器。

在 `worker.py::run_one()` 末尾增加：

```python
# 伪代码 - 待实现
def _notify_vps_result(result):
    """处理完成后主动通知 VPS 调度器"""
    if not VPS_SCHEDULER_URL:
        return
    try:
        requests.post(
            f'{VPS_SCHEDULER_URL}/api/worker-callback',
            json={
                'worker_id': WORKER_ID,
                'book_id': result.get('book_id'),
                'chapter_id': result.get('chapter_id'),
                'status': result.get('status'),  # uploaded / failed
                'error': result.get('error'),
                'duration': result.get('duration'),
                'bot_id': result.get('bot_id'),
            },
            timeout=10
        )
    except Exception:
        pass  # 回调失败不影响主流程
```

VPS 调度器增加 `/api/worker-callback` 接口，用于：
- 实时更新 Web 面板的「最近完成」列表
- 触发 Telegram 通知（可选）
- 累计统计 Worker 业绩

---

## 六、部署实施步骤

### 6.1 前置条件

- [ ] VPS 已部署 PostgreSQL（`docker-compose.self-db.yml` 或 `docker-compose.yml`）
- [ ] `audiobook_chapters` 表已通过 `docker/init-db.sql` 创建
- [ ] 已有至少 2 个 Telegram Bot Token（建议 3-5 个以分散限流）
- [ ] 已有 Telegram Chat ID（音频存储目标群/频道）
- [ ] 已注册 Hugging Face 账号

### 6.2 步骤 1：部署 VPS 调度器

```bash
cd /path/to/yt_aduio_book_one_to_all/audiobook_pipeline/vps_scheduler

# 编辑 docker-compose.yml
vi docker-compose.yml
```

修改关键配置：

```yaml
environment:
  - POSTGRES_DSN=postgresql://audiobook_app:inriynisse1991@host.docker.internal:5432/audiobook
  # ⚠️ 改成你的 HF Space URL (多个用逗号分隔)
  - HF_SPACE_URLS=https://你的用户名-worker1.hf.space,https://你的用户名-worker2.hf.space,https://你的用户名-worker3.hf.space
  - MAX_SLOTS=2
  - CHECK_INTERVAL=15
  # TG 配置可留空, 启动后在 Web 面板配置 (更安全)
  - TG_CHAT_ID=
  - TG_BOT_TOKENS=
  - TELEGRAM_API_BASE=
  - WEB_PORT=38080
  - WEB_PASSWORD=你的管理面板密码   # ⚠️ 建议设置
```

启动：

```bash
docker compose up -d --build
```

访问 `http://你的VPS_IP:38080`，在 Web 面板配置 Telegram 信息（Chat ID + Bot Tokens）。

### 6.3 步骤 2：部署 HF Space Worker（每个 Space 重复此步骤）

#### 6.3.1 创建 HF Space

1. 登录 https://huggingface.co
2. 点击头像 → New Space
3. 配置：
   - **Name**: `audiobook-worker-1`（后续 Worker 递增编号）
   - **License**: 随意
   - **SDK**: **Docker**（⚠️ 必须选 Docker，不是 Gradio/Streamlit）
   - **Visibility**: **Private**（保护密钥）
4. Create Space

#### 6.3.2 上传代码

将 `audiobook_pipeline/hf_space/` 目录下的文件上传到 Space 仓库：

```
hf_space/
├── app.py           → 根目录
├── worker.py        → 根目录
├── Dockerfile       → 根目录
└── requirements.txt → 根目录
```

可用 git 方式：

```bash
git clone https://huggingface.co/spaces/你的用户名/audiobook-worker-1
cd audiobook-worker-1
cp /path/to/yt_aduio_book_one_to_all/audiobook_pipeline/hf_space/* .
git add .
git commit -m "Initial worker"
git push
```

#### 6.3.3 配置环境变量

在 Space 的 **Settings → Repository secrets** 中添加：

| 变量名 | 值 | 说明 |
|--------|-----|------|
| `POSTGRES_DSN` | `postgresql://audiobook_app:inriynisse1991@你的VPS公网IP:5432/audiobook` | ⚠️ 用 VPS 公网 IP，不能用 127.0.0.1 |
| `VPS_SCHEDULER_URL` | `http://你的VPS公网IP:38080` | 用于自动拉取 TG 配置 |
| `NUM_SLOTS` | `2` | HF 免费 2 vCPU |
| `BOT_MIN_INTERVAL` | `3` | 单 Bot 上传最小间隔 |
| `MAX_RETRIES` | `5` | 最大重试 |
| `MAX_CHAPTERS` | `0` | 批量处理上限（0=不限） |
| `NUM_WORKERS` | `2` | 批量处理线程数 |

> 💡 **无需配置** `BOT_TOKENS` / `CHAT_ID` / `TELEGRAM_API_BASE`——Worker 启动时会自动从 VPS 调度器的 `/api/tg-config` 拉取，密钥不落地 HF。

#### 6.3.4 获取 Space API 地址

Space 构建启动后，API 地址格式为：

```
https://你的用户名-空间名.hf.space
```

例如：`https://r777r7-audiobook-worker-1.hf.space`

⚠️ **不要用** `https://huggingface.co/spaces/用户名/空间名`（那是页面地址，不是 API 地址）。

#### 6.3.5 验证 Worker

```bash
# 健康检查
curl https://你的用户名-audiobook-worker-1.hf.space/health
# 应返回: {"ok":true,"worker_id":"hf_xxxx","free_slots":2,"total_slots":2}

# 测试 TG 连通性
curl https://你的用户名-audiobook-worker-1.hf.space/test-telegram
```

### 6.4 步骤 3：配置调度器并启动

回到 VPS 调度器 Web 面板（`http://VPS_IP:38080`）：

1. **配置 HF Space URLs**：填入所有 Worker 的 API 地址（逗号分隔）
2. **配置 Telegram**：填入 Chat ID + Bot Tokens（逗号分隔）
3. **配置 TG API 中继**：API Base 填 `http://VPS_IP:38080/tg-api`
   - ⚠️ HF Space 无法直连 `api.telegram.org`（ReadTimeout），必须走 VPS 中继
4. 点击 **保存配置** → **启动调度器**

### 6.5 步骤 4：投入任务

将需要处理的章节写入 `audiobook_chapters` 表（`upload_status='pending'`）：

```sql
-- 方式 A: 全量待处理
UPDATE audiobook_chapters SET upload_status = 'pending'
WHERE upload_status IN ('failed') OR upload_status IS NULL;

-- 方式 B: 指定某本书
UPDATE audiobook_chapters SET upload_status = 'pending'
WHERE book_id = '目标book_id' AND upload_status != 'uploaded';
```

调度器会自动检测到 pending 章节，并触发空闲 Worker 处理。

### 6.6 步骤 5：监控

| 监控位置 | 地址 | 内容 |
|---------|------|------|
| VPS 调度器面板 | `http://VPS_IP:38080` | 全局统计 + Worker 健康 + 实时日志 |
| Worker #1 面板 | `https://用户名-audiobook-worker-1.hf.space` | 槽位状态 + Bot 池 + 最近结果 |
| Worker #2 面板 | `https://用户名-audiobook-worker-2.hf.space` | 同上 |
| 数据库直查 | `psql` | `SELECT upload_status, COUNT(*) FROM audiobook_chapters GROUP BY 1;` |

---

## 七、优化建议与后续工作

### 7.1 优先级高

| 优化项 | 说明 | 实现位置 |
|--------|------|---------|
| **结果回调通知** | Worker 完成后主动 POST 通知 VPS，实现实时感知（见 5.2） | `hf_space/worker.py` + `vps_scheduler/web_app.py` |
| **TG 通知集成** | 整书完成时，调度器发送 Telegram 消息通知管理员 | `vps_scheduler/scheduler.py` |
| **Worker 业绩统计** | 记录每个 Worker 处理了多少章节、成功率、平均耗时 | `vps_scheduler/scheduler.py` |

### 7.2 优先级中

| 优化项 | 说明 |
|--------|------|
| **优先级队列** | `audiobook_chapters` 增加 `priority` 字段，认领查询 `ORDER BY priority DESC, book_id, chapter_id` |
| **整书批量派发** | 当前按章节派发，可优化为「把一整本书派给一个 Worker 批量处理」，减少认领开销 |
| **DeepFilter 预热** | HF Space 冷启动时先跑一个空音频预热 DeepFilter 模型，避免首章处理超时 |
| **Worker 自动注册** | Worker 启动时主动向 VPS 注册 URL，调度器自动纳入 Worker 池（免去手动配置 HF_SPACE_URLS） |

### 7.3 优先级低

| 优化项 | 说明 |
|--------|------|
| **备用 Worker 源** | 除 HF Space 外，增加 Google Colab Worker（已有 `colab/audiobook_worker_multi_bot.ipynb`）作为备选 |
| **Prometheus 监控** | 调度器暴露 `/metrics` 端点，接入 Grafana 监控 |
| **Webhook 触发** | 章节写入 DB 后，主动 POST 调度器立即触发，免去 15 秒轮询延迟 |

---

## 八、风险与应对

### 8.1 HF Space 限制

| 风险 | 影响 | 应对 |
|------|------|------|
| 免费版 16GB 内存 / 2 vCPU | DeepFilter 大文件可能 OOM | Worker 已有降级策略：降噪失败则标记 failed 继续，不阻塞 |
| 15 分钟无流量自动休眠 | 首次触发需冷启动 30-60s | 调度器已容错：超时视为冷启动，跳过该 Worker 尝试其他 |
| HF 可能随时调整免费政策 | Worker 不可用 | 多 Worker 分散 + Colab 备选 + VPS 直跑兜底（`run_worker.py`） |
| 单 Space 并发限制 | 槽位数固定 | 部署多个 Space 扩容 |

### 8.2 Telegram 限流

| 风险 | 应对 |
|------|------|
| Bot 上传 429 限流 | 已实现 BotPool 多 Bot 轮换 + 429 自动冷却切换（`worker.py::upload_to_telegram_multi_bot`） |
| 单 Bot 每秒上传限制 | `BOT_MIN_INTERVAL` 默认 3 秒，可调整 |
| Chat 被封禁 | 多 Chat ID 备选（需扩展） |

### 8.3 数据一致性

| 风险 | 应对 |
|------|------|
| Worker 崩溃导致 processing 卡住 | 调度器定期清理（默认 600s，超时 1440 分钟重置） |
| 多 Worker 并发认领同一行 | `FOR UPDATE SKIP LOCKED` 原子操作保证 |
| DB 连接失败 | `safe_pg_execute` 已有 3 次重试 |
| 网络分区导致 Worker 处理完但写不回 DB | 超时清理后重新排队，重复上传幂等（TG 同 caption 不会报错，file_id 会更新） |

### 8.4 安全

| 风险 | 应对 |
|------|------|
| HF Space 密钥泄露 | Space 设为 Private + TG 配置通过 VPS API 动态拉取（不落地 HF） |
| VPS 调度器面板被入侵 | 设置 `WEB_PASSWORD` + 反向代理加 HTTPS |
| Bot Token 泄露 | VPS 面板输入框为 password 类型 + 不回显已保存值 |
| PostgreSQL 暴露公网 | 仅开放 VPS 内网 + HF Space 通过 VPS 公网 IP 连接（建议加防火墙白名单） |

---

## 九、验收标准

部署完成后，按以下清单验收：

- [ ] VPS 调度器面板可访问，显示所有 Worker 在线
- [ ] 每个 HF Space Worker 的 `/health` 返回 `{"ok": true}`
- [ ] 在 Web 面板手动触发某个 Worker，能成功处理一个章节
- [ ] 数据库写入若干 `pending` 章节后，调度器自动触发 Worker 处理
- [ ] 多个 Worker 并行处理时不发生认领冲突（检查无重复 `worker_id` 认领同一章节）
- [ ] 模拟 Worker 休眠（停止某个 Space），调度器自动跳过该 Worker
- [ ] 模拟 Worker 恢复（重启 Space），调度器自动重新纳入
- [ ] 处理结果正确写回 DB（`upload_status` + `telegram_file_id` + `error_message`）
- [ ] 整书所有章节上传完成后，`books.book_status` 自动标记为 `success`
- [ ] 强制杀死 Worker 进程模拟崩溃，调度器在超时后自动重置该章节为 `pending`

---

## 十、附录

### 10.1 环境变量速查

#### HF Space Worker（`hf_space/`）

| 变量 | 必填 | 默认值 | 说明 |
|------|:---:|--------|------|
| `POSTGRES_DSN` | ✅ | — | PostgreSQL 连接串（用 VPS 公网 IP） |
| `VPS_SCHEDULER_URL` | ✅ | — | VPS 调度器地址，用于拉取 TG 配置 |
| `NUM_SLOTS` | | `2` | 并行槽位数（= HF vCPU 数） |
| `BOT_MIN_INTERVAL` | | `3` | 单 Bot 上传最小间隔（秒） |
| `MAX_RETRIES` | | `5` | 上传最大重试次数 |
| `MAX_CHAPTERS` | | `0` | 批量处理上限（0=不限） |
| `NUM_WORKERS` | | CPU 核数 | 批量处理线程数 |
| `BOT_TOKENS` | | (从 VPS 拉) | 多 Bot Token（逗号分隔） |
| `CHAT_ID` | | (从 VPS 拉) | Telegram Chat ID |
| `TELEGRAM_API_BASE` | | (从 VPS 拉) | TG API 中继地址 |

#### VPS 调度器（`vps_scheduler/`）

| 变量 | 必填 | 默认值 | 说明 |
|------|:---:|--------|------|
| `POSTGRES_DSN` | ✅ | — | PostgreSQL 连接串 |
| `HF_SPACE_URLS` | ✅ | — | HF Space API 地址（逗号分隔多个） |
| `MAX_SLOTS` | | `2` | 每 Worker 槽位数 |
| `CHECK_INTERVAL` | | `15` | 调度检查间隔（秒） |
| `STUCK_TIMEOUT_M` | | `1440` | 卡住超时（分钟） |
| `TG_CHAT_ID` | | (Web 面板配) | Telegram Chat ID |
| `TG_BOT_TOKENS` | | (Web 面板配) | 多 Bot Token |
| `TELEGRAM_API_BASE` | | (Web 面板配) | TG 中继地址 |
| `WEB_PORT` | | `38080` | Web 面板端口 |
| `WEB_PASSWORD` | | (空) | Web 面板密码 |
| `CLEANUP_INTERVAL` | | `600` | 自动清理间隔（秒） |
| `CLEANUP_RESET_FAILED` | | `false` | 是否自动重置 failed |
| `CLEANUP_AUTO_ENABLED` | | `true` | 是否启用自动清理 |

### 10.2 API 速查

#### HF Space Worker API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 状态面板（HTML） |
| GET | `/health` | 健康检查 → `{ok, worker_id, free_slots, total_slots}` |
| GET | `/status` | 详细状态 JSON |
| POST | `/process` | 触发处理一个章节 |
| POST | `/process-batch` | 启动批量处理（参数：`max_chapters`, `num_workers`） |
| GET | `/batch-status` | 批量处理进度 |
| POST | `/batch-stop` | 停止批量处理 |
| GET | `/bots` | Bot 池状态 |
| GET | `/test-telegram` | 测试 TG 连通性 |
| POST | `/refresh-tg-config` | 从 VPS 刷新 TG 配置 |

#### VPS 调度器 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 管理面板（HTML） |
| GET | `/api/status` | 全局状态 JSON |
| GET/POST | `/api/config` | 读取/修改配置 |
| GET | `/api/logs` | 最近日志 |
| POST | `/api/scheduler/start` | 启动调度器 |
| POST | `/api/scheduler/stop` | 停止调度器 |
| POST | `/api/trigger` | 手动触发 Worker |
| POST | `/api/reset-stuck` | 重置卡住任务 |
| POST | `/api/cleanup/run` | 立即执行清理 |
| POST | `/api/check-workers` | 刷新 Worker 健康状态 |
| GET | `/api/tg-config` | TG 配置分发（Worker 拉取用） |
| ANY | `/tg-api/<path>` | Telegram API 中继 |

### 10.3 相关文件索引

| 文件 | 说明 |
|------|------|
| `audiobook_pipeline/hf_space/app.py` | HF Space Worker Flask 服务 |
| `audiobook_pipeline/hf_space/worker.py` | Worker 核心逻辑（认领→下载→降噪→上传→回报） |
| `audiobook_pipeline/hf_space/Dockerfile` | HF Space Docker 镜像定义 |
| `audiobook_pipeline/vps_scheduler/scheduler.py` | VPS 调度器核心（`Scheduler` 类） |
| `audiobook_pipeline/vps_scheduler/web_app.py` | VPS 调度器 Web 面板 + TG 中继 |
| `audiobook_pipeline/vps_scheduler/docker-compose.yml` | VPS 调度器一键部署 |
| `pipeline/tg_audio.py` | 主项目的 TG 音频缓存下载模块（消费 Worker 上传的 file_id） |
| `docker/init-db.sql` | 数据库表结构（含 `audiobook_chapters` 任务队列表） |
| `audiobook_pipeline/worker.py` | Worker 核心模块的非 HF 版本（VPS 直跑用） |
| `audiobook_pipeline/run_worker.py` | VPS 直接运行 Worker 的入口（兜底方案） |
