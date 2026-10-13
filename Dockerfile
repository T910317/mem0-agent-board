FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.9,<2" httpx uvicorn
COPY server.py .
ENV PYTHONUNBUFFERED=1
EXPOSE 8890
CMD ["python", "server.py"]
