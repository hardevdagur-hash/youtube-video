"""
YouTube CSV Export â€” FastAPI Application (v3)

Architecture
============
- Exports run as background threads managed by JobManager.
- POST /api/export returns immediately with a job_id.
- Frontend polls GET /api/export/{job_id}/progress for real-time updates.
- Supports cancellation via POST /api/export/{job_id}/cancel.
- Redis/in-memory caching for channel lookups and playlist IDs.
- Streaming CSV writer with constant memory usage.
- Structured JSON logging with correlation IDs.
- Metrics collection for monitoring.

Endpoints
=========
GET    /api/health                          Health check
POST   /api/export                          Start export (returns job_id)
GET    /api/export/{job_id}/progress        Poll export progress
GET    /api/export/{job_id}/result          Get export result
GET    /api/export/{job_id}/download        Download CSV
POST   /api/export/{job_id}/cancel          Cancel export
DELETE /api/export/{job_id}                 Delete export and cleanup
GET    /api/export/jobs/active              List active jobs
GET    /api/export/jobs/history             List completed jobs
GET    /api/metrics                         System metrics
GET    /api/cache/stats                     Cache statistics
GET    /api/quota                           YouTube API quota status
GET    /api/validate-url                    Validate YouTube URL
GET    /api/video-metadata/{video_id}       Get single video metadata
GET    /api/transcript/{video_id}           Get transcript
(plus all existing transcript and metadata export endpoints)
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config.settings import is_youtube_api_key_valid, settings
from export_engine.models import ExportRequest
from export_engine.async_pipeline import AsyncExportPipeline
from infrastructure.logging import setup_logging
from infrastructure.monitoring import metrics
from infrastructure.rate_limiter import quota_tracker, rate_limiter
from infrastructure.validation import RequestValidationMiddleware
from models.api_response import error_response, success_response
from services.english_converter import english_converter

from observability.config import ObservabilityConfig
from observability.instrumentation import instrument_fastapi
from observability.telemetry import TelemetryOrchestrator
from security.web_auth import (
    SESSION_COOKIE,
    AuthMiddleware,
    AuthSettings,
    Principal,
    WebAuthenticator,
    can_access_owned,
    cors_options,
)
from services.english_converter import english_converter

setup_logging()

CURRENT_DIR = Path(__file__).resolve().parent
RUNS_DIR = CURRENT_DIR / "runs"
TEMPLATES_DIR = CURRENT_DIR / "templates"
STATIC_DIR = CURRENT_DIR / "static"
FRONTEND_DIST = CURRENT_DIR.parent / "frontend" / "dist"

logger = logging.getLogger("webapp")

_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")

_export_path = Path(__file__).resolve().parent.parent
import sys as _sys
if str(_export_path) not in _sys.path:
    _sys.path.insert(0, str(_export_path))


def _safe_run_id(raw: str) -> str | None:
    if _RUN_ID_PATTERN.match(raw):
        return raw
    return None


# ---------------------------------------------------------------------------
# Global services
# ---------------------------------------------------------------------------

_async_pipeline = AsyncExportPipeline()
_running_tasks: dict[str, asyncio.Task] = {}
_running_tasks_lock = asyncio.Lock()


async def _run_async_pipeline(job_id: str, request: ExportRequest) -> None:
    try:
        await _async_pipeline.run(job_id, request)
    except asyncio.CancelledError:
        logger.info("Job %s was cancelled", job_id)
        await _async_pipeline.cancel_job(job_id)
    except Exception:
        logger.exception("Job %s failed with unexpected error", job_id)
    finally:
        async with _running_tasks_lock:
            _running_tasks.pop(job_id, None)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------



def _to_thread(func, *args, **kwargs):
    """Run a sync function in a thread pool to avoid blocking the event loop."""
    import asyncio
    loop = asyncio.get_running_loop()
    import functools
    return loop.run_in_executor(None, functools.partial(func, *args, **kwargs))



@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Async webapp v3 starting")
    logger.info("Open http://localhost:8000 in your browser")

    # Initialize observability (structured logging, tracing, metrics, Sentry)
    _telemetry.initialize()

    # Clean stale run dirs
    if RUNS_DIR.exists():
        stale_cutoff = time.time() - 3600
        for entry in RUNS_DIR.iterdir():
            if entry.is_dir() and _RUN_ID_PATTERN.match(entry.name):
                result_file = entry / "result.json"
                if not result_file.exists() and entry.stat().st_mtime < stale_cutoff:
                    shutil.rmtree(str(entry), ignore_errors=True)

    yield

    # Cleanup running tasks on shutdown
    async with _running_tasks_lock:
        for task in _running_tasks.values():
            task.cancel()
        _running_tasks.clear()
    await _async_pipeline.close()

    # Shutdown observability
    _telemetry.shutdown()


# Security configuration: fails fast in production when credentials/secrets are unsafe
_auth_settings = AuthSettings.from_env()
_auth_settings.validate()
_authenticator = WebAuthenticator(_auth_settings)

app = FastAPI(
    title="YouTube Export Tool v3 (Async)",
    lifespan=lifespan,
    # API schema/explorer are not exposed in production
    openapi_url=None if _auth_settings.is_production else "/openapi.json",
    docs_url=None if _auth_settings.is_production else "/docs",
    redoc_url=None if _auth_settings.is_production else "/redoc",
)

# Observability: create orchestrator at module level; initialize() called in lifespan
_telemetry = TelemetryOrchestrator(ObservabilityConfig.from_env())

# Middleware (last added = outermost):
#   instrumentation -> CORS -> request validation -> auth/authz/rate limit -> routes
app.add_middleware(AuthMiddleware, authenticator=_authenticator)
app.add_middleware(RequestValidationMiddleware)
app.add_middleware(CORSMiddleware, **cors_options(_auth_settings))  # '*' is refused in production

# Instrument FastAPI last (outermost wrapping â€” captures all requests)
instrument_fastapi(app, _telemetry.metrics, _telemetry.tracing)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
_frontend_assets = FRONTEND_DIST / "assets"
_frontend_assets.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(_frontend_assets)), name="frontend-assets")


@app.middleware("http")
async def add_request_id_middleware(request: Request, call_next):
    rid = uuid.uuid4().hex[:8]
    request.state.request_id = rid
    start = time.time()
    response = await call_next(request)
    elapsed = round(time.time() - start, 3)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Elapsed-Ms"] = str(int(elapsed * 1000))
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response



@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full details go to the server log only; clients get a generic message + trace id.
    rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:8]
    logger.exception("[%s] Unhandled exception on %s %s: %s", rid, request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error.",
            "error_code": "INTERNAL_ERROR",
            "trace_id": rid,
        },
        headers={"X-Request-ID": rid},
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


def _not_found(rid: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=404, content={"success": False, "error": "Job not found", "trace_id": rid})


_EXPORT_OWNER_FILE = "owner"


def _can_access_export(request: Request, job_id: str) -> bool:
    """Export jobs record their creator in runs/<job_id>/owner; legacy jobs are admin-only."""
    owner_path = RUNS_DIR / job_id / _EXPORT_OWNER_FILE
    try:
        owner = owner_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        owner = None
    return can_access_owned(_principal(request), owner)


def _can_access_transcript_job(request: Request, job) -> bool:
    return can_access_owned(_principal(request), getattr(job, "owner", None))


_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._-]{1,100}$")
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")
_VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
_JOB_ID_PATTERN = r"^[0-9a-f]{12}$"
_HANDLE_PATTERN = r"^@?[A-Za-z0-9._-]{1,100}$"
_MAX_SEGMENTS = 20000
_MAX_CONCURRENCY = 10


def _safe_filename_part(text: str) -> str:
    """Restrict user-influenced text used in Content-Disposition filenames."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:80] or "channel"


@app.post("/api/auth/login")
async def api_auth_login(body: LoginRequest, request: Request):
    ip = _authenticator.client_ip(request)
    username = body.username.strip()
    if not _authenticator.login_limiter.allow(f"ip:{ip}") or _authenticator.is_locked_out(username):
        return JSONResponse(
            status_code=429,
            content={"success": False, "error": "Too many login attempts. Try again later.", "error_code": "RATE_LIMITED"},
            headers={"Retry-After": "60"},
        )
    if not _authenticator.origin_allowed(request):
        return JSONResponse(status_code=403, content={"success": False, "error": "Cross-site request rejected.", "error_code": "CSRF_REJECTED"})
    principal = await _to_thread(_authenticator.authenticate_password, username, body.password)
    if principal is None:
        logger.warning("Failed login for user=%r from ip=%s", username[:64], ip)
        return JSONResponse(status_code=401, content={"success": False, "error": "Invalid username or password.", "error_code": "INVALID_CREDENTIALS"})
    token = _authenticator.issue_session(principal)
    response = JSONResponse(content={"success": True, "user": {"username": principal.subject, "role": principal.role}})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=_auth_settings.session_ttl_minutes * 60,
        httponly=True,
        secure=_auth_settings.is_production,
        samesite="strict",
        path="/",
    )
    logger.info("User %s logged in from ip=%s", principal.subject, ip)
    return response


