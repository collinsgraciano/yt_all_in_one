# HF 免费 Docker 空间外包处理计划书

> 项目：有声书 YouTube 频道管理系统（当前项目）
> 目标：用 HF 免费 Docker 空间替代即将删除的上游项目 `audiobook_pipeline`，外包完成当前项目缺失的「下载音频 → DeepFilter 降噪 → 上传 Telegram → 写回 file_id」算力密集环节；同时把「测试实验」也做一套 HF 外包。VPS 仅负责轻量调度与中继。
> 编制日期：2026-07-19
> 修订：2026-07-19（v2）厘清上游依赖关系，重写为「替代上游 + 双轨并行」方案

---

## 一、项目背景与目标

### 1.1 关键前提：上游依赖即将切断

当前项目的「仅TG缓存完整书处理+上传」功能（`backend/templates/channel_detail.html` 的 `tg_cache_only` 按钮）依赖一条**跨项目的数据链**：

```
[上游项目 audiobook_pipeline]（即将删除 ❌）
   下载掌阅音频 → DeepFilter 降噪 → 上传 Telegram
   → 把 file_id / bot_user_id 写入 audiobook_chapters 表
        │
        ▼  (migrate-tg-chapters 迁移 / 或共享同一数据库)
[当前项目 pipeline]
   "仅TG缓存完整书" 过滤：只保留所有章节均已上传 TG 的书
   → 从 TG 下载已降噪音频 (pipeline/tg_audio.py 用 file_id)
   → 后续 YouTube 上传流程
```

**删除 `audiobook_pipeline` 后，缺口出现**：再也没有谁来执行「下载→降噪→上传 TG→写 file_id」这一算力密集环节。当前项目的「仅TG缓存完整书」功能将因为 `audiobook_chapters` 表无 file_id 数据而失效。

### 1.2 HF 外包的真正定位

> **HF 外包不是给当前项目 pipeline 锦上添花，而是替代即将删除的上游项目，补齐当前项目缺失的数据生产环节。**

HF Worker 要实现的核心能力，正是 `audiobook_pipeline` 原来做的事：

1. **下载音频**：从掌阅源下载章节 MP3（用 `books.book_data` 里的 mp3Url）
2. **DeepFilter 降噪**：CPU 密集型，单章节 10-60 秒
3. **上传 Telegram**：多 Bot 轮换上传，处理 429 限流
4. **写回 file_id**：把 `telegram_file_id` / `telegram_bot_user_id` / `upload_status` 写入当前项目的 `audiobook_chapters` 表

这样当前项目的 `pipeline/tg_audio.py` 才能从 TG 下载已降噪音频，「仅TG缓存完整书」功能得以延续。

### 1.3 测试实验外包

除 TG 缓存生产环节外，当前项目的「测试实验」（`backend/api/tests.py` 的 4 个测试：AI / 上传 / TG下载 / BGM混音）也做一套 HF 外包，把算力密集的 BGM 混音、AI 图片生成卸载到 HF。

### 1.4 双轨架构：本机自跑 + HF 外包

| 轨道 | 执行环境 | 适用场景 | 是否保留 |
|------|---------|---------|---------|
| **轨道A：本机/VPS 自跑** | 本机或高性能 VPS 直跑 `pipeline/` | 高性能 VPS 部署、调试 | ✅ 完整保留 |
| **轨道B：HF 外包** | HF 免费 Docker Space | 低配 VPS 卸载算力 | ✅ 新增 |

两套互不干扰，通过不同的 API / 任务入口切换。`pipeline/` 和 `backend/` 代码零改动。

### 1.5 目标

| 目标 | 说明 |
|------|------|
| **补齐缺口** | HF Worker 替代 audiobook_pipeline，完成「下载→降噪→上传TG→写file_id」 |
| **测试实验外包** | 测试实验（尤其 BGM 混音）也外包给 HF |
| **多空间随机待机** | 部署 N 个 HF Space，谁空闲谁接活 |
| **排队处理** | TG 缓存任务通过 PostgreSQL 共享队列下发，多 Worker 并行不冲突 |
| **结果回报** | 每完成一个任务，写回数据库 + 可选回调通知 |
| **本机套件保留** | `pipeline/` + `backend/` 零改动，未来可部署高性能 VPS 直跑 |
| **文件隔离** | 所有 HF 外包项目文件放入**项目根目录新建文件夹**（不与 audiobook_pipeline 混） |
| **零运维** | HF Space 自动休眠/冷启动，调度器自动容错 |

---

## 二、当前项目架构梳理（不引用 audiobook_pipeline）

### 2.1 当前项目的数据流

```
┌─────────────────────────────────────────────────────────────┐
│                    当前项目（保留 + 扩展）                     │
│                                                              │
│  ┌──────────────┐    ┌──────────────────────────────────┐   │
│  │  backend/     │    │  pipeline/                        │   │
│  │  FastAPI      │    │  - tg_audio.py 从TG下载已降噪音频  │   │
│  │  - tests.py   │    │  - deepfilter.py 降噪(本机自跑)   │   │
│  │  (4个测试)    │    │  - pipeline.py 主流程             │   │
│  │  - tasks.py   │    │  - bgm.py / cover.py / youtube.py │   │
│  └──────┬───────┘    └──────────────┬───────────────────┘   │
│         │                           │                        │
│         │           ┌───────────────▼───────────────┐        │
│         │           │  PostgreSQL                    │        │
│         │           │  - books (book_data 含 mp3Url) │        │
│         └──────────▶│  - audiobook_chapters          │        │
│                     │    (file_id / upload_status)   │        │
│                     └───────────────┬───────────────┘        │
│                                     │                        │
│                     【缺口】file_id 从哪来?                  │
│                                     │                        │
└─────────────────────────────────────┼────────────────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              │                       │                       │
        ❌ 旧: audiobook_pipeline   ✅ 新: HF Worker          │
        (即将删除)                  (本计划书要实现的)          │
              │                       │                       │
              └───────────────────────┼───────────────────────┘
                                      │
                               ┌──────▼──────┐
                               │  Telegram   │
                               │ (多Bot存储)  │
                               └─────────────┘
```

