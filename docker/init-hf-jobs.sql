-- docker/init-hf-jobs.sql
-- HF 外包任务队列表（流水线 Worker 队列认领模式使用）
-- 可独立执行，也已合并到 docker/init-db.sql 末尾（首次建库自动创建）

-- ═══════════════════════════════════════════════════════════
-- hf_jobs — HF 外包任务队列
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.hf_jobs (
    job_id        serial PRIMARY KEY,
    job_type      varchar(50) NOT NULL,          -- 'tg_cache_pipeline' / 'test_*'
    book_id       text,                           -- 流水线任务的书ID
    channel_name  text,                           -- YouTube 频道名
    status        varchar(50) DEFAULT 'pending',  -- pending/processing/done/failed
    worker_id     varchar(100),                   -- 认领的 Worker ID
    claimed_at    timestamptz,                    -- 认领时间
    result        jsonb,                          -- 处理结果 (youtube_url 等)
    error_message text,                           -- 失败原因
    retry_count   integer NOT NULL DEFAULT 0,     -- 重试次数
    params        jsonb,                           -- 测试任务参数（AI/BGM/TG下载测试的输入参数）
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

CREATE INDEX IF NOT EXISTS idx_hf_jobs_status      ON public.hf_jobs(status);
CREATE INDEX IF NOT EXISTS idx_hf_jobs_type_status ON public.hf_jobs(job_type, status);
CREATE INDEX IF NOT EXISTS idx_hf_jobs_channel      ON public.hf_jobs(channel_name);
CREATE INDEX IF NOT EXISTS idx_hf_jobs_created_at   ON public.hf_jobs(created_at DESC);

COMMENT ON TABLE  public.hf_jobs IS 'HF 外包任务队列：流水线 Worker 通过 FOR UPDATE SKIP LOCKED 原子认领';
COMMENT ON COLUMN public.hf_jobs.job_type   IS '任务类型：tg_cache_pipeline=仅TG缓存完整书处理+上传；test_*=测试实验';
COMMENT ON COLUMN public.hf_jobs.status     IS 'pending=待处理；processing=处理中；done=成功；failed=失败';
COMMENT ON COLUMN public.hf_jobs.worker_id  IS '认领此任务的 HF Worker ID（原子认领后写入）';
COMMENT ON COLUMN public.hf_jobs.result     IS '处理结果 JSON：{youtube_url, video_id, ...}';

-- ═══════════════════════════════════════════════════════════
-- hf_worker_stats — Worker 业绩统计（可选，用于监控）
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.hf_worker_stats (
    worker_id      varchar(100) PRIMARY KEY,
    worker_type    varchar(50),                    -- pipeline / test
    total_jobs     integer NOT NULL DEFAULT 0,
    success_jobs   integer NOT NULL DEFAULT 0,
    failed_jobs    integer NOT NULL DEFAULT 0,
    total_seconds  bigint NOT NULL DEFAULT 0,      -- 累计处理耗时(秒)
    last_job_at    timestamptz,
    last_seen_at   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.hf_worker_stats IS 'HF Worker 业绩统计：每完成一个任务更新一次';
