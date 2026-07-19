# ═══════════════════════════════════════════════════════════
# 流水线 Worker Dockerfile — Hugging Face Space 部署
# ═══════════════════════════════════════════════════════════
# 作为「远程 Pipeline 执行器」：把项目 pipeline/ 打包进镜像，
# 通过 Flask API 触发，调用 process_book() 执行单书完整流水线：
#   TG下载 → BGM混音 → AI封面 → SEO文案 → MP4封装 → YouTube上传（经VPS中继）
#
# HF Space 约定：
#   - 监听 0.0.0.0:7860
#   - 持久目录 /data（HF 提供的持久存储，用于 music 缓存）
#   - 临时输出 /tmp/output（HF 重启后清空，中间文件无需持久化）
#
# 构建上下文：项目根目录
#   docker build -f hf_workers/pipeline_worker/Dockerfile -t hf-pipeline-worker .
# ═══════════════════════════════════════════════════════════

FROM python:3.11-slim

# ─── 系统依赖 ───
# ffmpeg: 视频封装 + 音频处理
# wget/curl: DeepFilter 下载
# libpq5: psycopg 运行时依赖
RUN apt-get update -o Acquire::Retries=5 -o Acquire::Timeout=30 \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        wget \
        curl \
        libpq5 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── Python 依赖 ───
COPY hf_workers/pipeline_worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ─── 复制 pipeline 包（远程执行器核心：复用本机自跑的全部逻辑）───
COPY pipeline/ /app/pipeline/

# ─── 复制 Worker 应用 ───
COPY hf_workers/pipeline_worker/app.py /app/app.py

# ─── DeepFilter 二进制 ───
# TG 缓存模式默认 ENABLE_DEEPFILTER=False（跳过降噪，直接用已降噪的 TG 缓存），
# 但仍下载 DeepFilter 作为兜底（断点续跑可能遇到未缓存章节需要原始下载+降噪）。
# HF 无持久卷，构建时下载到镜像内。
RUN mkdir -p /opt/deepfilter \
    && wget -q -O /opt/deepfilter/deep-filter-0.5.6-x86_64-unknown-linux-musl \
        "https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl" \
    && chmod +x /opt/deepfilter/deep-filter-0.5.6-x86_64-unknown-linux-musl \
    && echo "DeepFilter 下载完成" \
    || echo "⚠️ DeepFilter 下载失败（TG缓存模式不受影响）"

# ─── 输出目录 + 音乐目录 ───
# /tmp/output: 中间文件（HF 重启清空，任务完成后自动清理）
# /data/music: BGM 音乐池（HF 持久存储，首次需从 VPS 同步或手动上传）
RUN mkdir -p /tmp/output /data/music

# ─── 环境变量 ───
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    PORT=7860 \
    OUTPUT_ROOT=/tmp/output \
    MUSIC_DIR=/data/music \
    DEEP_FILTER_PATH=/opt/deepfilter/deep-filter-0.5.6-x86_64-unknown-linux-musl

EXPOSE 7860

# ─── 健康检查 ───
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
    CMD curl -f http://localhost:7860/health || exit 1

CMD ["python", "app.py"]
