"""配置参数 Schema 定义 — 95+ 参数的元数据。

每个参数的 schema 包含：
  - type: str / int / float / bool / enum
  - category: 分组名称
  - label: 界面显示名
  - default: 默认值
  - description: 说明
  - min / max: 范围限制（int/float）
  - options: 枚举选项（enum）
  - secret: 是否敏感信息（界面脱敏）
  - global: 是否全局共享（存 global_settings 表）
  - readonly: 是否只读
"""

from __future__ import annotations


CONFIG_SCHEMA: dict[str, dict] = {
    # ═══ 🔑 密钥与认证 ═══
    "POSTGRES_DSN": {
        "type": "str", "category": "🔑 密钥与认证", "label": "PostgreSQL 连接串",
        "default": "", "description": "PostgreSQL 数据库连接串", "secret": True, "global": True,
    },
    "MODELSCOPE_TOKEN": {
        "type": "str", "category": "🔑 密钥与认证", "label": "本地ModelScope Token",
        "default": "", "secret": True, "global": True,
    },
    "SENSENOVA_API_KEY": {
        "type": "str", "category": "🔑 密钥与认证", "label": "Sensenova API密钥",
        "default": "", "secret": True, "global": True,
    },
    "SENSENOVA_BASE_URL": {
        "type": "str", "category": "🔑 密钥与认证", "label": "Sensenova API地址",
        "default": "https://token.sensenova.cn/v1",
    },

    # ═══ 🗄️ 数据库与表 ═══
    "BOOK_STATE_TABLE": {
        "type": "str", "category": "🗄️ 数据库与表", "label": "状态表名",
        "default": "book_processing_states", "readonly": True,
    },
    "CLOUD_RUNTIME_SETTINGS_TABLE": {
        "type": "str", "category": "🗄️ 数据库与表", "label": "云端设置表",
        "default": "channel_runtime_settings", "readonly": True,
    },
    "MODELSCOPE_TOKEN_TABLE": {
        "type": "str", "category": "🗄️ 数据库与表", "label": "Token表名",
        "default": "modelscope_tokens", "readonly": True,
    },
    "YOUTUBE_PODCAST_SHOW_PLAYLIST_SETTING_KEY": {
        "type": "str", "category": "🗄️ 数据库与表", "label": "Show Playlist键名",
        "default": "podcast_longform_show_playlist_id", "readonly": True,
    },
    "REMOTE_DATABASE_URL": {
        "type": "str", "category": "🗄️ 数据库与表", "label": "远程数据库DSN",
        "default": "postgresql://audiobook_app:inriynisse1991@85.121.48.55:5432/audiobook",
        "description": "audiobook_pipeline 远程数据库连接串，用于同步章节状态到远程库",
        "secret": True, "global": True,
    },

    # ═══ 📁 存储路径 ═══
    "OUTPUT_ROOT": {
        "type": "str", "category": "📁 存储路径", "label": "输出根目录",
        "default": "/data/output", "description": "输出根目录",
    },
    "LOCAL_MUSIC_DIR": {
        "type": "str", "category": "📁 存储路径", "label": "本地音乐目录",
        "default": "/data/music",
    },
    "MUSIC_DIR": {
        "type": "str", "category": "📁 存储路径", "label": "BGM源目录",
        "default": "/data/music",
    },
    "YOUTUBE_PODCAST_FONT_CACHE_DIRNAME": {
        "type": "str", "category": "📁 存储路径", "label": "字体缓存目录名",
        "default": "_podcast_font_cache", "readonly": True,
    },

    # ═══ 📥 下载与网络 ═══
    "DOWNLOAD_WORKERS": {
        "type": "int", "category": "📥 下载与网络", "label": "并发下载线程数",
        "default": 4, "min": 1, "max": 16,
    },
    "REQUEST_DELAY": {
        "type": "float", "category": "📥 下载与网络", "label": "请求间隔(秒)",
        "default": 0.3, "min": 0, "max": 10,
    },
    "REQUEST_TIMEOUT": {
        "type": "int", "category": "📥 下载与网络", "label": "HTTP超时(秒)",
        "default": 300, "min": 10, "max": 3600,
    },
    "MAX_RETRIES": {
        "type": "int", "category": "📥 下载与网络", "label": "最大重试次数",
        "default": 3, "min": 0, "max": 20,
    },
    "AUDIO_DOWNLOAD_CONNECT_TIMEOUT": {
        "type": "int", "category": "📥 下载与网络", "label": "音频连接超时(秒)",
        "default": 20, "min": 5, "max": 120,
    },
    "AUDIO_DOWNLOAD_READ_TIMEOUT": {
        "type": "int", "category": "📥 下载与网络", "label": "音频读取超时(秒)",
        "default": 90, "min": 10, "max": 600,
    },
    "AUDIO_DOWNLOAD_MAX_RETRY_ATTEMPTS": {
        "type": "int", "category": "📥 下载与网络", "label": "音频最大重试次数",
        "default": 12, "min": 1, "max": 50,
    },
    "AUDIO_DOWNLOAD_MAX_TOTAL_SECONDS": {
        "type": "int", "category": "📥 下载与网络", "label": "单章节总耗时上限(秒)",
        "default": 1800, "min": 60, "max": 7200,
    },
    "AUDIO_DOWNLOAD_STUCK_LOG_INTERVAL_SECONDS": {
        "type": "int", "category": "📥 下载与网络", "label": "卡住检测日志间隔(秒)",
        "default": 30, "min": 5, "max": 300,
    },

    # ═══ ⚙️ 流程控制 ═══
    "MAX_PROCESS_COUNT": {
        "type": "int", "category": "⚙️ 流程控制", "label": "最多处理书籍数",
        "default": 10, "min": 0, "max": 100,
        "description": "本次最多成功处理多少本书（0=不限制）",
    },
    "PROJECT_FLAG": {
        "type": "str", "category": "⚙️ 流程控制", "label": "项目标记",
        "default": "", "description": "写入 books.status 防重复处理，空时回退为频道名",
    },
    "TARGET_CATEGORY": {
        "type": "str", "category": "⚙️ 流程控制", "label": "图书分类过滤",
        "default": "文学小说", "description": "只处理该分类的书籍（空=全部）",
    },
    "SKIP_EXISTING": {
        "type": "bool", "category": "⚙️ 流程控制", "label": "跳过已存在文件",
        "default": True,
    },
    "FORCE_REPROCESS": {
        "type": "bool", "category": "⚙️ 流程控制", "label": "强制重新处理",
        "default": False,
    },
    "QUIET_RUNTIME_OUTPUT": {
        "type": "bool", "category": "⚙️ 流程控制", "label": "静默模式",
        "default": True,
    },
    "CLEANUP_COMPLETED_SPLIT_STATES": {
        "type": "bool", "category": "⚙️ 流程控制", "label": "清理已完成状态",
        "default": True,
    },
    "CLEANUP_INTERMEDIATE_FILES_AFTER_SUCCESS": {
        "type": "bool", "category": "⚙️ 流程控制", "label": "结束后清理中间文件",
        "default": True, "global": True,
        "description": "任务结束后（无论成功/失败/中断/跳过）自动删除 book_dir 中的中间文件（章节音频、降噪音频、MP4、封面等），仅保留结果报告和上传回执。断点续跑信息已在数据库中，开启可防止磁盘被残留文件占满",
    },
    "PRIORITIZE_INTERRUPTED_BOOKS": {
        "type": "bool", "category": "⚙️ 流程控制", "label": "优先恢复中断书籍",
        "default": True,
    },

    # ═══ 🔊 音频处理 ═══
    "ENABLE_DEEPFILTER": {
        "type": "bool", "category": "🔊 音频处理", "label": "启用降噪",
        "default": True,
    },
    "segment_duration_minutes": {
        "type": "int", "category": "🔊 音频处理", "label": "降噪分片时长(分钟)",
        "default": 60, "min": 5, "max": 240,
    },
    "DEEPFILTER_WORKERS": {
        "type": "int", "category": "🔊 音频处理", "label": "降噪并行数",
        "default": 2, "min": 1, "max": 8,
    },
    "ENABLE_BGM_MIX": {
        "type": "bool", "category": "🔊 音频处理", "label": "启用BGM混音",
        "default": True,
    },
    "VOLUME_OFFSET_DB": {
        "type": "int", "category": "🔊 音频处理", "label": "BGM音量偏移(dB)",
        "default": -25, "min": -60, "max": 0,
    },
    "HIGHPASS_FREQ": {
        "type": "int", "category": "🔊 音频处理", "label": "高通滤波频率(Hz)",
        "default": 150, "min": 50, "max": 500,
    },
    "FADE_DURATION_MS": {
        "type": "int", "category": "🔊 音频处理", "label": "淡入淡出(ms)",
        "default": 3000, "min": 0, "max": 10000,
    },
    "MIN_VOLUME_DB": {
        "type": "int", "category": "🔊 音频处理", "label": "最小音量阈值(dB)",
        "default": -40, "min": -80, "max": 0,
    },
    "ENABLE_DYNAMIC_VOLUME": {
        "type": "bool", "category": "🔊 音频处理", "label": "动态音量均衡",
        "default": True,
    },
    "ENABLE_SPECTRAL_SHAPING": {
        "type": "bool", "category": "🔊 音频处理", "label": "频谱塑形增强",
        "default": True,
    },
    "STEREO_OFFSET": {
        "type": "float", "category": "🔊 音频处理", "label": "立体声偏移",
        "default": 0.0, "min": 0, "max": 1,
    },
    "LONG_AUDIO_SPLIT_TRIGGER_HOURS": {
        "type": "float", "category": "🔊 音频处理", "label": "分片触发阈值(小时)",
        "default": 12.0, "min": 1, "max": 48,
    },
    "LONG_AUDIO_PART_TARGET_HOURS": {
        "type": "float", "category": "🔊 音频处理", "label": "每片目标时长(小时)",
        "default": 11.8, "min": 0.5, "max": 48,
    },

    # ═══ 🎨 AI 生成 ═══
    "ENABLE_COVER_GENERATION": {
        "type": "bool", "category": "🎨 AI 生成", "label": "启用封面生成",
        "default": True,
    },
    "ENABLE_SEO_GENERATION": {
        "type": "bool", "category": "🎨 AI 生成", "label": "启用SEO生成",
        "default": True,
    },
    "API_PRIORITY_ORDER": {
        "type": "str", "category": "🎨 AI 生成", "label": "API优先级",
        "default": "modelscope,sensenova",
    },
    "MODELSCOPE_TOKEN_SWITCH_DELAY_SECONDS": {
        "type": "int", "category": "🎨 AI 生成", "label": "Token切换间隔(秒)",
        "default": 30, "min": 5, "max": 300,
    },
    "MODELSCOPE_IMAGE_CONNECT_TIMEOUT": {
        "type": "int", "category": "🎨 AI 生成", "label": "生图连接超时(秒)",
        "default": 300, "min": 30, "max": 600,
    },
    "MODELSCOPE_IMAGE_READ_TIMEOUT": {
        "type": "int", "category": "🎨 AI 生成", "label": "生图读取超时(秒)",
        "default": 300, "min": 30, "max": 600,
    },
    "MODELSCOPE_IMAGE_POLL_CONNECT_TIMEOUT": {
        "type": "int", "category": "🎨 AI 生成", "label": "轮询连接超时(秒)",
        "default": 300, "min": 30, "max": 600,
    },
    "MODELSCOPE_IMAGE_POLL_READ_TIMEOUT": {
        "type": "int", "category": "🎨 AI 生成", "label": "轮询读取超时(秒)",
        "default": 300, "min": 30, "max": 600,
    },

    # ═══ 📺 YouTube 上传 ═══
    # ── 核心上传 ──
    "YOUTUBE_CHANNEL_NAME": {
        "type": "str", "category": "📺 YouTube 上传", "label": "YouTube 频道名",
        "default": "", "description": "当前绑定的 YouTube 频道名", "readonly": True,
    },
    "ENABLE_YOUTUBE_UPLOAD": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "启用上传",
        "default": True,
    },
    "YOUTUBE_PRIVACY_STATUS": {
        "type": "enum", "category": "📺 YouTube 上传", "label": "发布隐私",
        "default": "schedule", "options": ["private", "unlisted", "public", "schedule"],
    },
    "YOUTUBE_SCHEDULE_AFTER_HOURS": {
        "type": "int", "category": "📺 YouTube 上传", "label": "预约延迟(小时)",
        "default": 24, "min": 1, "max": 720,
    },
    "YOUTUBE_DAILY_PUBLISH_LIMIT": {
        "type": "int", "category": "📺 YouTube 上传", "label": "每日发布上限",
        "default": 3, "min": 1, "max": 50,
    },
    "YOUTUBE_CATEGORY_ID": {
        "type": "str", "category": "📺 YouTube 上传", "label": "视频分类ID",
        "default": "", "description": "留空=自动",
    },

    # ── 视频封装 ──
    "ENABLE_VIDEO_GENERATION": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "启用MP4封装",
        "default": True,
    },
    "VIDEO_RESOLUTION": {
        "type": "enum", "category": "📺 YouTube 上传", "label": "视频分辨率",
        "default": "1080p", "options": ["720p", "1080p"],
    },

    # ═══ 📦 Telegram 音频缓存 ═══
    "ENABLE_TG_AUDIO_CACHE": {
        "type": "bool", "category": "📦 Telegram 音频缓存", "label": "启用TG音频缓存",
        "default": True,
        "description": "启用后，已上传到 Telegram 的章节将直接从 TG 下载已降噪音频，跳过原始下载和 DeepFilter",
        "global": True,
    },
    "TG_BOT_TOKEN": {
        "type": "str", "category": "📦 Telegram 音频缓存", "label": "TG Bot Token",
        "default": "", "secret": True, "global": True,
        "description": "Telegram Bot Token，支持逗号分隔的多个 Token 实现多 Bot 轮换下载（如：token1,token2,token3）。每个文件用上传时记录的 bot_user_id 匹配对应的 Token 下载，避免单 Bot 触发 TG API 限流",
    },
    "TG_CHAT_ID": {
        "type": "str", "category": "📦 Telegram 音频缓存", "label": "TG Chat ID",
        "default": "", "global": True,
        "description": "音频缓存所在的 Telegram 聊天/频道 ID",
    },
    "ONLY_TG_CACHED_BOOKS": {
        "type": "bool", "category": "📦 Telegram 音频缓存", "label": "仅TG缓存完整书",
        "default": False,
        "description": "启用后，只处理所有章节音频均已DF降噪并上传到TG的书籍（通常由「仅TG缓存完整书」任务按钮自动设置，无需手动开启）",
        "global": True,
    },
    "TG_SERIAL_DOWNLOAD": {
        "type": "bool", "category": "📦 Telegram 音频缓存", "label": "串行下载",
        "default": True,
        "description": "启用后，TG 音频逐个串行下载（一次只下载一个章节），避免并发请求触发 Telegram API 限流或 DNS 解析失败",
        "global": True,
    },
    "TG_DOWNLOAD_INTERVAL_SECONDS": {
        "type": "int", "category": "📦 Telegram 音频缓存", "label": "下载间隔(秒)",
        "default": 5, "min": 0, "max": 60,
        "description": "每完成一个 TG 章节下载后，等待多少秒再下载下一个（防止请求过快被 Telegram 限流）",
        "global": True,
    },

    # ── 本地化 ──
    "YOUTUBE_DEFAULT_LANGUAGE": {
        "type": "str", "category": "📺 YouTube 上传", "label": "默认语言",
        "default": "zh-CN",
    },
    "ENABLE_YOUTUBE_TRADITIONAL_LOCALIZATION": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "繁体本地化",
        "default": True,
    },
    "YOUTUBE_LOCALIZATION_LOCALES": {
        "type": "str", "category": "📺 YouTube 上传", "label": "本地化地区",
        "default": "zh-TW,zh-HK,zh-SG,zh-Hant",
    },
    "YOUTUBE_TRADITIONAL_LOCALE": {
        "type": "str", "category": "📺 YouTube 上传", "label": "主要繁体地区",
        "default": "zh-TW",
    },
    "YOUTUBE_TRADITIONAL_OPENCC_CONFIG": {
        "type": "str", "category": "📺 YouTube 上传", "label": "OpenCC配置",
        "default": "s2t",
    },
    "ENABLE_AUTO_INSTALL_OPENCC": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "自动安装OpenCC",
        "default": True,
    },

    # ── 标签 ──
    "APPEND_TAGS_TO_TITLE": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "标签追加到标题",
        "default": False,
    },
    "APPEND_TAGS_TO_DESC": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "标签追加到描述",
        "default": True,
    },

    # ── Podcast ──
    "ENABLE_YOUTUBE_PODCAST_RUNTIME": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "启用Podcast",
        "default": True,
    },
    "ENABLE_YOUTUBE_PODCAST_UNIFIED_SHOW": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "统一Show",
        "default": True,
    },
    "ENABLE_YOUTUBE_PODCAST_SPLIT_PLAYLIST": {
        "type": "bool", "category": "📺 YouTube 上传", "label": "分片播放列表",
        "default": True,
    },
    "YOUTUBE_PODCAST_SHOW_TITLE_TEMPLATE": {
        "type": "str", "category": "📺 YouTube 上传", "label": "Show标题模板",
        "default": "{channel_name}｜长篇有声书全集",
    },
    "YOUTUBE_PODCAST_IMAGE_SIZE": {
        "type": "int", "category": "📺 YouTube 上传", "label": "封面尺寸(像素)",
        "default": 2048, "min": 512, "max": 4096,
    },
    "YOUTUBE_PODCAST_IMAGE_MAX_BYTES": {
        "type": "int", "category": "📺 YouTube 上传", "label": "封面大小上限(字节)",
        "default": 2097152, "readonly": True,
    },
    "YOUTUBE_PODCAST_TEXT_MODEL_PRIMARY": {
        "type": "str", "category": "📺 YouTube 上传", "label": "文本主模型",
        "default": "deepseek-v4-flash",
    },
    "YOUTUBE_PODCAST_TEXT_MODEL_FALLBACK": {
        "type": "str", "category": "📺 YouTube 上传", "label": "文本备选模型",
        "default": "sensenova-6.7-flash-lite",
    },
    "YOUTUBE_PODCAST_IMAGE_MODEL_PRIMARY": {
        "type": "str", "category": "📺 YouTube 上传", "label": "图片生成模型",
        "default": "sensenova-u1-fast",
    },
    "YOUTUBE_PODCAST_TEXT_MODEL_RETRIES": {
        "type": "int", "category": "📺 YouTube 上传", "label": "文本重试次数",
        "default": 2, "min": 0, "max": 10,
    },
    "YOUTUBE_PODCAST_IMAGE_MODEL_RETRIES": {
        "type": "int", "category": "📺 YouTube 上传", "label": "图片重试次数",
        "default": 3, "min": 0, "max": 10,
    },
    "YOUTUBE_PODCAST_AI_RETRY_BASE_SECONDS": {
        "type": "float", "category": "📺 YouTube 上传", "label": "AI重试间隔(秒)",
        "default": 30.0, "min": 5, "max": 120,
    },
    "YOUTUBE_PODCAST_YT_RETRIES": {
        "type": "int", "category": "📺 YouTube 上传", "label": "YT重试次数",
        "default": 5, "min": 0, "max": 20,
    },
    "YOUTUBE_PODCAST_YT_RETRY_BASE_SECONDS": {
        "type": "float", "category": "📺 YouTube 上传", "label": "YT重试间隔(秒)",
        "default": 3.0, "min": 1, "max": 30,
    },

    # ═══ 🛰️ HF 外包 ═══
    "VPS_RELAY_URL": {
        "type": "str", "category": "🛰️ HF 外包", "label": "VPS中继地址",
        "default": "",
        "description": "HF 外包 VPS 中继调度器地址（如 http://VPS_IP:38080），用于任务投递和调度控制",
        "secret": True, "global": True,
    },
    "HF_TEST_WORKER_URLS": {
        "type": "str", "category": "🛰️ HF 外包", "label": "测试Worker地址",
        "default": "",
        "description": "HF 测试实验 Worker 地址，多个用英文逗号分隔（如 https://user-audiobook-test-worker-1.hf.space）",
        "secret": True, "global": True,
    },
}


