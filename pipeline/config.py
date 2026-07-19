"""运行核心：配置管理。

对应原 runtime_core.py:
- DEFAULT_RUNTIME_CONFIG（行 8-79）
- apply_runtime_config（行 81-96）
- 顶层模块级配置全局（被 globals().update 写入）
- POSTGRES_SCHEMA / SUPPORTED_AUDIO_EXTENSIONS（行 111-112）

设计：所有配置项作为 config 模块的模块级全局暴露，
apply_runtime_config() 通过 globals().update(merged) 写入本模块，
其它模块用 `from . import config as cfg` + `cfg.MAX_RETRIES` 读取，
行为与原文件完全一致（运行时读取最新值）。
"""
from __future__ import annotations

import sys

# ---------------------------------------------------------------------------
# PostgreSQL schema 常量（原文件行 112）
# ---------------------------------------------------------------------------
POSTGRES_SCHEMA = "public"

# ---------------------------------------------------------------------------
# 支持的音频扩展名（原文件行 111）
# ---------------------------------------------------------------------------
SUPPORTED_AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma")

# ---------------------------------------------------------------------------
# DEFAULT_RUNTIME_CONFIG（原文件行 8-79）
# ---------------------------------------------------------------------------
DEFAULT_RUNTIME_CONFIG = {
    "POSTGRES_DSN": "",
    "YOUTUBE_CHANNEL_NAME": "",
    "MAX_PROCESS_COUNT": 10,
    "PROJECT_FLAG": "",
    "OUTPUT_ROOT": "/data/output",
    "TARGET_CATEGORY": "文学小说",
    "DOWNLOAD_WORKERS": 2,
    "REQUEST_DELAY": 0.3,
    "REQUEST_TIMEOUT": 300,
    "MODELSCOPE_IMAGE_CONNECT_TIMEOUT": 300,
    "MODELSCOPE_IMAGE_READ_TIMEOUT": 300,
    "MODELSCOPE_IMAGE_POLL_CONNECT_TIMEOUT": 300,
    "MODELSCOPE_IMAGE_POLL_READ_TIMEOUT": 300,
    "MODELSCOPE_TOKEN_SWITCH_DELAY_SECONDS": 30,
    "API_PRIORITY_ORDER": "modelscope,sensenova",
    "MAX_RETRIES": 3,
    "AUDIO_DOWNLOAD_CONNECT_TIMEOUT": 20,
    "AUDIO_DOWNLOAD_READ_TIMEOUT": 90,
    "AUDIO_DOWNLOAD_MAX_RETRY_ATTEMPTS": 12,
    "AUDIO_DOWNLOAD_MAX_TOTAL_SECONDS": 1800,
    "AUDIO_DOWNLOAD_STUCK_LOG_INTERVAL_SECONDS": 30,
    "SKIP_EXISTING": True,
    "FORCE_REPROCESS": False,
    "LONG_AUDIO_SPLIT_TRIGGER_HOURS": 12.0,
    "LONG_AUDIO_PART_TARGET_HOURS": 11.8,
    "BOOK_STATE_TABLE": "book_processing_states",
    "CLEANUP_COMPLETED_SPLIT_STATES": True,
    "PRIORITIZE_INTERRUPTED_BOOKS": True,
    "QUIET_RUNTIME_OUTPUT": True,
    "ENABLE_DEEPFILTER": True,
    "segment_duration_minutes": 60,
    "DEEPFILTER_WORKERS": 1,
    "ENABLE_COVER_GENERATION": True,
    "CLOUD_RUNTIME_SETTINGS_TABLE": "channel_runtime_settings",
    "MODELSCOPE_TOKEN_TABLE": "modelscope_tokens",
    "MODELSCOPE_TOKEN": "",
    "ENABLE_SEO_GENERATION": True,
    "ENABLE_YOUTUBE_UPLOAD": True,
    "YOUTUBE_PRIVACY_STATUS": "schedule",
    "YOUTUBE_SCHEDULE_AFTER_HOURS": 24,
    "YOUTUBE_DAILY_PUBLISH_LIMIT": 3,
    "YOUTUBE_CATEGORY_ID": "",
    "YOUTUBE_DEFAULT_LANGUAGE": "zh-CN",
    "ENABLE_YOUTUBE_TRADITIONAL_LOCALIZATION": True,
    "YOUTUBE_LOCALIZATION_LOCALES": "zh-TW,zh-HK,zh-SG,zh-Hant",
    "YOUTUBE_TRADITIONAL_LOCALE": "zh-TW",
    "YOUTUBE_TRADITIONAL_OPENCC_CONFIG": "s2t",
    "ENABLE_AUTO_INSTALL_OPENCC": True,
    "APPEND_TAGS_TO_TITLE": False,
    "APPEND_TAGS_TO_DESC": True,
    "ENABLE_VIDEO_GENERATION": True,
    "VIDEO_RESOLUTION": "1080p",
    "LOCAL_MUSIC_DIR": "/data/music",
    "ENABLE_BGM_MIX": True,
    "MUSIC_DIR": "/data/music",
    "VOLUME_OFFSET_DB": -25,
    "HIGHPASS_FREQ": 150,
    "FADE_DURATION_MS": 3000,
    "MIN_VOLUME_DB": -40,
    "ENABLE_DYNAMIC_VOLUME": True,
    "ENABLE_SPECTRAL_SHAPING": True,
    "STEREO_OFFSET": 0.0,
    "PIPELINE_TASK_ID": "",
    # TG Bot Token 支持逗号分隔的多个 Token（多 Bot 轮换下载）
    # 每个文件用 DB 中记录的 telegram_bot_user_id 匹配对应的 Token
    "TG_BOT_TOKEN": "",
    "TG_CHAT_ID": "",
    "ENABLE_TG_AUDIO_CACHE": True,
    "ONLY_TG_CACHED_BOOKS": False,
    "TG_SERIAL_DOWNLOAD": True,
    "TG_DOWNLOAD_INTERVAL_SECONDS": 5,
    # VPS 中继地址（HF Worker 外包模式专用，本机自跑留空）
    # TELEGRAM_API_BASE: HF 无法直连 api.telegram.org，经 VPS /tg-api/ 代理
    # YOUTUBE_OAUTH_BASE: HF 不持有 YouTube OAuth 凭证，经 VPS /yt-api/ 代理上传
    "TELEGRAM_API_BASE": "",
    "YOUTUBE_OAUTH_BASE": "",
    # 任务成功后自动清理 book_dir 中的中间文件（章节音频、降噪音频、MP4、封面等），
    # 仅保留 book_result.json 和 youtube_upload_receipt.json。
    # 断点续跑信息已存储在数据库中，成功后无需保留中间文件。
    "CLEANUP_INTERMEDIATE_FILES_AFTER_SUCCESS": True,
}


