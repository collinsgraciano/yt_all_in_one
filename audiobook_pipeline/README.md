# 有声书处理管线 (audiobook_pipeline) — 重构版

掌阅有声书：远程 PG 迁移 → DeepFilter 降噪 → 多 Bot 轮换上传 Telegram 的完整管线。

## 重构亮点

- **books 表与远程数据库完全一致**: 保留 `book_name`, `author`, `category`, `tags` 等顶层列
- **PG→PG 直接迁移**: 不再需要 DuckDB, 简单高效
- **无 dual category 问题**: `category` 是真实顶层列, 查询直接用 `WHERE category = 'xxx'`
- **统一迁移脚本**: 一个 `migrate.py` 搞定, 支持 `--force` / `--skip-chapters` / `--chapters-only`

## 目录结构

```
audiobook_pipeline/
├── README.md                  # 本文档
├── DATABASE_GUIDE_FOR_AI.md   # 数据库指南 (AI 代码生成专用)
├── init.sql                   # PostgreSQL 初始化 (books + audiobook_chapters)
├── migrate.py                 # PG→PG 迁移 (远程 books → 本地 + 解析章节)
├── reset_data.sql             # 重置所有上传状态
├── cleanup.py                 # 定时清理卡住/失败的章节
├── worker.py                  # Worker 核心模块 (多Bot轮换)
├── run_worker.py              # VPS 直接运行 Worker (支持多线程)
├── test_download.py           # Telegram 下载测试脚本
├── docker-compose.yml         # 一体化部署 (DB + 迁移 + 清理)
├── Dockerfile                 # 工具镜像 (ffmpeg + DeepFilter)
├── requirements.txt           # Python 依赖 (psycopg2, requests, tqdm)
│
├── hf_space/                  # Hugging Face Space Worker
│   ├── app.py                 # Flask 服务器 (状态面板 + API)
│   ├── worker.py              # HF Space 专用 Worker 核心
│   ├── Dockerfile
│   └── requirements.txt
│
├── colab/                     # Google Colab Worker
│   └── audiobook_worker_multi_bot.ipynb
│
├── vps_scheduler/             # VPS 调度器 (Serverless 模式)
│   ├── scheduler.py           # 后台调度线程 (轮询 PG + 触发 Worker)
│   ├── web_app.py             # Web 管理面板 + Telegram API 中继
│   ├── docker-compose.yml
│   ├── Dockerfile
│   └── requirements.txt
│
└── web_admin/                 # 独立 Web 管理面板
    ├── app.py                 # Flask CRUD 界面 (支持分类筛选)
    ├── templates/             # HTML 模板
    ├── Dockerfile
    └── requirements.txt
```

## 系统架构

```
                    ┌─────────────┐
                    │  远程 PG    │  (85.121.48.55)
                    │ books 表    │  (13,858 条, 11 列)
                    └──────┬──────┘
                           │ migrate.py (PG→PG)
                           ▼
                    ┌─────────────┐
                    │ PostgreSQL  │
                    │  books      │  (11 列 + book_status)
                    │ chapters    │
                    └──────┬──────┘
                           │
           ┌───────────────┼───────────────┐
           │               │               │
     ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
     │ HF Space  │  │  Colab    │  │ VPS 直跑  │
     │  Worker   │  │  Worker   │  │ run_worker│
     └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
           │               │               │
           │    下载音频 → DeepFilter 降噪 → 多Bot轮换上传
           │               │               │
           └───────────────┼───────────────┘
                           │
                    ┌──────▼──────┐
                    │  Telegram   │
                    │ (多Bot存储) │
                    └─────────────┘
```

## 彻底删除旧版本并重新部署 (升级指南)

