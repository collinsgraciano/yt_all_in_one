# ============================================================
# VPS 调度器 - Web 管理面板 + 后台调度器
# 极轻量: 仅 Flask + PG 查询 + HTTP 请求
# ============================================================

FROM python:3.12-slim

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY scheduler.py /app/scheduler.py
COPY web_app.py /app/web_app.py
WORKDIR /app

EXPOSE 38080

CMD ["python", "web_app.py"]
