# HF 外包架构部署指南

## 架构概览

本系统采用 **「本机自跑 + HF 外包」双轨架构**，在保留原有本机 pipeline 直跑能力的基础上，将算力密集型任务外包至 Hugging Face (HF) 免费 Docker 空间。

```
┌─────────────────────────────────────────────────────────┐
│                    VPS（你的服务器）                       │
│                                                         │
│  ┌─────────────┐     ┌──────────────────────────────┐  │
│  │  Backend     │     │  VPS 中继调度器 (:38080)      │  │
│  │  (FastAPI)   │────▶│  · 任务调度                   │  │
│  │  轨道A：本机  │     │  · TG API 中继               │  │
│  │  自跑pipeline │     │  · YouTube OAuth 中继         │  │
│  └─────────────┘     │  · 配置/密钥分发              │  │
│         │             │  · 结果回调                   │  │
│         │             └──────────┬───────────────────┘  │
│         │                        │ HTTP                  │
│         │             ┌──────────▼───────────────────┐  │
│         │             │     PostgreSQL 数据库         │  │
│         │             │  (hf_jobs 队列 + 章节状态)     │  │
│         │             └──────────────────────────────┘  │
│         │                        │                      │
└─────────┼────────────────────────┼──────────────────────┘
          │ 轨道A                   │ 轨道B
          │ 本机直跑                 │ HF 外包
          ▼                        ▼
   ┌──────────────┐     ┌──────────────────────────┐
   │  本机 pipeline │     │  HF Space (免费 Docker)    │
   │  process_book()│     │  · 流水线 Worker (×N)     │
   │  串行执行      │     │  · 测试 Worker (×N)       │
   └──────────────┘     │  复用 pipeline/ 全部逻辑   │
                        │  凭证不落地（经 VPS 中继）  │
                        └──────────────────────────┘
```

### 双轨对照

| 维度 | 轨道A（本机自跑） | 轨道B（HF 外包） |
|------|-------------------|------------------|
| 入口 | 频道详情页「仅TG缓存完整书处理+上传」按钮 | 频道详情页「HF外包：TG缓存完整书投递」按钮 |
| 执行位置 | VPS 本机 | HF Space 远程 Worker |
| 凭证 | 本机直接持有 | 经 VPS 中继代理，不落地 HF |
| 并发 | 串行（单 VPS） | 并行（多 HF Space） |
| API | `/api/tasks` | `/api/tasks-hf/seed` |
| 测试 | `/api/tests/*` | `/api/tests-hf/*` |

---

## 组件清单

```
hf_workers/
├── vps_relay/          # VPS 中继调度器（部署在 VPS 上）
│   ├── app.py          # Flask 应用：调度 + 中继 + 配置分发
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── requirements.txt
├── pipeline_worker/    # HF 流水线 Worker（部署在 HF Space）
│   ├── app.py          # Flask 应用：认领任务 → 执行 pipeline
│   ├── Dockerfile      # 构建上下文为项目根目录
│   └── requirements.txt
├── test_worker/        # HF 测试 Worker（部署在 HF Space）
│   ├── app.py          # Flask 应用：同步执行测试
│   ├── runner.py       # 测试执行器：AI/上传/TG下载/BGM混音
│   ├── Dockerfile
│   └── requirements.txt
└── README.md           # 本文件
```

---

## 部署步骤

### 第一步：部署 VPS 中继调度器

VPS 中继调度器与当前项目部署在同一台 VPS 上，连接同一个 PostgreSQL 数据库。

```bash
# 1. 进入 VPS 中继目录
cd hf_workers/vps_relay

# 2. 编辑 docker-compose.yml，修改环境变量
#    - POSTGRES_DSN: 你的 PostgreSQL 连接串
#    - PIPELINE_WORKER_URLS: HF 流水线 Worker 地址（部署后填写）
#    - TEST_WORKER_URLS: HF 测试 Worker 地址（部署后填写）
#    - TG_CHAT_ID / TG_BOT_TOKENS: Telegram 通知用
#    - WEB_PASSWORD: 管理面板密码

# 3. 构建并启动
docker compose up -d --build

# 4. 验证
curl http://localhost:38080/api/status
```

启动后访问 `http://VPS_IP:38080` 可查看管理面板。

### 第二步：部署 HF 流水线 Worker

#### 2.1 在 Hugging Face 创建 Space

