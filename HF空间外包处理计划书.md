# HF 免费 Docker 空间外包处理计划书

> 项目：有声书 YouTube 频道管理系统（当前项目）
> 目标：把当前项目的「仅TG缓存完整书处理+上传」流水线（`pipeline/` 在 `ONLY_TG_CACHED_BOOKS=True` 时的完整流程）外包给多个 HF 免费 Docker 空间执行；同时把「测试实验」也做一套 HF 外包。VPS 仅负责轻量调度与凭证中继。
> 编制日期：2026-07-19
> 修订：2026-07-19（v3）纠正对外包对象的认知，重写为「远程pipeline执行器」方案

---

## 一、项目背景与目标

### 1.1 外包对象（纠正）

本次外包的对象是**当前项目自己的**「仅TG缓存完整书处理+上传」功能。该功能由 `pipeline/pipeline.py` 在 `ONLY_TG_CACHED_BOOKS=True` 时执行，是一条完整的单书处理流水线：

```
触发: backend 频道详情页「仅TG缓存完整书处理+上传」按钮 (task_type='tg_cache_only')
  │
  ▼
backend/services/task_service.py: 设置 ONLY_TG_CACHED_BOOKS=True
  │
  ▼
pipeline/pipeline.py::run_pipeline()
  ├─ 过滤: 只保留所有章节均已上传TG的完整书 (第1934行)
  └─ 对每本书调用 process_book() (第1664行)
       │
       ▼
     process_standard_book() (第405行) ← 这是外包的核心
       1. download_chapter_items()        从TG下载已降噪音频 (tg_audio.py)
       2. denoise_audio_paths_parallel()  DeepFilter降噪 (TG缓存章节跳过)
       3. build_final_audio_from_chapter_paths()  合并+BGM混音 (bgm.py)
       4. prepare_standard_book_cover_and_seo()   AI封面+SEO生成 (cover.py)
       5. generate_video()                MP4封装 (ffmpeg)
       6. upload_to_youtube_detailed()    YouTube上传 (youtube.py)
```

**算力密集环节**：BGM混音（STFT/ISTFT，CPU密集）、AI封面图片生成（调外部API，占内存）、MP4封装（ffmpeg）。这些在低配VPS上跑会拖垮整机。

### 1.2 目标

| 目标 | 说明 |
|------|------|
| **流水线外包** | 把 `process_standard_book()` 这套完整流水线搬到 HF Worker 执行，VPS 零算力负担 |
| **多空间随机待机** | 部署 N 个 HF Space，谁空闲谁接活 |
| **排队处理** | 书籍处理任务通过 PostgreSQL 共享队列下发，多 Worker 并行不冲突 |
| **结果回报** | 每完成一本书，写回 DB（youtube_url / 状态）+ 可选回调通知 |
| **凭证不落地** | TG Bot Token / ModelScope Token / YouTube OAuth 经 VPS 中继，HF 不存明文 |
| **本机套件保留** | `pipeline/` + `backend/` 代码零改动，未来可部署高性能 VPS 直跑 |
| **测试实验外包** | 测试实验（AI/BGM/TG下载/YouTube上传）也做一套 HF 外包 |
| **文件隔离** | 所有 HF 外包项目文件放入项目根目录新建文件夹 `hf_workers/` |

### 1.3 双轨架构

| 轨道 | 执行环境 | 说明 | 是否保留 |
|------|---------|------|---------|
| **轨道A：本机/VPS 自跑** | 本机或高性能 VPS 直跑 `pipeline/` | `task_service.py` 后台线程直接调用 `run_pipeline()` | ✅ 完整保留，零改动 |
| **轨道B：HF 外包** | HF 免费 Docker Space | HF Worker 调用 `process_book()` 执行单书处理 | ✅ 新增 |

两套通过不同的任务入口切换，`pipeline/` 代码完全不动。

---

## 二、当前项目流水线梳理（外包对象）

### 2.1 「仅TG缓存完整书处理+上传」完整链路

以 `process_standard_book()`（`pipeline/pipeline.py` 第405-603行）为基准：

| 步骤 | 函数 | 文件 | 算力特征 | 依赖凭证 |
|------|------|------|---------|---------|
| 1. 下载章节音频 | `download_chapter_items()` | `pipeline/audio.py:474` | IO密集 | TG Bot Token（经中继） |
| 1a. TG缓存下载 | `download_audio_from_telegram()` | `pipeline/tg_audio.py:472` | IO密集 | TG Bot Token + file_id |
| 2. DeepFilter降噪 | `denoise_audio_paths_parallel()` | `pipeline/deepfilter.py:264` | CPU密集（TG缓存章节跳过） | 无 |
| 3. 合并+BGM混音 | `build_final_audio_from_chapter_paths()` → `mix_with_bgm()` | `pipeline/audio.py` + `pipeline/bgm.py` | 🔥 CPU密集 STFT/ISTFT | 无（需音乐池） |
| 4. AI封面生成 | `auto_create_youtube_cover()` | `pipeline/cover.py` | 调外部API，占内存 | MODELSCOPE_TOKEN |
| 5. SEO文案生成 | `auto_create_youtube_seo()` | `pipeline/cover.py` + `pipeline/seo.py` | 调外部API | MODELSCOPE_TOKEN |
| 6. MP4封装 | `generate_video()` | `pipeline/`（ffmpeg） | CPU密集 | 无 |
| 7. YouTube上传 | `upload_to_youtube_detailed()` | `pipeline/youtube.py:178` | 网络+API | YouTube OAuth 凭证 |