### 2.2 当前项目的「仅TG缓存完整书」功能链路

| 步骤 | 代码位置 | 作用 |
|------|---------|------|
| 1. 触发任务 | `backend/templates/channel_detail.html` 第37行 `tg_cache_only` 按钮 | 用户点击触发 |
| 2. 设置过滤标志 | `backend/services/task_service.py` 第75-77行 | `ONLY_TG_CACHED_BOOKS=True` |
| 3. 过滤完整书 | `pipeline/pipeline.py` 第1934-1971行 | 只保留所有章节 `upload_status='uploaded'` 且有 `telegram_file_id` 的书 |
| 4. 从TG下载 | `pipeline/tg_audio.py` `download_audio_from_telegram()` | 用 file_id + bot_user_id 匹配 Bot Token，下载已降噪音频 |
| 5. 后续流程 | `pipeline/pipeline.py` | BGM混音 → 封面 → YouTube上传 |

> ⚠️ 步骤 3 的过滤条件是「所有章节都已上传 TG」。如果 `audiobook_chapters` 表里没有 file_id（删了 audiobook_pipeline 后），这个过滤会得到空列表，功能失效。**HF Worker 的职责就是往这个表里填 file_id。**

### 2.3 当前项目的数据库支撑

`audiobook_chapters` 表（`docker/init-db.sql` 第172-214行）是 HF Worker 与当前项目 pipeline 的**共享契约**：

| 列名 | HF Worker 写入 | pipeline 读取 | 说明 |
|------|:---:|:---:|------|
| `book_id` | ✅ | ✅ | 书籍 ID |
| `chapter_id` | ✅ | ✅ | 章节 ID |
| `book_name` | ✅ | — | 书名 |
| `chapter_name` | ✅ | — | 章节名 |
| `audio_url` | ✅ | ✅ | 原始掌阅 MP3 URL（pipeline 用此匹配） |
| `telegram_file_id` | ✅ | ✅ | 上传 TG 后的文件 ID（核心数据） |
| `telegram_message_id` | ✅ | — | TG 消息 ID |
| `telegram_bot_id` | ✅ | ✅ | 上传 Bot 的数组索引 |
| `telegram_bot_user_id` | ✅ | ✅ | 上传 Bot 的永久 User ID（下载匹配依据） |
| `upload_status` | ✅ | ✅ | `pending`/`processing`/`uploaded`/`failed` |
| `uploaded_at` | ✅ | — | 上传时间 |
| `worker_id` | ✅ | — | 认领该章节的 HF Worker ID |
| `claimed_at` | ✅ | — | 认领时间（检测卡住任务） |
| `error_message` | ✅ | — | 失败原因 |

> ✅ 这个表结构已经满足 HF Worker 的需求，无需改表。HF Worker 只需往这张表写数据。

### 2.4 当前项目的本机测试实验（保留）

| 端点 | 文件 | 功能 | 算力特征 |
|------|------|------|---------|
| `POST /api/tests/ai` | `backend/api/tests.py` | AI 生成（SEO/封面） | 调外部 API，图片生成占内存 |
| `POST /api/tests/upload` | 同上 | YouTube 上传凭证测试 | OAuth + API，低算力 |
| `POST /api/tests/tg-download` | 同上 | TG 音频下载测试 | IO 密集 |
| `POST /api/tests/bgm/mix` | 同上 | BGM 混音（后台异步） | 🔥 STFT/ISTFT 高 CPU |

这些端点**完整保留不动**，HF 外包是旁路实现。

---

## 三、HF 外包总体架构

### 3.1 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│  VPS（低配，仅调度 + 中继，零算力负担）                            │
│                                                                  │
│  ┌────────────────────────┐   ┌─────────────────────────────┐   │
│  │ backend (FastAPI)      │   │ hf_workers/vps_relay/       │   │
│  │ - tests.py (本机测试)  │   │ (新建,轻量 Flask)            │   │
│  │ - tests_hf.py (转发层) │   │ - 调度 TG缓存 Worker          │   │
│  │ - tasks.py (任务)      │   │ - TG API 中继                │   │
│  └──────────┬─────────────┘   │ - 配置/密钥分发(不落地HF)     │   │
│             │                 │ - YouTube OAuth 中继         │   │
│             │                 └──────────────┬──────────────┘   │
│             │                                │                  │
│  ┌──────────▼────────────────────────────────▼──────────────┐   │
│  │  PostgreSQL                                                │   │
│  │  - books (book_data 含 mp3Url, HF Worker 读此下载)         │   │
│  │  - audiobook_chapters (HF Worker 写 file_id, pipeline 读)  │   │
│  └──────────┬─────────────────────────────────┬─────────────┘   │
└─────────────┼─────────────────────────────────┼─────────────────┘
              │                                 │
   ┌──────────┴──────────┐           ┌──────────┴──────────┐
   │ TG缓存生产 Worker池  │           │ 测试实验 Worker池    │
   │ (队列认领模式)       │           │ (即时触发模式)       │
   └──────────┬──────────┘           └──────────┬──────────┘
              │                                 │
   ┌──────────▼──────────┐           ┌──────────▼──────────┐
   │ HF Space #1..N      │           │ HF Space #1..2      │
   │ audiobook-tg-worker │           │ audiobook-test-worker│
   │ ┌─────────────────┐ │           │ ┌─────────────────┐ │
   │ │ 下载掌阅音频     │ │           │ │ AI 测试          │ │
   │ │ DeepFilter 降噪  │ │           │ │ BGM 混音         │ │
   │ │ 上传TG(多Bot轮换)│ │           │ │ TG 下载测试      │ │
   │ │ 写回 file_id     │ │           │ │ YouTube 上传测试 │ │
   │ └─────────────────┘ │           │ └─────────────────┘ │
   └──────────┬──────────┘           └──────────┬──────────┘
              │                                 │
              ▼ (经VPS中继)                     ▼ (经VPS中继)
        ┌─────────────┐                  ┌─────────────┐
        │  Telegram   │                  │ 外部API/    │
        │ (多Bot存储)  │                  │ YouTube/TG  │
        └─────────────┘                  └─────────────┘
