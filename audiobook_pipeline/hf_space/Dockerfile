FROM python:3.11-slim

ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata ffmpeg wget ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN wget -q -O /opt/deep-filter \
    https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl \
    && chmod +x /opt/deep-filter

COPY --chown=user:user worker.py /app/worker.py
COPY --chown=user:user app.py /app/app.py
WORKDIR /app

RUN chmod +x /opt/deep-filter && \
    chown user:user /opt/deep-filter && \
    mkdir -p /tmp/audiobook_temp && \
    chown user:user /tmp/audiobook_temp

USER user

ENV DEEP_FILTER_BIN=/opt/deep-filter \
    TEMP_DIR=/tmp/audiobook_temp \
    PORT=7860 \
    NUM_SLOTS=2 \
    BOT_MIN_INTERVAL=3 \
    MAX_RETRIES=5 \
    MAX_CHAPTERS=0

EXPOSE 7860

CMD ["python", "app.py"]