### 2.2 数据库交互

流水线读写当前项目的 PostgreSQL：

| 表 | 读/写 | 用途 |
|----|:---:|------|
| `books` | 读 | 获取 `book_data`（含章节mp3Url）、`book_status` |
| `audiobook_chapters` | 读 | 查询 `telegram_file_id` / `telegram_bot_user_id`（TG缓存匹配） |
| `run_tasks` | 读/写 | 任务状态、停止标志 |
| `global_settings` | 读 | 配置项（MODELSCOPE_TOKEN 等） |
| `book_state` | 读/写 | 分片模式状态（长书断点续传） |

### 2.3 配置依赖

`process_book()` 通过 `pipeline/config.py` 的模块级全局 `cfg` 读取配置（`apply_runtime_config()` 注入）。关键配置：

| 配置项 | 用途 |
|--------|------|
| `ONLY_TG_CACHED_BOOKS` | 过滤标志（tg_cache_only 模式） |
| `TG_BOT_TOKEN` | TG下载凭证（多Bot逗号分隔） |
| `MODELSCOPE_TOKEN` | AI封面/SEO凭证 |
| `YOUTUBE_CHANNEL_NAME` | YouTube频道名 |
| `ENABLE_BGM_MIX` / `ENABLE_COVER_GENERATION` / `ENABLE_SEO_GENERATION` / `ENABLE_DEEPFILTER` / `ENABLE_VIDEO_GENERATION` / `ENABLE_YOUTUBE_UPLOAD` | 各环节开关 |
| `OUTPUT_ROOT` | 输出目录 |
| `POSTGRES_DSN` | 数据库连接串 |
| `PIPELINE_TASK_ID` | 任务ID（停止标志检查） |

---

## 三、HF 外包总体架构

### 3.1 核心思路：远程 pipeline 执行器

HF Worker **不重写流水线逻辑**，而是把当前项目的 `pipeline/` 目录打包进 Docker 镜像，直接调用 `pipeline.process_book()` 执行单书处理。这样：

- 与轨道A（本机自跑）逻辑完全一致，零重复代码
- `pipeline/` 升级时，HF Worker 重新构建镜像即可同步
- HF Worker 本质是"跑在 HF 上的 pipeline 实例"

### 3.2 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│  VPS（低配，仅调度 + 中继，零算力负担）                            │
│                                                                  │
│  ┌──────────────────────┐   ┌──────────────────────────────┐    │
│  │ backend (FastAPI)    │   │ hf_workers/vps_relay/ (新建)  │    │
│  │ - tasks.py (本机任务) │   │ 轻量 Flask:                   │    │
│  │ - tests.py (本机测试) │   │ - 筛选TG缓存完整书 + 派发Worker │    │
│  │ - tasks_hf.py (转发)  │   │ - TG API 中继 (/tg-api)       │    │
│  └──────────┬───────────┘   │ - YouTube OAuth 中继 (/yt-api)│    │
│             │               │ - 配置/密钥分发 (/api/config)  │    │
│             │               │ - 结果回调 (/api/callback)    │    │
│             │               └──────────────┬───────────────┘    │
│  ┌──────────▼──────────────────────────────▼───────────────┐    │
│  │  PostgreSQL                                               │    │
│  │  - books (book_data, book_status)                         │    │
│  │  - audiobook_chapters (file_id, upload_status)            │    │
│  │  - hf_jobs (新建: HF外包任务队列)                          │    │
│  └──────────┬──────────────────────────────┬───────────────┘    │
└─────────────┼──────────────────────────────┼────────────────────┘
              │                              │
   ┌──────────┴──────────┐         ┌─────────┴──────────┐
   │ 流水线 Worker池      │         │ 测试实验 Worker池   │
   │ (队列认领模式)       │         │ (即时触发模式)      │
   └──────────┬──────────┘         └─────────┬──────────┘
              │                              │
   ┌──────────▼──────────┐         ┌─────────▼──────────┐
   │ HF Space #1..N      │         │ HF Space #1..2     │
   │ audiobook-pipeline- │         │ audiobook-test-    │
   │ worker              │         │ worker             │
   │ ┌─────────────────┐ │         │ ┌────────────────┐ │
   │ │ 打包 pipeline/  │ │         │ │ 独立实现4个测试 │ │
   │ │ process_book()  │ │         │ │ AI/BGM/TG/YT    │ │
   │ │ 执行单书完整流程 │ │         │ └────────────────┘ │
   │ └─────────────────┘ │         └────────────────────┘ │
   └──────────┬──────────┘                  │
              │                              │
       ┌──────▼──────┐                ┌──────▼──────┐
       │ TG/YouTube  │                │ 外部API/    │
       │ (经VPS中继) │                │ TG/YouTube  │
       └─────────────┘                └─────────────┘