@app.post("/api/auth/logout")
async def api_auth_logout():
    response = JSONResponse(content={"success": True})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=_auth_settings.is_production, samesite="strict")
    return response


@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    principal = _principal(request)
    return {"success": True, "user": {"username": principal.subject, "role": principal.role, "auth_method": principal.auth_method}}


def _spa_index() -> FileResponse | HTMLResponse:
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html")
    return HTMLResponse(
        "<h1>Frontend not built</h1><p>Run <code>npm --prefix frontend run build</code> to build frontend production assets.</p>",
        status_code=503,
    )


@app.get("/")
@app.get("/metadata")
@app.get("/transcript")
@app.get("/docs")
async def spa_routes(path: str = ""):
    return _spa_index()


# ---------------------------------------------------------------------------
# Export API (Async)
# ---------------------------------------------------------------------------


@app.post("/api/export")
async def api_export(request: Request) -> dict:
    rid = uuid.uuid4().hex[:8]
    body = await request.json()

    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"success": False, "error": "Request body must be a JSON object.", "trace_id": rid})
    raw_channel = body.get("channel", "")
    channel_input = raw_channel.strip() if isinstance(raw_channel, str) else ""
    try:
        limit = int(body.get("limit", 0) or 0)
    except (TypeError, ValueError):
        return JSONResponse(status_code=422, content={"success": False, "error": "limit must be an integer.", "trace_id": rid})
    if limit < 0 or limit > settings.max_videos_per_job:
        return JSONResponse(status_code=422, content={"success": False, "error": f"limit must be between 0 and {settings.max_videos_per_job}.", "trace_id": rid})
    if limit == 0:
        limit = settings.max_videos_per_job

    if not channel_input or len(channel_input) > 500:
        return JSONResponse(status_code=400, content={"success": False, "error": "Channel identifier cannot be empty.", "trace_id": rid})

    client_host = _authenticator.client_ip(request)
    if not rate_limiter.allow(f"export:{client_host}"):
        return JSONResponse(status_code=429, content={"success": False, "error": "Too many requests.", "trace_id": rid})

    key_ok, key_error = is_youtube_api_key_valid()
    if not key_ok:
        return JSONResponse(status_code=503, content={"success": False, "error": key_error, "trace_id": rid})

    if not quota_tracker.record_call(1):
        return JSONResponse(status_code=429, content={"success": False, "error": "Daily API quota reached.", "trace_id": rid})

    job_id = uuid.uuid4().hex[:12]
    export_req = ExportRequest(channel_input=channel_input, limit=limit)

    # Start async background task
    run_dir = RUNS_DIR / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    principal = _principal(request)
    (run_dir / _EXPORT_OWNER_FILE).write_text(principal.subject if principal else "", encoding="utf-8")

    task = asyncio.create_task(_run_async_pipeline(job_id, export_req))
    async with _running_tasks_lock:
        _running_tasks[job_id] = task

    logger.info("[%s] Async export started: channel=%s, limit=%d, job_id=%s", rid, channel_input, limit, job_id)

    return {"success": True, "job_id": job_id, "trace_id": rid, "status": "pending"}


@app.get("/api/export/{job_id}/progress")
async def api_export_progress(job_id: str, request: Request) -> dict:
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return {"success": False, "error": "Invalid job ID", "trace_id": rid}
    if not _can_access_export(request, job_id):
        return {"success": False, "error": "Job not found", "trace_id": rid}

    progress = await _async_pipeline.get_progress(job_id)
    if progress is None:
        # Fall back to disk
        progress_path = RUNS_DIR / job_id / "progress.json"
        if progress_path.exists():
            try:
                progress = json.loads(progress_path.read_text())
            except Exception:
                pass
    if progress is None:
        return {"success": False, "error": "Job not found", "trace_id": rid}

    return {"success": True, "job_id": job_id, "trace_id": rid, **progress}


@app.get("/api/export/{job_id}/result")
async def api_export_result(job_id: str, request: Request) -> dict:
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return {"success": False, "error": "Invalid job ID", "trace_id": rid}
    if not _can_access_export(request, job_id):
        return {"success": False, "error": "Export not ready", "trace_id": rid}

    result = await _async_pipeline.get_result(job_id)
    if result is None:
        result_path = RUNS_DIR / job_id / "result.json"
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text())
            except Exception:
                pass
    if result is None:
        return {"success": False, "error": "Export not ready", "trace_id": rid}

    return {"success": True, "job_id": job_id, "result": result, "trace_id": rid}


@app.get("/api/export/{job_id}/download")
async def api_export_download(job_id: str, request: Request):
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid job ID", "trace_id": rid})
    if not _can_access_export(request, job_id):
        return JSONResponse(status_code=404, content={"success": False, "error": "CSV file not found", "trace_id": rid})

    csv_path = RUNS_DIR / job_id / "videos.csv"
    if not csv_path.exists():
        return JSONResponse(status_code=404, content={"success": False, "error": "CSV file not found", "trace_id": rid})
    if csv_path.stat().st_size == 0:
        return JSONResponse(status_code=422, content={"success": False, "error": "CSV file is empty", "trace_id": rid})

    return FileResponse(str(csv_path), media_type="text/csv", filename="videos.csv")


@app.post("/api/export/{job_id}/cancel")
async def api_export_cancel(job_id: str, request: Request) -> dict:
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return {"success": False, "error": "Invalid job ID", "trace_id": rid}
    if not _can_access_export(request, job_id):
        return {"success": False, "error": "Job not running or already completed", "trace_id": rid}

    async with _running_tasks_lock:
        task = _running_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            _running_tasks.pop(job_id, None)
            logger.info("[%s] Job %s cancelled", rid, job_id)
            return {"success": True, "job_id": job_id, "status": "cancelled", "trace_id": rid}
    return {"success": False, "error": "Job not running or already completed", "trace_id": rid}


@app.delete("/api/export/{job_id}")
async def api_export_delete(job_id: str, request: Request):
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return {"success": False, "error": "Invalid job ID", "trace_id": rid}
    if not _can_access_export(request, job_id):
        return _not_found(rid)
    run_dir = RUNS_DIR / job_id
    if run_dir.exists():
        shutil.rmtree(str(run_dir), ignore_errors=True)
    return {"success": True, "job_id": job_id, "trace_id": rid}


@app.get("/api/export/jobs/active")
async def api_export_active() -> dict:
    async with _running_tasks_lock:
        jobs = [
            {"job_id": jid, "status": "running"}
            for jid in _running_tasks
            if not _running_tasks[jid].done()
        ]
    return {"success": True, "count": len(jobs), "jobs": jobs}


# ---------------------------------------------------------------------------
# Monitoring & Metrics
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def api_health(request: Request):
    key_ok, key_error = is_youtube_api_key_valid()
    if _principal(request) is None:
        # Anonymous probes (load balancers, uptime checks) only learn the coarse status.
        return success_response(data={"status": "ok" if key_ok else "degraded"}, message="Service health check")
    redis_healthy = False
    try:
        import redis as _redis
        r = _redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        r.ping()
        redis_healthy = True
    except Exception:
        pass
    import shutil
    base_path = Path(__file__).resolve().parent.parent
    usage = shutil.disk_usage(base_path)
    disk_healthy = usage.free / usage.total > 0.1
    status = "ok" if key_ok else "degraded"
    if not key_ok:
        logger.warning("Health check: degraded state - %s", key_error)
    async with _running_tasks_lock:
        active_count = len([t for t in _running_tasks.values() if not t.done()])
    return success_response(
        data={
            "status": status,
            "version": "3.0",
            "database": {"healthy": True, "type": "filesystem"},
            "redis": {"healthy": redis_healthy},
            "storage": {"healthy": disk_healthy, "free_percent": round(usage.free / usage.total * 100, 1)},
            "youtube_api_key_configured": key_ok,
            "youtube_api_key_error": key_error if not key_ok else None,
            "active_exports": active_count,
        },
        message="Service health check",
    )


@app.get("/api/metrics")
async def api_metrics():
    return metrics.get_metrics()


@app.get("/api/quota")
async def api_quota():
    return {"success": True, "quota": quota_tracker.usage()}


@app.get("/api/cache/stats")
async def api_cache_stats():
    from infrastructure.cache import cache_service
    return {"success": True, "cache": cache_service.stats}


@app.get("/api/active-exports")
async def api_active_exports():
    return await api_export_active()


# ---------------------------------------------------------------------------
# Core Services & Helpers
# ---------------------------------------------------------------------------

import contextvars
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

_url_parser = None
_metadata_service = None
_transcript_service = None
_transcript_processor = None
_channel_service = None
_video_service = None