> ⚠️ 本节针对**从旧版本升级**的用户。全新部署请直接跳到 [快速开始](#快速开始)。

### 第 1 步: 停止所有服务

```bash
# 进入项目目录
cd /path/to/audiobook_pipeline

# 停止主项目所有服务 (数据库 + 迁移 + 清理)
docker compose down

# 停止 VPS 调度器 (如果部署了)
cd vps_scheduler
docker compose down
cd ..

# 停止 Web 管理面板 (如果部署了)
cd web_admin
docker compose down
cd ..

# 杀掉手动运行的 Worker 进程 (如果有)
pkill -f run_worker.py
pkill -f "python.*worker.py"
```

### 第 2 步: 备份旧数据 (可选但强烈建议)

```bash
# 备份整个数据库 (含已上传章节的 telegram_file_id)
docker exec audiobook_pg pg_dump -U audiobook_app audiobook > backup_$(date +%Y%m%d).sql

# 备份已上传章节的 Telegram 信息 (仅关键数据, 快速恢复用)
docker exec audiobook_pg psql -U audiobook_app -d audiobook -c \
    "COPY (SELECT book_id, chapter_id, telegram_file_id, telegram_message_id, telegram_bot_id, telegram_bot_user_id, upload_status FROM audiobook_chapters WHERE upload_status = 'uploaded') TO '/tmp/uploaded_backup.csv' WITH CSV HEADER"

# 从容器中取出备份文件
docker cp audiobook_pg:/tmp/uploaded_backup.csv ./uploaded_backup.csv

# 备份文件保存好
ls -lh backup_*.sql uploaded_backup.csv
```

### 第 3 步: 删除旧容器和数据卷 (彻底清除)

```bash
# 删除容器 (主项目)
docker compose down -v --rmi local

# 删除容器 (VPS 调度器)
cd vps_scheduler && docker compose down -v --rmi local && cd ..

# 删除容器 (Web 管理面板)
cd web_admin && docker compose down -v --rmi local && cd ..

# 确认删除数据卷 (这会删除所有数据库数据!)
docker volume ls | grep audiobook
docker volume rm audiobook_pg_data   # ⚠️ 不可恢复!

# 清理悬空镜像和构建缓存
docker image prune -f
docker builder prune -f

# 确认没有残留
docker ps -a | grep audiobook    # 应该无输出
docker volume ls | grep audiobook  # 应该无输出
```

### 第 4 步: 替换代码

```bash
# 方式 A: 如果用 git
cd /path/to/audiobook_pipeline
git pull origin main

# 方式 B: 手动替换 (删除旧代码目录, 上传新代码)
cd /path/to
mv audiobook_pipeline audiobook_pipeline_old_backup
# 上传新的 audiobook_pipeline 文件夹到 /path/to/
cd audiobook_pipeline

# 确认新代码的 migrate.py 是 PG→PG 版本 (不再是 DuckDB)
head -5 migrate.py
# 应看到: "有声书数据迁移脚本: 远程 PostgreSQL → 本地 PostgreSQL (重构版)"

# 确认旧的迁移文件已删除
ls re_migrate.py migrate_old_pg/ migrate_duckdb_to_pg.ipynb 2>/dev/null
# 应该全部报错 "No such file or directory"
```

### 第 5 步: 重新启动数据库

```bash
cd /path/to/audiobook_pipeline

# 启动数据库 (会自动执行 init.sql 创建新表结构)
docker compose up -d db

# 等待数据库就绪
docker compose exec db pg_isready -U audiobook_app -d audiobook
# 应输出: /var/run/postgresql:5432 - accepting connections

# 验证新表结构
docker compose exec db psql -U audiobook_app -d audiobook -c "\d books"
# 应看到 12 列: book_id, book_name, author, category, total_chapters,
#              book_data, tags, note, status, created_at, updated_at, book_status
```

### 第 6 步: 重新迁移数据

有两种方式, 选一种即可。

#### 方式 A: Docker 命令行迁移 (推荐)

```bash
# 构建迁移工具镜像
docker compose build migrate

# 运行迁移 (从远程 PG 迁移 books + 解析章节, 约 5-10 分钟)
docker compose run --rm migrate

# 迁移完成后查看报告
docker compose run --rm migrate python3 migrate.py --chapters-only  # 不会重复, DO NOTHING
```

#### 方式 B: Google Colab 迁移

如果 VPS 网络不稳定或无法连接远程数据库 (85.121.48.55), 可以用 Colab 迁移:

1. 上传 `colab/migrate_pg_to_pg.ipynb` 到 Google Drive
2. 用 Google Colab 打开
3. 运行 **"1. 安装依赖"** 单元格
4. 在 **"2. 配置连接参数"** 单元格里修改 DSN, 然后运行它:
   - `REMOTE_DSN` 保持默认 (远程数据库, Colab 可直接访问)
   - `LOCAL_DSN` 改成你 VPS 的公网地址, 例如:
     ```python
     LOCAL_DSN = 'postgresql://audiobook_app:inriynisse1991@你的VPS公网IP:5432/audiobook'
     ```
   - ⚠️ 确保 VPS 的 5432 端口对 Colab 开放 (或用 SSH 隧道转发)
5. 依次运行单元格 3→4→5→6 (工具函数 → 迁移 books → 解析章节 → 查看报告)
   - 或跳过中间步骤, 直接运行 **"7. 一键执行全部迁移"** 单元格
6. 迁移完成后, 运行 **"9. 测试连接 (可选)"** 确认数据正常

> 💡 Colab 的优势: Google 网络稳定, 连接远程数据库成功率高; 缺点是需要手动改 `LOCAL_DSN` 为 VPS 公网地址。

### 第 7 步: 恢复已上传章节的 Telegram 信息 (如果做了备份)

```bash
# 如果之前备份了 uploaded_backup.csv, 恢复 telegram_file_id 等
# (books 和 chapters 数据已重新迁移, 但上传状态丢失, 需要恢复)

docker exec -i audiobook_pg psql -U audiobook_app -d audiobook <<'EOF'
-- 创建临时表导入 CSV
CREATE TEMP TABLE uploaded_backup (
    book_id text, chapter_id text, telegram_file_id text,
    telegram_message_id bigint, telegram_bot_id int,
    telegram_bot_user_id bigint, upload_status text
);

-- 导入备份 (将 CSV 内容粘贴在下方, 或用 \copy)
-- \copy uploaded_backup FROM '/tmp/uploaded_backup.csv' WITH CSV HEADER;

-- 更新已上传章节的状态
UPDATE audiobook_chapters ch
SET
    upload_status = 'uploaded',
    telegram_file_id = b.telegram_file_id,
    telegram_message_id = b.telegram_message_id,
    telegram_bot_id = b.telegram_bot_id,
    telegram_bot_user_id = b.telegram_bot_user_id,
    uploaded_at = now()
FROM uploaded_backup b
WHERE ch.book_id = b.book_id AND ch.chapter_id = b.chapter_id;

-- 更新书籍状态 (所有章节都已上传的书标记为 success)
UPDATE books SET book_status = 'success'
WHERE book_id IN (
    SELECT book_id FROM audiobook_chapters
    GROUP BY book_id
    HAVING COUNT(*) = COUNT(*) FILTER (WHERE upload_status = 'uploaded')
);

-- 查看恢复结果
SELECT
    COUNT(*) FILTER (WHERE upload_status = 'uploaded') as uploaded,
    COUNT(*) FILTER (WHERE upload_status = 'pending') as pending
FROM audiobook_chapters;
EOF
```

> 💡 如果你用 `psql` 的 `\copy` 导入 CSV, 需要把文件放到容器内或用 `docker cp` 传入:
> ```bash
> docker cp uploaded_backup.csv audiobook_pg:/tmp/
> docker exec audiobook_pg psql -U audiobook_app -d audiobook -c \
>     "\copy uploaded_backup FROM '/tmp/uploaded_backup.csv' WITH CSV HEADER"
> ```

### 第 8 步: 启动服务

```bash
cd /path/to/audiobook_pipeline

# 启动定时清理
docker compose up -d cleanup

# 启动 VPS 调度器 (如果用 HF Space 模式)
cd vps_scheduler
# 修改 docker-compose.yml 中的 HF_SPACE_URLS
docker compose up -d --build
cd ..

# 启动 Web 管理面板 (可选)
cd web_admin
docker compose up -d --build
cd ..

# 启动 Worker (选择一种)
# A. VPS 直接运行
python3 run_worker.py --workers 2
# B. HF Space (已在调度器中配置)
# C. Colab (打开 notebook 运行)
```

### 第 9 步: 验证一切正常

```bash
# 检查所有容器状态
docker compose ps
cd vps_scheduler && docker compose ps && cd ..
cd web_admin && docker compose ps && cd ..

# 检查数据库状态
docker exec audiobook_pg psql -U audiobook_app -d audiobook -c "
    SELECT 'books' as table_name, COUNT(*) FROM books
    UNION ALL
    SELECT 'audiobook_chapters', COUNT(*) FROM audiobook_chapters
    UNION ALL
    SELECT 'uploaded', COUNT(*) FROM audiobook_chapters WHERE upload_status = 'uploaded'
    UNION ALL
    SELECT 'pending', COUNT(*) FROM audiobook_chapters WHERE upload_status = 'pending';
"

# 检查分类是否正确 (新功能)
docker exec audiobook_pg psql -U audiobook_app -d audiobook -c "
    SELECT category, COUNT(*) FROM books
    WHERE category IS NOT NULL AND category != ''
    GROUP BY category ORDER BY COUNT(*) DESC LIMIT 10;
"

# 测试 Telegram 下载 (如果恢复了上传数据)
python3 test_download.py \
    --dsn "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook" \
    --tokens "token1,token2" \
    --sample 5 --check-only
```

### 升级总结

| 步骤 | 操作 | 耗时 | 数据影响 |
|------|------|------|----------|
| 1. 停止服务 | `docker compose down` | 30s | 无 |
| 2. 备份数据 | `pg_dump` + CSV 导出 | 1-5min | 无 |
| 3. 删除容器 | `docker compose down -v` | 1min | **删除数据库** |
| 4. 替换代码 | `git pull` 或手动 | 1min | 无 |
| 5. 启动数据库 | `docker compose up -d db` | 30s | 创建空表 |
| 6. 迁移数据 | `docker compose run --rm migrate` | 5-10min | 从远程拉取 |
| 7. 恢复上传状态 | 导入 CSV 更新 | 1min | 恢复 telegram_file_id |
| 8. 启动服务 | `docker compose up -d` | 1min | 无 |
| 9. 验证 | 检查容器+查询 | 1min | 无 |

---

## 快速开始

> 全新部署从这开始。从旧版本升级请看 [升级指南](#彻底删除旧版本并重新部署-升级指南)。

### 1. 启动数据库

```bash
docker compose up -d db
```

### 2. 迁移数据 (远程 PG → 本地 PG)

```bash
# 构建迁移工具镜像
docker compose build migrate

# 运行迁移 (从远程 PG 迁移 books 表 + 解析章节)
docker compose run --rm migrate

# 或直接运行 (需要本地 Python 环境)
python3 migrate.py \
    --remote-dsn "postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook" \
    --local-dsn  "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook"
```

### 3. 运行 Worker (选择一种方式)

#### 方式 A: VPS 直接运行

```bash
export POSTGRES_DSN="postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook"
export BOT_TOKENS="token1,token2,token3"
export CHAT_ID="7485554965"

python3 run_worker.py --workers 2
```

#### 方式 B: HF Space (Serverless)

1. 部署 `hf_space/` 到 Hugging Face Space
2. 配置环境变量: `POSTGRES_DSN`, `BOT_TOKENS`, `CHAT_ID`
3. VPS 上运行调度器: `python3 vps_scheduler/scheduler.py --hf-urls https://your-space.hf.space`

#### 方式 C: Google Colab

打开 `colab/audiobook_worker_multi_bot.ipynb`, 填入配置后运行。

### 4. 管理面板

```bash
# Web 管理面板 (搜索、查看、编辑、分类筛选)
cd web_admin
pip install -r requirements.txt
python3 app.py
# 打开 http://localhost:5000
```

### 5. 定时清理

```bash
# Docker 定时清理
docker compose up -d cleanup

# 或手动清理
python3 cleanup.py --timeout 24
python3 cleanup.py --reset-failed  # 同时重置 failed
```

## 数据库结构

### books 表 (与远程 DB 一致 + book_status)

| 列名 | 类型 | 说明 |
|------|------|------|
| `book_id` | text | 主键 |
| `book_name` | text | 书名 (顶层列) |
| `author` | text | 作者 (顶层列) |
| `category` | text | 分类 (顶层列) |
| `total_chapters` | integer | 总章节数 |
| `book_data` | jsonb | 完整原始 JSON |
| `tags` | text[] | 标签数组 |
| `note` | text | 备注 |
| `status` | text | 远程系统状态 |
| `created_at` | timestamptz | 创建时间 |
| `updated_at` | timestamptz | 更新时间 |
| `book_status` | varchar(50) | 项目状态: pending/success |

### audiobook_chapters 表

| 列名 | 类型 | 说明 |
|------|------|------|
| `book_id` | varchar(255) | 书籍 ID (联合主键) |
| `chapter_id` | varchar(255) | 章节 ID (联合主键) |
| `book_name` | text | 书名 |
| `chapter_name` | text | 章节名 |
| `audio_url` | text | 音频 URL |
| `telegram_file_id` | text | Telegram 文件 ID |
| `telegram_message_id` | bigint | Telegram 消息 ID |
| `telegram_bot_id` | int | Bot 数组索引 |
| `telegram_bot_user_id` | bigint | Bot 永久 ID (推荐) |
| `upload_status` | varchar(50) | pending/processing/uploaded/failed |
| `worker_id` | varchar(100) | Worker ID |
| `claimed_at` | timestamp | 认领时间 |
| `uploaded_at` | timestamp | 上传时间 |
| `error_message` | text | 错误信息 |

详细说明请参考 [DATABASE_GUIDE_FOR_AI.md](DATABASE_GUIDE_FOR_AI.md)。

## 多 Bot 轮换机制

- 多个 Bot Token 轮换上传, 分散限流压力
- 每个 Bot 独立追踪 429 状态, 被限流时自动切换
- **`telegram_bot_user_id`**: Bot 的永久 Telegram User ID, 不受 Token 顺序/增删影响
- **`telegram_bot_id`**: Bot 数组索引, Token 顺序变化可能失效 (向后兼容)

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `POSTGRES_DSN` | 本地 PostgreSQL 连接串 | `postgresql://...@127.0.0.1:5432/audiobook` |
| `REMOTE_POSTGRES_DSN` | 远程 PostgreSQL 连接串 (迁移用) | `postgresql://...@85.121.48.55:5432/audiobook` |
| `BOT_TOKENS` | 多 Bot Token (逗号分隔) | — |
| `CHAT_ID` | Telegram Chat ID | — |
| `TELEGRAM_API_BASE` | Telegram API 中继地址 | `https://api.telegram.org` |
| `BOT_MIN_INTERVAL` | 单 Bot 最小上传间隔 | `3` |
| `MAX_RETRIES` | 最大重试次数 | `5` |

## 重构变更 (2026-07)

| 变更 | 说明 |
|------|------|
| books 表结构 | 与远程 DB 完全一致 (11 列) + `book_status` 扩展列 |
| 迁移方式 | PG→PG 直接迁移, 不再需要 DuckDB |
| 迁移脚本 | 统一为 `migrate.py` (支持 `--force` / `--skip-chapters` / `--chapters-only`) |
| 查询方式 | 使用顶层列 `book_name` / `author` / `category`, 不再从 JSON 提取 |
| 删除文件 | `re_migrate.py`, `migrate_old_pg/`, `migrate_duckdb_to_pg.ipynb` |
| 删除依赖 | DuckDB CLI (Dockerfile), duckdb (requirements.txt) |
| Web 管理面板 | 新增分类筛选, 显示分类/标签 |
