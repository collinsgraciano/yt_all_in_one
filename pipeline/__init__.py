"""pipeline 包 — 有声书处理流水线的核心运行库。

导入顺序严格匹配原 runtime_core.py 的模块级执行顺序：
  1. config      → apply_runtime_config() 注入默认全局
  2. runtime     → log / 工具就绪
  3. db          → PostgreSQL 操作就绪
  4. state       → 断点续跑状态管理
  5. audio       → 音频下载/合并/时长
  6. deepfilter  → 模块级 if ENABLE_DEEPFILTER: setup_deep_filter()
  7. bgm         → 信号处理（numpy/scipy）
  8. music_download → 版权音乐
  9. cover       → AI 封面（ModelScope token 池）
  10. seo        → SEO 文案
  11. youtube    → YouTube API / 上传 / 视频编码
  12. podcast    → 二次 apply_runtime_config + monkey-patch 覆盖
  13. pipeline   → 主流程 run_pipeline
"""

# ── Layer 0 ──
from . import config
from . import runtime

# ── Layer 1 ──
from . import db
from . import state
from . import audio
from . import deepfilter
from . import bgm
from . import music_download
from . import cover
from . import seo

# ── Layer 2 ──
from . import youtube

# ── Layer 3: podcast 必须在 pipeline 之前导入，以触发 monkey-patch ──
from . import podcast

# ── Layer 4: 主流程编排 ──
from . import pipeline

# ── 安装 podcast monkey-patch：覆盖 process_standard_book / sync_split_playlist /
#    sync_result_from_split_state / finalize_book_result 以接入 Podcast Show 同步 ──
podcast._podcast_install_monkey_patches()

# ── 公开 API ──
from .pipeline import run_pipeline
from .config import apply_runtime_config
from .db import close_pool

__all__ = [
    "run_pipeline",
    "apply_runtime_config",
    "close_pool",
]