def _get_request_id() -> str:
    rid = _request_id_var.get()
    if not rid:
        rid = uuid.uuid4().hex[:8]
        _request_id_var.set(rid)
    return rid


def _get_url_parser():
    global _url_parser
    if _url_parser is None:
        from services.youtube_url_parser import YouTubeURLParser
        _url_parser = YouTubeURLParser()
    return _url_parser


def _get_metadata_service():
    global _metadata_service
    if _metadata_service is None:
        from services.youtube_metadata_service import YouTubeMetadataService
        _metadata_service = YouTubeMetadataService()
    return _metadata_service


def _get_transcript_service():
    global _transcript_service
    if _transcript_service is None:
        from services.transcript_service import TranscriptService
        _transcript_service = TranscriptService()
    return _transcript_service


def _get_transcript_processor():
    global _transcript_processor
    if _transcript_processor is None:
        from services.transcript_processor import TranscriptProcessor
        _transcript_processor = TranscriptProcessor()
    return _transcript_processor


def _get_channel_service():
    global _channel_service
    if _channel_service is None:
        from api.channel_service import ChannelService
        _channel_service = ChannelService()
    return _channel_service


def _get_video_service():
    global _video_service
    if _video_service is None:
        from api.video_service import VideoService
        _video_service = VideoService()
    return _video_service


# --- Transcript endpoints ---

class UnifiedTranscriptRequest(BaseModel):
    video_url: str
    output_language: str = "en"  # "en" (Simple English default) | "original" | "hi"


_shared_transcript_repo = None
_shared_transcript_service = None
_shared_translation_service = None


def _get_unified_services():
    global _shared_transcript_repo, _shared_transcript_service, _shared_translation_service
    if _shared_transcript_repo is None:
        from repositories.transcript_repository import TranscriptRepository
        from services.transcription.service import TranscriptService
        from services.translation.service import TranslationService
        _shared_transcript_repo = TranscriptRepository(persist_dir="data/transcripts")
        _shared_transcript_service = TranscriptService(repository=_shared_transcript_repo)
        _shared_translation_service = TranslationService(repository=_shared_transcript_repo)
    return _shared_transcript_service, _shared_translation_service


_OUTPUT_MODE_PATTERN = r"^(original|original_spoken|en|hi)$"
_OUTPUT_MODE_LABELS = {"en": "Simple English", "hi": "Simple Hindi"}


async def _localized_transcript(
    video_id: str, raw_text: str, source_language: str | None, output_language: str | None, rid: str,
) -> tuple[str, str | None, bool]:
    """Return ``(text, language_label, fell_back)`` for the requested output mode.

    'original' always returns the verbatim source text. 'en' / 'hi' go through the
    explicit TranslationService; on failure the original text is returned labelled
    with its real language (never mislabelled as English).
    """
    mode = (output_language or "original").lower().strip()
    if mode in ("original", "original_spoken") or not raw_text:
        return raw_text, source_language, False
    try:
        _, trans_svc = _get_unified_services()
        result = await _to_thread(
            trans_svc.translate,
            video_id=video_id,
            original_text=raw_text,
            target_language=mode,
            source_language=source_language or "auto",
        )
        text = (result or {}).get("transcript") or ""
        if text:
            return text, _OUTPUT_MODE_LABELS.get(mode, mode), False
    except Exception as exc:
        logger.warning("[%s] Translation of %s to %s failed: %s; returning original transcript", rid, video_id, mode, exc)
    return raw_text, source_language or "Original (untranslated)", True


@app.post("/api/transcript")
async def api_transcript_unified(request: UnifiedTranscriptRequest):
    """Unified transcript acquisition and translation endpoint.

    Pipeline:
      1. Validates YouTube URL / Video ID
      2. Checks canonical transcript cache (Redis/disk)
      3. Fetches YouTube captions (zero cost)
      4. Falls back to yt-dlp audio extraction + Groq Whisper Large V3 if captions unavailable
      5. Cleans and normalizes transcript
      6. Validates transcript quality and checks for hallucination loops
      7. Caches canonical transcript
      8. Defaults to 'en' (Simple English) with strict educational pedagogical simplification
    """
    from exceptions import YouTubeURLError
    from observability.transcript_metrics import transcript_metrics
    from services.transcription.groq import (
        GroqAuthError,
        GroqRateLimitError,
        GroqTimeoutError,
        GroqTranscriptionError,
    )
    from services.transcription.service import TranscriptService
    from services.transcription.validator import (
        TranscriptEmptyError,
        TranscriptValidationError,
    )
    from services.translation.service import TranslationError, TranslationService
    from services.youtube.audio import AudioExtractionError
    from services.youtube.captions import CaptionsUnavailableError

    rid = uuid.uuid4().hex[:8]
    transcript_metrics.record_request_start()
    out_lang = (request.output_language or "en").lower().strip()
    if out_lang not in ("original", "en", "hi"):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error_code": "INVALID_REQUEST",
                "message": f"Unsupported output_language '{request.output_language}'. Must be 'original', 'en', or 'hi'.",
                "retryable": False,
                "trace_id": rid,
            },
        )

    logger.info("[%s] Unified transcript request: url=%s, out_lang=%s", rid, request.video_url, out_lang)

    try:
        ts_service, trans_service = _get_unified_services()
        # Fetch or generate canonical transcript in worker thread
        canonical = await _to_thread(ts_service.get_canonical_transcript, request.video_url)

        video_id = canonical["video_id"]
        source_lang = canonical["source_language"]
        canonical_text = canonical["transcript"]
        segments = canonical["segments"]
        provider = canonical["provider"]
        duration_seconds = canonical.get("duration_seconds")
        confidence = canonical.get("confidence", 1.0)
        from_cache = canonical.get("from_cache", False)

        # Record metrics based on source
        if not from_cache:
            if provider == "youtube_captions":
                transcript_metrics.record_caption_result(success=True)
            elif provider == "groq_whisper_large_v3":
                transcript_metrics.record_caption_result(success=False)
                transcript_metrics.record_groq_fallback(
                    success=True,
                    duration_sec=canonical.get("processing_time_seconds", 0.0),
                    video_dur_sec=duration_seconds or 0.0,
                    words=canonical.get("word_count", 0),
                )

        # Handle on-demand translation / Simple English if requested
        final_text = canonical_text
        final_segments = segments
        final_provider = provider
        final_from_cache = from_cache
        fallback_to_original = False

        if out_lang in ("en", "hi"):
            try:
                trans_result = await _to_thread(
                    trans_service.translate,
                    video_id=video_id,
                    original_text=canonical_text,
                    target_language=out_lang,
                    source_language=source_lang,
                    original_segments=segments,
                )
                final_text = trans_result["transcript"]
                final_segments = trans_result.get("segments", [])
                final_provider = trans_result.get("provider", f"groq_translation_{out_lang}")
                final_from_cache = trans_result.get("from_cache", False)
                transcript_metrics.record_translation(lang=out_lang, cache_hit=final_from_cache)
            except Exception as trans_exc:
                logger.warning(
                    "[%s] Translation to '%s' failed: %s; gracefully falling back to canonical transcript",
                    rid, out_lang, trans_exc,
                )
                fallback_to_original = True
                final_text = canonical_text
                final_segments = segments
                final_provider = provider
                final_from_cache = from_cache

        # Try to retrieve video title if available
        title = None
        try:
            video_svc = _get_video_service()
            items = await _to_thread(video_svc.get_videos_batch, [video_id])
            if items:
                title = items[0].get("snippet", {}).get("title")
        except Exception:
            pass

        transcript_metrics.record_final_result(success=True)

        return {
            "success": True,
            "video_id": video_id,
            "title": title,
            "source_language": source_lang,
            "output_language": out_lang if not fallback_to_original else "original",
            "provider": final_provider,
            "transcript": final_text,
            "raw_transcript": canonical_text,
            "raw_segments": segments,
            "fallback_to_original": fallback_to_original,
            "segments": final_segments,
            "word_count": len(final_text.split()),
            "duration_seconds": duration_seconds,
            "confidence": confidence,
            "from_cache": final_from_cache,
            "trace_id": rid,
        }

    except YouTubeURLError as exc:
        logger.warning("[%s] Invalid YouTube URL: %s", rid, exc)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error_code": "INVALID_YOUTUBE_URL",
                "message": str(exc),
                "retryable": False,
                "trace_id": rid,
            },
        )
    except CaptionsUnavailableError as exc:
        logger.warning("[%s] Captions unavailable: %s", rid, exc)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "error_code": exc.error_code,
                "message": exc.message,
                "retryable": False,
                "trace_id": rid,
            },
        )
    except AudioExtractionError as exc:
        logger.error("[%s] Audio extraction failed: %s", rid, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error_code": exc.error_code,
                "message": "Audio could not be extracted from this YouTube video for speech-to-text.",
                "retryable": True,
                "trace_id": rid,
            },
        )
    except GroqAuthError as exc:
        logger.error("[%s] Groq authentication failed: %s", rid, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "error_code": exc.error_code,
                "message": "Groq API key is missing or invalid. Please check your backend server configuration.",
                "retryable": False,
                "trace_id": rid,
            },
        )
    except GroqRateLimitError as exc:
        logger.warning("[%s] Groq rate limit hit: %s", rid, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "error_code": exc.error_code,
                "message": exc.message,
                "retryable": True,
                "trace_id": rid,
            },
        )
    except GroqTimeoutError as exc:
        logger.warning("[%s] Groq timeout: %s", rid, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=504,
            content={
                "success": False,
                "error_code": exc.error_code,
                "message": exc.message,
                "retryable": True,
                "trace_id": rid,
            },
        )
    except (GroqTranscriptionError, TranslationError) as exc:
        logger.error("[%s] STT/Translation failed: %s", rid, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error_code": getattr(exc, "error_code", "TRANSCRIPTION_FAILED"),
                "message": "We could not generate a transcript or translation for this video.",
                "retryable": getattr(exc, "retryable", True),
                "trace_id": rid,
            },
        )
    except (TranscriptEmptyError, TranscriptValidationError) as exc:
        logger.warning("[%s] Quality validation failed: %s", rid, exc)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error_code": exc.error_code,
                "message": exc.message,
                "retryable": False,
                "trace_id": rid,
            },
        )
    except Exception as exc:
        logger.exception("[%s] Unexpected error in unified transcript route: %s", rid, exc)
        transcript_metrics.record_final_result(success=False)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error_code": "TRANSCRIPTION_FAILED",
                "message": "An unexpected error occurred while acquiring the transcript.",
                "retryable": True,
                "trace_id": rid,
            },
        )


