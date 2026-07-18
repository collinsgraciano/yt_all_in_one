# 有声书项目数据库指南 (AI 参考) — 重构版

> **重要**: 本文档是 AI 理解项目数据库结构的唯一权威参考。
> 最后更新: 2026-07 (重构版)

---

## 1. 数据库概览

```
PostgreSQL 16
├── books              — 书籍完整数据 (与远程数据库结构一致 + book_status)
└── audiobook_chapters — 解析后的章节, 每行一个章节
```

**连接信息:**
- Docker 内部: `postgresql://audiobook_app:inriynisse1991@db:5432/audiobook`
- VPS 本地: `postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook`
- 远程源库: `postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook`

---

## 2. books 表 (核心表)

### 设计理念
`books` 表与远程数据库 (85.121.48.55) 的 `books` 表**完全一致**, 只增加了一个 `book_status` 列用于本项目处理状态追踪。

**不再有**:
- ~~`_top_level` 合并 hack~~
- ~~dual category 问题~~ (category 现在是真实顶层列)
- ~~从 `book_data` JSON 中提取 `bookName` 的查询~~ (直接用 `book_name` 列)

### 列结构

| 列名 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `book_id` | text | — | **主键**, 书籍 ID |
| `book_name` | text | NULL | 书名 (**顶层列**, 不需要从 JSON 提取) |
| `author` | text | NULL | 作者 (**顶层列**) |
| `category` | text | NULL | 分类 (**顶层列**, 无 dual category 问题) |
| `total_chapters` | integer | NULL | 总章节数 |
| `book_data` | jsonb | NULL | 完整原始 JSON 数据 |
| `tags` | text[] | NULL | 标签数组 |
| `note` | text | NULL | 备注 |
| `status` | text | `''` | 远程系统原始状态 (爬虫状态, 非本项目状态) |
| `created_at` | timestamptz | now() | 远程系统创建时间 |
| `updated_at` | timestamptz | now() | 远程系统更新时间 |
| `book_status` | varchar(50) | `'pending'` | **项目扩展列**: `pending` / `success` |

### book_data JSONB 结构

```json
{
  "bookId": 30025231,
  "bookName": "额尔古纳河右岸（解读）",
  "bookAuthor": "迟子建",
  "bookPlayer": "悠悠",
  "tingAuthor": "迟子建",
  "tingPlayer": "悠悠",
  "bookType": 2,
  "chapterCount": 1,
  "chapters_data": [...],   // 章节列表, 迁移时解析
  "price": "4.00币",
  "basePrice": "4.00币",
  "feeUnit": 10,
  "completeState": "Y",
  "keyWord": "长篇小说,中国小说,苦难,乡土,茅盾文学奖",
  "picUrl": "https://tingbk.img.zhangyue01.com/...",
  "bookDescription": "..."
}
```

### 索引

| 索引名 | 类型 | 列 |
|--------|------|-----|
| `books_pkey` | btree (unique) | `book_id` |
| `idx_books_category` | btree | `category` |
| `idx_books_status` | btree | `status` |
| `idx_books_book_status` | btree | `book_status` |
| `idx_books_tags_gin` | gin | `tags` |
| `idx_books_updated_at` | btree (DESC) | `updated_at` |

### 重要区分: `status` vs `book_status`

- **`status`** — 远程爬虫系统的状态 (如爬取完成/进行中), 本项目**不修改**此列
- **`book_status`** — 本项目的处理状态, Worker 上传完所有章节后设为 `success`

---

## 3. audiobook_chapters 表

### 列结构

| 列名 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `book_id` | varchar(255) | — | 书籍 ID (联合主键) |
| `chapter_id` | varchar(255) | — | 章节 ID (联合主键) |
| `book_name` | text | NULL | 书名 (迁移时从 books 表复制) |
| `chapter_name` | text | NULL | 章节名 |
| `audio_url` | text | NULL | 原始音频下载 URL |
| `telegram_file_id` | text | NULL | Telegram 文件 ID |
| `telegram_message_id` | bigint | NULL | Telegram 消息 ID |
| `telegram_bot_id` | int | NULL | 上传 Bot 数组索引 (**可能失效**) |
| `telegram_bot_user_id` | bigint | NULL | 上传 Bot 永久 ID (**推荐使用**) |
| `upload_status` | varchar(50) | `'pending'` | `pending` / `processing` / `uploaded` / `failed` |
| `worker_id` | varchar(100) | NULL | 处理此章节的 Worker ID |
| `claimed_at` | timestamp | NULL | 认领时间 |
| `uploaded_at` | timestamp | NULL | 上传完成时间 |
| `error_message` | text | NULL | 错误信息 |

### 索引

| 索引名 | 列 |
|--------|-----|
| `audiobook_chapters_pkey` | `(book_id, chapter_id)` |
| `idx_chapters_upload_status` | `upload_status` |
| `idx_chapters_book_id` | `book_id` |
| `idx_chapters_book_status` | `(book_id, upload_status)` |

### upload_status 状态流转

```
pending → processing → uploaded
                    └→ failed → (reset) → pending
```

### Bot ID 追踪

- **`telegram_bot_id`** (int): Bot Token 在 `BOT_TOKENS` 数组中的索引
  - **问题**: 如果 Token 顺序变化或增删, 索引会失效
- **`telegram_bot_user_id`** (bigint): Bot 的永久 Telegram User ID (从 Token 中提取)
  - **推荐**: 不受顺序/增删影响, 下载时通过 user_id 反查 Token

---

## 4. 迁移流程 (migrate.py)

### 架构

