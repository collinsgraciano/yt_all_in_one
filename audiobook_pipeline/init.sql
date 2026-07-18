-- ============================================================
-- 有声书项目 PostgreSQL 初始化脚本 (重构版)
--
-- 设计理念:
--   books 表与远程数据库 (85.121.48.55) 完全一致,
--   只增加一个 book_status 列用于项目处理状态追踪。
--   不再需要 _top_level 合并 hack, category/author/tags
--   都是真实的顶层列, 查询简单高效。
--
-- Docker 容器首次启动时自动执行。
-- ============================================================

-- ============================================================
-- 1. 创建应用用户 (如果不存在)
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'audiobook_app') THEN
        CREATE ROLE audiobook_app WITH LOGIN PASSWORD 'inriynisse1991';
        RAISE NOTICE '>>> 创建用户: audiobook_app';
    ELSE
        RAISE NOTICE '>>> 用户已存在: audiobook_app';
    END IF;
END
$$;

-- ============================================================
-- 2. 授予权限
-- ============================================================
GRANT ALL PRIVILEGES ON DATABASE audiobook TO audiobook_app;

-- 切换到 audiobook 数据库
\c audiobook

-- ============================================================
-- 3. 创建 books 表
--    与远程数据库 books 表结构完全一致 + book_status 扩展列
-- ============================================================
CREATE TABLE IF NOT EXISTS books (
    -- === 远程数据库原始列 (原封不动保留) ===
    book_id          text        PRIMARY KEY,
    book_name        text,
    author           text,
    category         text,
    total_chapters   integer,
    book_data        jsonb,
    tags             text[],
    note             text,
    status           text        DEFAULT '',
    created_at       timestamptz DEFAULT now(),
    updated_at       timestamptz DEFAULT now(),
    -- === 项目扩展列 ===
    book_status      varchar(50) DEFAULT 'pending'
);

-- 兼容已存在的表: 补充列
ALTER TABLE books ADD COLUMN IF NOT EXISTS book_name        text;
ALTER TABLE books ADD COLUMN IF NOT EXISTS author           text;
ALTER TABLE books ADD COLUMN IF NOT EXISTS category         text;
ALTER TABLE books ADD COLUMN IF NOT EXISTS total_chapters   integer;
ALTER TABLE books ADD COLUMN IF NOT EXISTS tags             text[];
ALTER TABLE books ADD COLUMN IF NOT EXISTS note             text;
ALTER TABLE books ADD COLUMN IF NOT EXISTS status           text DEFAULT '';
ALTER TABLE books ADD COLUMN IF NOT EXISTS created_at       timestamptz DEFAULT now();
ALTER TABLE books ADD COLUMN IF NOT EXISTS updated_at       timestamptz DEFAULT now();
ALTER TABLE books ADD COLUMN IF NOT EXISTS book_status      varchar(50) DEFAULT 'pending';

COMMENT ON TABLE books IS '有声书完整数据 (与远程数据库结构一致 + book_status 扩展列)';
COMMENT ON COLUMN books.book_id IS '书籍 ID (主键)';
COMMENT ON COLUMN books.book_name IS '书名 (顶层列, 不需要从 book_data JSON 中提取)';
COMMENT ON COLUMN books.author IS '作者 (顶层列)';
COMMENT ON COLUMN books.category IS '分类 (顶层列, 不再有 dual category 问题)';
COMMENT ON COLUMN books.total_chapters IS '总章节数';
COMMENT ON COLUMN books.book_data IS '完整原始 JSON 数据 (bookName, bookAuthor, chapters_data 等)';
COMMENT ON COLUMN books.tags IS '标签数组';
COMMENT ON COLUMN books.note IS '备注';
COMMENT ON COLUMN books.status IS '远程系统原始状态 (爬虫状态, 非本项目处理状态)';
COMMENT ON COLUMN books.created_at IS '远程系统创建时间';
COMMENT ON COLUMN books.updated_at IS '远程系统更新时间';
COMMENT ON COLUMN books.book_status IS '项目处理状态: pending(待处理) / success(所有章节上传完成)';

