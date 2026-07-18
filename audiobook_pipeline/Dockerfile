# ============================================================
# 有声书项目 - Python 工具容器 (重构版)
# 用于运行 migrate.py (PG→PG 迁移) 和 cleanup.py
# 包含: ffmpeg + DeepFilter 降噪 + Python 依赖
# 不再需要 DuckDB CLI
# ============================================================

FROM python:3.11-slim

WORKDIR /app

ENV TZ=Asia/Shanghai
ENV MALLOC_ARENA_MAX=2
ENV JE_MALLOC_CONF=narenas:2

# 安装系统依赖: ffmpeg + curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        ffmpeg \
        curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 下载 DeepFilter
ENV DEEP_FILTER_URL=https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl
RUN curl -L -o /app/deep-filter "$DEEP_FILTER_URL" \
    && chmod +x /app/deep-filter \
    && /app/deep-filter --help 2>/dev/null || true

# 安装 Python 依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制脚本
COPY migrate.py /app/migrate.py
COPY cleanup.py /app/cleanup.py

CMD ["python3", "migrate.py"]
