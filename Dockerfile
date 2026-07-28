# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# No .pyc, unbuffered logs (so JSON log lines reach docker logs immediately).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependencies first so a code-only change doesn't reinstall the world.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Run unprivileged.
RUN useradd --create-home --uid 10001 wrapper \
    && chown -R wrapper:wrapper /app
USER wrapper

# Documentational only — the published port comes from ${PORT} in compose.
EXPOSE 8000

# Liveness without adding curl to the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4).status==200 else 1)"]

# Shell form so ${PORT} expands at RUNTIME (not build time); exec so uvicorn is PID 1
# and receives SIGTERM directly for a clean shutdown.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
