# ═══════════════════════════════════════════════════════════
# 统一 Worker Dockerfile — Hugging Face Space 部署
# ═══════════════════════════════════════════════════════════
# 合并流水线 Worker + 测试 Worker，通过双槽位组隔离资源：
#   pipeline_slots: 队列认领，重型任务（TG下载→混音→AI→封装→上传）
#   test_slots:     同步执行，轻量测试（AI/上传/TG下载/BGM混音）
#
# HF Space 约定：
#   - 监听 0.0.0.0:7860
#   - 持久目录 /data（BGM 音乐缓存）
#   - 临时输出 /tmp/output（HF 重启后清空，中间文件无需持久化）
#
# 构建上下文：项目根目录
#   docker build -f hf_workers/unified_worker/Dockerfile -t hf-unified-worker .
# ═══════════════════════════════════════════════════════════

FROM python:3.11-slim

# ─── 系统依赖 ───
# ffmpeg: 视频封装 + 音频处理
# wget/curl: DeepFilter 下载
# libpq5: psycopg 运行时依赖
# libsndfile1: librosa 读取音频文件
RUN apt-get update -o Acquire::Retries=5 -o Acquire::Timeout=30 \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        wget \
        curl \
        libpq5 \
        libsndfile1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── Python 依赖 ───
COPY hf_workers/unified_worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ─── 复制 pipeline 包（复用本机自跑的全部逻辑）───
COPY pipeline/ /app/pipeline/

# ─── 复制统一 Worker 应用 ───
COPY hf_workers/unified_worker/app.py /app/app.py
COPY hf_workers/unified_worker/runner.py /app/runner.py

# ─── DeepFilter 二进制 ───
# TG 缓存模式默认 ENABLE_DEEPFILTER=False，但作为断点续跑兜底。
RUN mkdir -p /opt/deepfilter \
    && wget -q -O /opt/deepfilter/deep-filter-0.5.6-x86_64-unknown-linux-musl \
        "https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl" \
    && chmod +x /opt/deepfilter/deep-filter-0.5.6-x86_64-unknown-linux-musl \
    && echo "DeepFilter 下载完成" \
    || echo "⚠️ DeepFilter 下载失败（TG缓存模式不受影响）"

# ─── 输出目录 + 音乐目录 ───
RUN mkdir -p /tmp/output /data/music

# ─── 环境变量 ───
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    PORT=7860 \
    OUTPUT_ROOT=/tmp/output \
    MUSIC_DIR=/data/music \
    PIPELINE_SLOTS=1 \
    TEST_SLOTS=1 \
    DEEP_FILTER_PATH=/opt/deepfilter/deep-filter-0.5.6-x86_64-unknown-linux-musl

EXPOSE 7860

# ─── 健康检查 ───
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
    CMD curl -f http://localhost:7860/health || exit 1

CMD ["python", "app.py"]