def apply_runtime_config(runtime_config: dict | None = None):
    """合并并应用运行配置到本模块全局（对应原文件行 81-93）。"""
    merged = dict(DEFAULT_RUNTIME_CONFIG)
    if runtime_config:
        merged.update(runtime_config)

    if not str(merged.get("PROJECT_FLAG", "") or "").strip():
        merged["PROJECT_FLAG"] = str(merged.get("YOUTUBE_CHANNEL_NAME", "") or "").strip()

    if not str(merged.get("MUSIC_DIR", "") or "").strip():
        merged["MUSIC_DIR"] = str(merged.get("LOCAL_MUSIC_DIR", "") or "").strip()

    globals().update(merged)
    return merged


def set_config(key, value):
    """回写某个配置项到本模块全局。

    对应原文件中大量 `globals()["KEY"] = ...` 的写回操作。
    """
    globals()[key] = value
    return value


def get_config(key, default=None):
    """读取某个配置项（等价于其它模块的 cfg.XXX，但支持动态 key）。"""
    return globals().get(key, default)


# ---------------------------------------------------------------------------
# 模块级占位：apply_runtime_config() 会把这些键写进本模块 globals。
# 此处显式声明一份，保证：
#   1) IDE / 类型检查器可见；
#   2) 其它模块顶层 `from . import config as cfg` 后 `cfg.MAX_RETRIES`
#      在 import 时即有值（原文件依赖 import 时求值的默认值，如函数签名默认值）。
# 注意：值必须与 DEFAULT_RUNTIME_CONFIG 的默认一致。
# ---------------------------------------------------------------------------
POSTGRES_DSN = ""
YOUTUBE_CHANNEL_NAME = ""
MAX_PROCESS_COUNT = 10
PROJECT_FLAG = ""
OUTPUT_ROOT = "/data/output"
TARGET_CATEGORY = "文学小说"
DOWNLOAD_WORKERS = 2
REQUEST_DELAY = 0.3
REQUEST_TIMEOUT = 300
MODELSCOPE_IMAGE_CONNECT_TIMEOUT = 300
MODELSCOPE_IMAGE_READ_TIMEOUT = 300
MODELSCOPE_IMAGE_POLL_CONNECT_TIMEOUT = 300
MODELSCOPE_IMAGE_POLL_READ_TIMEOUT = 300
MODELSCOPE_TOKEN_SWITCH_DELAY_SECONDS = 30
API_PRIORITY_ORDER = "modelscope,sensenova"
MAX_RETRIES = 3
AUDIO_DOWNLOAD_CONNECT_TIMEOUT = 20
AUDIO_DOWNLOAD_READ_TIMEOUT = 90
AUDIO_DOWNLOAD_MAX_RETRY_ATTEMPTS = 12
AUDIO_DOWNLOAD_MAX_TOTAL_SECONDS = 1800
AUDIO_DOWNLOAD_STUCK_LOG_INTERVAL_SECONDS = 30
SKIP_EXISTING = True
FORCE_REPROCESS = False
LONG_AUDIO_SPLIT_TRIGGER_HOURS = 12.0
LONG_AUDIO_PART_TARGET_HOURS = 11.8
BOOK_STATE_TABLE = "book_processing_states"
CLEANUP_COMPLETED_SPLIT_STATES = True
PRIORITIZE_INTERRUPTED_BOOKS = True
QUIET_RUNTIME_OUTPUT = True
ENABLE_DEEPFILTER = True
segment_duration_minutes = 60
DEEPFILTER_WORKERS = 1
ENABLE_COVER_GENERATION = True
CLOUD_RUNTIME_SETTINGS_TABLE = "channel_runtime_settings"
MODELSCOPE_TOKEN_TABLE = "modelscope_tokens"
MODELSCOPE_TOKEN = ""
ENABLE_SEO_GENERATION = True
ENABLE_YOUTUBE_UPLOAD = True
YOUTUBE_PRIVACY_STATUS = "schedule"
YOUTUBE_SCHEDULE_AFTER_HOURS = 24
YOUTUBE_DAILY_PUBLISH_LIMIT = 3
YOUTUBE_CATEGORY_ID = ""
YOUTUBE_DEFAULT_LANGUAGE = "zh-CN"
ENABLE_YOUTUBE_TRADITIONAL_LOCALIZATION = True
YOUTUBE_LOCALIZATION_LOCALES = "zh-TW,zh-HK,zh-SG,zh-Hant"
YOUTUBE_TRADITIONAL_LOCALE = "zh-TW"
YOUTUBE_TRADITIONAL_OPENCC_CONFIG = "s2t"
ENABLE_AUTO_INSTALL_OPENCC = True
APPEND_TAGS_TO_TITLE = False
APPEND_TAGS_TO_DESC = True
ENABLE_VIDEO_GENERATION = True
VIDEO_RESOLUTION = "1080p"
LOCAL_MUSIC_DIR = "/data/music"
ENABLE_BGM_MIX = True
MUSIC_DIR = "/data/music"
VOLUME_OFFSET_DB = -25
HIGHPASS_FREQ = 150
FADE_DURATION_MS = 3000
MIN_VOLUME_DB = -40
ENABLE_DYNAMIC_VOLUME = True
ENABLE_SPECTRAL_SHAPING = True
STEREO_OFFSET = 0.0
PIPELINE_TASK_ID = ""
TG_BOT_TOKEN = ""
TG_CHAT_ID = ""
ENABLE_TG_AUDIO_CACHE = True
ONLY_TG_CACHED_BOOKS = False
TG_SERIAL_DOWNLOAD = True
TG_DOWNLOAD_INTERVAL_SECONDS = 5
TELEGRAM_API_BASE = ""
YOUTUBE_OAUTH_BASE = ""
CLEANUP_INTERMEDIATE_FILES_AFTER_SUCCESS = True

