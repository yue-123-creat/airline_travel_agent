FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY contracts ./contracts
COPY scripts ./scripts
COPY data/knowledge ./data/knowledge
# app/static/chat.html 随 app 复制，供 /chat 会话预览（小程序对接同一 /v1/chat/stream）

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
