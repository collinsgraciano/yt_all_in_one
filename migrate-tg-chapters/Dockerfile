# migrate-tg-chapters/Dockerfile
# 轻量级迁移工具镜像 — 仅需 Python + psycopg
# 支持两表迁移: books + audiobook_chapters
FROM python:3.11-slim

RUN apt-get update -o Acquire::Retries=5 -o Acquire::Timeout=30 \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "psycopg[binary]==3.1.18"

WORKDIR /app

# 拷贝两个迁移脚本
COPY migrate_tg_chapters.py /app/migrate_tg_chapters.py
COPY migrate_books.py /app/migrate_books.py

ENV PYTHONUNBUFFERED=1

# 不设 ENTRYPOINT，run.sh 通过 command 直接指定要运行的命令
# 单脚本:  docker-compose run --rm migrate-tg python /app/migrate_books.py
# 多脚本:  docker-compose run --rm migrate-tg bash -c "python ... && python ..."
CMD ["python", "/app/migrate_tg_chapters.py", "--help"]