# ---------------------------------------------------------------------------
# Podcast 运行配置（原文件行 8278-8298 由 _PODCAST_RUNTIME_DEFAULTS 二次注入）。
# 此处先以默认值占位；podcast.py 在 import 时会 DEFAULT_RUNTIME_CONFIG.update(...)
# 再 apply_runtime_config()，与原文件等价。
# ---------------------------------------------------------------------------
ENABLE_YOUTUBE_PODCAST_RUNTIME = True
ENABLE_YOUTUBE_PODCAST_UNIFIED_SHOW = True
ENABLE_YOUTUBE_PODCAST_SPLIT_PLAYLIST = True
YOUTUBE_PODCAST_SHOW_TITLE_TEMPLATE = "{channel_name}｜长篇有声书全集"
YOUTUBE_PODCAST_IMAGE_SIZE = 2048
YOUTUBE_PODCAST_IMAGE_MAX_BYTES = 2097152
YOUTUBE_PODCAST_SHOW_PLAYLIST_SETTING_KEY = "podcast_longform_show_playlist_id"
SENSENOVA_BASE_URL = "https://token.sensenova.cn/v1"
SENSENOVA_API_KEY = "sk-8Tr86c17YvA5jBEoem2uYYAQGXGzmpDU"
YOUTUBE_PODCAST_TEXT_MODEL_PRIMARY = "deepseek-v4-flash"
YOUTUBE_PODCAST_TEXT_MODEL_FALLBACK = "sensenova-6.7-flash-lite"
YOUTUBE_PODCAST_IMAGE_MODEL_PRIMARY = "sensenova-u1-fast"
YOUTUBE_PODCAST_TEXT_MODEL_RETRIES = 2
YOUTUBE_PODCAST_IMAGE_MODEL_RETRIES = 3
YOUTUBE_PODCAST_AI_RETRY_BASE_SECONDS = 30.0
YOUTUBE_PODCAST_YT_RETRIES = 5
YOUTUBE_PODCAST_YT_RETRY_BASE_SECONDS = 3.0
YOUTUBE_PODCAST_FONT_CACHE_DIRNAME = "_podcast_font_cache"

