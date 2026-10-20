# Mem0 官方 REST 伺服器（mem0ai/mem0 的 server/），固定在測過的版本，再套兩個小修改（見 patch_mem0.py）。
FROM python:3.12-slim
ARG MEM0_COMMIT=d26ba5e4518cef7fff3c66d4f715b21adf9feabe
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git init -q /src && cd /src \
 && git remote add origin https://github.com/mem0ai/mem0.git \
 && git fetch -q --depth 1 origin "$MEM0_COMMIT" && git checkout -q FETCH_HEAD \
 && mkdir /app && cp -r server/. /app/ && rm -rf /src
WORKDIR /app
COPY constraints.txt patch_mem0.py ./
RUN python patch_mem0.py main.py requirements.txt \
 && pip install --no-cache-dir -r requirements.txt -c constraints.txt \
 && rm patch_mem0.py
ENV PYTHONUNBUFFERED=1 MEM0_TELEMETRY=false
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000"]
