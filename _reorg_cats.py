"""
重排 config_schema.py 的分类，让相关配置聚合在一起。
新分组设计（10 个类别，由浅入深）：
  1. 密钥与连接 — 所有 secret/token/API key
  2. 存储路径   — OUTPUT_ROOT, MUSIC_DIR 等
  3. 下载控制   — 并发、超时、重试
  4. 音频处理   — 降噪 + BGM 混音（同属音频流水线）
  5. AI 生成    — 封面 + SEO（同属 AI 调用）
  6. YouTube 上传 — 所有 YT 发布相关
  7. 视频封装   — MP4 生成
  8. 音乐库     — HF datasets/buckets 下载
  9. Podcast    — 所有 Podcast 相关
  10. 流程控制  — skip/force/split/resume
"""

import re

PATH = r'H:\2026_main_project\yt_aduio_book_one_to_all\backend\config_schema.py'
with open(PATH, 'r', encoding='utf-8') as f:
    content = f.read()

# ── 分类重映射 ──
CATEGORY_MAP = {
    # 基础连接 → 拆分
    "POSTGRES_DSN":     "🔑 密钥与连接",
    "YOUTUBE_CHANNEL_NAME": "📺 YouTube 上传",
    "MAX_PROCESS_COUNT":    "⚙️ 流程控制",
    "PROJECT_FLAG":         "⚙️ 流程控制",
    "OUTPUT_ROOT":          "📁 存储路径",
    "TARGET_CATEGORY":      "⚙️ 流程控制",

    # 下载参数 → 保留
    "DOWNLOAD_WORKERS":                 "📥 下载控制",
    "REQUEST_DELAY":                    "📥 下载控制",
    "REQUEST_TIMEOUT":                  "📥 下载控制",
    "MAX_RETRIES":                      "📥 下载控制",
    "AUDIO_DOWNLOAD_CONNECT_TIMEOUT":   "📥 下载控制",
    "AUDIO_DOWNLOAD_READ_TIMEOUT":      "📥 下载控制",
    "AUDIO_DOWNLOAD_MAX_RETRY_ATTEMPTS":"📥 下载控制",
    "AUDIO_DOWNLOAD_MAX_TOTAL_SECONDS": "📥 下载控制",
    "AUDIO_DOWNLOAD_STUCK_LOG_INTERVAL_SECONDS": "📥 下载控制",

    # 运行时长 → 流程控制
    "SKIP_EXISTING":        "⚙️ 流程控制",
    "FORCE_REPROCESS":      "⚙️ 流程控制",
    "QUIET_RUNTIME_OUTPUT": "⚙️ 流程控制",

    # 长音频分片 → 流程控制
    "LONG_AUDIO_SPLIT_TRIGGER_HOURS":  "⚙️ 流程控制",
    "LONG_AUDIO_PART_TARGET_HOURS":    "⚙️ 流程控制",
    "BOOK_STATE_TABLE":                "⚙️ 流程控制",
    "CLEANUP_COMPLETED_SPLIT_STATES":  "⚙️ 流程控制",
    "PRIORITIZE_INTERRUPTED_BOOKS":    "⚙️ 流程控制",

    # DeepFilter 降噪 → 音频处理
    "ENABLE_DEEPFILTER":        "🔊 音频处理",
    "segment_duration_minutes": "🔊 音频处理",
    "DEEPFILTER_WORKERS":       "🔊 音频处理",

    # AI封面&SEO → 拆分
    "ENABLE_COVER_GENERATION":              "🎨 AI 生成",
    "ENABLE_SEO_GENERATION":                "🎨 AI 生成",
    "MODELSCOPE_TOKEN":                     "🔑 密钥与连接",
    "MODELSCOPE_IMAGE_CONNECT_TIMEOUT":     "🎨 AI 生成",
    "MODELSCOPE_IMAGE_READ_TIMEOUT":        "🎨 AI 生成",
    "MODELSCOPE_IMAGE_POLL_CONNECT_TIMEOUT":"🎨 AI 生成",
    "MODELSCOPE_IMAGE_POLL_READ_TIMEOUT":   "🎨 AI 生成",
    "MODELSCOPE_TOKEN_SWITCH_DELAY_SECONDS":"🎨 AI 生成",
    "API_PRIORITY_ORDER":                   "🎨 AI 生成",
    "CLOUD_RUNTIME_SETTINGS_TABLE":         "🔑 密钥与连接",
    "MODELSCOPE_TOKEN_TABLE":               "🔑 密钥与连接",

    # YouTube 上传 → 保留
    "ENABLE_YOUTUBE_UPLOAD":                "📺 YouTube 上传",
    "YOUTUBE_PRIVACY_STATUS":               "📺 YouTube 上传",
    "YOUTUBE_SCHEDULE_AFTER_HOURS":         "📺 YouTube 上传",
    "YOUTUBE_DAILY_PUBLISH_LIMIT":          "📺 YouTube 上传",
    "YOUTUBE_CATEGORY_ID":                  "📺 YouTube 上传",
    "YOUTUBE_DEFAULT_LANGUAGE":             "📺 YouTube 上传",
    "ENABLE_YOUTUBE_TRADITIONAL_LOCALIZATION":"📺 YouTube 上传",
    "YOUTUBE_LOCALIZATION_LOCALES":         "📺 YouTube 上传",
    "YOUTUBE_TRADITIONAL_LOCALE":           "📺 YouTube 上传",
    "YOUTUBE_TRADITIONAL_OPENCC_CONFIG":    "📺 YouTube 上传",
    "ENABLE_AUTO_INSTALL_OPENCC":           "📺 YouTube 上传",
    "APPEND_TAGS_TO_TITLE":                 "📺 YouTube 上传",
    "APPEND_TAGS_TO_DESC":                  "📺 YouTube 上传",

    # 视频生成 → 保留
    "ENABLE_VIDEO_GENERATION":  "🎬 视频封装",
    "VIDEO_RESOLUTION":         "🎬 视频封装",

    # 音乐库&BGM → 拆分
    "DOWNLOAD_FROM_BUCKETS":    "🎵 音乐库下载",
    "HF_MUSIC_DOWNLOAD_METHOD": "🎵 音乐库下载",
    "HF_DATASET_ZIP_URLS":      "🎵 音乐库下载",
    "BUCKET_IDS":               "🎵 音乐库下载",
    "HF_TOKEN":                 "🔑 密钥与连接",
    "LOCAL_MUSIC_DIR":          "📁 存储路径",
    "ENABLE_BGM_MIX":           "🔊 音频处理",
    "MUSIC_DIR":                "📁 存储路径",
    "VOLUME_OFFSET_DB":         "🔊 音频处理",
    "HIGHPASS_FREQ":            "🔊 音频处理",
    "FADE_DURATION_MS":         "🔊 音频处理",
    "MIN_VOLUME_DB":            "🔊 音频处理",
    "ENABLE_DYNAMIC_VOLUME":    "🔊 音频处理",
    "ENABLE_SPECTRAL_SHAPING":  "🔊 音频处理",
    "STEREO_OFFSET":            "🔊 音频处理",

    # Podcast 模式 → 保留
    "ENABLE_YOUTUBE_PODCAST_RUNTIME":           "🎙️ Podcast",
    "ENABLE_YOUTUBE_PODCAST_UNIFIED_SHOW":      "🎙️ Podcast",
    "ENABLE_YOUTUBE_PODCAST_SPLIT_PLAYLIST":    "🎙️ Podcast",
    "YOUTUBE_PODCAST_SHOW_TITLE_TEMPLATE":      "🎙️ Podcast",
    "YOUTUBE_PODCAST_IMAGE_SIZE":               "🎙️ Podcast",
    "YOUTUBE_PODCAST_IMAGE_MAX_BYTES":          "🎙️ Podcast",
    "YOUTUBE_PODCAST_SHOW_PLAYLIST_SETTING_KEY":"🎙️ Podcast",
    "SENSENOVA_BASE_URL":                       "🔑 密钥与连接",
    "SENSENOVA_API_KEY":                        "🔑 密钥与连接",
    "YOUTUBE_PODCAST_TEXT_MODEL_PRIMARY":       "🎙️ Podcast",
    "YOUTUBE_PODCAST_TEXT_MODEL_FALLBACK":      "🎙️ Podcast",
    "YOUTUBE_PODCAST_IMAGE_MODEL_PRIMARY":      "🎙️ Podcast",
    "YOUTUBE_PODCAST_TEXT_MODEL_RETRIES":       "🎙️ Podcast",
    "YOUTUBE_PODCAST_IMAGE_MODEL_RETRIES":      "🎙️ Podcast",
    "YOUTUBE_PODCAST_AI_RETRY_BASE_SECONDS":    "🎙️ Podcast",
    "YOUTUBE_PODCAST_YT_RETRIES":               "🎙️ Podcast",
    "YOUTUBE_PODCAST_YT_RETRY_BASE_SECONDS":    "🎙️ Podcast",
    "YOUTUBE_PODCAST_FONT_CACHE_DIRNAME":       "🎙️ Podcast",
}