@app.get("/api/transcript/metrics")
async def api_transcript_metrics():
    """Retrieve production transcript and STT monitoring metrics."""
    from observability.transcript_metrics import transcript_metrics
    return {
        "success": True,
        "metrics": transcript_metrics.get_snapshot(),
    }


@app.get("/api/transcript/limiter/status")
@app.get("/api/transcript-limiter/status")
async def api_transcript_limiter_status():
    """Return current transcript rate limiter and circuit breaker diagnostic metrics."""
    from transcript_reliability.transcript_limiter import transcript_limiter
    return success_response(data=transcript_limiter.get_status())


@app.get("/api/transcript/{video_id}")
async def api_transcript(
    video_id: str = PathParam(pattern=_VIDEO_ID_PATTERN),
    language: str | None = Query(None, max_length=16),
    force_refresh: bool = False,
    allow_whisper: bool = True,
) -> dict:
    rid = uuid.uuid4().hex[:8]
    logger.info("[%s] Transcript request: video_id=%s", rid, video_id)
    try:
        service = _get_transcript_service()
        result = await _to_thread(service.get_transcript, video_id=video_id, language=language, force_refresh=force_refresh, allow_whisper=allow_whisper)
        return result.model_dump()
    except Exception as exc:
        logger.exception("[%s] Transcript fetch failed for %s", rid, video_id)
        raise


@app.get("/api/transcript/{video_id}/all")
async def api_transcript_all(video_id: str = PathParam(pattern=_VIDEO_ID_PATTERN)) -> dict:
    rid = uuid.uuid4().hex[:8]
    service = _get_transcript_service()
    return await _to_thread(service.get_all_transcripts, video_id)


@app.get("/api/transcript/{video_id}/status")
async def api_transcript_status(video_id: str = PathParam(pattern=_VIDEO_ID_PATTERN)) -> dict:
    service = _get_transcript_service()
    return await _to_thread(service.get_transcript_status, video_id)


@app.get("/api/transcript/{video_id}/translate/{target_language}")
async def api_translate_transcript(
    video_id: str = PathParam(pattern=_VIDEO_ID_PATTERN),
    target_language: str = PathParam(pattern=_LANG_RE.pattern),
) -> dict:
    rid = uuid.uuid4().hex[:8]
    try:
        service = _get_transcript_service()
        result = await _to_thread(service.translate_transcript, video_id=video_id, target_language=target_language)
        return result.model_dump()
    except Exception as exc:
        logger.exception("[%s] Translation failed", rid)
        raise


@app.get("/api/transcript/{video_id}/list-all")
async def api_transcript_list_all(video_id: str = PathParam(pattern=_VIDEO_ID_PATTERN)) -> dict:
    try:
        service = _get_transcript_service()
        return await _to_thread(service.get_transcript_status, video_id)
    except Exception:
        logger.exception("Transcript list-all failed for %s", video_id)
        return {"success": False, "video_id": video_id, "error": "Failed to list transcripts."}


@app.post("/api/transcript/{video_id}/process")
async def api_process_transcript(video_id: str = PathParam(pattern=_VIDEO_ID_PATTERN), remove_fillers: bool = False) -> dict:
    import json
    transcript_service = _get_transcript_service()
    transcript = await _to_thread(transcript_service.get_transcript, video_id)
    if not transcript.success:
        raise HTTPException(status_code=404, detail={"success": False, "error": transcript.error or "No transcript available."})
    processor = _get_transcript_processor()
    result = await _to_thread(processor.process, segments=transcript.segments, video_id=video_id, remove_fillers=remove_fillers)
    return json.loads(result.model_dump_json())


@app.post("/api/process-transcript")
async def api_process_transcript_direct(request: Request) -> dict:
    import json
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail={"success": False, "error": "Request body must be a JSON object."})
    segments = body.get("segments", [])
    vid = body.get("video_id", "")
    remove = body.get("remove_fillers", False)
    if not segments:
        raise HTTPException(status_code=400, detail={"success": False, "error": "No segments provided."})
    if not isinstance(segments, list) or len(segments) > _MAX_SEGMENTS:
        raise HTTPException(status_code=422, detail={"success": False, "error": f"segments must be a list of at most {_MAX_SEGMENTS} items."})
    if not isinstance(vid, str) or len(vid) > 64:
        raise HTTPException(status_code=422, detail={"success": False, "error": "Invalid video_id."})
    processor = _get_transcript_processor()
    result = await _to_thread(processor.process, segments=segments, video_id=vid, remove_fillers=remove)
    return json.loads(result.model_dump_json())


# ---------------------------------------------------------------------------
# ISO 8601 Duration Helpers
# ---------------------------------------------------------------------------


def _parse_iso_duration(iso: str) -> int:
    """Parse ISO 8601 duration string (e.g. PT15M51S, PT1H2M3S) to total seconds."""
    import re
    match = re.match(r'^PT?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', iso)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _format_duration(total_seconds: int) -> str:
    """Format seconds to readable duration (e.g. 845 -> '14:05', 3720 -> '1:02:00')."""
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


# --- Simplified Transcript v2 endpoint (Workflow 2 — returns ONLY title, duration, transcript) ---


@app.get("/api/transcriptv2/{video_id}")
async def api_transcript_v2(video_id: str = PathParam(pattern=_VIDEO_ID_PATTERN)):
    """Return ONLY title, duration, and transcript text for a video.

    Minimal endpoint for the simplified Transcript workflow. Fetches only
    the bare minimum metadata (title, duration) alongside the transcript.
    No statistics, no segments, no pipeline info, no source metadata.
    """
    rid = uuid.uuid4().hex[:8]
    logger.info("[%s] Transcript v2 request: video_id=%s", rid, video_id)

    title = ""
    duration = "0:00"
    transcript_text = ""

    # Fetch minimal metadata (only title + duration needed)
    try:
        video_svc = _get_video_service()
        items = await _to_thread(video_svc.get_videos_batch, [video_id])
        if items:
            snippet = items[0].get("snippet", {})
            cd = items[0].get("contentDetails", {})
            title = snippet.get("title", "")
            channel_title = snippet.get("channelTitle", "")
            duration = _format_duration(_parse_iso_duration(cd.get("duration", "PT0S")))
    except Exception as exc:
        logger.warning("[%s] Minimal metadata fetch failed for %s: %s", rid, video_id, exc)

    # Fetch transcript
    transcript = None
    try:
        transcript_svc = _get_transcript_service()
        transcript = await _to_thread(
            transcript_svc.get_transcript,
            video_id,
            video_title=title,
            channel_title=channel_title if 'channel_title' in locals() else None,
        )
        if transcript.success:
            transcript_text = transcript.plain_text or transcript.paragraph_text or ""
    except Exception as exc:
        logger.warning("[%s] Transcript fetch failed for %s: %s", rid, video_id, exc)

    status = "success" if (transcript and transcript.success and transcript_text) else "failed"
    source = None
    method = None
    language = "en"
    raw_transcript_text = getattr(transcript, "raw_transcript", "") or transcript_text
    error_code = None
    error_message = None

    if transcript:
        source = getattr(transcript.source, "value", str(transcript.source)) if transcript.source else None
        method = getattr(transcript, "method", None)
        if getattr(transcript, "source_language", None):
            language = transcript.source_language
        elif str(transcript.language).lower() in ("hi", "hindi") or (raw_transcript_text and english_converter.contains_non_roman_script(raw_transcript_text)):
            language = "Hindi"
        else:
            language = transcript.language or "English"

        if not transcript.success:
            error_code = getattr(transcript, "error_code", None) or "TRANSCRIPT_NOT_FOUND"
            error_message = transcript.error or "Failed to fetch transcript"

    if status == "failed":
        if not error_code:
            error_code = "TRANSCRIPT_NOT_FOUND"
            error_message = "No transcript could be extracted for this video."
        if error_code == "VIDEO_UNAVAILABLE" and not error_message:
            error_message = "This video is unavailable, private, or deleted."

    is_non_roman = False
    if isinstance(raw_transcript_text, str) and raw_transcript_text:
        is_non_roman = english_converter.contains_non_roman_script(raw_transcript_text)
    script = "Devanagari" if is_non_roman else "Standard"

    logger.info("[%s] Returning v2 result for %s: title='%s', status='%s', method='%s', len=%d",
                rid, video_id, title, status, method, len(transcript_text))

    return {
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "duration": duration,
        "language": language,
        "script": script,
        "status": status,
        "transcript": transcript_text,
        "raw_transcript": raw_transcript_text,
        "source": source,
        "method": method,
        "error_code": error_code,
        "error_message": error_message,
    }


