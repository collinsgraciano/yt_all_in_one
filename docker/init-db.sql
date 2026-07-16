-- docker/init-db.sql
-- 在 PostgreSQL 首次启动时自动执行

-- 创建扩展
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ═══════════════════════════════════════════════════════════
-- 复用参考代码的 6 张核心表
-- ═══════════════════════════════════════════════════════════

-- 1. books — 书籍库
CREATE TABLE IF NOT EXISTS public.books (
    book_id   text PRIMARY KEY,
    book_name text,
    author    text,
    category  text,
    total_chapters integer,
    book_data jsonb,
    tags      text[],
    note      text,
    status    text DEFAULT '',
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

-- 2. book_processing_states — 断点续跑状态
CREATE TABLE IF NOT EXISTS public.book_processing_states (
    book_id       text NOT NULL,
    project_flag  text NOT NULL,
    book_name     text,
    category      text,
    pending_resume boolean NOT NULL DEFAULT true,
    state_status  text NOT NULL DEFAULT 'in_progress',
    current_part_index integer,
    completed_part_count integer NOT NULL DEFAULT 0,
    part_count    integer NOT NULL DEFAULT 1,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    state_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT book_processing_states_pkey PRIMARY KEY (book_id, project_flag)
);

-- 3. youtube_credentials — YouTube OAuth 凭证
CREATE TABLE IF NOT EXISTS public.youtube_credentials (
    channel_name text PRIMARY KEY,
    token_json   jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- 4. modelscope_tokens — AI 生图 Token
CREATE TABLE IF NOT EXISTS public.modelscope_tokens (
    channel_name text PRIMARY KEY,
    token_text   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- 5. channel_runtime_settings — 频道级运行配置
CREATE TABLE IF NOT EXISTS public.channel_runtime_settings (
    channel_name  text NOT NULL,
    setting_key   text NOT NULL,
    setting_value text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT channel_runtime_settings_pkey PRIMARY KEY (channel_name, setting_key)
);

-- 6. task_queue — 任务队列
CREATE TABLE IF NOT EXISTS public.task_queue (
    book_id   text PRIMARY KEY,
    status    text NOT NULL DEFAULT 'pending',
    worker_id text,
    claimed_at timestamptz,
    finished_at timestamptz,
    retry_count integer NOT NULL DEFAULT 0,
    error_msg text,
    category  text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════
-- Web 管理层新增表
-- ═══════════════════════════════════════════════════════════

-- 7. channels — 频道注册表
CREATE TABLE IF NOT EXISTS public.channels (
    channel_id    text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    channel_name  text NOT NULL UNIQUE,
    display_name  text,
    description   text,
    is_active     boolean NOT NULL DEFAULT true,
    oauth_status  text NOT NULL DEFAULT 'pending',
    oauth_client_secret jsonb,
    last_auth_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 8. channel_configs — 频道完整配置快照
CREATE TABLE IF NOT EXISTS public.channel_configs (
    channel_name  text PRIMARY KEY REFERENCES public.channels(channel_name) ON DELETE CASCADE,
    config_json   jsonb NOT NULL DEFAULT '{}'::jsonb,
    config_version integer NOT NULL DEFAULT 1,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- 9. run_tasks — 运行任务记录
CREATE TABLE IF NOT EXISTS public.run_tasks (
    task_id       text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    channel_name  text NOT NULL,
    task_type     text NOT NULL DEFAULT 'full_pipeline',
    status        text NOT NULL DEFAULT 'queued',
    config_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at    timestamptz,
    finished_at   timestamptz,
    stop_requested boolean NOT NULL DEFAULT false,
    stop_reason   text,
    result_json   jsonb,
    error_msg     text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
-- 兼容已存在的表: 补充可能缺失的列
ALTER TABLE public.run_tasks ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_run_tasks_channel ON public.run_tasks(channel_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_tasks_status ON public.run_tasks(status);

-- 10. run_task_logs — 任务日志
CREATE TABLE IF NOT EXISTS public.run_task_logs (
    id          bigserial PRIMARY KEY,
    task_id     text NOT NULL REFERENCES public.run_tasks(task_id) ON DELETE CASCADE,
    log_level   text NOT NULL DEFAULT 'INFO',
    message     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_run_task_logs_task_id ON public.run_task_logs(task_id, created_at);

-- 11. global_settings — 全局共享设置
CREATE TABLE IF NOT EXISTS public.global_settings (
    setting_key   text PRIMARY KEY,
    setting_value text NOT NULL DEFAULT '',
    description   text,
    is_secret     boolean DEFAULT false,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
-- 兼容已存在的表: is_secret 改为可空（支持 UPSERT 时 COALESCE 保留已有值）
ALTER TABLE public.global_settings ALTER COLUMN is_secret DROP NOT NULL;

-- 12. oauth_states — OAuth 授权状态临时存储（替代 Redis）
CREATE TABLE IF NOT EXISTS public.oauth_states (
    state         text PRIMARY KEY,
    channel_name  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_oauth_states_created ON public.oauth_states(created_at);

-- ═══════════════════════════════════════════════════════════
-- TG 音频缓存表（从旧项目「下载掌阅有声书到tg」整合）
-- 存储已上传到 Telegram 的章节信息，pipeline 处理时可直接从 TG 下载已降噪音频
-- ═══════════════════════════════════════════════════════════

-- 13. audiobook_chapters — 章节级 TG 缓存
CREATE TABLE IF NOT EXISTS public.audiobook_chapters (
    book_id              text NOT NULL,
    chapter_id           text NOT NULL,
    book_name            text,
    chapter_name         text,
    audio_url            text,
    telegram_file_id     text,
    telegram_message_id  bigint,
    upload_status        text DEFAULT 'pending',
    uploaded_at          timestamptz,
    CONSTRAINT audiobook_chapters_pkey PRIMARY KEY (book_id, chapter_id)
);

-- 兼容已存在的表: 补充可能缺失的列
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_file_id text;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS telegram_message_id bigint;
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS upload_status text DEFAULT 'pending';
ALTER TABLE public.audiobook_chapters ADD COLUMN IF NOT EXISTS uploaded_at timestamptz;

-- 索引: 按书查询 + 按音频URL查询（pipeline 用 audio_url 匹配章节）
CREATE INDEX IF NOT EXISTS idx_audiobook_chapters_book_id ON public.audiobook_chapters(book_id);
CREATE INDEX IF NOT EXISTS idx_audiobook_chapters_audio_url ON public.audiobook_chapters(book_id, audio_url);
CREATE INDEX IF NOT EXISTS idx_audiobook_chapters_upload_status ON public.audiobook_chapters(upload_status);

-- 初始化全局共享设置
INSERT INTO public.global_settings (setting_key, setting_value, description, is_secret) VALUES
    ('HF_TOKEN', '', 'Hugging Face API Token（用于下载音乐库）', true),
    ('HF_DATASET_ZIP_URLS', '', 'Hugging Face Datasets ZIP 下载链接', false),
    ('BUCKET_IDS', '', 'Hugging Face Bucket ID 列表', false),
    ('SENSENOVA_API_KEY', '', 'Sensenova/DeepSeek API 密钥（用于Podcast文案和封面）', true),
    ('MODELSCOPE_TOKEN', '', 'ModelScope API Token（用于AI封面生成，逗号分隔多Token）', true),
    ('TG_BOT_TOKEN', '', 'Telegram Bot Token（用于从TG下载已降噪音频缓存）', true),
    ('TG_CHAT_ID', '', 'Telegram Chat ID（音频缓存所在的聊天/频道ID）', false)
ON CONFLICT (setting_key) DO NOTHING;
