# migrate-tg-chapters/Dockerfile
# 轻量级迁移工具镜像 — 仅需 Python + psycopg
FROM python:3.11-slim

RUN apt-get update -o Acquire::Retries=5 -o Acquire::Timeout=30 \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "psycopg[binary]==3.1.18"

WORKDIR /app
COPY migrate_tg_chapters.py /app/migrate_tg_chapters.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/migrate_tg_chapters.py"]
CMD ["--help"]