```

### 3.2 两类 Worker 的差异

| 维度 | TG缓存生产 Worker | 测试实验 Worker |
|------|-------------------|----------------|
| **触发模式** | 队列认领（PostgreSQL `FOR UPDATE SKIP LOCKED`） | 即时触发（HTTP 请求-响应） |
| **任务来源** | `audiobook_chapters` 表 `upload_status='pending'` 的行 | 前端用户手动点击 |
| **并发** | 多 Worker 多槽位并行 | 单 Worker 单槽位 |
| **核心算力** | DeepFilter 降噪（CPU 密集） | BGM 混音 STFT/ISTFT + AI 图片生成 |
| **输出** | 写 file_id 到 DB + 上传 TG | HTTP 响应返回结果 |
| **Worker 数** | 推荐 3-5 个 | 推荐 1-2 个 |

### 3.3 新文件夹位置（项目根目录）

> ⚠️ **不放 `audiobook_pipeline/` 下**（那是上游，会删）。所有 HF 外包文件集中在项目根目录的新建文件夹 `hf_workers/`。

```
yt_aduio_book_one_to_all/
├── backend/                 # ✅ 当前项目后端(保留,新增tests_hf转发层)
├── pipeline/                # ✅ 当前项目流水线(保留,零改动)
├── docker/                  # ✅ 当前项目Docker配置(保留)
├── migrate-tg-chapters/     # ✅ 迁移工具(保留,过渡期用)
├── audiobook_pipeline/      # ❌ 上游项目(即将删除,本计划书不依赖)
│
└── hf_workers/              # ⭐ 新建:HF外包项目所有文件(独立自包含)
    ├── tg_worker/           #   TG缓存生产Worker(替代audiobook_pipeline)
    │   ├── app.py           #     Flask服务+状态面板+API
    │   ├── worker.py        #     核心:认领→下载→降噪→上传TG→写file_id
    │   ├── Dockerfile       #     python:3.11-slim + ffmpeg + DeepFilter
    │   └── requirements.txt
    ├── test_worker/         #   测试实验Worker
    │   ├── app.py           #     Flask服务+测试API
    │   ├── test_runner.py   #     核心:AI/上传/TG下载/BGM混音
    │   ├── Dockerfile       #     python:3.11-slim + ffmpeg + librosa
    │   └── requirements.txt
    ├── vps_relay/           #   VPS中继调度器(轻量)
    │   ├── app.py           #     Flask:调度+TG中继+配置分发+OAuth中继
    │   ├── Dockerfile
    │   └── docker-compose.yml
    └── README.md            #   统一部署说明
```

**设计原则**：
1. **自包含**：`hf_workers/` 内所有文件独立，不 import 当前项目的 `pipeline/`（HF 环境无主项目代码）
2. **与上游隔离**：完全不依赖 `audiobook_pipeline/`，删除它不影响 HF Worker
3. **与当前项目解耦**：HF Worker 只通过 PostgreSQL（`audiobook_chapters` / `books` 表）与当前项目交互
4. **单文件夹集中**：满足「生成的 HF 项目所有文件单独放入新建的文件夹」

---

## 四、TG缓存生产 Worker（替代 audiobook_pipeline）

### 4.1 职责

HF Worker 接管 audiobook_pipeline 的核心算力环节：

```
1. claim_next_chapter()        — 原子认领一个 pending 章节 (FOR UPDATE SKIP LOCKED)
2. download_audio()            — 从 books.book_data 解析 mp3Url，下载 MP3 + ffprobe 校验
3. denoise_audio()             — DeepFilter 降噪 (内置二进制)
4. upload_to_telegram()        — 多Bot轮换上传，429 自动冷却切换
5. record_result()             — 写回 telegram_file_id / upload_status / error_message
6. check_book_complete()       — 整书完成则标记 books.book_status
```

### 4.2 任务队列与原子认领

HF Worker 共享当前项目的 PostgreSQL，通过 `audiobook_chapters.upload_status` 字段实现队列。

**原子认领**（多 Worker 并行不冲突）：

```sql
UPDATE audiobook_chapters
SET upload_status = 'processing', worker_id = %s, claimed_at = NOW()
WHERE ctid IN (
    SELECT ctid FROM audiobook_chapters
    WHERE upload_status = 'pending'
    ORDER BY book_id, chapter_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED   -- 关键: 跳过已被其他 Worker 锁定的行
)
RETURNING book_id, chapter_id, book_name, chapter_name, audio_url
```

**任务生命周期**：

```
   pending ──[claim]──▶ processing ──┬─[成功]─▶ uploaded (file_id 写入)
                                      └─[失败]─▶ failed (error_message 记录)
                                                   │
                                          [清理脚本重置]
                                                   ▼
                                               pending (重新排队)
```

### 4.3 章节音频 URL 的获取

> ⚠️ 与 audiobook_pipeline 不同，HF Worker 不自行维护书源。它从当前项目的 `books.book_data`（JSONB）解析章节 mp3Url。

`audiobook_chapters.audio_url` 字段在任务写入时已填好（由当前项目的书籍管理流程写入）。HF Worker 直接用这个 URL 下载，无需解析 book_data。若 `audio_url` 为空，则从 `books.book_data` 按 `chapter_id` 匹配提取。

### 4.4 多 Bot 轮换上传

```
BotPool 管理 N 个 Bot Token:
  - 轮询选择可用 Bot
  - 遇 429 自动冷却（按 retry_after 秒）
  - 冷却期间切换到下一个 Bot
  - 记录每个 Bot 的上传数 / 429数 / 错误数
  - 上传成功后记录 bot_user_id（永久ID，供 pipeline 下载时匹配）
