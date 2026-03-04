FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt /tmp/backend-requirements.txt
RUN pip install --no-cache-dir -r /tmp/backend-requirements.txt

COPY backend ./backend
COPY ai ./ai
COPY llms.txt ./llms.txt
COPY recipe.md ./recipe.md

EXPOSE 8000

CMD uvicorn backend.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}
