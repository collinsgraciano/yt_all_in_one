# ============================================================
# Web 管理面板 - Dockerfile
# ============================================================

FROM python:3.12-slim

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app.py /app/app.py
COPY templates/ /app/templates/
WORKDIR /app

EXPOSE 5000

CMD ["python", "app.py"]