# 频道专属 Key（这些配置需要按频道独立设置，不进全局）
CHANNEL_SPECIFIC_KEYS = {
    "YOUTUBE_CHANNEL_NAME",   # 频道名（只读，自动绑定）
    "PROJECT_FLAG",           # 项目标记
    "TARGET_CATEGORY",        # 图书分类过滤
    "MAX_PROCESS_COUNT",      # 最多处理书籍数
    "VOLUME_OFFSET_DB",       # BGM 音量偏移
}

# 全局共享配置键列表：除了频道专属 Key 外，其余所有 Key 均为全局共享
# 原因：绝大部分配置在所有频道间保持一致，没必要每个频道重复设置
GLOBAL_CONFIG_KEYS = [k for k in CONFIG_SCHEMA if k not in CHANNEL_SPECIFIC_KEYS]

# 按分类分组的配置
def get_config_by_category() -> dict[str, list[dict]]:
    """返回按分类分组的配置 schema。"""
    result: dict[str, list[dict]] = {}
    for key, schema in CONFIG_SCHEMA.items():
        cat = schema.get("category", "其他")
        entry = {"key": key, **schema}
        result.setdefault(cat, []).append(entry)
    return result

# 默认配置字典
DEFAULT_CONFIG = {k: v["default"] for k, v in CONFIG_SCHEMA.items()}


def coerce_value(key: str, value):
    """将输入值转换为 schema 要求的类型。"""
    schema = CONFIG_SCHEMA.get(key)
    if not schema:
        return value

    vtype = schema.get("type", "str")
    if value is None:
        return schema.get("default")

    try:
        if vtype == "int":
            return int(value)
        elif vtype == "float":
            return float(value)
        elif vtype == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("true", "1", "yes", "on")
        elif vtype == "enum":
            options = schema.get("options", [])
            val = str(value).strip()
            if options and val not in options:
                return schema.get("default")
            return val
        else:
            return str(value)
    except (ValueError, TypeError):
        return schema.get("default")