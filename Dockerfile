# syntax=docker/dockerfile:1
#
# Transcript service image: FastAPI API + built React SPA in one container.
# Production runs exactly one instance with one Uvicorn worker behind nginx
# (docker-compose.yml); background jobs live in this process, so never scale it out.

# ---------------------------------------------------------------------------
# Frontend build
# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend

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
# Owned by root (read-only to the app user), so a compromised process cannot rewrite its
# own code; compose also mounts the root filesystem read-only.
COPY api/ api/
COPY clients/ clients/
COPY config/ config/
COPY exceptions/ exceptions/
COPY infrastructure/ infrastructure/
COPY interfaces/ interfaces/
COPY models/ models/
COPY pipeline/ pipeline/
COPY providers/ providers/
COPY repositories/ repositories/
COPY security/ security/
COPY services/ services/
COPY utils/ utils/
COPY validators/ validators/
COPY webapp/ webapp/
COPY scripts/healthcheck.py scripts/healthcheck.py
COPY --from=frontend /build/dist frontend/dist

# /app/data is the persistent volume (jobs, transcript cache, speech-to-text audio under
# data/tmp) and the only app-owned path. Created here so a fresh named volume inherits
# app ownership. Everything else the app writes goes to the /tmp tmpfs (XDG_CACHE_HOME).
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