# --- Channel Transcript endpoint ---


@app.get("/api/channel/{handle}/transcripts")
async def api_channel_transcripts(
    handle: str = PathParam(pattern=_HANDLE_PATTERN),
    limit: int = Query(100, ge=1, le=settings.max_videos_sync_export),
    concurrency: int = Query(5, ge=1, le=_MAX_CONCURRENCY),
    allow_whisper: bool = True,
    output_language: str = Query("original", pattern=_OUTPUT_MODE_PATTERN),
):
    """Fetch transcripts for a YouTube channel with duration filtering (3–30 min).

    Resolves the channel handle, discovers video IDs from the upload playlist,
    fetches video metadata for duration filtering, and fetches transcripts with
    caption-first retrieval and speech-to-text (Whisper) fallback.

    Returns channel info, statistics, and per-video results including metadata.
    Only processes videos with duration 3:00 <= duration < 30:00 (180s <= dur < 1800s).
    """
    rid = uuid.uuid4().hex[:8]
    clean = handle.lstrip("@")
    start_time = time.time()
    logger.info(
        "[%s] Channel transcript request: handle=%s, limit=%d, concurrency=%d, allow_whisper=%s",
        rid, clean, limit, concurrency, allow_whisper,
    )

    try:
        channel_svc = _get_channel_service()
        video_svc = _get_video_service()
        transcript_svc = _get_transcript_service()

        # Step 1: Resolve channel
        logger.info("[%s] Stage 1/5: Resolving channel handle: %s", rid, clean)
        channel_data = await _to_thread(channel_svc.resolve_handle, clean)
        channel_id = channel_data["id"]
        channel_title = channel_data["snippet"]["title"]
        logger.info("[%s] Channel resolved: id=%s, title='%s'", rid, channel_id, channel_title)

        # Step 2: Get upload playlist ID
        logger.info("[%s] Stage 2/5: Getting upload playlist for channel %s", rid, channel_id)
        playlist_id = await _to_thread(video_svc.get_uploads_playlist_id, channel_id)
        logger.info("[%s] Upload playlist: %s", rid, playlist_id)

        # Step 3: Paginate playlist for video IDs
        logger.info("[%s] Stage 3/5: Discovering videos from upload playlist", rid)
        all_video_ids: list[str] = []
        next_page: str | None = None
        pages_fetched = 0
        while len(all_video_ids) < limit:
            page = await _to_thread(
                video_svc.get_playlist_items, playlist_id, next_page,
            )
            video_ids = page.get("video_ids", [])
            for vid in video_ids:
                if vid and vid not in all_video_ids:
                    all_video_ids.append(vid)
                    if len(all_video_ids) >= limit:
                        break
            next_page = page.get("next_page_token")
            pages_fetched += 1
            logger.info(
                "[%s]  Playlist page %d: %d videos (total %d so far)",
                rid, pages_fetched, len(video_ids), len(all_video_ids),
            )
            if not next_page:
                break
        logger.info("[%s] Stage 3/5 complete: %d videos discovered", rid, len(all_video_ids))

        # Step 4: Fetch video metadata for duration filtering
        logger.info("[%s] Stage 4/5: Fetching video metadata (durations, titles)", rid)
        videos_metadata: dict[str, dict] = {}
        for i in range(0, len(all_video_ids), 50):
            batch = all_video_ids[i:i + 50]
            try:
                metadata_items = await _to_thread(video_svc.get_videos_batch, batch)
                for item in metadata_items:
                    vid = item.get("id")
                    if not vid:
                        continue
                    snippet = item.get("snippet", {})
                    content_details = item.get("contentDetails", {})
                    duration_iso = content_details.get("duration", "PT0S")
                    duration_seconds = _parse_iso_duration(duration_iso)
                    thumbnails = snippet.get("thumbnails", {})
                    thumbnail = None
                    for quality in ("medium", "high", "standard", "default"):
                        if quality in thumbnails:
                            thumbnail = thumbnails[quality].get("url")
                            break
                    live_status = snippet.get("liveBroadcastContent", "none")
                    videos_metadata[vid] = {
                        "title": snippet.get("title", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "duration_seconds": duration_seconds,
                        "duration_readable": _format_duration(duration_seconds),
                        "live_status": live_status,
                    }
            except Exception as exc:
                logger.warning("[%s] Failed to fetch metadata batch of %d videos: %s", rid, len(batch), exc)

        # Filter by duration (3:00 <= duration < 30:00) and exclude live streams using evaluate_duration
        from services.duration_filter import evaluate_duration
        eligible_videos: list[str] = []
        short_videos: list[str] = []
        long_videos: list[str] = []
        live_videos: list[str] = []

        for vid in all_video_ids:
            meta = videos_metadata.get(vid, {})
            duration = meta.get("duration_seconds", 0)
            live_status = meta.get("live_status", "none")

            filter_res = evaluate_duration(
                duration_seconds=duration,
                live_status=live_status,
                min_seconds=180,
                max_seconds=1800,
            )
            if filter_res.is_eligible:
                eligible_videos.append(vid)
            elif filter_res.skip_reason == "LIVE_STREAM":
                live_videos.append(vid)
            elif filter_res.skip_reason == "TOO_SHORT":
                short_videos.append(vid)
            elif filter_res.skip_reason == "TOO_LONG":
                long_videos.append(vid)
            else:
                short_videos.append(vid)

        logger.info(
            "[%s] Stage 4/5 complete: %d eligible (3–30 min), %d short, %d long, %d live",
            rid, len(eligible_videos), len(short_videos), len(long_videos), len(live_videos),
        )

        # Step 5: Fetch transcripts in parallel
        logger.info(
            "[%s] Stage 5/5: Fetching transcripts for %d eligible videos (concurrency=%d)",
            rid, len(eligible_videos), concurrency,
        )
        from transcript_reliability.transcript_limiter import transcript_limiter
        eff_concurrency = min(concurrency, settings.transcript_max_concurrency)
        semaphore = asyncio.Semaphore(eff_concurrency)

        async def _fetch_one(video_id: str, idx: int) -> dict:
            async with semaphore:
                await transcript_limiter.acquire(video_id)
                meta = videos_metadata.get(video_id, {})
                title = meta.get("title", "")
                published_at = meta.get("published_at", "")
                dur_sec = meta.get("duration_seconds", 0)
                dur_str = meta.get("duration_readable", "0:00")
                video_url = f"https://www.youtube.com/watch?v={video_id}"

                # 1. Try caption first
                try:
                    res = await _to_thread(
                        transcript_svc.get_transcript, video_id, allow_whisper=False,
                        output_format=output_language,
                    )
                    if res.success and (res.plain_text or res.paragraph_text):
                        raw_text = getattr(res, "raw_transcript", "") or (res.plain_text or res.paragraph_text or "")
                        out_text, out_label, _ = await _localized_transcript(
                            video_id, raw_text, res.source_language or res.language, output_language, rid,
                        )
                        return {
                            "index": idx,
                            "video_id": video_id,
                            "video_url": video_url,
                            "channel_id": channel_id,
                            "channel_title": channel_title,
                            "title": title,
                            "published_at": published_at,
                            "duration_seconds": dur_sec,
                            "duration": dur_str,
                            "language": out_label or res.language or "en",
                            "status": "success",
                            "transcript": out_text,
                            "raw_transcript": raw_text,
                            "source": "youtube",
                            "method": "caption",
                            "error_code": None,
                            "error_message": None,
                        }
                except Exception as exc:
                    logger.debug("[%s] Caption fetch exception for %s: %s", rid, video_id, exc)

                # 2. Try Whisper fallback if enabled
                if allow_whisper and settings.whisper_enabled:
                    try:
                        w_res = await _to_thread(
                            transcript_svc.get_transcript,
                            video_id,
                            allow_whisper=True,
                            video_title=title,
                            channel_title=channel_title,
                            output_format=output_language,
                        )
                        if w_res.success and (w_res.plain_text or w_res.paragraph_text):
                            raw_text = getattr(w_res, "raw_transcript", "") or (w_res.plain_text or w_res.paragraph_text or "")
                            out_text, out_label, _ = await _localized_transcript(
                                video_id, raw_text, w_res.source_language or w_res.language, output_language, rid,
                            )
                            return {
                                "index": idx,
                                "video_id": video_id,
                                "video_url": video_url,
                                "channel_id": channel_id,
                                "channel_title": channel_title,
                                "title": title,
                                "published_at": published_at,
                                "duration_seconds": dur_sec,
                                "duration": dur_str,
                                "language": out_label or w_res.language or "unknown",
                                "status": "success",
                                "transcript": out_text,
                                "raw_transcript": raw_text,
                                "source": "whisper",
                                "method": "speech_to_text",
                                "error_code": None,
                                "error_message": None,
                            }
                        else:
                            return {
                                "index": idx,
                                "video_id": video_id,
                                "video_url": video_url,
                                "channel_id": channel_id,
                                "channel_title": channel_title,
                                "title": title,
                                "published_at": published_at,
                                "duration_seconds": dur_sec,
                                "duration": dur_str,
                                "language": "en",
                                "status": "failed",
                                "transcript": "",
                                "source": None,
                                "method": None,
                                "error_code": w_res.error_code or "NO_CAPTIONS",
                                "error_message": w_res.error or "No captions available",
                            }
                    except Exception as exc:
                        logger.warning("[%s] Whisper fallback failed for %s: %s", rid, video_id, exc)
                        return {
                            "index": idx,
                            "video_id": video_id,
                            "video_url": video_url,
                            "channel_id": channel_id,
                            "channel_title": channel_title,
                            "title": title,
                            "published_at": published_at,
                            "duration_seconds": dur_sec,
                            "duration": dur_str,
                            "language": "en",
                            "status": "failed",
                            "transcript": "",
                            "source": None,
                            "method": None,
                            "error_code": "STT_FAILED",
                            "error_message": "Speech-to-text failed.",
                        }

                # Captions unavailable and whisper disabled
                return {
                    "index": idx,
                    "video_id": video_id,
                    "video_url": video_url,
                    "channel_id": channel_id,
                    "channel_title": channel_title,
                    "title": title,
                    "published_at": published_at,
                    "duration_seconds": dur_sec,
                    "duration": dur_str,
                    "language": "en",
                    "status": "failed",
                    "transcript": "",
                    "source": None,
                    "method": None,
                    "error_code": "NO_CAPTIONS",
                    "error_message": "No transcript/caption track is available for this video.",
                }

        tasks = [_fetch_one(vid, i) for i, vid in enumerate(eligible_videos)]
        raw_results = await asyncio.gather(*tasks)
        raw_results.sort(key=lambda r: r["index"])
        video_results = [{k: v for k, v in r.items() if k != "index"} for r in raw_results]

        successful_count = sum(1 for v in video_results if v.get("status") == "success")
        caption_count = sum(1 for v in video_results if v.get("method") == "caption")
        whisper_count = sum(1 for v in video_results if v.get("method") == "speech_to_text")
        failed_count = sum(1 for v in video_results if v.get("status") != "success")
        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        logger.info(
            "[%s] Complete: %d/%d transcripts (%d captions, %d whisper, %d failed), %dms elapsed",
            rid, successful_count, len(eligible_videos), caption_count, whisper_count, failed_count, elapsed_ms,
        )

        return success_response(
            data={
                "channel_id": channel_id,
                "channel_title": channel_title,
                "total_discovered": len(all_video_ids),
                "eligible_count": len(eligible_videos),
                "skipped_count": len(short_videos) + len(long_videos) + len(live_videos),
                "successful_count": successful_count,
                "caption_count": caption_count,
                "whisper_count": whisper_count,
                "failed_count": failed_count,
                "videos": video_results,
            },
            message=(
                f"Fetched {successful_count} transcript(s) ({caption_count} captions, {whisper_count} STT) "
                f"for {len(eligible_videos)} eligible videos in {channel_title}"
            ),
        )
    except Exception as exc:
        logger.exception("[%s] Channel transcript failed for %s", rid, handle)
        return error_response(
            message=f"Failed to fetch channel transcripts (trace {rid}).",
            status_code=500,
        )


# --- Transcript Background Job endpoints ---


class TranscriptJobCreateRequest(BaseModel):
    max_videos: int = Field(0, ge=0, le=settings.max_videos_per_job)
    force_refresh: bool = False
    caption_concurrency: int = Field(1, ge=1, le=_MAX_CONCURRENCY)
    whisper_concurrency: int = Field(1, ge=1, le=_MAX_CONCURRENCY)
    published_after: str | None = Field(None, max_length=40)
    published_before: str | None = Field(None, max_length=40)
    output_language: str = Field("en", max_length=20)


@app.post("/api/channel/{handle}/transcript-job")
async def api_start_channel_transcript_job(
    request: Request,
    handle: str = PathParam(pattern=_HANDLE_PATTERN),
    max_videos: int = Query(0, ge=0, le=settings.max_videos_per_job),
    force_refresh: bool = False,
    published_after: str | None = Query(None, max_length=40),
    published_before: str | None = Query(None, max_length=40),
    output_language: str = Query("en", max_length=20),
    req: TranscriptJobCreateRequest | None = None,
):
    """Launch asynchronous background job for channel transcripts."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    rid = uuid.uuid4().hex[:8]
    principal = _principal(request)
    try:
        eff_max = req.max_videos if req and req.max_videos > 0 else max_videos
        # 0 means "all eligible videos", bounded by the configured per-job cap
        eff_max = min(eff_max or settings.max_videos_per_job, settings.max_videos_per_job)
        eff_refresh = req.force_refresh if req else force_refresh
        eff_caption_conc = req.caption_concurrency if req else settings.transcript_max_concurrency
        eff_whisper_conc = req.whisper_concurrency if req else 1
        eff_pub_after = req.published_after if req and req.published_after else published_after
        eff_pub_before = req.published_before if req and req.published_before else published_before
        eff_out_lang = (req.output_language if req and req.output_language else output_language) or "en"

        progress = await transcript_job_manager.start_channel_job(
            channel_handle=handle,
            max_videos=eff_max,
            force_refresh=eff_refresh,
            caption_concurrency=eff_caption_conc,
            whisper_concurrency=eff_whisper_conc,
            published_after=eff_pub_after,
            published_before=eff_pub_before,
            output_language=eff_out_lang,
            owner=principal.subject if principal else None,
        )
        return success_response(
            data=progress.model_dump(),
            message=f"Transcript job {progress.job_id} queued for {progress.channel_title or handle}",
        )
    except Exception as exc:
        logger.exception("[%s] Failed to start channel transcript job: %s", rid, exc)
        return error_response(message=f"Failed to start job (trace {rid}).", status_code=500)


@app.get("/api/transcript/jobs/{job_id}")
async def api_get_transcript_job_status(request: Request, job_id: str = PathParam(pattern=_JOB_ID_PATTERN)):
    """Poll progress, counters, and video results for a background job."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    job = transcript_job_manager.get_job(job_id)
    if not job or not _can_access_transcript_job(request, job):
        return error_response(message=f"Job '{job_id}' not found", status_code=404)
    return success_response(data=job.model_dump())


@app.post("/api/transcript/jobs/{job_id}/cancel")
async def api_cancel_transcript_job(request: Request, job_id: str = PathParam(pattern=_JOB_ID_PATTERN)):
    """Cancel a running background transcript job."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    job = transcript_job_manager.get_job(job_id)
    if not job or not _can_access_transcript_job(request, job):
        return error_response(message=f"Job '{job_id}' could not be cancelled or not found", status_code=400)
    cancelled = await transcript_job_manager.cancel_job(job_id)
    if not cancelled:
        return error_response(message=f"Job '{job_id}' could not be cancelled or not found", status_code=400)
    return success_response(message=f"Job '{job_id}' successfully cancelled")


@app.post("/api/transcript/jobs/{job_id}/resume")
async def api_resume_transcript_job(request: Request, job_id: str = PathParam(pattern=_JOB_ID_PATTERN)):
    """Resume a pending, paused, or rate-limited background job."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    existing = transcript_job_manager.get_job(job_id)
    if not existing or not _can_access_transcript_job(request, existing):
        return error_response(message=f"Job '{job_id}' not found", status_code=404)
    job = await transcript_job_manager.resume_job(job_id)
    if not job:
        return error_response(message=f"Job '{job_id}' not found", status_code=404)
    return success_response(data=job.model_dump(), message=f"Job '{job_id}' resumed")



@app.get("/api/transcript/jobs/{job_id}/download")
async def api_download_transcript_job(
    request: Request, job_id: str = PathParam(pattern=_JOB_ID_PATTERN), audit: bool = False,
):
    """Download CSV for a transcript background job with exact mode representation."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    job = transcript_job_manager.get_job(job_id)
    if not job or not _can_access_transcript_job(request, job):
        return error_response(message=f"Job '{job_id}' not found", status_code=404)
    csv_content = transcript_job_manager.generate_csv(job_id, include_audit_columns=audit)
    clean_handle = _safe_filename_part((job.channel_handle or "channel").lstrip("@"))
    filename = f"{clean_handle}_transcripts_{job_id}.csv"
    encoded = csv_content.encode("utf-8-sig")
    return StreamingResponse(
        iter([encoded]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(encoded)),
        },
    )


# ---------------------------------------------------------------------------
# Transcript CSV Export
# ---------------------------------------------------------------------------


class TranscriptExportRequest(BaseModel):
    video_url: str | None = Field(None, max_length=2048)
    channel_handle: str | None = Field(None, pattern=_HANDLE_PATTERN)
    format: str = Field("csv", max_length=10)
    # Synchronous: every video is fetched inside the request, so the cap is lower
    max_videos: int = Field(
        default_factory=lambda: settings.max_videos_sync_export,
        ge=1,
        le=settings.max_videos_sync_export,
    )
    output_language: str = Field("original", pattern=_OUTPUT_MODE_PATTERN)
    include_audit_columns: bool = False


def _csv_escape(text: str | None) -> str:
    """Escape a string for CSV, handling commas, quotes, and newlines."""
    if text is None:
        return ""
    s = str(text)
    if "," in s or '"' in s or "\n" in s or "\r" in s:
        s = s.replace('"', '""')
        s = f'"{s}"'
    return s


def _build_csv_rows(videos: list[dict], output_language: str = "original", include_audit_columns: bool = False) -> str:
    """Build a compliant CSV string from a list of video dicts with exact mode representation.

    Columns:
        video_id, video_url, channel_id, channel_title, title, published_at,
        duration_seconds, duration, language, status, transcript,
        source, method, error_code, error_message
        (and optional audit columns: output_format, source_language, source_language_code)
    """
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

    out_mode = (output_language or "original").lower().strip()

    header = [
        "video_id",
        "video_url",
        "channel_id",
        "channel_title",
        "title",
        "published_at",
        "duration_seconds",
        "duration",
        "language",
        "status",
        "transcript",
        "source",
        "method",
        "error_code",
        "error_message",
    ]
    if include_audit_columns:
        header.extend(["output_format", "source_language", "source_language_code"])
    writer.writerow(header)

    for v in videos:
        # Determine exact transcript representation
        if out_mode in ("original", "original_spoken"):
            transcript_text = v.get("raw_transcript") or v.get("transcript", "")
        elif out_mode == "hi":
            transcript_text = v.get("simple_hindi_transcript") or v.get("transcript", "")
        else:
            transcript_text = v.get("simple_english_transcript") or v.get("transcript", "")

        if isinstance(transcript_text, dict):
            transcript_text = transcript_text.get("plain_text") or transcript_text.get("paragraph_text") or ""

        vid = v.get("video_id", "")
        url = v.get("video_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
        status = v.get("status") or ("success" if transcript_text else "failed")

        # Determine true source language without hardcoded corruption
        raw_lang = v.get("source_language") or v.get("language", "")
        is_hindi = (
            str(raw_lang).lower() in ("hi", "hindi")
            or (transcript_text and english_converter.contains_non_roman_script(str(transcript_text)))
        )
        if is_hindi:
            source_lang = "Hindi"
            source_code = "hi"
        elif str(raw_lang).lower().startswith("en"):
            source_lang = "English"
            source_code = "en"
        else:
            source_lang = raw_lang or "auto"
            source_code = str(raw_lang).lower()[:2] if raw_lang else "auto"

        if out_mode in ("original", "original_spoken"):
            lang = source_lang
        elif v.get("output_language_label"):
            # Set by the translation step; reports the real language if translation fell back
            lang = v["output_language_label"]
        elif out_mode == "hi":
            lang = "Simple Hindi"
        else:
            lang = "Simple English"

        row = [
            vid,
            url,
            v.get("channel_id", ""),
            v.get("channel_title", ""),
            v.get("title", ""),
            v.get("published_at", ""),
            v.get("duration_seconds", ""),
            v.get("duration", "") or v.get("duration_readable", ""),
            lang,
            status,
            transcript_text,
            v.get("source", ""),
            v.get("method", ""),
            v.get("error_code", ""),
            v.get("error_message", "") or v.get("error", ""),
        ]
        if include_audit_columns:
            row.extend([out_mode, source_lang, source_code])
        writer.writerow(row)

    return output.getvalue()


def _resolve_video_id(url: str) -> str | None:
    """Extract a YouTube video ID from a URL."""
    patterns = [
        r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


@app.post("/api/transcript/export")
async def api_transcript_csv_export(req: TranscriptExportRequest):
    """Export transcripts as CSV for a single video or entire channel.

    Accepts either a video_url or a channel_handle. Returns a downloadable
    CSV file with one row per video. Never aborts on individual video failures.
    """
    rid = uuid.uuid4().hex[:8]
    start_time = time.time()
    logger.info("[%s] Transcript CSV export request: video_url=%s, channel_handle=%s",
                rid, req.video_url, req.channel_handle)

    if not req.video_url and not req.channel_handle:
        return error_response(
            message="Either video_url or channel_handle is required.",
            status_code=400,
        )

    try:
        videos_data: list[dict] = []

        if req.channel_handle:
            # --- Channel handle path ---
            channel_svc = _get_channel_service()
            video_svc = _get_video_service()
            transcript_svc = _get_transcript_service()

            clean = req.channel_handle.lstrip("@")

            # Resolve channel
            channel_data = await _to_thread(channel_svc.resolve_handle, clean)
            channel_id = channel_data["id"]
            channel_title = channel_data["snippet"]["title"]

            # Get upload playlist
            playlist_id = await _to_thread(video_svc.get_uploads_playlist_id, channel_id)

            # Paginate playlist for video IDs
            all_video_ids: list[str] = []
            next_page: str | None = None
            while len(all_video_ids) < req.max_videos:
                page = await _to_thread(
                    video_svc.get_playlist_items, playlist_id, next_page,
                )
                video_ids = page.get("video_ids", [])
                for vid in video_ids:
                    if vid and vid not in all_video_ids:
                        all_video_ids.append(vid)
                        if len(all_video_ids) >= req.max_videos:
                            break
                next_page = page.get("next_page_token")
                if not next_page:
                    break

            logger.info("[%s] Discovered %d videos for CSV export", rid, len(all_video_ids))

            # Fetch metadata for duration filtering
            videos_meta: dict[str, dict] = {}
            for i in range(0, len(all_video_ids), 50):
                batch = all_video_ids[i:i + 50]
                try:
                    items = await _to_thread(video_svc.get_videos_batch, batch)
                    for item in items:
                        vid = item.get("id")
                        if not vid:
                            continue
                        snippet = item.get("snippet", {})
                        content_details = item.get("contentDetails", {})
                        duration_iso = content_details.get("duration", "PT0S")
                        duration_seconds = _parse_iso_duration(duration_iso)
                        live_status = snippet.get("liveBroadcastContent", "none")
                        videos_meta[vid] = {
                            "title": snippet.get("title", ""),
                            "published_at": snippet.get("publishedAt", ""),
                            "duration_seconds": duration_seconds,
                            "duration_readable": _format_duration(duration_seconds),
                            "live_status": live_status,
                        }
                except Exception as exc:
                    logger.warning("[%s] Metadata batch failed for %d videos: %s", rid, len(batch), exc)

            # Filter 3:00 <= duration < 30:00 and exclude live streams using evaluate_duration
            from services.duration_filter import evaluate_duration
            eligible: list[str] = []
            for vid in all_video_ids:
                meta = videos_meta.get(vid, {})
                duration = meta.get("duration_seconds", 0)
                live_status = meta.get("live_status", "none")
                f_res = evaluate_duration(
                    duration_seconds=duration,
                    live_status=live_status,
                    min_seconds=180,
                    max_seconds=1800,
                )
                if f_res.is_eligible:
                    eligible.append(vid)

            logger.info("[%s] %d eligible (3–30 min, no live), fetching transcripts...", rid, len(eligible))

            semaphore = asyncio.Semaphore(5)

            async def _fetch_one(video_id: str) -> dict:
                async with semaphore:
                    meta = videos_meta.get(video_id, {})
                    title = meta.get("title", "")
                    published_at = meta.get("published_at", "")
                    dur_sec = meta.get("duration_seconds", 0)
                    dur_str = meta.get("duration_readable", "0:00")
                    video_url = f"https://www.youtube.com/watch?v={video_id}"

                    try:
                        transcript = await _to_thread(
                            transcript_svc.get_transcript,
                            video_id,
                            allow_whisper=settings.whisper_enabled,
                            video_title=title,
                            channel_title=channel_title,
                            output_format=req.output_language,
                        )
                        if transcript and transcript.success and (transcript.plain_text or transcript.paragraph_text):
                            src = getattr(transcript.source, "value", str(transcript.source)) if transcript.source else "youtube"
                            mth = getattr(transcript, "method", None) or ("speech_to_text" if src == "whisper" else "caption")
                            raw_text = transcript.raw_transcript or transcript.plain_text or transcript.paragraph_text or ""
                            out_text, out_label, _ = await _localized_transcript(
                                video_id, raw_text, transcript.source_language, req.output_language, rid,
                            )
                            return {
                                "video_id": video_id,
                                "video_url": video_url,
                                "channel_id": channel_id,
                                "channel_title": channel_title,
                                "title": title,
                                "published_at": published_at,
                                "duration_seconds": dur_sec,
                                "duration": dur_str,
                                "language": transcript.source_language or transcript.language or ("Hindi" if english_converter.contains_non_roman_script(transcript.raw_transcript or transcript.plain_text) else "English"),
                                "source_language": transcript.source_language or ("Hindi" if english_converter.contains_non_roman_script(transcript.raw_transcript or transcript.plain_text) else "English"),
                                "source_language_code": transcript.source_language_code or ("hi" if english_converter.contains_non_roman_script(transcript.raw_transcript or transcript.plain_text) else "en"),
                                "status": "success",
                                "output_language_label": out_label,
                                "transcript": out_text,
                                "raw_transcript": raw_text,
                                "source": src,
                                "method": mth,
                                "error_code": None,
                                "error_message": None,
                            }
                        else:
                            return {
                                "video_id": video_id,
                                "video_url": video_url,
                                "channel_id": channel_id,
                                "channel_title": channel_title,
                                "title": title,
                                "published_at": published_at,
                                "duration_seconds": dur_sec,
                                "duration": dur_str,
                                "language": "English (India)",
                                "status": "failed",
                                "transcript": "",
                                "source": None,
                                "method": None,
                                "error_code": getattr(transcript, "error_code", None) or "NO_CAPTIONS",
                                "error_message": getattr(transcript, "error", None) or "No captions available",
                            }
                    except Exception as exc:
                        logger.warning("[%s] Transcript fetch failed for %s: %s", rid, video_id, exc)
                        return {
                            "video_id": video_id,
                            "video_url": video_url,
                            "channel_id": channel_id,
                            "channel_title": channel_title,
                            "title": title,
                            "published_at": published_at,
                            "duration_seconds": dur_sec,
                            "duration": dur_str,
                            "language": "English (India)",
                            "status": "failed",
                            "transcript": "",
                            "source": None,
                            "method": None,
                            "error_code": "EXTRACTION_ERROR",
                            "error_message": "Speech-to-text failed.",
                        }

            tasks = [_fetch_one(vid) for vid in eligible]
            raw = await asyncio.gather(*tasks)
            videos_data = list(raw)

            logger.info("[%s] CSV export: %d rows generated for channel %s",
                        rid, len(videos_data), channel_title)

        elif req.video_url:
            # --- Single video URL path ---
            video_svc = _get_video_service()
            transcript_svc = _get_transcript_service()
            video_id = _resolve_video_id(req.video_url)
            if not video_id:
                return error_response(
                    message="Could not extract a valid video ID from the URL.",
                    status_code=400,
                )

            # Fetch minimal metadata
            title = ""
            published_at = ""
            duration = "0:00"
            duration_seconds = 0
            channel_id = ""
            channel_title = ""
            try:
                items = await _to_thread(video_svc.get_videos_batch, [video_id])
                if items:
                    snippet = items[0].get("snippet", {})
                    cd = items[0].get("contentDetails", {})
                    title = snippet.get("title", "")
                    published_at = snippet.get("publishedAt", "")
                    channel_id = snippet.get("channelId", "")
                    channel_title = snippet.get("channelTitle", "")
                    duration_seconds = _parse_iso_duration(cd.get("duration", "PT0S"))
                    duration = _format_duration(duration_seconds)
            except Exception as exc:
                logger.warning("[%s] Metadata fetch failed for %s: %s", rid, video_id, exc)

            # Fetch transcript (captions first, then whisper fallback)
            transcript_text = ""
            status = "failed"
            source = None
            method = None
            language = "en"
            error_code = None
            error_message = None
            try:
                transcript = await _to_thread(
                    transcript_svc.get_transcript,
                    video_id,
                    allow_whisper=settings.whisper_enabled,
                    video_title=title,
                    channel_title=channel_title,
                    output_format=req.output_language,
                )
                if transcript and transcript.success and (transcript.plain_text or transcript.paragraph_text or getattr(transcript, "raw_transcript", None)):
                    raw_text = getattr(transcript, "raw_transcript", "") or transcript.plain_text or transcript.paragraph_text or ""
                    status = "success"
                    source = getattr(transcript.source, "value", str(transcript.source)) if transcript.source else "youtube"
                    method = getattr(transcript, "method", None) or ("speech_to_text" if source == "whisper" else "caption")
                    is_hindi_vid = english_converter.contains_non_roman_script(raw_text) or str(transcript.language).lower() in ("hi", "hindi")
                    source_lang = transcript.source_language or ("Hindi" if is_hindi_vid else "English")
                    source_code = transcript.source_language_code or ("hi" if is_hindi_vid else "en")

                    # 'original' -> verbatim source; 'en'/'hi' -> explicit translation step
                    transcript_text, language, _ = await _localized_transcript(
                        video_id, raw_text, source_lang, req.output_language, rid,
                    )
                else:
                    error_code = getattr(transcript, "error_code", None) or "NO_CAPTIONS"
                    error_message = getattr(transcript, "error", None) or "No captions available"
            except Exception as exc:
                logger.warning("[%s] Transcript fetch failed for %s: %s", rid, video_id, exc)
                error_code = "EXTRACTION_ERROR"
                error_message = "Transcript extraction failed."

            videos_data = [{
                "video_id": video_id,
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
                "channel_id": channel_id,
                "channel_title": channel_title,
                "title": title,
                "published_at": published_at,
                "duration_seconds": duration_seconds,
                "duration": duration,
                "language": language,
                "output_language_label": language if status == "success" else None,
                "source_language": source_lang if 'source_lang' in locals() else language,
                "source_language_code": source_code if 'source_code' in locals() else "en",
                "status": status,
                "transcript": transcript_text,
                "raw_transcript": getattr(transcript, "raw_transcript", "") if transcript else "",
                "source": source,
                "method": method,
                "error_code": error_code,
                "error_message": error_message,
            }]

            logger.info("[%s] CSV export: 1 row for video %s", rid, video_id)

        # Generate CSV
        csv_content = _build_csv_rows(
            videos_data,
            output_language=req.output_language,
            include_audit_columns=req.include_audit_columns,
        )
        elapsed = round(time.time() - start_time, 2)
        logger.info("[%s] CSV export complete: %d rows, %d bytes, %.2fs elapsed",
                    rid, len(videos_data), len(csv_content.encode("utf-8")), elapsed)

        filename = "transcripts.csv"
        if req.channel_handle:
            clean = _safe_filename_part(req.channel_handle.lstrip("@"))
            filename = f"{clean}_transcripts.csv"

        return StreamingResponse(
            iter([csv_content.encode("utf-8-sig")]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(csv_content.encode("utf-8-sig"))),
            },
        )

    except Exception as exc:
        logger.exception("[%s] CSV export failed", rid)
        return error_response(
            message=f"Transcript CSV export failed (trace {rid}).",
            status_code=500,
        )



# --- Validation endpoint ---

@app.get("/api/validate-url")
async def api_validate_url(url: str = Query("", max_length=2048)) -> dict:
    from services.youtube_url_parser import YouTubeURLParser
    parser = YouTubeURLParser()
    result = await _to_thread(parser.parse, url)
    return result.model_dump()


# --- Video metadata endpoint ---

@app.get("/api/video-metadata/{video_id}")
async def api_video_metadata(video_id: str = PathParam(pattern=_VIDEO_ID_PATTERN)) -> dict:
    service = _get_metadata_service()
    result = await _to_thread(service.get_metadata, video_id)
    return result.model_dump()


# ---------------------------------------------------------------------------
# SPA catch-all
# ---------------------------------------------------------------------------


@app.api_route("/{path:path}", methods=["GET"])
async def spa_catch_all(path: str):
    if (
        path.startswith("api")
        or path.startswith("static")
        or path.startswith("assets")
        or path.startswith("docs")
        or path.startswith("openapi")
        or path.startswith("redoc")
    ):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return _spa_index()
