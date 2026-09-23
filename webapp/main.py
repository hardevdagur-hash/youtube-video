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

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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


app = FastAPI(title="YouTube Export Tool v3 (Async)", lifespan=lifespan)

# Observability: create orchestrator at module level; initialize() called in lifespan
_telemetry = TelemetryOrchestrator(ObservabilityConfig.from_env())

# Network/transport middleware first (innermost wrapping)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.cors_origins] if settings.cors_origins != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestValidationMiddleware)

# Instrument FastAPI last (outermost wrapping â€” captures all requests)
instrument_fastapi(app, _telemetry.metrics, _telemetry.tracing)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
_frontend_assets = FRONTEND_DIST / "assets"
_frontend_assets.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(_frontend_assets)), name="frontend-assets")


@app.middleware("http")
async def add_request_id_middleware(request: Request, call_next):
    rid = uuid.uuid4().hex[:8]
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
    rid = uuid.uuid4().hex[:8]
    logger.exception("[%s] Unhandled exception on %s %s: %s", rid, request.method, request.url.path, exc)
    import traceback
    content: dict = {
        "success": False,
        "error": str(exc) or "An unexpected error occurred.",
        "trace_id": rid,
        "exception_type": type(exc).__name__,
    }
    return JSONResponse(
        status_code=500,
        content=content,
    )


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

    channel_input = body.get("channel", "").strip()
    limit = int(body.get("limit", 0))

    if not channel_input:
        return JSONResponse(status_code=400, content={"success": False, "error": "Channel identifier cannot be empty.", "trace_id": rid})

    client_host = getattr(request.client, "host", None) or "unknown"
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

    task = asyncio.create_task(_run_async_pipeline(job_id, export_req))
    async with _running_tasks_lock:
        _running_tasks[job_id] = task

    logger.info("[%s] Async export started: channel=%s, limit=%d, job_id=%s", rid, channel_input, limit, job_id)

    return {"success": True, "job_id": job_id, "trace_id": rid, "status": "pending"}


@app.get("/api/export/{job_id}/progress")
async def api_export_progress(job_id: str) -> dict:
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return {"success": False, "error": "Invalid job ID", "trace_id": rid}

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
async def api_export_result(job_id: str) -> dict:
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return {"success": False, "error": "Invalid job ID", "trace_id": rid}

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
async def api_export_download(job_id: str):
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid job ID", "trace_id": rid})

    csv_path = RUNS_DIR / job_id / "videos.csv"
    if not csv_path.exists():
        return JSONResponse(status_code=404, content={"success": False, "error": "CSV file not found", "trace_id": rid})
    if csv_path.stat().st_size == 0:
        return JSONResponse(status_code=422, content={"success": False, "error": "CSV file is empty", "trace_id": rid})

    return FileResponse(str(csv_path), media_type="text/csv", filename="videos.csv")


@app.post("/api/export/{job_id}/cancel")
async def api_export_cancel(job_id: str) -> dict:
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return {"success": False, "error": "Invalid job ID", "trace_id": rid}

    async with _running_tasks_lock:
        task = _running_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            _running_tasks.pop(job_id, None)
            logger.info("[%s] Job %s cancelled", rid, job_id)
            return {"success": True, "job_id": job_id, "status": "cancelled", "trace_id": rid}
    return {"success": False, "error": "Job not running or already completed", "trace_id": rid}


@app.delete("/api/export/{job_id}")
async def api_export_delete(job_id: str) -> dict:
    rid = uuid.uuid4().hex[:8]
    if not _safe_run_id(job_id):
        return {"success": False, "error": "Invalid job ID", "trace_id": rid}
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
async def api_health():
    key_ok, key_error = is_youtube_api_key_valid()
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
    output_language: str = "original"  # "original" | "en" | "hi"


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
      8. If output_language is 'en' or 'hi', derives on-demand translation with caching
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
    out_lang = (request.output_language or "original").lower().strip()
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

        # Handle on-demand translation if requested
        final_text = canonical_text
        final_segments = segments
        final_provider = provider
        final_from_cache = from_cache

        if out_lang in ("en", "hi"):
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
            "output_language": out_lang,
            "provider": final_provider,
            "transcript": final_text,
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


@app.get("/api/transcript/{video_id}")
async def api_transcript(video_id: str, language: str | None = None, force_refresh: bool = False, allow_whisper: bool = True) -> dict:
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
async def api_transcript_all(video_id: str) -> dict:
    rid = uuid.uuid4().hex[:8]
    service = _get_transcript_service()
    return await _to_thread(service.get_all_transcripts, video_id)


@app.get("/api/transcript/{video_id}/status")
async def api_transcript_status(video_id: str) -> dict:
    service = _get_transcript_service()
    return await _to_thread(service.get_transcript_status, video_id)