1. 登录 [huggingface.co](https://huggingface.co)
2. 点击右上角头像 → New Space
3. 设置：
   - **Owner**: 你的用户名
   - **Space name**: `audiobook-pipeline-worker-1`（可创建多个实现并行）
   - **License**: MIT
   - **SDK**: Docker
   - **Visibility**: Public（免费版必须 Public）或 Private（付费）

#### 2.2 上传代码到 HF Space

HF Space 的 Docker 构建上下文是 Space 仓库根目录，但我们的 Dockerfile 需要项目根目录作为上下文（因为要 COPY pipeline/）。

**方法一：使用 HF Git LFS 直接推送**

```bash
# 1. 在 HF Space 仓库根目录放置以下文件：
#    - Dockerfile（从 hf_workers/pipeline_worker/Dockerfile 复制）
#    - app.py（从 hf_workers/pipeline_worker/app.py 复制）
#    - requirements.txt（从 hf_workers/pipeline_worker/requirements.txt 复制）
#    - pipeline/（整个目录，从项目根目录复制）

# 2. 克隆 HF Space 仓库
git clone https://huggingface.co/spaces/你的用户名/audiobook-pipeline-worker-1
cd audiobook-pipeline-worker-1

# 3. 复制文件
cp /path/to/project/hf_workers/pipeline_worker/Dockerfile .
cp /path/to/project/hf_workers/pipeline_worker/app.py .
cp /path/to/project/hf_workers/pipeline_worker/requirements.txt .
cp -r /path/to/project/pipeline/ ./pipeline/

# 4. 修改 Dockerfile 中的 COPY 路径
#    将 COPY hf_workers/pipeline_worker/requirements.txt 改为 COPY requirements.txt
#    将 COPY hf_workers/pipeline_worker/app.py 改为 COPY app.py
#    将 COPY pipeline/ 保持不变

# 5. 在 HF Space 的 Settings → Repository secrets 中添加环境变量：
#    - POSTGRES_DSN: 你的 PostgreSQL 连接串
#    - VPS_RELAY_URL: http://VPS_IP:38080

# 6. 提交推送
git add .
git commit -m "Initial pipeline worker"
git push
```

#### 2.3 验证流水线 Worker

```bash
# 等待 HF Space 构建完成（约 5-10 分钟），然后：
curl https://你的用户名-audiobook-pipeline-worker-1.hf.space/health
# 应返回: {"ok": true, "worker_id": "hf_pipeline_xxxx", ...}
```

### 第三步：部署 HF 测试 Worker

#### 3.1 创建 HF Space

- **Space name**: `audiobook-test-worker-1`

#### 3.2 上传代码

```bash
git clone https://huggingface.co/spaces/你的用户名/audiobook-test-worker-1
cd audiobook-test-worker-1

# 复制文件（同流水线 Worker，但使用 test_worker 的文件）
cp /path/to/project/hf_workers/test_worker/Dockerfile .
cp /path/to/project/hf_workers/test_worker/app.py .
cp /path/to/project/hf_workers/test_worker/runner.py .
cp /path/to/project/hf_workers/test_worker/requirements.txt .
cp -r /path/to/project/pipeline/ ./pipeline/

# 修改 Dockerfile 中的 COPY 路径（同上）

# 在 HF Space Settings → Repository secrets 中添加：
#    - POSTGRES_DSN
#    - VPS_RELAY_URL

git add .
git commit -m "Initial test worker"
git push
```

#### 3.3 验证测试 Worker

```bash
curl https://你的用户名-audiobook-test-worker-1.hf.space/health
# 应返回: {"ok": true, "worker_id": "hf_test_xxxx", "busy": false, ...}
```

### 第四步：配置 VPS 中继的 Worker 地址

将 HF Space 的地址填入 VPS 中继调度器：

```bash
# 方法一：编辑 docker-compose.yml 重启
# 在 PIPELINE_WORKER_URLS 和 TEST_WORKER_URLS 中填入 HF Space 地址

# 方法二：通过 VPS 中继管理面板 API 动态更新
curl -X POST http://VPS_IP:38080/api/config \
  -H "Content-Type: application/json" \
  -d '{
    "pipeline_worker_urls": ["https://你的用户名-audiobook-pipeline-worker-1.hf.space"],
    "test_worker_urls": ["https://你的用户名-audiobook-test-worker-1.hf.space"]
  }'
```

### 第五步：在后端全局设置中配置

登录本机后端管理系统 → 全局设置 → 找到 **🛰️ HF 外包** 分类：

| 配置项 | 说明 | 示例值 |
|--------|------|--------|
| `VPS_RELAY_URL` | VPS 中继调度器地址 | `http://VPS_IP:38080` |
| `HF_TEST_WORKER_URLS` | HF 测试 Worker 地址（逗号分隔） | `https://你的用户名-audiobook-test-worker-1.hf.space` |

保存后即可在前端使用 HF 外包功能。

---

## 使用指南

### 流水线外包（轨道B）

1. **投递任务**：进入「频道管理」→ 选择频道 → 点击「HF外包：TG缓存完整书投递」
   - 系统自动筛选所有章节均已上传 TG 的完整书
   - 写入 `hf_jobs` 队列，状态为 `pending`

2. **自动调度**：VPS 中继调度器后台运行，自动将 `pending` 任务派发给空闲 HF Worker
   - 调度间隔默认 15 秒
   - Worker 超时默认 1440 分钟（24 小时）

3. **查看进度**：点击侧栏「HF外包任务」查看任务队列
   - 统计卡片：待处理 / 处理中 / 已完成 / 失败
   - 任务列表：点击行查看详情（结果、错误信息）
   - 调度器控制：启动 / 停止 / 重置卡住任务

4. **Worker 执行流程**：
   ```
   认领任务(atomic) → 拉取配置(VPS中继) → TG下载(VPS中继代理) 
   → BGM混音 → AI封面 → SEO文案 → MP4封装 
   → YouTube上传(VPS中继持Token) → 写回结果 → 通知VPS
   ```

### 测试实验外包

在任意测试页面（AI测试 / 上传测试 / TG音频下载 / BGM混音测试）中：

1. 打开「**使用 HF 外包执行（远程 Worker）**」开关
2. 正常填写测试参数并运行
3. 请求自动转发到 HF 测试 Worker 执行
4. 结果返回前端展示

> **注意**：HF Space 冷启动可能需要 30-60 秒，首次请求请耐心等待。

---

## 凭证安全架构

HF Worker **不持有任何敏感凭证**，所有 API 调用均通过 VPS 中继代理：

| 凭证 | 存储位置 | HF Worker 访问方式 |
|------|----------|-------------------|
| TG Bot Token | VPS 中继环境变量 | `/tg-api/*` 代理转发 |
| YouTube OAuth Token | VPS `/data/oauth_tokens/` | `/yt-api/<channel>/<action>` 中继 |
| ModelScope Token | VPS 中继配置 | `/api/test-config` 动态分发 |
| PostgreSQL DSN | HF Space Secrets | Worker 直连（只用于读写任务状态） |

### TG API 中继流程

```
HF Worker → POST VPS:38080/tg-api/bot{token}/getFile
         → VPS 持 Token 调用 api.telegram.org
         → 返回结果给 HF Worker
         → HF Worker 下载文件（直连 TG CDN，无需 Token）
```

### YouTube 上传中继流程

```
HF Worker → 生成 MP4 → 流式传输到 VPS
         → POST VPS:38080/yt-api/<channel>/upload (multipart)
         → VPS 持 OAuth Token 调用 YouTube Data API 上传
         → 返回 video_id 给 HF Worker
         → HF Worker 写回数据库
```

---

## 环境变量参考

### VPS 中继调度器

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `POSTGRES_DSN` | ✅ | - | PostgreSQL 连接串 |
| `PIPELINE_WORKER_URLS` | ✅ | - | HF 流水线 Worker 地址（逗号分隔） |
| `TEST_WORKER_URLS` | - | - | HF 测试 Worker 地址（逗号分隔） |
| `TG_CHAT_ID` | - | - | 整书完成通知的 TG Chat ID |
| `TG_BOT_TOKENS` | - | - | TG Bot Token（逗号分隔，用于通知） |
| `TEST_MODELSCOPE_TOKEN` | - | - | 测试用 ModelScope Token |
| `WEB_PORT` | - | 38080 | 管理面板端口 |
| `WEB_PASSWORD` | - | - | 管理面板密码（空=无密码） |
| `CHECK_INTERVAL` | - | 15 | 调度器检查间隔（秒） |
| `STUCK_TIMEOUT_M` | - | 1440 | Worker 超时阈值（分钟） |
| `AUTO_START_SCHEDULER` | - | 1 | 启动时自动开启调度器 |

### HF 流水线 Worker

| 变量 | 必填 | 说明 |
|------|------|------|
| `POSTGRES_DSN` | ✅ | PostgreSQL 连接串（HF Space Secrets） |
| `VPS_RELAY_URL` | ✅ | VPS 中继地址（如 `http://VPS_IP:38080`） |
| `PORT` | - | 监听端口（HF 默认 7860） |
| `OUTPUT_ROOT` | - | 输出目录（默认 `/tmp/output`） |
| `MUSIC_DIR` | - | BGM 音乐目录（默认 `/data/music`） |

### HF 测试 Worker

| 变量 | 必填 | 说明 |
|------|------|------|
| `POSTGRES_DSN` | ✅ | PostgreSQL 连接串 |
| `VPS_RELAY_URL` | ✅ | VPS 中继地址 |
| `PORT` | - | 监听端口（默认 7860） |
| `OUTPUT_ROOT` | - | 输出目录（默认 `/tmp/output`） |
| `MUSIC_DIR` | - | BGM 音乐目录（默认 `/data/music`） |

---

## API 端点参考

### 后端转发层（本机 Backend）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/tasks-hf/seed` | POST | 投递 TG 缓存完整书到 HF 队列 |
| `/api/tasks-hf/seed-direct` | POST | 直接写入 hf_jobs（不经 VPS 中继） |
| `/api/tasks-hf/status` | GET | 查询 HF 任务全局状态 |
| `/api/tasks-hf/jobs` | GET | 分页查询 HF 任务列表 |
| `/api/tasks-hf/jobs/{id}` | GET | 查询单个 HF 任务详情 |
| `/api/tasks-hf/scheduler/start` | POST | 启动 VPS 调度器 |
| `/api/tasks-hf/scheduler/stop` | POST | 停止 VPS 调度器 |
| `/api/tasks-hf/reset-stuck` | POST | 重置卡住的任务 |
| `/api/tasks-hf/relay-status` | GET | 查询 VPS 中继状态 |
| `/api/tests-hf/ai` | POST | 转发 AI 测试到 HF Worker |
| `/api/tests-hf/upload` | POST | 转发上传测试到 HF Worker |
| `/api/tests-hf/tg-download` | POST | 转发 TG 下载测试到 HF Worker |
| `/api/tests-hf/bgm/download` | POST | 转发 BGM 音频下载到 HF Worker |
| `/api/tests-hf/bgm/mix` | POST | 转发 BGM 混音到 HF Worker |
| `/api/tests-hf/workers` | GET | 查询所有测试 Worker 健康状态 |

### VPS 中继调度器

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 全局状态（调度器、Worker、队列） |
| `/api/seed-jobs` | POST | 筛选 TG 缓存完整书并入队 |
| `/api/scheduler/start` | POST | 启动调度器 |
| `/api/scheduler/stop` | POST | 停止调度器 |
| `/api/reset-stuck` | POST | 重置卡住的任务 |
| `/api/pipeline-config` | GET | 分发 pipeline 配置给 Worker |
| `/api/test-config` | GET | 分发测试配置给 Worker |
| `/api/callback` | POST | 接收 Worker 完成回调 |
| `/tg-api/<path>` | ANY | TG API 中继代理 |
| `/yt-api/<channel>/<action>` | POST | YouTube API 中继（upload/info/playlist-sync） |

### HF Worker

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/status` | GET | 详细状态 |
| `/process` | POST | 触发认领并处理任务（VPS 调度器调用） |
| `/run-sync` | POST | 同步执行测试（直接传参） |

---

## 故障排查

### HF Worker 冷启动超时

HF 免费 Space 会在闲置后休眠，首次请求可能需要 30-60 秒唤醒。

**解决方案**：
- 前端已显示「冷启动可能需 30-60 秒」提示
- 可通过 VPS 中继管理面板手动触发 Worker 唤醒
- 升级 HF Pro 账号可减少休眠频率

### TG API 限流

HF Worker 通过 VPS 中继访问 TG API，如果多个 Worker 并发可能触发限流。

**解决方案**：
- 配置多个 TG Bot Token（逗号分隔），系统自动轮换
- 设置 `TG_SERIAL_DOWNLOAD=true` 串行下载
- 设置 `TG_DOWNLOAD_INTERVAL_SECONDS=5` 降低请求频率

### Worker 卡住（processing 状态不更新）

如果 Worker 崩溃或网络中断，任务可能卡在 `processing` 状态。

**解决方案**：
1. 在「HF外包任务」页面点击「重置卡住的任务」
2. 或调用 API：`POST /api/tasks-hf/reset-stuck`
3. VPS 中继会自动将超时任务（默认 24 小时）重置为 `pending`

### BGM 音乐池为空

HF Worker 的 BGM 音乐目录默认为空，BGM 混音测试会失败。

**解决方案**：
1. 在 HF Space 的 `/data/music` 目录上传 BGM 音乐文件
2. 或通过 VPS 中继配置同步（如已实现）
3. TG 缓存模式的 pipeline 不依赖 BGM 音乐池（使用已混音的 TG 缓存）

### Docker 构建失败（DeepFilter 下载）

流水线 Worker 的 Dockerfile 会下载 DeepFilter 二进制，网络不稳定可能失败。

**解决方案**：
- Dockerfile 已加 `|| echo` 容错，TG 缓存模式不受影响
- 如需 DeepFilter，可手动下载后放入 HF Space

---

## 扩展：创建多个 Worker 实现并行

HF 免费 Space 每个实例资源有限，可通过创建多个 Space 实现并行处理：

1. 复制现有 Space（Settings → Duplicate this Space）
2. 命名 `audiobook-pipeline-worker-2`、`audiobook-pipeline-worker-3` 等
3. 在 VPS 中继配置中添加所有 Worker 地址（逗号分隔）
4. VPS 调度器自动分配任务给空闲 Worker

```
PIPELINE_WORKER_URLS=https://user-audiobook-pipeline-worker-1.hf.space,https://user-audiobook-pipeline-worker-2.hf.space
```
