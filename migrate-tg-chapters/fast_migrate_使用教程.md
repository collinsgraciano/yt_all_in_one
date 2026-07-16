# fast_migrate.sh 使用教程

> 快速数据迁移工具 — 纯 SQL 管道方式，比 Python 逐行处理快 **10-50 倍**

---

## 目录

- [1. 简介](#1-简介)
- [2. 前置条件](#2-前置条件)
- [3. 环境变量配置](#3-环境变量配置)
- [4. 参数说明](#4-参数说明)
- [5. 使用场景](#5-使用场景)
- [6. 迁移原理](#6-迁移原理)
- [7. 表结构映射](#7-表结构映射)
- [8. 常见问题](#8-常见问题)

---

## 1. 简介

`fast_migrate.sh` 用于将旧项目「下载掌阅有声书到tg」数据库中的 `books` 和 `audiobook_chapters` 两张表，快速迁移到新项目数据库。

**核心优势：**

| 对比项 | Python 脚本 (`run.sh`) | SQL 管道 (`fast_migrate.sh`) |
|--------|----------------------|----------------------------|
| 传输方式 | 逐行 `fetchone` → Python 解析 → 逐行 `INSERT` | `COPY TO STDOUT` 管道直传 → 临时表 → `INSERT...SELECT` |
| 52 万行 chapters | 10-30 分钟 | **几秒~几十秒** |
| JSON 解析 | Python 代码解析 | PostgreSQL 原生 `->>` / `jsonb_array_length()` |
| Docker 镜像 | 需构建自定义镜像 | 官方 `postgres:16-alpine`（首次自动拉取） |
| 冲突处理 | `ON CONFLICT DO NOTHING` | 相同（SQL 层面） |
| 后台运行 | 支持 | 支持 |

---

## 2. 前置条件

- **Docker** 已安装并运行（脚本通过 `docker run` 在容器内执行 `psql`）
- **源数据库**（旧项目）可从当前机器网络访问
- **目标数据库**（新项目）已在运行，且 `init-db.sql` 已执行（表结构已创建）
- 源库和目标库的 PostgreSQL 版本 ≥ 12（需要 JSONB 支持）

### 验证 Docker

```bash
docker --version
# Docker version 24.x+ 即可
```

### 验证目标库表结构

```bash
# 确认目标库有 books 和 audiobook_chapters 表
docker exec -i <目标PG容器名> psql -U audiobook_app -d audiobook -c "\dt"
```

---

## 3. 环境变量配置

脚本通过两个环境变量读取数据库连接串：

| 环境变量 | 用途 | 默认值 |
|---------|------|-------|
| `SOURCE_DATABASE_URL` | 旧项目数据库（源库） | `postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook` |
| `DATABASE_URL` | 新项目数据库（目标库） | `postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook` |

### 连接串格式

```
postgresql://用户名:密码@主机:端口/数据库名
```

### 配置方式

**方式一：临时设置（当前终端有效）**

```bash
export SOURCE_DATABASE_URL="postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook"
export DATABASE_URL="postgresql://audiobook_app:你的密码@127.0.0.1:5432/audiobook"

bash fast_migrate.sh --all
```

**方式二：写入 .env 文件**

```bash
cat > .env << 'EOF'
SOURCE_DATABASE_URL=postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook
DATABASE_URL=postgresql://audiobook_app:你的密码@127.0.0.1:5432/audiobook
EOF

# 加载后执行
source .env && bash fast_migrate.sh --all
```

### 关于 host.docker.internal

脚本会自动将连接串中的 `127.0.0.1` 和 `localhost` 替换为 `host.docker.internal`，因为容器内需要通过这个特殊域名访问宿主机上的服务。Linux 上由 `--add-host=host.docker.internal:host-gateway` 自动解析。

**如果你的目标库在其他 VPS 上**（不是本机），直接用外部 IP 即可，脚本不会替换外部 IP：

```bash
export DATABASE_URL="postgresql://audiobook_app:密码@另一台VPS的IP:5432/audiobook"
```

---

## 4. 参数说明

| 参数 | 说明 |
|------|------|
| `--all` | 同时迁移 `books` 和 `audiobook_chapters` 两张表（推荐） |
| `--books` | 仅迁移 `books` 表 |
| `--chapters` | 仅迁移 `audiobook_chapters` 表 |
| `--only-complete-books` | 仅迁移「整本所有章节都已上传到 Telegram」的书（配合 `--chapters` 使用） |
| `--dry-run` | 试运行模式：只统计源库和目标库的数据量，不写入任何数据 |
| `--bg` | 后台运行模式：断开 SSH 不中断，日志写入 `logs/` 目录 |

> **如果既不指定 `--all` 也不指定 `--books`/`--chapters`，默认只迁移 chapters。**

### 参数组合规则

- `--books` 和 `--chapters` 可以同时使用，效果等同 `--all`
- `--only-complete-books` 只影响 chapters 的迁移范围，对 books 无影响
- `--dry-run` 可以和任何参数组合
- `--bg` 可以和任何参数组合

---

## 5. 使用场景

### 场景一：首次完整迁移（推荐）

迁移全部 books + 全部 chapters：

```bash
bash fast_migrate.sh --all
```

### 场景二：仅迁移整本完整的书

迁移全部 books + 仅迁移整本已上传完的 chapters：

```bash
bash fast_migrate.sh --all --only-complete-books
```

> **什么算"整本完整"？**
> 源库中某个 `book_id` 的所有章节，`upload_status` 全部为 `uploaded` 且 `telegram_file_id` 不为 NULL。
> 这样迁移过来的章节都有有效的 Telegram 缓存，新项目可以直接从 TG 下载已降噪音频，跳过 DeepFilter 处理。

### 场景三：后台运行（断开 SSH 不中断）

```bash
bash fast_migrate.sh --bg --all --only-complete-books
```

输出示例：

```
[INFO] 后台运行模式
[INFO] 日志文件: /opt/audiobook/migrate-tg-chapters/logs/fast_migrate_20250716_033000.log
[INFO]
[OK]   后台进程已启动 (PID: 12345)
[INFO]
[INFO]   查看日志:   tail -f logs/fast_migrate_20250716_033000.log
[INFO]   停止迁移:   kill 12345
[INFO]
[INFO]   你可以断开 SSH 了
```

### 场景四：试运行（先看看数据量）

```bash
bash fast_migrate.sh --all --only-complete-books --dry-run
```

输出示例：

```
[INFO] [DRY-RUN] 只统计，不写入

  === 源库统计 ===
  books 总数:          11027
  chapters 总数:       520000
  完整书数量:          3500
  完整书 chapters 数:  180000

  === 目标库统计 ===
  books 现有:          0
  chapters 现有:       0

[OK]   [DRY-RUN] 统计完成
```

### 场景五：单独迁移某张表

```bash
# 只迁移 books
bash fast_migrate.sh --books

# 只迁移 chapters（全部）
bash fast_migrate.sh --chapters

# 只迁移 chapters（仅完整书）
bash fast_migrate.sh --chapters --only-complete-books
```

### 场景六：指定外部数据库

```bash
export SOURCE_DATABASE_URL="postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook"
export DATABASE_URL="postgresql://audiobook_app:新密码@另一台VPS:5432/audiobook"

bash fast_migrate.sh --all
```

---

## 6. 迁移原理

### 传统 Python 方式（慢）

```
源库 ──fetchone()──→ Python 进程 ──INSERT──→ 目标库
         逐行取出      逐行解析 JSON    逐行写入
```

每行数据需要：网络往返 → Python 对象创建 → JSON 解析 → SQL 拼接 → 网络往返。52 万行 = 52 万次往返。

### SQL 管道方式（快）

```
源库 ──COPY TO STDOUT──→ 管道 ──\copy FROM STDIN──→ 目标库临时表
                                                        ↓
                                              INSERT...SELECT ON CONFLICT
                                              （一条 SQL 批量写入）
```

整个流程只有 **2 次网络连接**，数据在 `psql` 进程间通过管道流式传输，不经过 Python 解释。

### books 表的 JSON 转换

旧库 `books` 表只有 3 列（`book_id`, `book_data` JSONB, `book_status`），新库需要 11 列。脚本用 PostgreSQL 原生 JSON 函数在 SQL 层面完成转换：

```sql
INSERT INTO public.books (book_id, book_name, author, total_chapters, book_data, status)
SELECT
    book_id,
    -- 从 JSON 提取书名（兼容多种字段名）
    COALESCE(
        NULLIF(book_data->>'bookName', ''),
        NULLIF(book_data->>'title', ''),
        NULLIF(book_data->>'name', ''),
        '未知_' || book_id
    ),
    -- 从 JSON 提取作者
    COALESCE(
        NULLIF(book_data->>'bookAuthor', ''),
        NULLIF(book_data->>'author', ''),
        NULLIF(book_data->>'writer', '')
    ),
    -- 从 JSON 数组计算章节数
    CASE
        WHEN book_data ? 'tingChapterList' THEN jsonb_array_length(book_data->'tingChapterList')
        WHEN book_data ? 'chapterList'     THEN jsonb_array_length(book_data->'chapterList')
        WHEN book_data ? 'chapters'        THEN jsonb_array_length(book_data->'chapters')
        WHEN book_data ? 'list'            THEN jsonb_array_length(book_data->'list')
        WHEN book_data ? 'tingChapters'    THEN jsonb_array_length(book_data->'tingChapters')
        WHEN book_data ? 'sectionList'     THEN jsonb_array_length(book_data->'sectionList')
        ELSE 0
    END,
    book_data,
    COALESCE(book_status, 'pending')
FROM _old_books
ON CONFLICT (book_id) DO NOTHING;
```

---

## 7. 表结构映射

### books 表

| 旧库列 | 类型 | 新库列 | 类型 | 说明 |
|-------|------|-------|------|------|
| `book_id` | VARCHAR(255) | `book_id` | text | 主键，直接映射 |
| `book_data` | JSONB | `book_data` | jsonb | 完整 JSON，直接映射 |
| `book_data->>'bookName'` | — | `book_name` | text | 从 JSON 提取 |
| `book_data->>'bookAuthor'` | — | `author` | text | 从 JSON 提取 |
| `book_data->'tingChapterList'` | — | `total_chapters` | integer | JSON 数组长度 |
| `book_status` | VARCHAR(50) | `status` | text | 直接映射，NULL → 'pending' |
| — | — | `category` | text | 留空 NULL |
| — | — | `tags` | text[] | 留空 NULL |
| — | — | `note` | text | 留空 NULL |
| — | — | `created_at` | timestamptz | 自动填充 now() |
| — | — | `updated_at` | timestamptz | 自动填充 now() |

### audiobook_chapters 表

| 旧库列 | 新库列 | 说明 |
|-------|-------|------|
| `book_id` | `book_id` | 直接映射 |
| `chapter_id` | `chapter_id` | 直接映射 |
| `book_name` | `book_name` | 直接映射 |
| `chapter_name` | `chapter_name` | 直接映射 |
| `audio_url` | `audio_url` | 直接映射 |
| `telegram_file_id` | `telegram_file_id` | 直接映射（TG 缓存关键字段） |
| `telegram_message_id` | `telegram_message_id` | 直接映射 |
| `upload_status` | `upload_status` | 直接映射 |
| `uploaded_at` | `uploaded_at` | 直接映射 |
| `worker_id` | — | 旧库独有，丢弃 |
| `claimed_at` | — | 旧库独有，丢弃 |
| `error_message` | — | 旧库独有，丢弃 |

---

## 8. 常见问题

### Q: 重复运行会怎样？

**不会重复写入。** 所有 `INSERT` 都带有 `ON CONFLICT DO NOTHING`，已存在的记录会被自动跳过。

### Q: 后台运行后怎么查看进度？

```bash
# 查看实时日志
tail -f logs/fast_migrate_*.log

# 查看最新日志的最后 20 行
tail -20 $(ls -t logs/fast_migrate_*.log | head -1)
```

### Q: 后台运行怎么停止？

```bash
# 找到进程
ps aux | grep fast_migrate

# 杀掉进程
kill <PID>
```

### Q: 报错 "Could not connect to server" / "Connection refused"

1. 检查源库 IP 和端口是否正确
2. 检查目标库是否在运行：`docker ps | grep postgres`
3. 如果目标库在 Docker 中，确认端口映射：`docker port <容器名>`
4. 如果目标库在另一台 VPS 上，确认防火墙放行了 5432 端口

### Q: 报错 "relation public.books does not exist"

目标库的表结构还没创建。执行 `init-db.sql`：

```bash
docker exec -i <目标PG容器名> psql -U audiobook_app -d audiobook < docker/init-db.sql
```

### Q: 首次运行很慢，卡在 "构建镜像"？

`fast_migrate.sh` 不需要构建镜像，使用官方 `postgres:16-alpine`。首次运行会自动拉取（约 80MB），之后会缓存在本地。

### Q: 可以在 Windows 上用吗？

`fast_migrate.sh` 是 Bash 脚本，适用于 Linux/macOS。Windows 上建议通过 WSL 或 SSH 到 VPS 上运行。

### Q: `--only-complete-books` 的筛选逻辑是什么？

```sql
SELECT book_id FROM audiobook_chapters
GROUP BY book_id
HAVING COUNT(*) = COUNT(
    CASE WHEN upload_status = 'uploaded' AND telegram_file_id IS NOT NULL THEN 1 END
)
```

即：某个 `book_id` 的**所有**章节都满足 `upload_status = 'uploaded'` 且 `telegram_file_id` 不为空。只有这些书对应的 chapters 会被迁移。

### Q: 迁移后怎么验证数据正确？

```bash
# 在目标库检查
docker exec -it <目标PG容器名> psql -U audiobook_app -d audiobook -c "
    SELECT 'books' AS table_name, count(*) FROM public.books
    UNION ALL
    SELECT 'audiobook_chapters', count(*) FROM public.audiobook_chapters;
"

# 抽查几条数据
docker exec -it <目标PG容器名> psql -U audiobook_app -d audiobook -c "
    SELECT book_id, book_name, author, total_chapters, status
    FROM public.books
    LIMIT 5;
"
```