```

上传调用 Telegram `sendAudio` API，经 VPS 中继（HF 无法直连 api.telegram.org）：

```
HF Worker ──POST──▶ VPS中继 /tg-api/bot<token>/sendAudio ──▶ api.telegram.org
```

### 4.5 结果写回

```sql
UPDATE audiobook_chapters SET
    telegram_file_id = %s,
    telegram_message_id = %s,
    telegram_bot_id = %s,
    telegram_bot_user_id = %s,
    upload_status = 'uploaded',
    uploaded_at = NOW(),
    error_message = NULL
WHERE book_id = %s AND chapter_id = %s
```

当前项目的 `pipeline/pipeline.py` 过滤逻辑（第1957-1962行）会检测到 `upload_status='uploaded'` 且 `telegram_file_id` 非空，该书即被视为「TG缓存完整书」，可进入处理流程。

### 4.6 DeepFilter 降噪实现

HF Worker 内置 DeepFilter 二进制（Dockerfile 下载），独立实现降噪逻辑（不 import `pipeline/deepfilter.py`）：

```
1. ffprobe 探测时长
2. ffmpeg 分片为 16kHz WAV（每段60分钟）
3. deep-filter 二进制处理每个分片
4. pydub 合并降噪后的分片
5. ffmpeg 转回 MP3 (192kbps)
```

### 4.7 API 设计

#### HF TG缓存 Worker API（`hf_workers/tg_worker/app.py`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 状态面板（HTML：槽位 + Bot池 + DB统计） |
| GET | `/health` | 健康检查 → `{ok, worker_id, free_slots, total_slots}` |
| GET | `/status` | 详细状态 JSON |
| POST | `/process` | 触发处理一个章节（调度器调用） |
| POST | `/process-batch` | 启动批量处理（多线程并发） |
| GET | `/batch-status` | 批量处理进度 |
| POST | `/batch-stop` | 停止批量处理 |
| GET | `/bots` | Bot 池状态 |
| GET | `/test-telegram` | 测试 TG 连通性 |
| POST | `/refresh-config` | 从 VPS 拉取最新配置（Bot Tokens等） |

---

## 五、测试实验 Worker

### 5.1 外包适配性分析

| 测试 | 算力特征 | 敏感凭证 | 外包价值 | 建议 |
|------|---------|---------|---------|------|
| **BGM 混音** | 🔥 STFT/ISTFT 高 CPU，数分钟 | 无 | ⭐⭐⭐⭐⭐ | 优先外包 |
| **AI 测试** | 调外部API，图片生成占内存 | MODELSCOPE_TOKEN | ⭐⭐⭐ | 外包 |
| **TG 下载测试** | getFile + 下载，IO 密集 | TG Bot Token | ⭐⭐ | 外包（复用中继） |
| **YouTube 上传测试** | OAuth + API，低算力 | OAuth 凭证 | ⭐ | 外包（凭证中继） |

### 5.2 触发模式

与 TG缓存 Worker 的队列模式不同，测试实验是**即时触发**：

```python
# backend/api/tests_hf.py 伪代码(轻量转发层)
def _pick_idle_test_worker() -> str | None:
    """选择一个空闲的测试Worker"""
    for url in HF_TEST_WORKER_URLS:
        try:
            r = requests.get(f'{url}/health', timeout=5)
            if r.json().get('ok') and not r.json().get('busy'):
                return url
        except Exception:
            continue  # 冷启动中,尝试下一个
    return None
```

### 5.3 API 设计

#### HF 测试 Worker API（`hf_workers/test_worker/app.py`）

| 方法 | 路径 | 说明 | 对应本机端点 |
|------|------|------|------------|
| GET | `/` | 状态面板 | — |
| GET | `/health` | 健康检查 → `{ok, worker_id, busy}` | — |
| POST | `/test/ai` | AI 测试（seo/cover/both） | `POST /api/tests/ai` |
| POST | `/test/upload` | YouTube 上传凭证测试 | `POST /api/tests/upload` |
| POST | `/test/tg-download` | TG 音频下载测试 | `POST /api/tests/tg-download` |
| POST | `/test/bgm/download` | 随机下载章节音频 | `POST /api/tests/bgm/download` |
| POST | `/test/bgm/mix` | 启动 BGM 混音（异步，返回 job_id） | `POST /api/tests/bgm/mix` |
| GET | `/test/bgm/mix/status` | 轮询混音进度 | `GET /api/tests/bgm/mix/status` |
| POST | `/refresh-config` | 从 VPS 拉取配置 | — |

#### VPS 转发层（新增 `backend/api/tests_hf.py`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tests-hf/ai` | 转发 AI 测试 |
| POST | `/api/tests-hf/upload` | 转发 YouTube 上传测试 |
| POST | `/api/tests-hf/tg-download` | 转发 TG 下载测试 |
| POST | `/api/tests-hf/bgm/download` | 转发 BGM 音频下载 |
| POST | `/api/tests-hf/bgm/mix` | 转发 BGM 混音 |
| GET | `/api/tests-hf/bgm/mix/status` | 透传混音进度 |
| GET | `/api/tests-hf/workers` | 查询测试 Worker 健康 |

> 💡 `tests_hf.py` 是**轻量转发层**，不执行算力逻辑，仅：选空闲 Worker → 转发 → 透传响应。

### 5.4 BGM 混音外包特殊处理