@app.get("/api/transcript/{video_id}/translate/{target_language}")
async def api_translate_transcript(video_id: str, target_language: str) -> dict:
    rid = uuid.uuid4().hex[:8]
    try:
        service = _get_transcript_service()
        result = await _to_thread(service.translate_transcript, video_id=video_id, target_language=target_language)
        return result.model_dump()
    except Exception as exc:
        logger.exception("[%s] Translation failed", rid)
        raise


@app.get("/api/transcript/{video_id}/list-all")
async def api_transcript_list_all(video_id: str) -> dict:
    try:
        service = _get_transcript_service()
        return await _to_thread(service.get_transcript_status, video_id)
    except Exception as exc:
        return {"success": False, "video_id": video_id, "error": str(exc)}


@app.post("/api/transcript/{video_id}/process")
async def api_process_transcript(video_id: str, remove_fillers: bool = False) -> dict:
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
    segments = body.get("segments", [])
    vid = body.get("video_id", "")
    remove = body.get("remove_fillers", False)
    if not segments:
        raise HTTPException(status_code=400, detail={"success": False, "error": "No segments provided."})
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
async def api_transcript_v2(video_id: str):
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
        language = transcript.language or "English (India)"
        if language and language.lower() in ("hinglish", "en", "english", "en-in", "hi", "hindi", "english (india)"):
            language = "English (India)"
        if not transcript.success:
            error_code = getattr(transcript, "error_code", None) or "TRANSCRIPT_NOT_FOUND"
            error_message = transcript.error or "Failed to fetch transcript"

    if status == "failed":
        if not error_code:
            error_code = "TRANSCRIPT_NOT_FOUND"
            error_message = "No transcript could be extracted for this video."
        if error_code == "VIDEO_UNAVAILABLE" and not error_message:
            error_message = "This video is unavailable, private, or deleted."

    script = "Roman"

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
    handle: str,
    limit: int = 100,
    concurrency: int = 5,
    allow_whisper: bool = True,
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
        semaphore = asyncio.Semaphore(concurrency)

        async def _fetch_one(video_id: str, idx: int) -> dict:
            async with semaphore:
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
                    )
                    if res.success and (res.plain_text or res.paragraph_text):
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
                            "language": res.language or "en",
                            "status": "success",
                            "transcript": res.plain_text or res.paragraph_text or "",
                            "raw_transcript": getattr(res, "raw_transcript", "") or (res.plain_text or res.paragraph_text or ""),
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
                        )
                        if w_res.success and (w_res.plain_text or w_res.paragraph_text):
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
                                "language": w_res.language or "English (India)",
                                "status": "success",
                                "transcript": w_res.plain_text or w_res.paragraph_text or "",
                                "raw_transcript": getattr(w_res, "raw_transcript", "") or (w_res.plain_text or w_res.paragraph_text or ""),
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
                            "error_message": f"Speech-to-text failed: {exc}",
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
            message=f"Failed to fetch channel transcripts: {exc}",
            status_code=500,
        )


# --- Transcript Background Job endpoints ---


class TranscriptJobCreateRequest(BaseModel):
    max_videos: int = 0
    force_refresh: bool = False
    caption_concurrency: int = 5
    whisper_concurrency: int = 1


@app.post("/api/channel/{handle}/transcript-job")
async def api_start_channel_transcript_job(
    handle: str,
    max_videos: int = 0,
    force_refresh: bool = False,
    req: TranscriptJobCreateRequest | None = None,
):
    """Launch asynchronous background job for channel transcripts."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    try:
        eff_max = req.max_videos if req and req.max_videos > 0 else max_videos
        eff_refresh = req.force_refresh if req else force_refresh
        eff_caption_conc = req.caption_concurrency if req else 5
        eff_whisper_conc = req.whisper_concurrency if req else 1
        progress = await transcript_job_manager.start_channel_job(
            channel_handle=handle,
            max_videos=eff_max,
            force_refresh=eff_refresh,
            caption_concurrency=eff_caption_conc,
            whisper_concurrency=eff_whisper_conc,
        )
        return success_response(
            data=progress.model_dump(),
            message=f"Transcript job {progress.job_id} queued for {progress.channel_title or handle}",
        )
    except Exception as exc:
        logger.exception("Failed to start channel transcript job: %s", exc)
        return error_response(message=f"Failed to start job: {exc}", status_code=500)


@app.get("/api/transcript/jobs/{job_id}")
async def api_get_transcript_job_status(job_id: str):
    """Poll progress, counters, and video results for a background job."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    job = transcript_job_manager.get_job(job_id)
    if not job:
        return error_response(message=f"Job '{job_id}' not found", status_code=404)
    return success_response(data=job.model_dump())


