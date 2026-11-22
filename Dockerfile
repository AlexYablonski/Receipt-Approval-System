FROM python:3.13-slim

# No .pyc files in the image; print logs immediately instead of buffering them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Unprivileged user (uid 1000). It can write only to /app/data, so the code itself stays read-only to it.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data

# Dependencies before app code: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Migration tooling, so "alembic upgrade head" can run when the container starts (see docker-compose.yml).
COPY alembic.ini ./
COPY alembic ./alembic

USER appuser
EXPOSE 8000

# On every start: bring the database up to date, then run the server. "exec" makes uvicorn the
# main process, so "docker compose stop" reaches it directly. 0.0.0.0 so the port is reachable
# from outside the container.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