1. **音乐池**：HF Worker 内置一组样本 BGM（随镜像打包）
2. **章节音频**：Worker 通过 POSTGRES_DSN 读 `books.book_data` 获取 mp3Url 下载
3. **混音结果**：输出转 base64 嵌入 JSON 返回（< 10MB）；超大则上传 VPS 临时存储
4. **异步执行**：job_id 模式，前端轮询进度

---

## 六、VPS 中继调度器

### 6.1 职责

VPS 上运行一个轻量 Flask 服务（`hf_workers/vps_relay/app.py`），承担四项职能：

| 职能 | 说明 |
|------|------|
| **调度 TG缓存 Worker** | 后台线程轮询 PG，有空闲槽位时 POST `/process` 触发 Worker |
| **TG API 中继** | `/tg-api/<path>` 代理转发到 api.telegram.org（HF 无法直连） |
| **配置/密钥分发** | `/api/tg-config` 和 `/api/test-config` 分发配置（不落地 HF） |
| **YouTube OAuth 中继** | `/yt-oauth/<path>` 代理 YouTube API（凭证不离开 VPS） |

### 6.2 调度主循环

```
每 15 秒:
  1. 查询 pending/processing/uploaded/failed 数量
  2. 对所有 TG缓存 Worker 调 /health, 获取空闲槽位
  3. 若 pending > 0 且有空闲 Worker:
       POST /process 触发该 Worker 处理一个章节
  4. 所有 Worker 满载: 等待 5s
  5. 所有 Worker 离线: 等待 30s (冷启动)
  6. 定期清理卡住任务 (默认 600s, 超时 1440 分钟重置)
```

### 6.3 配置分发（密钥不落地 HF）

```
HF Worker 启动 / 手动刷新
  └─▶ POST /refresh-config
       └─▶ GET VPS /api/tg-config
            返回: { bot_tokens, chat_id, telegram_api_base }
       └─▶ GET VPS /api/test-config
            返回: { modelscope_token, bot_tokens, yt_oauth_base, postgres_dsn }
```

HF Space 的 Secrets 只存 `POSTGRES_DSN` 和 `VPS_RELAY_URL`，其余配置动态拉取。

### 6.4 冷启动容错

| 场景 | 调度器行为 | Worker 行为 |
|------|-----------|-------------|
| Worker 休眠 | `/health` 超时 → 跳过 | — |
| 冷启动中 | 超时 → 等待 30s 重试 | 启动后 `/health` 恢复 |
| 在线但满载 | `free_slots=0` → 等待 5s | 处理完释放槽位 |
| 彻底不可达 | 连续失败 → 尝试其他 Worker | — |

---

## 七、任务下发与投入

### 7.1 TG缓存任务的下发

方式一：往 `audiobook_chapters` 表写入 `pending` 章节

```sql
-- 写入待处理章节（book_id / chapter_id / audio_url 必填）
INSERT INTO audiobook_chapters (book_id, chapter_id, book_name, chapter_name, audio_url, upload_status)
VALUES ('xxx', 'ch001', '书名', '第一章', 'https://mp3-url...', 'pending');

-- 批量重置失败任务
UPDATE audiobook_chapters SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL
WHERE upload_status = 'failed';
```

方式二：当前项目可新增「投递到 TG缓存队列」的入口（在书籍管理页加按钮），把 `books.book_data` 里的章节批量写入 `audiobook_chapters` 表（`upload_status='pending'`）。

调度器自动检测 pending 章节，触发空闲 Worker 处理。

### 7.2 测试实验的触发

测试实验无需投入任务，前端测试页面点击「运行测试（HF外包）」按钮即时触发。

### 7.3 卡住任务恢复

```sql
-- 重置超时的 processing (默认 1440 分钟)
UPDATE audiobook_chapters
SET upload_status = 'pending', worker_id = NULL, claimed_at = NULL
WHERE upload_status = 'processing'
  AND claimed_at < NOW() - INTERVAL '1440 minutes';
```

---

## 八、双轨架构：本机自跑保留

### 8.1 保留原则

1. **零改动**：`pipeline/` 和 `backend/` 代码不因 HF 外包而修改（仅新增 `tests_hf.py` 转发层）
2. **高性能 VPS 友好**：未来部署 4核+/4G+ VPS 时，本机直跑性能足够
3. **调试优先**：开发调试用本机直跑，问题易定位
4. **凭证安全**：敏感凭证只在本机/VPS 处理

### 8.2 保留的组件

| 组件 | 文件 | 处理方式 |
|------|------|---------|
| 本机测试实验 | `backend/api/tests.py` | ✅ 保留不动 |
| 测试页面 | `backend/templates/*_test.html` | ✅ 保留（新增 HF 入口按钮） |
| Pipeline 模块 | `pipeline/*.py` | ✅ 全部保留不动 |
| TG缓存下载 | `pipeline/tg_audio.py` | ✅ 保留（消费 HF Worker 产出的 file_id） |
| 任务服务 | `backend/services/task_service.py` | ✅ 保留不动 |
| 迁移工具 | `migrate-tg-chapters/` | ✅ 保留（过渡期从旧库迁数据用） |

### 8.3 两轨切换

| 操作 | 轨道A（本机） | 轨道B（HF外包） |
|------|--------------|----------------|
| TG缓存生产 | 无（依赖 audiobook_pipeline 或 HF） | HF Worker 自动 |
| 测试实验 | 前端「运行测试」按钮 → `/api/tests/*` | 前端「运行测试(HF)」按钮 → `/api/tests-hf/*` |
| 「仅TG缓存完整书」任务 | `tg_cache_only` 按钮（需 file_id 已存在） | 同左（file_id 由 HF Worker 产出） |

---

## 九、部署实施步骤

### 9.1 前置条件