```

### 3.3 两类 Worker 的差异

| 维度 | 流水线 Worker（流水线外包） | 测试实验 Worker（测试外包） |
|------|--------------------------|--------------------------|
| **核心代码** | 打包 `pipeline/`，调用 `process_book()` | 独立实现4个测试逻辑 |
| **触发模式** | 队列认领（PostgreSQL `FOR UPDATE SKIP LOCKED`） | 即时触发（HTTP 请求-响应） |
| **任务粒度** | 一本书为一个任务 | 一个测试为一个任务 |
| **并发** | 多 Worker 多槽位并行 | 单 Worker 单槽位 |
| **核心算力** | BGM混音 + AI封面 + MP4封装 | BGM混音 + AI图片生成 |
| **输出** | YouTube视频 + DB状态更新 | HTTP 响应返回结果 |
| **Worker 数** | 推荐 3-5 个 | 推荐 1-2 个 |

### 3.4 新文件夹位置（项目根目录）

所有 HF 外包文件集中在项目根目录新建的 `hf_workers/`：

```
yt_aduio_book_one_to_all/
├── backend/                 # ✅ 当前项目后端(保留,新增 tasks_hf.py 转发层)
├── pipeline/                # ✅ 当前项目流水线(零改动,Worker镜像打包此目录)
├── docker/                  # ✅ 当前项目Docker配置(保留)
├── scripts/                 # ✅ 脚本(保留)
│
└── hf_workers/              # ⭐ 新建:HF外包项目所有文件(独立自包含)
    ├── pipeline_worker/     #   流水线外包Worker(执行 process_book)
    │   ├── app.py           #     Flask服务+API+状态面板
    │   ├── Dockerfile       #     COPY pipeline/ 进镜像 + ffmpeg + DeepFilter
    │   └── requirements.txt #     复用项目 pipeline 的依赖
    ├── test_worker/         #   测试实验Worker
    │   ├── app.py           #     Flask服务+测试API
    │   ├── test_runner.py   #     4个测试独立实现
    │   ├── Dockerfile       #     ffmpeg + librosa + numpy
    │   └── requirements.txt
    ├── vps_relay/           #   VPS中继调度器(轻量)
    │   ├── app.py           #     调度+TG中继+OAuth中继+配置分发+回调
    │   ├── Dockerfile
    │   └── docker-compose.yml
    └── README.md            #   统一部署说明
```

**设计原则**：
1. `pipeline_worker/` 镜像构建时 `COPY` 当前项目的 `pipeline/` 目录，不重写逻辑
2. `hf_workers/` 自包含，不依赖项目其他目录（构建镜像时从项目根 COPY pipeline/）
3. 与 `backend/` 解耦：Worker 只通过 PostgreSQL + HTTP 中继与 VPS 交互

---

## 四、流水线 Worker（核心：执行 process_book）

### 4.1 职责

HF Worker 接收一本书的处理任务，调用 `pipeline.process_book()` 执行完整流水线：

```
1. claim_next_job()           — 原子认领一本待处理的书 (FOR UPDATE SKIP LOCKED)
2. apply_runtime_config()     — 注入配置(TG/ModelScope/YouTube等,从VPS拉取)
3. process_book(book_record)  — 调用 pipeline 执行完整流水线
     ├─ download_chapter_items()    从TG下载已降噪音频(经VPS中继)
     ├─ mix_with_bgm()              BGM混音(CPU密集)
     ├─ auto_create_youtube_cover() AI封面(调ModelScope)
     ├─ auto_create_youtube_seo()   SEO文案(调ModelScope)
     ├─ generate_video()            MP4封装(ffmpeg)
     └─ upload_to_youtube_detailed() YouTube上传(经VPS OAuth中继)