# ---------------------------------------------------------------------------
# normalize_runtime_source（原文件行 125-131）—— 纯工具函数，无外部依赖
# ---------------------------------------------------------------------------


# 首次加载默认配置（对应原文件行 96）
apply_runtime_config()

# ---------------------------------------------------------------------------
# 运行配置快照（原文件行 2974-3019）
# ---------------------------------------------------------------------------
def collect_runtime_config_snapshot():
    return {
        "database_backend": "postgresql",
        "postgres_dsn_configured": bool(str(getattr(sys.modules[__name__], "POSTGRES_DSN", "") or "").strip()),
        "project_flag": getattr(sys.modules[__name__], "PROJECT_FLAG", ""),
        "target_category": getattr(sys.modules[__name__], "TARGET_CATEGORY", ""),
        "max_process_count": getattr(sys.modules[__name__], "MAX_PROCESS_COUNT", 10),
                "long_audio_split_trigger_hours": getattr(sys.modules[__name__], "LONG_AUDIO_SPLIT_TRIGGER_HOURS", 12.0),
        "long_audio_part_target_hours": getattr(sys.modules[__name__], "LONG_AUDIO_PART_TARGET_HOURS", 11.8),
        "book_state_table": getattr(sys.modules[__name__], "BOOK_STATE_TABLE", "book_processing_states"),
        "prioritize_interrupted_books": getattr(sys.modules[__name__], "PRIORITIZE_INTERRUPTED_BOOKS", True),
        "output_root": getattr(sys.modules[__name__], "OUTPUT_ROOT", "/data/output"),
        "download_workers": getattr(sys.modules[__name__], "DOWNLOAD_WORKERS", 4),
        "audio_download_connect_timeout": getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_CONNECT_TIMEOUT", 20),
        "audio_download_read_timeout": getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_READ_TIMEOUT", 90),
        "audio_download_max_retry_attempts": getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_MAX_RETRY_ATTEMPTS", 12),
        "audio_download_max_total_seconds": getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_MAX_TOTAL_SECONDS", 1800),
        "audio_download_stuck_log_interval_seconds": getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_STUCK_LOG_INTERVAL_SECONDS", 30),
        "enable_deepfilter": getattr(sys.modules[__name__], "ENABLE_DEEPFILTER", True),
        "deepfilter_workers": getattr(sys.modules[__name__], "DEEPFILTER_WORKERS", 2),
        "enable_bgm_mix": getattr(sys.modules[__name__], "ENABLE_BGM_MIX", True),
        "music_dir": getattr(sys.modules[__name__], "MUSIC_DIR", "/data/music"),
        "enable_cover_generation": getattr(sys.modules[__name__], "ENABLE_COVER_GENERATION", True),
        "cloud_runtime_settings_table": getattr(sys.modules[__name__], "CLOUD_RUNTIME_SETTINGS_TABLE", "channel_runtime_settings"),
        "modelscope_token_table": getattr(sys.modules[__name__], "MODELSCOPE_TOKEN_TABLE", "modelscope_tokens"),
        "modelscope_image_connect_timeout": getattr(sys.modules[__name__], "MODELSCOPE_IMAGE_CONNECT_TIMEOUT", 300),
        "modelscope_image_read_timeout": getattr(sys.modules[__name__], "MODELSCOPE_IMAGE_READ_TIMEOUT", 300),
        "modelscope_image_poll_connect_timeout": getattr(sys.modules[__name__], "MODELSCOPE_IMAGE_POLL_CONNECT_TIMEOUT", 300),
        "modelscope_image_poll_read_timeout": getattr(sys.modules[__name__], "MODELSCOPE_IMAGE_POLL_READ_TIMEOUT", 300),
        "modelscope_token_switch_delay_seconds": getattr(sys.modules[__name__], "MODELSCOPE_TOKEN_SWITCH_DELAY_SECONDS", 30),
        "enable_seo_generation": getattr(sys.modules[__name__], "ENABLE_SEO_GENERATION", True),
        "enable_video_generation": getattr(sys.modules[__name__], "ENABLE_VIDEO_GENERATION", True),
        "enable_youtube_upload": getattr(sys.modules[__name__], "ENABLE_YOUTUBE_UPLOAD", True),
        "youtube_channel_name": getattr(sys.modules[__name__], "YOUTUBE_CHANNEL_NAME", ""),
        "youtube_privacy_status": getattr(sys.modules[__name__], "YOUTUBE_PRIVACY_STATUS", "schedule"),
        "youtube_schedule_after_hours": getattr(sys.modules[__name__], "YOUTUBE_SCHEDULE_AFTER_HOURS", 24),
        "youtube_schedule_local_timezone": "Asia/Shanghai",
        "youtube_daily_publish_limit": getattr(sys.modules[__name__], "YOUTUBE_DAILY_PUBLISH_LIMIT", 3),
    }