- [ ] VPS 已部署 PostgreSQL + 当前项目（`docker-compose.yml`）
- [ ] `audiobook_chapters` 表已创建（`docker/init-db.sql`）
- [ ] 至少 2 个 Telegram Bot Token（建议 3-5 个）
- [ ] Telegram Chat ID（音频存储目标群/频道）
- [ ] Hugging Face 账号
- [ ] MODELSCOPE_TOKEN（AI 测试用，可选）

### 9.2 步骤 1：部署 VPS 中继调度器

```bash
cd /path/to/yt_aduio_book_one_to_all/hf_workers/vps_relay
vi docker-compose.yml
```

配置：

```yaml
environment:
  - POSTGRES_DSN=postgresql://audiobook_app:xxx@host.docker.internal:5432/audiobook
  # TG缓存 Worker URLs
  - TG_WORKER_URLS=https://用户名-audiobook-tg-worker-1.hf.space,https://用户名-audiobook-tg-worker-2.hf.space
  # 测试实验 Worker URLs
  - TEST_WORKER_URLS=https://用户名-audiobook-test-worker-1.hf.space
  - TG_CHAT_ID=
  - TG_BOT_TOKENS=
  - TEST_MODELSCOPE_TOKEN=
  - WEB_PORT=38080
  - WEB_PASSWORD=你的密码
  - CHECK_INTERVAL=15
  - STUCK_TIMEOUT_M=1440
  - CLEANUP_INTERVAL=600
```

```bash
docker compose up -d --build
```

访问 `http://VPS_IP:38080`，在 Web 面板配置 Telegram 信息。

### 9.3 步骤 2：部署 TG缓存 Worker（每个 Space 重复）

1. HF → New Space → Name=`audiobook-tg-worker-1` → SDK=**Docker** → Visibility=**Private**
2. 上传 `hf_workers/tg_worker/` 下所有文件到 Space 仓库
3. 配置 Secrets：

| 变量 | 值 |
|-----|-----|
| `POSTGRES_DSN` | `postgresql://...@VPS公网IP:5432/audiobook` |
| `VPS_RELAY_URL` | `http://VPS公网IP:38080` |
| `NUM_SLOTS` | `2` |

4. 验证：
```bash
curl https://用户名-audiobook-tg-worker-1.hf.space/health
# {"ok":true,"worker_id":"hf_xxxx","free_slots":2,"total_slots":2}
```

### 9.4 步骤 3：部署测试实验 Worker

1. HF → New Space → Name=`audiobook-test-worker-1` → SDK=**Docker** → **Private**
2. 上传 `hf_workers/test_worker/` 下所有文件
3. 配置 Secrets：

| 变量 | 值 |
|-----|-----|
| `POSTGRES_DSN` | `postgresql://...@VPS公网IP:5432/audiobook` |
| `VPS_RELAY_URL` | `http://VPS公网IP:38080` |

4. 验证：
```bash
curl https://用户名-audiobook-test-worker-1.hf.space/health
# {"ok":true,"worker_id":"hf_test_xxxx","busy":false}
```

### 9.5 步骤 4：配置并启动

VPS 调度器 Web 面板（`http://VPS_IP:38080`）：
1. 配置 TG缓存 Worker URLs
2. 配置测试 Worker URLs
3. 配置 Telegram（Chat ID + Bot Tokens）
4. 配置测试实验（MODELSCOPE_TOKEN）
5. 保存 → 启动调度器

### 9.6 步骤 5：投入 TG缓存任务

```sql
-- 写入 pending 章节（audio_url 从 books.book_data 提取）
INSERT INTO audiobook_chapters (book_id, chapter_id, book_name, chapter_name, audio_url, upload_status)
SELECT ... FROM ... WHERE ...;

-- 或重置失败任务
UPDATE audiobook_chapters SET upload_status='pending', worker_id=NULL, claimed_at=NULL
WHERE upload_status='failed';
```

调度器自动触发 Worker 处理。

### 9.7 步骤 6：删除上游项目（过渡完成后）

确认 HF Worker 稳定产出 file_id 后：
```bash
rm -rf audiobook_pipeline/  # 删除上游项目
```

当前项目不再依赖它。

---

## 十、推荐部署规模

| 组件 | 数量 | 并发 | 适用场景 |
|------|:---:|:---:|------|
| TG缓存 Worker | 1 | 2槽 | 测试 / 小批量 |
| TG缓存 Worker | 3 | 6槽 | **推荐**：日常处理 |
| TG缓存 Worker | 5 | 10槽 | 大批量（>1000章节） |
| 测试 Worker | 1 | 1槽 | 日常测试 |
| 测试 Worker | 2 | 2槽 | 并发测试 |

> ⚠️ TG缓存 Worker 并行度越高，TG Bot 限流压力越大。建议 Bot Token 数 ≥ 总槽位数 / 2。

---

## 十一、结果回报与监控

### 11.1 TG缓存 Worker 回报

每处理完一个章节，立即写回 PostgreSQL（`upload_status` + `telegram_file_id` + `error_message`）。

VPS 感知途径：
- 调度器 Web 面板（3秒刷新统计）
- 调度器日志
- Worker Web 面板
- `/status` API

### 11.2 测试 Worker 回报

| 测试类型 | 回报方式 |
|---------|---------|
| AI / 上传 / TG下载 / BGM下载 | 同步 HTTP 响应 |
| BGM 混音 | 异步：job_id → 轮询 |

### 11.3 结果回调通知（优化项）

Worker 完成后主动 POST 通知 VPS（实时感知，非轮询 DB）：

```python
def _notify_vps_result(result):
    requests.post(f'{VPS_RELAY_URL}/api/worker-callback',
                  json={'worker_id':..., 'book_id':..., 'status':...},
                  timeout=10)
```

---

## 十二、优化建议

### 12.1 优先级高