```
远程 PG (85.121.48.55)
  └── books 表 (13,858 条, 含 book_data JSONB)
        │
        ▼  migrate.py (PG→PG)
本地 PG
  ├── books 表 (保留所有列 + book_status='pending')
  └── audiobook_chapters 表 (从 book_data->'chapters_data' 解析)
```

### 关键逻辑

1. **books 迁移**: `SELECT * FROM books` → `INSERT INTO books` (保留所有 11 列)
2. **章节解析**: 从 `book_data->'chapters_data'` 提取章节, 插入 `audiobook_chapters`
3. **跳过无章节**: `chapters_data` 为空的书籍不入章节表
4. **`book_name` 来源**: 直接从 books 表的顶层 `book_name` 列复制 (不再从 JSON 提取)

### 命令

```bash
# 基本迁移
python3 migrate.py

# 指定 DSN
python3 migrate.py \
    --remote-dsn "postgresql://..." \
    --local-dsn  "postgresql://..."

# 覆盖已有数据
python3 migrate.py --force

# 仅迁移 books, 不解析章节
python3 migrate.py --skip-chapters

# 仅解析章节 (books 已迁移)
python3 migrate.py --chapters-only
```

---

## 5. 查询模式参考

### 5.1 书籍列表 (使用顶层列)

```sql
-- ✅ 正确: 使用顶层列
SELECT book_id, book_name, author, category, book_status
FROM books
WHERE book_name ILIKE '%keyword%'
ORDER BY book_id;

-- ❌ 错误: 不要从 JSON 提取 (已废弃)
-- SELECT book_data->>'bookName' FROM books
```

### 5.2 章节认领 (Worker 核心)

```sql
-- 原子认领一个 pending 章节
UPDATE audiobook_chapters
SET upload_status = 'processing',
    worker_id = %s,
    claimed_at = now()
WHERE book_id = %s AND chapter_id = %s AND upload_status = 'pending'
RETURNING book_id, chapter_id, book_name, chapter_name, audio_url;
```

### 5.3 书籍完成检查

```sql
-- 检查一本书是否所有章节都上传完成
SELECT
    CASE WHEN COUNT(*) = 0 OR COUNT(*) FILTER (WHERE upload_status = 'uploaded') = COUNT(*)
    THEN 'success' ELSE 'pending' END as book_status
FROM audiobook_chapters
WHERE book_id = %s;

-- 更新书籍状态
UPDATE books SET book_status = 'success' WHERE book_id = %s;
```

### 5.4 分类筛选

```sql
-- 分类分布
SELECT category, COUNT(*) FROM books
WHERE category IS NOT NULL AND category != ''
GROUP BY category ORDER BY COUNT(*) DESC;
```

### 5.5 Bot ID 反查 (下载时)

```python
# 从 telegram_bot_user_id 反查正确的 Bot Token
def build_user_id_to_token_map(bot_tokens):
    return {int(t.split(':')[0]): i for i, t in enumerate(bot_tokens)}

uid_map = build_user_id_to_token_map(BOT_TOKENS)
bot_index = uid_map.get(chapter['telegram_bot_user_id'])
token = BOT_TOKENS[bot_index]
```

---

## 6. 文件结构

```
audiobook_pipeline/
├── init.sql                    # PG 初始化 (books + audiobook_chapters)
├── migrate.py                  # PG→PG 迁移 (远程 books → 本地 + 解析章节)
├── cleanup.py                  # 定时清理卡住的 processing 章节
├── reset_data.sql              # 重置所有章节和书籍状态
├── requirements.txt            # Python 依赖 (psycopg2, requests, tqdm)
├── Dockerfile                  # 工具容器 (ffmpeg + DeepFilter)
├── docker-compose.yml          # PG + migrate + cleanup
├── worker.py                   # 核心处理 Worker (下载→降噪→上传)
├── run_worker.py               # VPS 直接运行 Worker
├── test_download.py            # 测试 Telegram 下载
├── hf_space/                   # HF Space 部署
│   ├── worker.py               # 同根目录 worker.py
│   ├── app.py                  # Flask Web 服务
│   └── Dockerfile
├── colab/                      # Google Colab 部署
│   └── audiobook_worker_multi_bot.ipynb
├── vps_scheduler/              # VPS 调度器
│   ├── scheduler.py            # 调度逻辑
│   ├── web_app.py              # 调度器 Web 管理面板
│   └── docker-compose.yml
└── web_admin/                  # 数据库管理面板
    ├── app.py                  # Flask Web 应用
    ├── templates/              # HTML 模板
    └── Dockerfile
```

---

## 7. 重构变更说明 (2026-07)

### 变更前
- `books` 表只有 3 列: `book_id`, `book_data`, `book_status`
- 迁移需要 DuckDB → PG, 复杂的 `_top_level` 合并逻辑
- 查询书名需要 `book_data->>'bookName'`
- 多个迁移脚本: `migrate.py`, `re_migrate.py`, `migrate_old_pg/`, `migrate_duckdb_to_pg.ipynb`

### 变更后
- `books` 表与远程 DB 一致 (11 列) + `book_status` (1 列) = 12 列
- 迁移改为 PG→PG 直接迁移, 简单清晰
- 查询书名直接用 `book_name` 顶层列
- 迁移脚本统一为 `migrate.py` (支持 `--force`, `--skip-chapters`, `--chapters-only`)
- 删除: `re_migrate.py`, `migrate_old_pg/`, `migrate_duckdb_to_pg.ipynb`
- 删除: DuckDB 依赖 (requirements.txt, Dockerfile)