@app.post("/api/transcript/jobs/{job_id}/cancel")
async def api_cancel_transcript_job(job_id: str):
    """Cancel a running background transcript job."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    cancelled = await transcript_job_manager.cancel_job(job_id)
    if not cancelled:
        return error_response(message=f"Job '{job_id}' could not be cancelled or not found", status_code=400)
    return success_response(message=f"Job '{job_id}' successfully cancelled")


@app.get("/api/transcript/jobs/{job_id}/download")
async def api_download_transcript_job(job_id: str):
    """Download 15-column CSV for a transcript background job."""
    from services.jobs.transcript_job_manager import transcript_job_manager
    job = transcript_job_manager.get_job(job_id)
    if not job:
        return error_response(message=f"Job '{job_id}' not found", status_code=404)
    csv_content = transcript_job_manager.generate_csv(job_id)
    clean_handle = (job.channel_handle or "channel").lstrip("@")
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
    video_url: str | None = None
    channel_handle: str | None = None
    format: str = "csv"
    max_videos: int = 1000


def _csv_escape(text: str | None) -> str:
    """Escape a string for CSV, handling commas, quotes, and newlines."""
    if text is None:
        return ""
    s = str(text)
    if "," in s or '"' in s or "\n" in s or "\r" in s:
        s = s.replace('"', '""')
        s = f'"{s}"'
    return s


def _build_csv_rows(videos: list[dict]) -> str:
    """Build a complete 15-column CSV string from a list of video dicts.

    Columns:
        video_id, video_url, channel_id, channel_title, title, published_at,
        duration_seconds, duration, language, status, transcript,
        source, method, error_code, error_message
    """
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

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
    writer.writerow(header)

    for v in videos:
        transcript_text = v.get("transcript", "")
        if isinstance(transcript_text, dict):
            transcript_text = transcript_text.get("plain_text") or transcript_text.get("paragraph_text") or ""

        vid = v.get("video_id", "")
        url = v.get("video_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
        status = v.get("status") or ("success" if transcript_text else "failed")

        lang = v.get("language", "")
        if lang and str(lang).lower() in ("hinglish", "en", "english", "en-in", "hi", "hindi", "english (india)"):
            lang = "English (India)"

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
                        )
                        if transcript and transcript.success and (transcript.plain_text or transcript.paragraph_text):
                            src = getattr(transcript.source, "value", str(transcript.source)) if transcript.source else "youtube"
                            mth = getattr(transcript, "method", None) or ("speech_to_text" if src == "whisper" else "caption")
                            return {
                                "video_id": video_id,
                                "video_url": video_url,
                                "channel_id": channel_id,
                                "channel_title": channel_title,
                                "title": title,
                                "published_at": published_at,
                                "duration_seconds": dur_sec,
                                "duration": dur_str,
                                "language": transcript.language or "English (India)",
                                "status": "success",
                                "transcript": transcript.plain_text or transcript.paragraph_text or "",
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
                            "error_message": f"Speech-to-text failed: {exc}",
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
                )
                if transcript and transcript.success and (transcript.plain_text or transcript.paragraph_text):
                    transcript_text = transcript.plain_text or transcript.paragraph_text or ""
                    status = "success"
                    source = getattr(transcript.source, "value", str(transcript.source)) if transcript.source else "youtube"
                    method = getattr(transcript, "method", None) or ("speech_to_text" if source == "whisper" else "caption")
                    language = transcript.language or "English (India)"
                else:
                    error_code = getattr(transcript, "error_code", None) or "NO_CAPTIONS"
                    error_message = getattr(transcript, "error", None) or "No captions available"
            except Exception as exc:
                logger.warning("[%s] Transcript fetch failed for %s: %s", rid, video_id, exc)
                error_code = "EXTRACTION_ERROR"
                error_message = str(exc)

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
                "status": status,
                "transcript": transcript_text,
                "source": source,
                "method": method,
                "error_code": error_code,
                "error_message": error_message,
            }]

            logger.info("[%s] CSV export: 1 row for video %s", rid, video_id)

        # Generate CSV
        csv_content = _build_csv_rows(videos_data)
        elapsed = round(time.time() - start_time, 2)
        logger.info("[%s] CSV export complete: %d rows, %d bytes, %.2fs elapsed",
                    rid, len(videos_data), len(csv_content.encode("utf-8")), elapsed)

        filename = "transcripts.csv"
        if req.channel_handle:
            clean = req.channel_handle.lstrip("@")
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
            message=f"Transcript CSV export failed: {exc}",
            status_code=500,
        )



# --- Validation endpoint ---

@app.get("/api/validate-url")
async def api_validate_url(url: str = "") -> dict:
    from services.youtube_url_parser import YouTubeURLParser
    parser = YouTubeURLParser()
    result = await _to_thread(parser.parse, url)
    return result.model_dump()


# --- Video metadata endpoint ---

@app.get("/api/video-metadata/{video_id}")
async def api_video_metadata(video_id: str) -> dict:
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
