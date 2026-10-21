# 圓桌轉送站（大腦）。docker compose --profile relay up -d --build 會建這個映像；單獨跑見 README.md。
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir httpx==0.28.1
COPY relay.py ./
ENV PYTHONUNBUFFERED=1 \
    RELAY_BIND=0.0.0.0 \
    RELAY_PORT=8891 \
    RELAY_DATA=/data/relay \
    ROUTES_FILE=/data/ui/session-routes.json \
    POLL_SECONDS=30
EXPOSE 8891
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8891/health', timeout=4)"]
CMD ["python", "relay.py"]