# ── 执行替换 ──
for key, new_cat in CATEGORY_MAP.items():
    # 匹配 "category": "旧分类名" 并替换
    old_pattern = rf'("{key}".*?"category":\s*)"[^"]*"'
    replacement = rf'\1"{new_cat}"'
    content, count = re.subn(old_pattern, replacement, content, flags=re.DOTALL)
    if count == 0:
        print(f"  ⚠️ 未找到 {key}")
    elif count > 1:
        print(f"  ⚠️ {key} 匹配 {count} 次（预期1次）")

# ── 同时更新注释分隔线 ──
comment_map = {
    '# ═══ 基础连接 ═══':          '# ═══ 🔑 密钥与连接 ═══',
    '# ═══ 下载参数 ═══':          '# ═══ 📥 下载控制 ═══',
    '# ═══ 运行控制 ═══':          '# ═══ ⚙️ 流程控制 ═══',
    '# ═══ 长音频分片 & 断点续跑 ═══': '',  # 合并到流程控制
    '# ═══ DeepFilter 降噪 ═══':   '# ═══ 🔊 音频处理 ═══',
    '# ═══ AI 封面 & SEO ═══':     '# ═══ 🎨 AI 生成 ═══',
    '# ═══ YouTube 上传 ═══':     '# ═══ 📺 YouTube 上传 ═══',
    '# ═══ 视频生成 ═══':          '# ═══ 🎬 视频封装 ═══',
    '# ═══ 音乐库 & BGM ═══':     '',  # 拆分到音频处理 + 音乐库下载
    '# ═══ Podcast 模式 ═══':     '# ═══ 🎙️ Podcast ═══',
}
for old_comment, new_comment in comment_map.items():
    if new_comment:
        content = content.replace(old_comment, new_comment)
    else:
        content = content.replace(old_comment + '\n', '')

# 新增存储路径和音乐库下载的注释
# 在 OUTPUT_ROOT 前插入 (现在OUTPUT_ROOT在密钥与连接组里, 需要单独处理)
# 简单做法: 在合适位置插入新注释分隔线
content = content.replace(
    '"OUTPUT_ROOT": {\n        "type": "str", "category": "📁 存储路径",',
    '\n    # ═══ 📁 存储路径 ═══\n    "OUTPUT_ROOT": {\n        "type": "str", "category": "📁 存储路径",'
)

content = content.replace(
    '"DOWNLOAD_FROM_BUCKETS": {\n        "type": "bool", "category": "🎵 音乐库下载",',
    '\n    # ═══ 🎵 音乐库下载 ═══\n    "DOWNLOAD_FROM_BUCKETS": {\n        "type": "bool", "category": "🎵 音乐库下载",'
)

with open(PATH, 'w', encoding='utf-8') as f:
    f.write(content)

print("\n✅ 分类重组完成")