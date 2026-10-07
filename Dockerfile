# syntax=docker/dockerfile:1
#
# Transcript service image: FastAPI API + built React SPA in one container.
# Production runs exactly one instance with one Uvicorn worker behind nginx
# (docker-compose.yml); background jobs live in this process, so never scale it out.

# ---------------------------------------------------------------------------
# Frontend build
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg lets yt-dlp repair/remux downloaded audio before speech-to-text.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# requirements.lock pins the exact, tested versions (regenerate after editing requirements.txt:
#   uv pip compile requirements.txt --universal --python-version 3.12 -o requirements.lock)
COPY requirements.lock ./
RUN pip install -r requirements.lock

# Application code only (no tests, docs, .env or local data; see also .dockerignore).
COPY --chown=app:app api/ api/
COPY --chown=app:app clients/ clients/
COPY --chown=app:app config/ config/
COPY --chown=app:app exceptions/ exceptions/
COPY --chown=app:app infrastructure/ infrastructure/
COPY --chown=app:app interfaces/ interfaces/
COPY --chown=app:app models/ models/
COPY --chown=app:app pipeline/ pipeline/
COPY --chown=app:app providers/ providers/
COPY --chown=app:app repositories/ repositories/
COPY --chown=app:app security/ security/
COPY --chown=app:app services/ services/
COPY --chown=app:app utils/ utils/
COPY --chown=app:app validators/ validators/
COPY --chown=app:app webapp/ webapp/
COPY --chown=app:app scripts/healthcheck.py scripts/healthcheck.py
COPY --from=frontend --chown=app:app /build/dist frontend/dist

# /app/data is the persistent volume (jobs, transcript cache). Created here so a fresh
# named volume inherits app ownership.
RUN mkdir -p /app/data && chown app:app /app/data

ENV APP_ENV=production \
    DATA_DIR=/app/data \
    LOG_TO_FILE=false \
    LOG_FORMAT=json \
    HOME=/app \
    XDG_CACHE_HOME=/tmp/.cache

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

# One worker, no reload. Listens on all interfaces *inside* the container only;
# docker-compose.yml does not publish this port, so it is reachable only via nginx.
CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--no-server-header", "--timeout-graceful-shutdown", "30"]