| 优化项 | 说明 |
|--------|------|
| **结果回调通知** | Worker 完成后主动 POST VPS，实时感知 |
| **整书完成 TG 通知** | 整书上传完，调度器发 Telegram 通知管理员 |
| **Worker 业绩统计** | 记录每 Worker 处理数 / 成功率 / 平均耗时 |
| **前端 HF 入口** | 测试页面增加「运行测试(HF外包)」按钮 |
| **任务投递入口** | 书籍管理页加「投递到TG缓存队列」按钮 |

### 12.2 优先级中

| 优化项 | 说明 |
|--------|------|
| **优先级队列** | `audiobook_chapters` 增加 `priority` 字段 |
| **整书批量派发** | 一整本书派给一个 Worker 批量处理 |
| **DeepFilter 预热** | 冷启动时预热模型 |
| **Worker 自动注册** | Worker 启动时主动注册到 VPS |

### 12.3 优先级低

| 优化项 | 说明 |
|--------|------|
| **Colab 备选** | Google Colab Worker 作为 HF 备选 |
| **Prometheus 监控** | 调度器暴露 `/metrics` |
| **Webhook 触发** | 写入 DB 后立即 POST 调度器，免去 15s 轮询 |

---

## 十三、风险与应对

### 13.1 HF Space 限制

| 风险 | 应对 |
|------|------|
| 16GB内存/2vCPU | DeepFilter/BGM混音失败则标记 failed 继续，不阻塞 |
| 15分钟休眠 | 调度器超时视为冷启动，跳过该 Worker 尝试其他 |
| HF 政策调整 | 多 Worker 分散 + 本机直跑兜底（轨道A） |

### 13.2 Telegram 限流

| 风险 | 应对 |
|------|------|
| Bot 429 限流 | BotPool 多 Bot 轮换 + 429 自动冷却 |
| 单 Bot 频率 | `BOT_MIN_INTERVAL` 默认 3 秒 |

### 13.3 数据一致性

| 风险 | 应对 |
|------|------|
| Worker 崩溃卡住 | 调度器定期清理（超时 1440 分钟重置） |
| 并发认领冲突 | `FOR UPDATE SKIP LOCKED` 原子保证 |
| DB 连接失败 | 3 次重试 |

### 13.4 安全

| 风险 | 应对 |
|------|------|
| HF 密钥泄露 | Space Private + 配置动态拉取不落地 |
| Bot Token 泄露 | VPS 面板 password 输入 + 不回显 |
| OAuth 凭证泄露 | HF Worker 不持有，经 VPS 中继代理 |
| PG 暴露公网 | 防火墙白名单 |

---

## 十四、验收标准

### 14.1 TG缓存生产验收

- [ ] VPS 调度器面板可访问，显示所有 TG缓存 Worker 在线
- [ ] 每个 Worker 的 `/health` 返回 `{"ok": true}`
- [ ] 写入若干 `pending` 章节后，调度器自动触发 Worker 处理
- [ ] Worker 成功：下载音频 → 降噪 → 上传 TG → 写回 `telegram_file_id`
- [ ] 当前项目 `pipeline/pipeline.py` 的「仅TG缓存完整书」过滤能识别到 uploaded 章节
- [ ] 多 Worker 并行无认领冲突
- [ ] 模拟 Worker 休眠，调度器自动跳过；恢复后自动纳入
- [ ] 整书所有章节 uploaded 后，该书进入「仅TG缓存完整书」可处理列表
- [ ] Worker 崩溃后，调度器超时重置该章节为 pending

### 14.2 测试实验外包验收

- [ ] 测试 Worker `/health` 返回 `{"ok": true, "busy": false}`
- [ ] 测试页面显示「运行测试(HF外包)」按钮
- [ ] HF外包运行 AI 测试，返回 SEO 文案/封面图片
- [ ] HF外包运行 BGM 混音，轮询进度至完成，输出可播放
- [ ] HF外包运行 TG 下载测试，getFile 验证成功（经中继）
- [ ] HF外包运行 YouTube 上传测试，返回频道信息（经中继）
- [ ] 配置动态拉取，HF Space 内无明文密钥

### 14.3 双轨架构验收

- [ ] `/api/tests/*`（本机）和 `/api/tests-hf/*`（HF）两套端点均可用
- [ ] `pipeline/` 和 `backend/api/tests.py` 代码零改动
- [ ] 删除 `audiobook_pipeline/` 后，当前项目功能正常（依赖 HF Worker 产出 file_id）

---

## 十五、附录

### 15.1 环境变量速查

#### TG缓存 Worker（`hf_workers/tg_worker/`）

| 变量 | 必填 | 默认 | 说明 |
|------|:---:|------|------|
| `POSTGRES_DSN` | ✅ | — | PG连接串（VPS公网IP） |
| `VPS_RELAY_URL` | ✅ | — | VPS中继地址（拉配置+TG中继） |
| `NUM_SLOTS` | | `2` | 并行槽位 |
| `BOT_MIN_INTERVAL` | | `3` | Bot上传间隔 |
| `MAX_RETRIES` | | `5` | 最大重试 |
| `BOT_TOKENS` | | (拉取) | 多Bot Token（不落地） |
| `CHAT_ID` | | (拉取) | TG Chat ID |
| `TELEGRAM_API_BASE` | | (拉取) | TG中继地址 |

#### 测试 Worker（`hf_workers/test_worker/`）

| 变量 | 必填 | 默认 | 说明 |
|------|:---:|------|------|
| `POSTGRES_DSN` | ✅ | — | PG连接串 |
| `VPS_RELAY_URL` | ✅ | — | VPS中继地址 |
| `MODELSCOPE_TOKEN` | | (拉取) | AI测试Token（不落地） |
| `BOT_TOKENS` | | (拉取) | TG下载测试用 |

#### VPS中继（`hf_workers/vps_relay/`）