-- ============================================================
-- 4. 创建 audiobook_chapters 表 (解析后的章节)
-- ============================================================
CREATE TABLE IF NOT EXISTS audiobook_chapters (
    book_id              VARCHAR(255),
    chapter_id           VARCHAR(255),
    book_name            TEXT,
    chapter_name         TEXT,
    audio_url            TEXT,
    telegram_file_id     TEXT,
    telegram_message_id  BIGINT,
    telegram_bot_id      INT,
    telegram_bot_user_id BIGINT,
    upload_status        VARCHAR(50) DEFAULT 'pending',
    worker_id            VARCHAR(100),
    claimed_at           TIMESTAMP,
    uploaded_at          TIMESTAMP,
    error_message        TEXT,
    PRIMARY KEY (book_id, chapter_id)
);

-- 兼容已存在的表
ALTER TABLE audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_bot_id INT;
ALTER TABLE audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_bot_user_id BIGINT;

COMMENT ON TABLE audiobook_chapters IS '解析后的章节表，每行一个章节';
COMMENT ON COLUMN audiobook_chapters.book_id IS '书籍 ID';
COMMENT ON COLUMN audiobook_chapters.chapter_id IS '章节 ID';
COMMENT ON COLUMN audiobook_chapters.book_name IS '书名';
COMMENT ON COLUMN audiobook_chapters.chapter_name IS '章节名';
COMMENT ON COLUMN audiobook_chapters.audio_url IS '原始音频下载 URL';
COMMENT ON COLUMN audiobook_chapters.telegram_file_id IS 'Telegram Bot API 返回的文件 ID';
COMMENT ON COLUMN audiobook_chapters.telegram_message_id IS 'Telegram 消息 ID';
COMMENT ON COLUMN audiobook_chapters.telegram_bot_id IS '上传 Bot 编号 (BOT_TOKENS 数组索引, 顺序变化可能失效, 优先用 telegram_bot_user_id)';
COMMENT ON COLUMN audiobook_chapters.telegram_bot_user_id IS '上传 Bot 的永久 Telegram User ID (从 Token 提取, 不受顺序/增删影响)';
COMMENT ON COLUMN audiobook_chapters.upload_status IS '章节处理标记: pending / processing / uploaded / failed';
COMMENT ON COLUMN audiobook_chapters.worker_id IS '处理此章节的 Worker ID';
COMMENT ON COLUMN audiobook_chapters.claimed_at IS '认领时间';
COMMENT ON COLUMN audiobook_chapters.uploaded_at IS '上传完成时间';
COMMENT ON COLUMN audiobook_chapters.error_message IS '错误信息';

-- ============================================================
-- 5. 创建索引
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_books_category    ON books(category);
CREATE INDEX IF NOT EXISTS idx_books_status      ON books(status);
CREATE INDEX IF NOT EXISTS idx_books_book_status ON books(book_status);
CREATE INDEX IF NOT EXISTS idx_books_tags_gin    ON books USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_books_updated_at  ON books(updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_chapters_upload_status ON audiobook_chapters(upload_status);
CREATE INDEX IF NOT EXISTS idx_chapters_book_id       ON audiobook_chapters(book_id);
CREATE INDEX IF NOT EXISTS idx_chapters_book_status   ON audiobook_chapters(book_id, upload_status);

-- ============================================================
-- 6. 授予应用用户权限
-- ============================================================
GRANT CREATE ON SCHEMA public TO audiobook_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO audiobook_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO audiobook_app;

ALTER TABLE IF EXISTS books OWNER TO audiobook_app;
ALTER TABLE IF EXISTS audiobook_chapters OWNER TO audiobook_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO audiobook_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO audiobook_app;

-- ============================================================
-- 7. 初始化完成提示
-- ============================================================
DO $$
BEGIN
    RAISE NOTICE '============================================================';
    RAISE NOTICE '  PostgreSQL 初始化完成! (重构版)';
    RAISE NOTICE '  用户: audiobook_app';
    RAISE NOTICE '  数据库: audiobook';
    RAISE NOTICE '  表: books (远程DB一致+book_status), audiobook_chapters';
    RAISE NOTICE '  索引: 已创建';
    RAISE NOTICE '============================================================';
END
$$;
