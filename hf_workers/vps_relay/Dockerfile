# VPS 中继调度器 Dockerfile
# 与当前项目同一 VPS 部署，连接同一个 PostgreSQL

FROM python:3.11-slim

# 系统依赖（ffmpeg 用于封面处理等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app.py .

# 数据卷（relay_config.json + oauth_tokens）
RUN mkdir -p /data
VOLUME ["/data"]

ENV WEB_PORT=38080
ENV PYTHONUNBUFFERED=1

EXPOSE 38080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:38080/api/status || exit 1

CMD ["python", "app.py"]
