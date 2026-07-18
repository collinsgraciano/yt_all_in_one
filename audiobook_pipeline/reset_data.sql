-- ============================================================
-- 重置所有章节的上传状态和 Telegram 字段 (不删除 books 数据)
--
-- 用途: 需要重新处理所有章节时使用 (例如切换了 Bot Token)
--
-- 用法:
--   docker exec audiobook_pg psql -U audiobook_app -d audiobook -f /dev/stdin < reset_data.sql
--   或:
--   psql "postgresql://audiobook_app:inriynisse1991@127.0.0.1:5432/audiobook" -f reset_data.sql
-- ============================================================

-- 1. 重置所有章节状态为 pending, 清空 Telegram 字段
UPDATE audiobook_chapters SET
    upload_status       = 'pending',
    telegram_file_id    = NULL,
    telegram_message_id = NULL,
    telegram_bot_id     = NULL,
    telegram_bot_user_id = NULL,
    worker_id           = NULL,
    claimed_at          = NULL,
    uploaded_at         = NULL,
    error_message       = NULL;

-- 2. 重置所有书籍处理状态为 pending
UPDATE books SET
    book_status = 'pending',
    updated_at  = now();

-- 3. 显示结果统计
SELECT '=== 重置完成 ===' as info;
SELECT
    COUNT(*) FILTER (WHERE upload_status = 'pending') as pending,
    COUNT(*) FILTER (WHERE upload_status = 'uploaded') as uploaded,
    COUNT(*) FILTER (WHERE upload_status = 'failed') as failed,
    COUNT(*) FILTER (WHERE telegram_file_id IS NOT NULL) as has_file_id,
    COUNT(*) FILTER (WHERE telegram_bot_id IS NOT NULL) as has_bot_id,
    COUNT(*) FILTER (WHERE telegram_bot_user_id IS NOT NULL) as has_bot_user_id
FROM audiobook_chapters;

SELECT
    COUNT(*) FILTER (WHERE book_status = 'pending') as pending,
    COUNT(*) FILTER (WHERE book_status = 'success') as success
FROM books;