4. record_result()            — 写回 youtube_url / book_status / 错误信息
5. notify_vps()               — 回调通知VPS(可选)
```

### 4.2 任务队列设计

新建轻量队列表 `hf_jobs`（或复用 `books.book_status` 字段）：

```sql
CREATE TABLE IF NOT EXISTS public.hf_jobs (
    job_id        serial PRIMARY KEY,
    job_type      varchar(50) NOT NULL,        -- 'tg_cache_pipeline' / 'test_*'
    book_id       text,                         -- 流水线任务的书ID
    channel_name  text,                         -- YouTube频道名
    status        varchar(50) DEFAULT 'pending',-- pending/processing/done/failed
    worker_id     varchar(100),                 -- 认领的Worker ID
    claimed_at    timestamptz,                  -- 认领时间
    result        jsonb,                        -- 处理结果(youtube_url等)
    error_message text,                         -- 失败原因
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS idx_hf_jobs_status ON public.hf_jobs(status);
CREATE INDEX IF NOT EXISTS idx_hf_jobs_type_status ON public.hf_jobs(job_type, status);
```

**原子认领**（多 Worker 并行不冲突）：

```sql
UPDATE hf_jobs
SET status = 'processing', worker_id = %s, claimed_at = NOW()
WHERE ctid IN (
    SELECT ctid FROM hf_jobs
    WHERE job_type = 'tg_cache_pipeline' AND status = 'pending'
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING job_id, book_id, channel_name
```

### 4.3 配置注入与凭证中继

HF Worker 启动时从 VPS 拉取配置，注入 `pipeline.config`：

```
HF Worker 启动 / 认领任务前
  └─▶ GET VPS /api/pipeline-config?channel=频道名
       返回: {
         TG_BOT_TOKEN: "***",              # 多Bot Token
         TELEGRAM_API_BASE: "http://VPS/tg-api",  # TG中继
         MODELSCOPE_TOKEN: "***",
         YOUTUBE_CHANNEL_NAME: "频道名",
         ENABLE_BGM_MIX: true,
         ENABLE_DEEPFILTER: false,          # TG缓存模式可关闭降噪
         OUTPUT_ROOT: "/tmp/output",
         POSTGRES_DSN: "***",
         YOUTUBE_OAUTH_BASE: "http://VPS/yt-api"  # YouTube中继
       }
  └─▶ apply_runtime_config(config)  # 注入 pipeline 模块级全局
```

**凭证处理策略**：

| 凭证 | HF Worker 获取方式 | 是否落地 HF |
|------|-------------------|:---:|
| TG Bot Token | `/api/pipeline-config` 拉取 | ❌ |
| ModelScope Token | `/api/pipeline-config` 拉取 | ❌ |
| YouTube OAuth | **不持有**，YouTube API 调用经 VPS `/yt-api` 中继 | ❌ |
| POSTGRES_DSN | HF Secret 存储 | ✅（连接串） |
| VPS_RELAY_URL | HF Secret 存储 | ✅ |

> ⚠️ **YouTube OAuth** 最敏感：HF Worker 不持有 token.json，`pipeline/youtube.py` 的 API 调用通过 VPS 中继代理，凭证永不离开 VPS。

### 4.4 TG API 中继

HF 无法直连 `api.telegram.org`，`pipeline/tg_audio.py` 的 getFile / 文件下载请求经 VPS 中继：

```
HF Worker (pipeline/tg_audio.py)
  └─▶ requests.get("http://VPS/tg-api/bot<token>/getFile?file_id=xxx")
       └─▶ VPS 转发到 https://api.telegram.org/bot<token>/getFile?file_id=xxx
       └─▶ 返回结果给 HF Worker
```

`pipeline/config.py` 的 `TELEGRAM_API_BASE` 配置为中继地址即可，`tg_audio.py` 代码不用改。

### 4.5 YouTube OAuth 中继

`pipeline/youtube.py` 的 YouTube API 调用经 VPS 中继：

```
HF Worker (pipeline/youtube.py)
  └─▶ VPS /yt-api/<channel>/upload  (上传视频)
       └─▶ VPS 用本地 token.json 认证
       └─▶ 调用 YouTube Data API
       └─▶ 返回 video_id 给 HF Worker
```

VPS 中继层持有各频道的 `token.json`，代理所有 YouTube API 调用。HF Worker 只传视频文件 + 元数据。

> 💡 这是对 `youtube.py` 的**适配层**：HF Worker 镜像里的 `pipeline/youtube.py` 需小幅改造，把直接认证改为走中继。改造通过环境变量 `YOUTUBE_OAUTH_BASE` 控制，默认（本机）走原逻辑，设置时走中继。

### 4.6 BGM 音乐池

HF Worker 无法访问 VPS 的 `music_dir`。两种方案：
- **方案A（推荐）**：Docker 镜像内置一组样本 BGM（随镜像打包），够处理用
- **方案B**：Worker 启动时从 VPS 下载音乐池到本地（增加冷启动时间）

`pipeline/bgm.py` 的 `mix_with_bgm(music_dir)` 参数指向 HF 本地音乐目录即可。

### 4.7 输出文件处理

`process_book()` 会在 `OUTPUT_ROOT` 下生成中间文件（章节音频、混音、封面、MP4）。HF Space 临时存储有限（16GB），需定期清理：

- 每本书处理完后，删除中间文件，只保留必要结果
- YouTube 上传成功后，MP4 可删除
- `OUTPUT_ROOT` 设为 `/tmp/output`（HF 容器临时空间）

### 4.8 API 设计

#### 流水线 Worker API（`hf_workers/pipeline_worker/app.py`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 状态面板（HTML：槽位 + 任务进度 + 最近结果） |
| GET | `/health` | 健康检查 → `{ok, worker_id, free_slots, total_slots}` |
| GET | `/status` | 详细状态 JSON |
| POST | `/process` | 触发认领并处理一个任务（调度器调用） |
| POST | `/process-batch` | 启动批量处理（连续认领多个任务） |
| GET | `/batch-status` | 批量处理进度 |
| POST | `/batch-stop` | 停止批量处理 |
| POST | `/refresh-config` | 从 VPS 拉取最新配置 |
| GET | `/test-telegram` | 测试 TG 中继连通性 |

---

## 五、测试实验 Worker

### 5.1 外包适配性分析

测试实验共 4 个功能（`backend/api/tests.py`）：

| 测试 | 算力特征 | 外包价值 | 建议 |
|------|---------|---------|------|
| **BGM 混音** | 🔥 STFT/ISTFT 高 CPU，数分钟 | ⭐⭐⭐⭐⭐ | 优先外包 |
| **AI 测试**（SEO+封面） | 调外部API，图片生成占内存 | ⭐⭐⭐ | 外包 |
| **TG 下载测试** | getFile + 下载，IO 密集 | ⭐⭐ | 外包（复用中继） |
| **YouTube 上传测试** | OAuth + API，低算力 | ⭐ | 外包（凭证中继） |

### 5.2 触发模式

即时触发（HTTP 请求-响应），无队列：

```python
# backend/api/tests_hf.py 伪代码(轻量转发层)
def _pick_idle_test_worker() -> str | None:
    for url in HF_TEST_WORKER_URLS:
        try:
            r = requests.get(f'{url}/health', timeout=5)
            if r.json().get('ok') and not r.json().get('busy'):
                return url
        except Exception:
            continue
    return None
```

### 5.3 API 设计

#### 测试 Worker API（`hf_workers/test_worker/app.py`）

| 方法 | 路径 | 说明 | 对应本机端点 |
|------|------|------|------------|
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

> 💡 `tests_hf.py` 是**轻量转发层**，不执行算力逻辑，仅：选空闲 Worker → 转发 → 透传响应。与轨道A 的 `tests.py`（重量级，直接跑 pipeline）形成对照。

### 5.4 BGM 混音外包特殊处理

1. **音乐池**：测试 Worker 内置样本 BGM（随镜像打包）
2. **章节音频**：Worker 通过 POSTGRES_DSN 读 `books.book_data` 获取 mp3Url 下载
3. **混音结果**：输出转 base64 嵌入 JSON 返回（< 10MB）
4. **异步执行**：job_id 模式，前端轮询进度

---

## 六、VPS 中继调度器

### 6.1 职责

VPS 上运行轻量 Flask 服务（`hf_workers/vps_relay/app.py`），承担五项职能：

| 职能 | 端点 | 说明 |
|------|------|------|
| **调度流水线 Worker** | 内部后台线程 | 筛选TG缓存完整书 → 写入 `hf_jobs` → 触发空闲 Worker |
| **TG API 中继** | `/tg-api/<path>` | 代理转发到 api.telegram.org |
| **YouTube OAuth 中继** | `/yt-api/<channel>/<action>` | 代理 YouTube API（持 token.json） |
| **配置/密钥分发** | `/api/pipeline-config` / `/api/test-config` | 分发配置（不落地 HF） |
| **结果回调** | `/api/callback` | 接收 Worker 完成通知 |

### 6.2 调度主循环

```
每 15 秒:
  1. 查询 hf_jobs 中 pending/processing/done/failed 数量
  2. 对所有流水线 Worker 调 /health, 获取空闲槽位
  3. 若 pending > 0 且有空闲 Worker:
       POST /process 触发该 Worker 认领并处理一个任务
  4. 所有 Worker 满载: 等待 5s
  5. 所有 Worker 离线: 等待 30s (冷启动)
  6. 定期清理卡住任务 (默认 600s, 超时 1440 分钟重置)
```

### 6.3 任务投递

用户在当前项目前端触发「仅TG缓存完整书处理+上传」时，可选择本机跑或 HF 外包：

- **本机跑（轨道A）**：`backend/services/task_service.py` 现有逻辑，后台线程调 `run_pipeline()`
- **HF外包（轨道B）**：把符合条件的书批量写入 `hf_jobs` 表（`job_type='tg_cache_pipeline'`），调度器自动派发

### 6.4 配置分发

```
GET /api/pipeline-config?channel=频道名
返回: {
  TG_BOT_TOKEN: "***",
  TELEGRAM_API_BASE: "http://VPS/tg-api",
  MODELSCOPE_TOKEN: "***",
  YOUTUBE_CHANNEL_NAME: "频道名",
  YOUTUBE_OAUTH_BASE: "http://VPS/yt-api",
  ENABLE_BGM_MIX: true,
  ENABLE_DEEPFILTER: false,
  OUTPUT_ROOT: "/tmp/output",
  POSTGRES_DSN: "***",
  ...
}
```

HF Space 的 Secrets 只存 `POSTGRES_DSN` 和 `VPS_RELAY_URL`，其余动态拉取。

### 6.5 冷启动容错

| 场景 | 调度器行为 | Worker 行为 |
|------|-----------|-------------|
| Worker 休眠 | `/health` 超时 → 跳过 | — |
| 冷启动中 | 超时 → 等待 30s 重试 | 启动后 `/health` 恢复 |
| 在线但满载 | `free_slots=0` → 等待 5s | 处理完释放槽位 |
| 彻底不可达 | 连续失败 → 尝试其他 Worker | — |

---

## 七、任务生命周期与排队

### 7.1 流水线任务生命周期

```
   用户投递 / 自动筛选
        │
        ▼
   ┌─────────┐
   │ pending │ ← 写入 hf_jobs
   └────┬────┘
        │ claim_next_job() [FOR UPDATE SKIP LOCKED]
        ▼
   ┌────────────┐
   │ processing │ ← worker_id 记录
   └─────┬──────┘
         │
   ┌─────┴─────┐
   │           │
   成功        失败
   │           │
   ▼           ▼
┌──────┐  ┌────────┐
│ done │  │ failed │ ← error_message 记录
└──┬───┘  └────┬───┘
   │           │ 清理重置
   │           ▼
   │       ┌─────────┐
   │       │ pending │ ← 重新排队
   │       └─────────┘
   ▼
DB 更新:
  books.book_status = 'success'
  hf_jobs.result = {youtube_url, ...}
```

### 7.2 卡住任务恢复

```sql
-- 重置超时的 processing (默认 1440 分钟)
UPDATE hf_jobs
SET status = 'pending', worker_id = NULL, claimed_at = NULL
WHERE job_type = 'tg_cache_pipeline'
  AND status = 'processing'
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
| 本机任务执行 | `backend/services/task_service.py` | ✅ 保留不动（轨道A 入口） |
| 本机测试实验 | `backend/api/tests.py` | ✅ 保留不动 |
| 测试页面 | `backend/templates/*_test.html` | ✅ 保留（新增 HF 入口按钮） |
| Pipeline 模块 | `pipeline/*.py` | ✅ 全部保留不动（Worker 镜像 COPY 此目录） |
| TG缓存下载 | `pipeline/tg_audio.py` | ✅ 保留（从TG下载已降噪音频） |
| YouTube上传 | `pipeline/youtube.py` | ✅ 保留（本机直跑用原逻辑） |

### 8.3 两轨切换

| 操作 | 轨道A（本机） | 轨道B（HF外包） |
|------|--------------|----------------|
| 「仅TG缓存完整书」任务 | 现有 `tg_cache_only` 按钮 → `run_pipeline()` 本机跑 | 新增「HF外包」按钮 → 写入 `hf_jobs` → Worker 处理 |
| 测试实验 | 「运行测试」按钮 → `/api/tests/*` | 「运行测试(HF)」按钮 → `/api/tests-hf/*` |

---

## 九、部署实施步骤

### 9.1 前置条件

- [ ] VPS 已部署 PostgreSQL + 当前项目（`docker-compose.yml`）
- [ ] `audiobook_chapters` 表已有 file_id 数据（TG缓存完整书可用）
- [ ] 至少 2 个 Telegram Bot Token
- [ ] Telegram Chat ID
- [ ] YouTube 频道已完成 OAuth 授权（`token.json` 在 VPS）
- [ ] MODELSCOPE_TOKEN 已配置
- [ ] Hugging Face 账号

### 9.2 步骤 1：部署 VPS 中继调度器

```bash
cd /path/to/yt_aduio_book_one_to_all/hf_workers/vps_relay
vi docker-compose.yml
```

配置：

```yaml
environment:
  - POSTGRES_DSN=postgresql://audiobook_app:xxx@host.docker.internal:5432/audiobook
  # 流水线 Worker URLs
  - PIPELINE_WORKER_URLS=https://用户名-audiobook-pipeline-worker-1.hf.space,https://用户名-audiobook-pipeline-worker-2.hf.space
  # 测试 Worker URLs
  - TEST_WORKER_URLS=https://用户名-audiobook-test-worker-1.hf.space
  - TG_CHAT_ID=
  - TG_BOT_TOKENS=
  - TEST_MODELSCOPE_TOKEN=
  - WEB_PORT=38080
  - WEB_PASSWORD=你的密码
  - CHECK_INTERVAL=15
  - STUCK_TIMEOUT_M=1440
  - CLEANUP_INTERVAL=600
  # YouTube OAuth 凭证目录
  - YT_OAUTH_DIR=/data/oauth_tokens
volumes:
  - ./data/oauth_tokens:/data/oauth_tokens  # 各频道 token.json
```

```bash
docker compose up -d --build
```

访问 `http://VPS_IP:38080`，在 Web 面板配置 Telegram / ModelScope 信息。

### 9.3 步骤 2：部署流水线 Worker（每个 Space 重复）

1. HF → New Space → Name=`audiobook-pipeline-worker-1` → SDK=**Docker** → **Private**
2. 上传 `hf_workers/pipeline_worker/` 下所有文件到 Space 仓库
3. **关键**：Dockerfile 构建时从当前项目 COPY `pipeline/` 目录（构建脚本处理）
4. 配置 Secrets：

| 变量 | 值 |
|-----|-----|
| `POSTGRES_DSN` | `postgresql://...@VPS公网IP:5432/audiobook` |
| `VPS_RELAY_URL` | `http://VPS公网IP:38080` |
| `NUM_SLOTS` | `1`（单书处理占满CPU，建议单槽） |

5. 验证：
```bash
curl https://用户名-audiobook-pipeline-worker-1.hf.space/health
# {"ok":true,"worker_id":"hf_xxxx","free_slots":1,"total_slots":1}
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
1. 配置流水线 Worker URLs
2. 配置测试 Worker URLs
3. 配置 Telegram（Chat ID + Bot Tokens）
4. 配置 ModelScope Token
5. 确认 YouTube OAuth 凭证目录有各频道 token.json
6. 保存 → 启动调度器

### 9.6 步骤 5：投递流水线任务

方式一：前端「仅TG缓存完整书处理+上传(HF外包)」按钮（新增），把符合条件的书写入 `hf_jobs`。

方式二：手动 SQL：
```sql
INSERT INTO hf_jobs (job_type, book_id, channel_name, status)
SELECT 'tg_cache_pipeline', book_id, '频道名', 'pending'
FROM books
WHERE book_status = 'pending';
```

调度器自动触发 Worker 处理。

---

## 十、推荐部署规模

| 组件 | 数量 | 并发 | 适用场景 |
|------|:---:|:---:|------|
| 流水线 Worker | 1 | 1槽 | 测试 |
| 流水线 Worker | 3 | 3槽 | **推荐**：日常处理 |
| 流水线 Worker | 5 | 5槽 | 大批量 |
| 测试 Worker | 1 | 1槽 | 日常测试 |

> ⚠️ 单本书的 BGM混音 + MP4封装很吃 CPU，建议每 Worker 单槽位（`NUM_SLOTS=1`），避免 CPU 争抢。多 Worker 扩容而非单 Worker 多槽。

---

## 十一、结果回报与监控

### 11.1 流水线 Worker 回报

每处理完一本书：
1. 写回 `hf_jobs.result`（youtube_url / 错误信息）+ `status='done'/'failed'`
2. 更新 `books.book_status`
3. 可选：POST 回调 VPS `/api/callback`（实时通知）

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

---

## 十二、优化建议

### 12.1 优先级高

| 优化项 | 说明 |
|--------|------|
| **结果回调通知** | Worker 完成后主动 POST VPS，实时感知 |
| **整书完成 TG 通知** | 一本书处理完，调度器发 Telegram 通知管理员 |
| **Worker 业绩统计** | 记录每 Worker 处理数 / 成功率 / 平均耗时 |
| **前端 HF 入口** | 频道详情页 / 测试页增加「HF外包」按钮 |
| **YouTube 中继适配** | `pipeline/youtube.py` 增加 `YOUTUBE_OAUTH_BASE` 环境变量控制走中继 |

### 12.2 优先级中

| 优化项 | 说明 |
|--------|------|
| **优先级队列** | `hf_jobs` 增加 `priority` 字段 |
| **DeepFilter 预热** | 冷启动时预热（TG缓存模式可关降噪） |
| **Worker 自动注册** | Worker 启动时主动注册到 VPS |
| **中间文件清理** | 每书处理完自动清理，避免 HF 磁盘满 |

### 12.3 优先级低

| 优化项 | 说明 |
|--------|------|
| **Colab 备选** | Google Colab Worker 作为 HF 备选 |
| **Prometheus 监控** | 调度器暴露 `/metrics` |
| **Webhook 触发** | 写入 `hf_jobs` 后立即 POST 调度器 |

---

## 十三、风险与应对

### 13.1 HF Space 限制

| 风险 | 应对 |
|------|------|
| 16GB内存/2vCPU | BGM混音/MP4封装失败则标记 failed，不阻塞队列 |
| 15分钟休眠 | 调度器超时视为冷启动，跳过尝试其他 |
| HF 政策调整 | 多 Worker 分散 + 本机直跑兜底（轨道A） |
| 磁盘空间 | 每书处理完清理中间文件 |

### 13.2 YouTube OAuth 中继

| 风险 | 应对 |
|------|------|
| 中继层单点 | VPS 稳定性保障 + 本机直跑兜底 |
| token 过期 | VPS 中继层自动刷新 token |
| 大文件传输 | MP4 经中继上传到 YouTube（HF→VPS→YouTube），注意带宽 |

### 13.3 数据一致性

| 风险 | 应对 |
|------|------|
| Worker 崩溃卡住 | 调度器定期清理（超时重置） |
| 并发认领冲突 | `FOR UPDATE SKIP LOCKED` 原子保证 |
| DB 连接失败 | 重试机制 |

### 13.4 安全

| 风险 | 应对 |
|------|------|
| HF 密钥泄露 | Space Private + 配置动态拉取不落地 |
| OAuth 凭证泄露 | HF Worker 不持有，经 VPS 中继代理 |
| PG 暴露公网 | 防火墙白名单 |

---

## 十四、验收标准

### 14.1 流水线外包验收

- [ ] VPS 调度器面板可访问，显示所有流水线 Worker 在线
- [ ] 每个 Worker 的 `/health` 返回 `{"ok": true}`
- [ ] 写入 `hf_jobs` 后，调度器自动触发 Worker 认领处理
- [ ] Worker 成功执行 `process_book()`：TG下载→BGM混音→封面→SEO→MP4→YouTube上传
- [ ] 处理结果写回 `hf_jobs.result`（含 youtube_url）
- [ ] `books.book_status` 正确更新
- [ ] 多 Worker 并行无认领冲突
- [ ] 模拟 Worker 休眠，调度器自动跳过；恢复后自动纳入
- [ ] Worker 崩溃后，调度器超时重置任务为 pending
- [ ] YouTube OAuth 凭证不落地 HF（经 VPS 中继）

### 14.2 测试实验外包验收

- [ ] 测试 Worker `/health` 返回 `{"ok": true, "busy": false}`
- [ ] 测试页面显示「运行测试(HF外包)」按钮
- [ ] HF外包运行 AI 测试，返回 SEO 文案/封面图片
- [ ] HF外包运行 BGM 混音，轮询进度至完成，输出可播放
- [ ] HF外包运行 TG 下载测试，getFile 验证成功（经中继）
- [ ] HF外包运行 YouTube 上传测试，返回频道信息（经中继）
- [ ] 配置动态拉取，HF Space 内无明文密钥

### 14.3 双轨架构验收

- [ ] 轨道A（本机）`/api/tests/*` 和「仅TG缓存完整书」本机任务正常
- [ ] 轨道B（HF）`/api/tests-hf/*` 和 `hf_jobs` 任务正常
- [ ] `pipeline/` 和 `backend/api/tests.py` 代码零改动
- [ ] 两轨互不干扰

---

## 十五、附录

### 15.1 环境变量速查

#### 流水线 Worker（`hf_workers/pipeline_worker/`）

| 变量 | 必填 | 默认 | 说明 |
|------|:---:|------|------|
| `POSTGRES_DSN` | ✅ | — | PG连接串（VPS公网IP） |
| `VPS_RELAY_URL` | ✅ | — | VPS中继地址（拉配置+TG中继+YT中继） |
| `NUM_SLOTS` | | `1` | 并行槽位（建议1） |
| `TG_BOT_TOKEN` | | (拉取) | 多Bot Token（不落地） |
| `MODELSCOPE_TOKEN` | | (拉取) | AI凭证（不落地） |
| `TELEGRAM_API_BASE` | | (拉取) | TG中继地址 |
| `YOUTUBE_OAUTH_BASE` | | (拉取) | YouTube中继地址 |

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
| `PIPELINE_WORKER_URLS` | ✅ | — | 流水线Worker地址 |
| `TEST_WORKER_URLS` | | — | 测试Worker地址 |
| `TG_CHAT_ID` | | (面板配) | TG Chat ID |
| `TG_BOT_TOKENS` | | (面板配) | 多Bot Token |
| `TEST_MODELSCOPE_TOKEN` | | (面板配) | 测试AI Token |
| `YT_OAUTH_DIR` | | `/data/oauth_tokens` | YouTube凭证目录 |
| `WEB_PORT` | | `38080` | 面板端口 |
| `WEB_PASSWORD` | | — | 面板密码 |
| `CHECK_INTERVAL` | | `15` | 调度间隔 |
| `STUCK_TIMEOUT_M` | | `1440` | 卡住超时 |

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
| GET | `/api/pipeline-config` | 流水线配置分发（Worker拉取） |
| GET | `/api/test-config` | 测试配置分发（Worker拉取） |
| ANY | `/tg-api/<path>` | Telegram API中继 |
| ANY | `/yt-api/<channel>/<action>` | YouTube OAuth中继 |
| POST | `/api/callback` | Worker结果回调 |

### 15.3 文件索引

#### 新建文件（`hf_workers/`）

| 文件 | 说明 |
|------|------|
| `hf_workers/pipeline_worker/app.py` | 流水线Worker Flask服务 |
| `hf_workers/pipeline_worker/Dockerfile` | COPY pipeline/ + ffmpeg + DeepFilter |
| `hf_workers/pipeline_worker/requirements.txt` | 复用项目pipeline依赖 |
| `hf_workers/test_worker/app.py` | 测试Worker Flask服务 |
| `hf_workers/test_worker/test_runner.py` | 4个测试独立实现 |
| `hf_workers/test_worker/Dockerfile` | ffmpeg + librosa + numpy |
| `hf_workers/vps_relay/app.py` | VPS中继调度器 |
| `hf_workers/vps_relay/docker-compose.yml` | 一键部署 |
| `hf_workers/README.md` | 统一部署说明 |
| `backend/api/tests_hf.py` | VPS端测试转发层 |

#### 现有文件（保留不动）

| 文件 | 说明 |
|------|------|
| `pipeline/pipeline.py` | 主流程（含 `process_book` / `process_standard_book` / `run_pipeline`） |
| `pipeline/tg_audio.py` | 从TG下载已降噪音频 |
| `pipeline/bgm.py` | BGM混音 |
| `pipeline/cover.py` | AI封面/SEO生成 |
| `pipeline/youtube.py` | YouTube认证上传（本机原逻辑保留） |
| `pipeline/deepfilter.py` | DeepFilter降噪 |
| `backend/api/tests.py` | 本机测试实验（轨道A保留） |
| `backend/services/task_service.py` | 任务服务（轨道A入口） |
| `docker/init-db.sql` | 数据库表结构 |

### 15.4 目录结构总览

```
yt_aduio_book_one_to_all/
├── backend/                 # ✅ 当前项目后端(保留,新增 tests_hf.py)
├── pipeline/                # ✅ 当前项目流水线(零改动,Worker镜像COPY此目录)
├── docker/                  # ✅ Docker配置(保留)
├── scripts/                 # ✅ 脚本(保留)
│
└── hf_workers/              # ⭐ 新建:HF外包所有文件
    ├── pipeline_worker/     #    流水线Worker(执行process_book)
    ├── test_worker/         #    测试实验Worker
    ├── vps_relay/           #    VPS中继调度器
    └── README.md
```

---

## 十六、实施路线图

### 阶段 1：流水线 Worker（核心）

1. [ ] 创建 `hf_workers/pipeline_worker/`
2. [ ] 编写 `Dockerfile`（COPY `pipeline/` + ffmpeg + DeepFilter + 依赖）
3. [ ] 实现 `app.py`（Flask + 认领任务 + 调用 `process_book` + 写回结果）
4. [ ] 实现 `hf_workers/vps_relay/`（调度 + TG中继 + YouTube中继 + 配置分发）
5. [ ] 新建 `hf_jobs` 表
6. [ ] `pipeline/youtube.py` 适配中继（`YOUTUBE_OAUTH_BASE` 环境变量控制）
7. [ ] 部署到 HF Space，验证单书完整流程
8. [ ] 前端新增「仅TG缓存完整书(HF外包)」入口

### 阶段 2：测试实验 Worker

1. [ ] 创建 `hf_workers/test_worker/`
2. [ ] 实现 `test_runner.py`（4个测试）
3. [ ] 实现 `app.py`
4. [ ] 编写 `Dockerfile`（ffmpeg + librosa）
5. [ ] 实现 `backend/api/tests_hf.py`（转发层）
6. [ ] 测试页面加 HF 入口按钮
7. [ ] 部署验证

### 阶段 3：优化增强

1. [ ] 结果回调通知
2. [ ] 整书完成 TG 通知
3. [ ] Worker 业绩统计
4. [ ] 中间文件自动清理
5. [ ] Prometheus 监控

---

> 📌 **核心要点**：本计划书外包的是**当前项目自己的**「仅TG缓存完整书处理+上传」流水线（`pipeline/pipeline.py` 的 `process_standard_book` 完整流程）。HF Worker 把 `pipeline/` 打包进镜像，作为"远程pipeline执行器"调用 `process_book()`。本机自跑套件完整保留（`pipeline/` + `backend/` 零改动），形成双轨架构。所有 HF 文件集中在项目根目录新建的 `hf_workers/` 文件夹。与 `audiobook_pipeline`（无关的上游项目）无任何依赖关系。