# ---------------------------------------------------------------------------
# 运行配置校验（原文件行 3120-3271）
# 注意：函数内 lazy-import runtime.log 以避免 config→runtime 循环依赖。
# ---------------------------------------------------------------------------
def validate_runtime_config():
    from .runtime import log as _vlog

    errors = []
    warnings = []
    ai_features_enabled = bool(getattr(sys.modules[__name__], "ENABLE_COVER_GENERATION", True)
                               or getattr(sys.modules[__name__], "ENABLE_SEO_GENERATION", True))
    local_modelscope_token = str(getattr(sys.modules[__name__], "MODELSCOPE_TOKEN", "") or "").strip()

    if not str(getattr(sys.modules[__name__], "POSTGRES_DSN", "") or "").strip():
        errors.append("POSTGRES_DSN 为空")
    if not str(getattr(sys.modules[__name__], "OUTPUT_ROOT", "")).strip():
        errors.append("OUTPUT_ROOT 为空")
    if not str(getattr(sys.modules[__name__], "BOOK_STATE_TABLE", "")).strip():
        errors.append("BOOK_STATE_TABLE 为空")
    if not str(getattr(sys.modules[__name__], "CLOUD_RUNTIME_SETTINGS_TABLE", "")).strip():
        errors.append("CLOUD_RUNTIME_SETTINGS_TABLE 为空")
    try:
        split_trigger_hours = float(getattr(sys.modules[__name__], "LONG_AUDIO_SPLIT_TRIGGER_HOURS", 12.0) or 12.0)
    except Exception:
        split_trigger_hours = 12.0
    try:
        part_target_hours = float(getattr(sys.modules[__name__], "LONG_AUDIO_PART_TARGET_HOURS", 11.8) or 11.8)
    except Exception:
        part_target_hours = 11.8
    if split_trigger_hours <= 0:
        errors.append("LONG_AUDIO_SPLIT_TRIGGER_HOURS 必须大于 0")
    if part_target_hours <= 0:
        errors.append("LONG_AUDIO_PART_TARGET_HOURS 必须大于 0")
    if part_target_hours > split_trigger_hours:
        warnings.append("LONG_AUDIO_PART_TARGET_HOURS 大于触发阈值，建议设成略小于 12 小时更稳")
    if bool(getattr(sys.modules[__name__], "ENABLE_YOUTUBE_UPLOAD", True)) and not str(
        getattr(sys.modules[__name__], "YOUTUBE_CHANNEL_NAME", "") or ""
    ).strip():
        errors.append("已开启 YouTube 上传，但 YOUTUBE_CHANNEL_NAME 为空")
    try:
        audio_connect_timeout = int(getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_CONNECT_TIMEOUT", 0) or 0)
    except Exception:
        audio_connect_timeout = 0
    try:
        audio_read_timeout = int(getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_READ_TIMEOUT", 0) or 0)
    except Exception:
        audio_read_timeout = 0
    try:
        audio_max_attempts = int(getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_MAX_RETRY_ATTEMPTS", 0) or 0)
    except Exception:
        audio_max_attempts = 0
    try:
        audio_max_total_seconds = int(getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_MAX_TOTAL_SECONDS", 0) or 0)
    except Exception:
        audio_max_total_seconds = 0
    try:
        audio_stuck_log_interval = int(getattr(sys.modules[__name__], "AUDIO_DOWNLOAD_STUCK_LOG_INTERVAL_SECONDS", 0) or 0)
    except Exception:
        audio_stuck_log_interval = 0
    try:
        modelscope_image_connect_timeout = int(getattr(sys.modules[__name__], "MODELSCOPE_IMAGE_CONNECT_TIMEOUT", 0) or 0)
    except Exception:
        modelscope_image_connect_timeout = 0
    try:
        modelscope_image_read_timeout = int(getattr(sys.modules[__name__], "MODELSCOPE_IMAGE_READ_TIMEOUT", 0) or 0)
    except Exception:
        modelscope_image_read_timeout = 0
    try:
        modelscope_image_poll_connect_timeout = int(getattr(sys.modules[__name__], "MODELSCOPE_IMAGE_POLL_CONNECT_TIMEOUT", 0) or 0)
    except Exception:
        modelscope_image_poll_connect_timeout = 0
    try:
        modelscope_image_poll_read_timeout = int(getattr(sys.modules[__name__], "MODELSCOPE_IMAGE_POLL_READ_TIMEOUT", 0) or 0)
    except Exception:
        modelscope_image_poll_read_timeout = 0
    if audio_connect_timeout <= 0:
        errors.append("AUDIO_DOWNLOAD_CONNECT_TIMEOUT 必须大于 0")
    if audio_read_timeout <= 0:
        errors.append("AUDIO_DOWNLOAD_READ_TIMEOUT 必须大于 0")
    if audio_max_attempts <= 0:
        errors.append("AUDIO_DOWNLOAD_MAX_RETRY_ATTEMPTS 必须大于 0")
    if audio_max_total_seconds <= 0:
        errors.append("AUDIO_DOWNLOAD_MAX_TOTAL_SECONDS 必须大于 0")
    if audio_stuck_log_interval <= 0:
        errors.append("AUDIO_DOWNLOAD_STUCK_LOG_INTERVAL_SECONDS 必须大于 0")
    if modelscope_image_connect_timeout <= 0:
        errors.append("MODELSCOPE_IMAGE_CONNECT_TIMEOUT 必须大于 0")
    if modelscope_image_read_timeout <= 0:
        errors.append("MODELSCOPE_IMAGE_READ_TIMEOUT 必须大于 0")
    if modelscope_image_poll_connect_timeout <= 0:
        errors.append("MODELSCOPE_IMAGE_POLL_CONNECT_TIMEOUT 必须大于 0")
    if modelscope_image_poll_read_timeout <= 0:
        errors.append("MODELSCOPE_IMAGE_POLL_READ_TIMEOUT 必须大于 0")
    # 音乐下载已移至部署脚本 + entrypoint.sh，不再需要 pipeline 验证
    import os as _vos
    if bool(getattr(sys.modules[__name__], "ENABLE_BGM_MIX", True)):
        music_dir = str(getattr(sys.modules[__name__], "MUSIC_DIR", "")).strip()
        if not music_dir or not _vos.path.exists(music_dir):
            warnings.append("已开启 BGM 混音，但本地 MUSIC_DIR 不存在；混音阶段会跳过")
    if ai_features_enabled:
        
        if not str(getattr(sys.modules[__name__], "MODELSCOPE_TOKEN_TABLE", "")).strip():
            errors.append("启用 AI 生成时，MODELSCOPE_TOKEN_TABLE 不能为空")
        
        if not local_modelscope_token:
            warnings.append(
                "MODELSCOPE_TOKEN 为空，AI 封面/SEO 生成将无法进行；"
                "请在 Web 管理面板 → 全局设置 中配置"
            )
    if str(getattr(sys.modules[__name__], "YOUTUBE_PRIVACY_STATUS", "")).strip().lower() == "schedule":
        try:
            hours = int(getattr(sys.modules[__name__], "YOUTUBE_SCHEDULE_AFTER_HOURS", 0) or 0)
        except Exception:
            hours = 0
        if hours <= 0:
            warnings.append("YOUTUBE_PRIVACY_STATUS=schedule 但预约小时数不大于 0，将回退到最小值 1")

    for msg in warnings:
        _vlog.warning("配置提醒：%s", msg)

    if errors:
        raise ValueError("；".join(errors))

    _vlog.info("✅ 运行配置校验通过")
