# ═══════════════════════════════════════════════════════════
# 测试 Worker Dockerfile — Hugging Face Space 部署
# ═══════════════════════════════════════════════════════════
# 在 HF Space 上运行 4 类测试实验（AI生成 / YouTube上传 / TG下载 / BGM混音），
# 复用项目 pipeline/ 逻辑，配置从 VPS 中继拉取。
#
# 构建上下文：项目根目录
#   docker build -f hf_workers/test_worker/Dockerfile -t hf-test-worker .
# ═══════════════════════════════════════════════════════════

FROM python:3.11-slim

# ─── 系统依赖 ───
# ffmpeg: 音频处理 + 视频封装
# libpq5: psycopg 运行时依赖
# libsndfile1: librosa 读取音频文件
RUN apt-get update -o Acquire::Retries=5 -o Acquire::Timeout=30 \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        libpq5 \
        libsndfile1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── Python 依赖 ───
COPY hf_workers/test_worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ─── 复制 pipeline 包（复用本机自跑的全部逻辑）───
COPY pipeline/ /app/pipeline/

# ─── 复制 Worker 应用 ───
COPY hf_workers/test_worker/app.py /app/app.py
COPY hf_workers/test_worker/runner.py /app/runner.py

# ─── 输出目录 + 音乐目录 ───
RUN mkdir -p /tmp/output /data/music

# ─── 环境变量 ───
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    PORT=7860 \
    OUTPUT_ROOT=/tmp/output \
    MUSIC_DIR=/data/music

EXPOSE 7860

# ─── 健康检查 ───
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
    CMD curl -f http://localhost:7860/health || exit 1

CMD ["python", "app.py"]
