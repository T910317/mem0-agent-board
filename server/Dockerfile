FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py experience_store.py store.py entrypoint.sh ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8890
CMD ["sh", "/app/entrypoint.sh"]