| 变量 | 必填 | 默认 | 说明 |
|------|:---:|------|------|
| `POSTGRES_DSN` | ✅ | — | PG连接串 |
| `TG_WORKER_URLS` | ✅ | — | TG缓存Worker地址 |
| `TEST_WORKER_URLS` | | — | 测试Worker地址 |
| `TG_CHAT_ID` | | (面板配) | TG Chat ID |
| `TG_BOT_TOKENS` | | (面板配) | 多Bot Token |
| `TEST_MODELSCOPE_TOKEN` | | (面板配) | 测试AI Token |
| `WEB_PORT` | | `38080` | 面板端口 |
| `WEB_PASSWORD` | | — | 面板密码 |
| `CHECK_INTERVAL` | | `15` | 调度间隔 |
| `STUCK_TIMEOUT_M` | | `1440` | 卡住超时 |
| `CLEANUP_INTERVAL` | | `600` | 清理间隔 |

### 15.2 VPS中继 API 速查

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 管理面板 |
| GET | `/api/status` | 全局状态 |
| GET/POST | `/api/config` | 读写配置 |
| POST | `/api/scheduler/start` | 启动调度 |
| POST | `/api/scheduler/stop` | 停止调度 |
| POST | `/api/trigger` | 手动触发Worker |
| POST | `/api/reset-stuck` | 重置卡住任务 |
| GET | `/api/tg-config` | TG配置分发（Worker拉取） |
| GET | `/api/test-config` | 测试配置分发（Worker拉取） |
| ANY | `/tg-api/<path>` | Telegram API中继 |
| ANY | `/yt-oauth/<path>` | YouTube OAuth中继 |
| POST | `/api/worker-callback` | Worker结果回调（优化项） |

### 15.3 文件索引

#### 新建文件（`hf_workers/`）

| 文件 | 说明 |
|------|------|
| `hf_workers/tg_worker/app.py` | TG缓存Worker Flask服务 |
| `hf_workers/tg_worker/worker.py` | 核心：认领→下载→降噪→上传TG→写file_id |
| `hf_workers/tg_worker/Dockerfile` | 含ffmpeg+DeepFilter |
| `hf_workers/test_worker/app.py` | 测试Worker Flask服务 |
| `hf_workers/test_worker/test_runner.py` | 测试执行核心 |
| `hf_workers/test_worker/Dockerfile` | 含ffmpeg+librosa |
| `hf_workers/vps_relay/app.py` | VPS中继调度器 |
| `hf_workers/vps_relay/docker-compose.yml` | 一键部署 |
| `hf_workers/README.md` | 统一部署说明 |
| `backend/api/tests_hf.py` | VPS端轻量转发层 |

#### 现有文件（保留不动）

| 文件 | 说明 |
|------|------|
| `pipeline/tg_audio.py` | 从TG下载已降噪音频（消费file_id） |
| `pipeline/pipeline.py` | 主流程（含「仅TG缓存完整书」过滤，第1934行） |
| `pipeline/deepfilter.py` | 本机降噪（轨道A保留） |
| `pipeline/bgm.py` | 本机BGM混音（轨道A保留） |
| `backend/api/tests.py` | 本机测试实验（轨道A保留） |
| `backend/services/task_service.py` | 任务服务（含 tg_cache_only 类型） |
| `docker/init-db.sql` | 数据库表结构（含 audiobook_chapters） |

### 15.4 目录结构总览

```
yt_aduio_book_one_to_all/
├── backend/                 # ✅ 当前项目后端(保留,新增tests_hf.py)
├── pipeline/                # ✅ 当前项目流水线(零改动)
├── docker/                  # ✅ Docker配置(保留)
├── migrate-tg-chapters/     # ✅ 迁移工具(过渡期保留)
├── audiobook_pipeline/      # ❌ 上游项目(即将删除,不依赖)
│
└── hf_workers/              # ⭐ 新建:HF外包所有文件
    ├── tg_worker/           #    TG缓存生产Worker(替代audiobook_pipeline)
    ├── test_worker/         #    测试实验Worker
    ├── vps_relay/           #    VPS中继调度器
    └── README.md
```

---

## 十六、实施路线图

### 阶段 1：TG缓存生产 Worker（核心，补齐缺口）

1. [ ] 创建 `hf_workers/tg_worker/` 
2. [ ] 实现 `worker.py`（认领→下载→降噪→上传TG→写file_id）
3. [ ] 实现 `app.py`（Flask服务+API）
4. [ ] 编写 `Dockerfile`（ffmpeg+DeepFilter）
5. [ ] 实现 `hf_workers/vps_relay/`（调度+TG中继+配置分发）
6. [ ] 部署到 HF Space，验证产出 file_id
7. [ ] 当前项目「仅TG缓存完整书」功能恢复

### 阶段 2：测试实验 Worker

1. [ ] 创建 `hf_workers/test_worker/`
2. [ ] 实现 `test_runner.py`（4个测试）
3. [ ] 实现 `app.py`
4. [ ] 编写 `Dockerfile`（ffmpeg+librosa）
5. [ ] 实现 `backend/api/tests_hf.py`（转发层）
6. [ ] 测试页面加 HF 入口按钮
7. [ ] 部署验证

### 阶段 3：删除上游 + 优化

1. [ ] 确认 HF Worker 稳定后删除 `audiobook_pipeline/`
2. [ ] 结果回调通知
3. [ ] 整书完成 TG 通知
4. [ ] Worker 业绩统计
5. [ ] 任务投递入口（书籍管理页）

---

> 📌 **核心要点**：本计划书完全不依赖 `audiobook_pipeline`（上游，即将删除）。HF 外包的定位是**替代上游**，补齐当前项目缺失的「下载→降噪→上传TG→写file_id」环节，让当前项目能独立运行。所有 HF 文件集中在项目根目录新建的 `hf_workers/` 文件夹。本机自跑套件（`pipeline/` + `backend/`）完整保留，形成双轨架构。
