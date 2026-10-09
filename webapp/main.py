"""
YouTube Transcript Service — FastAPI application.

Single-instance, single-worker service. Background channel jobs run as asyncio
tasks owned by ``TranscriptJobManager`` and are checkpointed to disk so they
survive restarts.

Endpoints
=========
GET    /api/health                                Liveness/readiness (public)
POST   /api/auth/login | /api/auth/logout         Session cookie auth (public)
GET    /api/auth/me                               Current principal
POST   /api/transcript                            Single video transcript (+ translation)
GET    /api/channel/{handle}/transcripts          Synchronous channel transcripts (bounded)
POST   /api/channel/{handle}/transcript-job       Start background channel job
GET    /api/transcript/jobs/{job_id}              Job status / progress / results
POST   /api/transcript/jobs/{job_id}/cancel       Cancel job
POST   /api/transcript/jobs/{job_id}/resume       Resume job
GET    /api/transcript/jobs/{job_id}/download     Job CSV export
POST   /api/transcript/export                     Synchronous CSV export (video or channel)
GET    /api/validate-url                          Parse/validate a YouTube URL
GET    /api/transcript/metrics                    Transcript counters (admin)
GET    /api/transcript/limiter/status             Rate limiter state (admin)
"""

from __future__ import annotations

import asyncio
import csv
import functools
import io
import logging
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config.settings import is_youtube_api_key_valid, settings
from infrastructure.logging import setup_logging
from infrastructure.request_context import (
    current_request_id,
    new_request_id,
    request_id_var,
    user_var,
)
from infrastructure.validation import RequestValidationMiddleware
from infrastructure.work_pool import run_transcript_work
from infrastructure.work_pool import shutdown as shutdown_transcript_work
from models.api_response import error_response, success_response
from security.google_oauth import (
    STATE_COOKIE,
    STATE_COOKIE_PATH,
    STATE_TTL_SECONDS,
    GoogleAuthError,
)
from security.google_users import GoogleUserStore
from security.web_auth import (
    SESSION_COOKIE,
    AuthMiddleware,
    AuthSettings,
    Principal,
    WebAuthenticator,
    can_access_owned,
    cors_options,
)
from services.csv_safety import safe_csv_row
from services.english_converter import english_converter

setup_logging()

CURRENT_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = CURRENT_DIR.parent / "frontend" / "dist"

logger = logging.getLogger("webapp")


def _to_thread(func, *args, **kwargs):
    """Run short blocking work in the default thread pool, keeping the logging context (request id)."""
    return asyncio.to_thread(func, *args, **kwargs)


def _transcript_work(func, *args, **kwargs):
    """Run long transcript work (captions, audio, speech-to-text, translation) on its own
    bounded pool, so it can never starve sign-in, health checks or other short calls."""
    return run_transcript_work(func, *args, **kwargs)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


_RETENTION_SWEEP_SECONDS = 3600


async def _retention_sweeper(manager) -> None:
    """Hourly job-retention sweep; failures are logged and retried next hour."""
    while True:
        await asyncio.sleep(_RETENTION_SWEEP_SECONDS)
        try:
            await manager.run_retention_cleanup()
        except Exception:
            logger.exception("Transcript job retention sweep failed")


def _clear_stale_audio() -> None:
    """Delete speech-to-text audio left behind by a crash or kill.

    Runs before the app serves requests; with a single instance nothing can be using
    these files yet. Failures are logged and never block startup.
    """
    audio_dir = settings.audio_temp_dir
    removed = 0
    try:
        entries = list(audio_dir.iterdir()) if audio_dir.is_dir() else []
    except OSError as exc:
        logger.warning("Could not list audio temp dir: %s", exc)
        return
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.warning("Could not remove stale audio %s: %s", entry.name, exc)
    if removed:
        logger.info("Removed %d stale audio file(s) from an earlier run", removed)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from services.jobs.transcript_job_manager import transcript_job_manager

    logger.info(
        "Transcript service starting (env=%s, data_dir=%s, stt_backend=%s)",
        _auth_settings.app_env, settings.data_dir, settings.stt_backend,
    )
    await _to_thread(_clear_stale_audio)
    # Single instance: anything left "running" on disk was interrupted by a crash or kill.
    await _to_thread(transcript_job_manager.recover_interrupted_jobs)
    await transcript_job_manager.run_retention_cleanup()
    sweeper = asyncio.create_task(_retention_sweeper(transcript_job_manager))
    try:
        yield
    finally:
        sweeper.cancel()
        await transcript_job_manager.shutdown()
        shutdown_transcript_work()
        logger.info("Transcript service stopped")


# Security configuration: fails fast in production when credentials/secrets are unsafe
_auth_settings = AuthSettings.from_env()
_auth_settings.validate()
# Google accounts persist in DATA_DIR like jobs and transcripts (included in backups).
_authenticator = WebAuthenticator(_auth_settings, google_users=GoogleUserStore(settings.data_dir / "users"))

app = FastAPI(
    title="YouTube Transcript Service",
    lifespan=lifespan,
    # API schema/explorer are not exposed in production
    openapi_url=None if _auth_settings.is_production else "/openapi.json",
    docs_url=None if _auth_settings.is_production else "/docs",
    redoc_url=None if _auth_settings.is_production else "/redoc",
)

# Middleware (last added = outermost):
#   CORS -> request validation -> auth/authz/rate limit -> routes
app.add_middleware(AuthMiddleware, authenticator=_authenticator)
app.add_middleware(RequestValidationMiddleware)
app.add_middleware(CORSMiddleware, **cors_options(_auth_settings))  # '*' is refused in production

# Built SPA assets (frontend/dist). Mounted only when present so the API can run without a build.
for _mount, _sub in (("/assets", "assets"), ("/static", "static")):
    _dir = FRONTEND_DIST / _sub
    if _dir.is_dir():
        app.mount(_mount, StaticFiles(directory=str(_dir)), name=f"frontend-{_sub}")


_access_logger = logging.getLogger("webapp.access")
# Probes and static files are logged at DEBUG so INFO logs stay readable.
_QUIET_PATHS = ("/api/health", "/assets/", "/static/")


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """Outermost middleware: request id, security headers and one access-log line per request."""
    rid = new_request_id(request.headers.get("x-request-id"))
    request.state.request_id = rid
    rid_token = request_id_var.set(rid)
    user_token = user_var.set("")
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Private application: nothing here is meant for search engines.
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        path = request.url.path
        quiet = path.startswith(_QUIET_PATHS) and status < 400
        principal = getattr(request.state, "principal", None)
        if principal is not None:
            user_var.set(principal.subject)
        _access_logger.log(
            logging.DEBUG if quiet else logging.INFO,
            "%s %s -> %d in %.1f ms", request.method, path, status, duration_ms,
            extra={
                "method": request.method, "path": path, "status": status, "duration_ms": duration_ms,
                "user": principal.subject if principal is not None else "-",
            },
        )
        user_var.reset(user_token)
        request_id_var.reset(rid_token)



@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full details go to the server log only; clients get a generic message + trace id.
    rid = getattr(request.state, "request_id", None) or current_request_id()
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


def _can_access_transcript_job(request: Request, job) -> bool:
    return can_access_owned(_principal(request), getattr(job, "owner", None))


_JOB_ID_PATTERN = r"^[0-9a-f]{12}$"
_HANDLE_PATTERN = r"^@?[A-Za-z0-9._-]{1,100}$"
_MAX_CONCURRENCY = 10


def _safe_filename_part(text: str) -> str:
    """Restrict user-influenced text used in Content-Disposition filenames."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:80] or "channel"


@app.post("/api/auth/login")
async def api_auth_login(body: LoginRequest, request: Request):
    ip = _authenticator.client_ip(request)
    username = body.username.strip()
    throttle = _authenticator.login_throttle
    decision = throttle.check(ip, username)
    if not decision.allowed:
        logger.warning(
            "Login throttled (%s) for user=%r from ip=%s; retry after %ds",
            decision.reason, username[:64], ip, decision.retry_after,
        )
        return JSONResponse(
            status_code=429,
            content={"success": False, "error": "Too many login attempts. Try again later.", "error_code": "RATE_LIMITED"},
            headers={"Retry-After": str(decision.retry_after)},
        )
    if not _authenticator.origin_allowed(request):
        return JSONResponse(status_code=403, content={"success": False, "error": "Cross-site request rejected.", "error_code": "CSRF_REJECTED"})
    principal = await _to_thread(_authenticator.authenticate_password, username, body.password)
    if principal is None:
        failures = throttle.record_failure(ip, username)
        logger.warning("Failed login for user=%r from ip=%s (consecutive failures from this ip: %d)", username[:64], ip, failures)
        return JSONResponse(status_code=401, content={"success": False, "error": "Invalid username or password.", "error_code": "INVALID_CREDENTIALS"})
    throttle.record_success(ip, username)
    response = JSONResponse(content={"success": True, "user": {"username": principal.subject, "role": principal.role}})
    _start_session(response, principal)
    logger.info("User %s logged in from ip=%s", principal.subject, ip)
    return response


def _start_session(response, principal: Principal) -> None:
    """The one way a browser session begins (password or Google): the existing JWT cookie."""
    response.set_cookie(
        SESSION_COOKIE,
        _authenticator.issue_session(principal),
        max_age=_auth_settings.session_ttl_minutes * 60,
        httponly=True,
        secure=_auth_settings.is_production,
        samesite="strict",
        path="/",
    )


@app.get("/api/auth/providers")
async def api_auth_providers():
    """Which sign-in methods the login page should offer (public)."""
    return {
        "success": True,
        "providers": {
            "password": bool(_auth_settings.users),
            "google": _authenticator.google_enabled,
        },
    }


_APP_HOME = "/transcript"


def _google_redirect(target: str) -> RedirectResponse:
    """Same-origin redirect (relative URL) that also ends the in-flight OAuth attempt."""
    response = RedirectResponse(target, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH, httponly=True,
                           secure=_auth_settings.is_production, samesite="lax")
    return response


def _google_failure(code: str) -> RedirectResponse:
    # Only a fixed, coarse code reaches the browser; details stay in the server log.
    return _google_redirect(f"{_APP_HOME}?auth_error={code}")


@app.get("/api/auth/google/start")
async def api_auth_google_start(request: Request):
    """Begin Google sign-in: new single-use state/nonce/PKCE, then redirect to Google."""
    ip = _authenticator.client_ip(request)
    if not _authenticator.oauth_limiter.allow(f"ip:{ip}"):
        logger.warning("Google sign-in start rate limited for ip=%s", ip)
        return _google_failure("rate_limited")
    if not _authenticator.google_enabled or _authenticator.google_client is None:
        return _google_failure("unavailable")
    state, pending = _authenticator.oauth_states.create()
    response = RedirectResponse(_authenticator.google_client.authorization_url(state, pending), status_code=302)
    response.headers["Cache-Control"] = "no-store"
    # Binds the attempt to this browser (login CSRF). Lax: it must survive the top-level
    # redirect back from accounts.google.com; it is scoped to the callback path only.
    response.set_cookie(
        STATE_COOKIE, state, max_age=STATE_TTL_SECONDS, httponly=True,
        secure=_auth_settings.is_production, samesite="lax", path=STATE_COOKIE_PATH,
    )
    return response


@app.get("/api/auth/google/callback")
async def api_auth_google_callback(request: Request):
    """Google redirects here with ?code&state (or ?error). Never returns tokens or details."""
    ip = _authenticator.client_ip(request)
    if not _authenticator.oauth_limiter.allow(f"ip:{ip}"):
        logger.warning("Google sign-in callback rate limited for ip=%s", ip)
        return _google_failure("rate_limited")
    params = request.query_params
    try:
        principal, created = await _to_thread(
            _authenticator.complete_google_login,
            state=params.get("state"),
            cookie_state=request.cookies.get(STATE_COOKIE),
            code=params.get("code"),
            error=params.get("error"),
        )
    except GoogleAuthError as exc:
        level = logging.INFO if exc.code == "cancelled" else logging.WARNING
        logger.log(level, "Google sign-in refused (%s) from ip=%s: %s", exc.code, ip, exc)
        return _google_failure(exc.public_code)
    except Exception:
        logger.exception("Google sign-in failed unexpectedly from ip=%s", ip)
        return _google_failure("failed")
    response = _google_redirect(_APP_HOME)
    _start_session(response, principal)
    logger.info("User %s signed in with Google (new account: %s, role=%s) from ip=%s",
                principal.subject, created, principal.role, ip)
    return response


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    # End the session server-side too, so a copy of the cookie stops working immediately.
    if _authenticator.revoke_session(request.cookies.get(SESSION_COOKIE)):
        logger.info("Session ended by logout")
    response = JSONResponse(content={"success": True})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=_auth_settings.is_production, samesite="strict")
    return response


@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    principal = _principal(request)
    return {
        "success": True,
        "user": {
            "username": principal.display_name or principal.subject,
            "role": principal.role,
            "auth_method": principal.auth_method,
            "provider": principal.provider,
        },
        # Server-enforced caps, so the UI can bound its inputs instead of hitting 422s.
        "limits": {
            "max_videos_per_job": settings.max_videos_per_job,
            "max_videos_sync": settings.max_videos_sync_export,
            "max_active_jobs_per_user": settings.max_active_jobs_per_user,
            "channel_min_video_seconds": settings.channel_min_video_seconds,
            "channel_max_video_seconds": settings.channel_max_video_seconds,
        },
    }


def _spa_index() -> FileResponse | HTMLResponse:
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html")
    return HTMLResponse(
        "<h1>Frontend not built</h1><p>Run <code>npm --prefix frontend run build</code> to build frontend production assets.</p>",
        status_code=503,
    )


@app.get("/")
@app.get("/transcript")
async def spa_routes():
    return _spa_index()


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> PlainTextResponse:
    """Ask crawlers to stay out; access control itself is enforced server-side."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def api_health(request: Request):
    """Cheap, non-blocking health probe.

    Returns 503 when the service cannot do useful work (missing YouTube API key or
    job storage not writable). Anonymous callers only learn the coarse status.
    """
    from services.jobs.transcript_job_manager import transcript_job_manager

    key_ok, _ = is_youtube_api_key_valid()
    storage_ok = transcript_job_manager.storage_writable()
    healthy = key_ok and storage_ok
    status_code = 200 if healthy else 503
    data: dict = {"status": "ok" if healthy else "unhealthy"}
    if _principal(request) is not None:
        data.update({
            "youtube_api_key_configured": key_ok,
            "groq_api_key_configured": bool(settings.groq_api_key),
            "job_storage_writable": storage_ok,
            "active_jobs": transcript_job_manager.active_job_count(),
        })
    return success_response(data=data, message="Service health check", status_code=status_code)


# ---------------------------------------------------------------------------
# Core Services & Helpers
# ---------------------------------------------------------------------------

_transcript_service = None
_channel_service = None
_video_service = None


def _get_transcript_service():
    global _transcript_service
    if _transcript_service is None:
        from services.transcript_service import TranscriptService
        _transcript_service = TranscriptService()
    return _transcript_service


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
    video_url: str = Field(min_length=1, max_length=2048)
    # "en" (Simple English, default) | "hi" (Simple Hindi) | "original" (verbatim source; alias "original_spoken")
    output_language: str = Field("en", max_length=20)


def _transcript_error(rid: str, code: str | None) -> JSONResponse:
    """Client-safe error body for the single-video transcript route (details stay in the log)."""
    from services.public_errors import public_error

    public, err = public_error(code)
    return JSONResponse(
        status_code=err.status_code,
        content={
            "success": False,
            "error_code": public,
            "message": err.message,
            "retryable": err.retryable,
            "trace_id": rid,
        },
        headers={"X-Request-ID": rid},
    )


def _normalize_output_mode(raw: str | None, default: str) -> str | None:
    """Canonical output mode ('original' | 'en' | 'hi') or None when unsupported."""
    mode = (raw or default).lower().strip()
    if mode == "original_spoken":
        mode = "original"
    return mode if mode in ("original", "en", "hi") else None


_shared_transcript_service = None


def _get_unified_services():
    """(canonical transcript service, shared translation service) for the single-video route."""
    global _shared_transcript_service
    from services.translation.service import get_translation_service
    if _shared_transcript_service is None:
        from services.transcription.service import TranscriptService
        _shared_transcript_service = TranscriptService()
    return _shared_transcript_service, get_translation_service()


_OUTPUT_MODE_PATTERN = r"^(original|original_spoken|en|hi)$"
# YYYY-MM-DD, optionally followed by an ISO 8601 time and offset
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?)?$"
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
        result = await _transcript_work(
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
    from services.transcript_metrics import transcript_metrics
    from services.transcription.groq import (
        GroqAuthError,
        GroqRateLimitError,
        GroqTimeoutError,
        GroqTranscriptionError,
    )
    from services.transcription.stt_gate import STTBusyError
    from services.transcription.validator import (
        TranscriptEmptyError,
        TranscriptValidationError,
    )
    from services.translation.service import TranslationError
    from services.youtube.audio import AudioExtractionError
    from services.youtube.captions import CaptionsUnavailableError

    rid = current_request_id()
    transcript_metrics.record_request_start()
    out_lang = _normalize_output_mode(request.output_language, "en")
    if out_lang is None:
        logger.info("[%s] Rejected unsupported output_language=%r", rid, request.output_language[:20])
        return _transcript_error(rid, "INVALID_REQUEST")

    logger.info("[%s] Unified transcript request: url=%r, out_lang=%s", rid, request.video_url[:200], out_lang)

    try:
        ts_service, trans_service = _get_unified_services()
        # Fetch or generate canonical transcript in worker thread
        canonical = await _transcript_work(ts_service.get_canonical_transcript, request.video_url)

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
                trans_result = await _transcript_work(
                    trans_service.translate,
                    video_id=video_id,
                    original_text=canonical_text,
                    target_language=out_lang,
                    source_language=source_lang,
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
        except Exception as exc:
            logger.info("[%s] Title lookup failed for %s: %s", rid, video_id, exc)

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
        logger.info("[%s] Invalid YouTube URL: %s", rid, exc)
        transcript_metrics.record_final_result(success=False)
        return _transcript_error(rid, "INVALID_YOUTUBE_URL")
    except CaptionsUnavailableError as exc:
        logger.warning("[%s] Captions unavailable (%s): %s", rid, exc.error_code, exc)
        transcript_metrics.record_final_result(success=False)
        return _transcript_error(rid, exc.error_code)
    except AudioExtractionError as exc:
        logger.error("[%s] Audio extraction failed (%s): %s", rid, exc.error_code, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        code = exc.error_code if exc.error_code in ("AUDIO_TOO_LONG", "BOT_BLOCKED") else "AUDIO_EXTRACTION_FAILED"
        return _transcript_error(rid, code)
    except STTBusyError as exc:
        logger.warning("[%s] Speech-to-text busy: %s", rid, exc)
        transcript_metrics.record_final_result(success=False)
        return _transcript_error(rid, exc.error_code)
    except GroqAuthError as exc:
        # Server misconfiguration (missing/invalid GROQ_API_KEY). Never 401: that status
        # means "your session is invalid" to the browser client and would log the user out.
        logger.error("[%s] Groq authentication failed; check GROQ_API_KEY: %s", rid, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        return _transcript_error(rid, "STT_UNAVAILABLE")
    except (GroqRateLimitError, GroqTimeoutError) as exc:
        logger.warning("[%s] Groq %s: %s", rid, exc.error_code, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        return _transcript_error(rid, exc.error_code)
    except GroqTranscriptionError as exc:
        logger.error("[%s] Speech-to-text failed (%s): %s", rid, exc.error_code, exc)
        transcript_metrics.record_groq_fallback(success=False)
        transcript_metrics.record_final_result(success=False)
        code = exc.error_code if exc.error_code in ("AUDIO_EXTRACTION_FAILED", "AUDIO_TOO_LONG") else "STT_FAILED"
        return _transcript_error(rid, code)
    except TranslationError as exc:
        logger.error("[%s] Translation failed: %s", rid, exc)
        transcript_metrics.record_final_result(success=False)
        return _transcript_error(rid, "TRANSLATION_FAILED")
    except (TranscriptEmptyError, TranscriptValidationError) as exc:
        logger.warning("[%s] Quality validation failed (%s): %s", rid, exc.error_code, exc)
        transcript_metrics.record_final_result(success=False)
        return _transcript_error(rid, exc.error_code)
    except Exception:
        logger.exception("[%s] Unexpected error in unified transcript route", rid)
        transcript_metrics.record_final_result(success=False)
        return _transcript_error(rid, "TRANSCRIPTION_FAILED")


@app.get("/api/transcript/metrics")
async def api_transcript_metrics():
    """Retrieve production transcript and STT monitoring metrics."""
    from services.transcript_metrics import transcript_metrics
    return {
        "success": True,
        "metrics": transcript_metrics.get_snapshot(),
    }


@app.get("/api/transcript/limiter/status")
async def api_transcript_limiter_status():
    """Return current transcript rate limiter and circuit breaker diagnostic metrics."""
    from services.transcript_limiter import transcript_limiter
    return success_response(data=transcript_limiter.get_status())


def _parse_iso_duration(iso: str) -> int:
    """Parse ISO 8601 duration string (e.g. PT15M51S, PT1H2M3S) to total seconds."""
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


def _scan_channel(video_svc, playlist_id: str, limit: int):
    """Up to ``limit`` eligible uploads (blocking; see services.channel_discovery)."""
    from services.channel_discovery import scan_channel_uploads

    return scan_channel_uploads(
        video_svc, playlist_id, limit,
        min_seconds=settings.channel_min_video_seconds,
        max_seconds=settings.channel_max_video_seconds,
        scan_cap=settings.channel_discovery_scan_cap,
    )


def _scan_metadata(scan) -> dict[str, dict]:
    """Per-video metadata in the shape the synchronous channel routes report."""
    return {
        v.video_id: {
            "title": v.title,
            "published_at": v.published_at,
            "duration_seconds": v.duration_seconds,
            "duration_readable": v.duration_formatted,
            "live_status": v.live_status,
        }
        for v in scan.scanned
    }


# --- Channel Transcript endpoint ---


_sync_channel_runs = 0


def _limit_sync_channel_runs(applies=lambda _call_kwargs: True):
    """Reject (429) synchronous channel runs beyond MAX_CONCURRENT_SYNC_CHANNEL_RUNS.

    Each such request fetches up to MAX_VIDEOS_SYNC_EXPORT transcripts inside one HTTP
    request; bounding them protects YouTube quota, Groq credits and the single worker.
    The counter is only touched on the event loop, so no lock is needed.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            global _sync_channel_runs
            if not applies(kwargs):
                return await func(*args, **kwargs)
            if _sync_channel_runs >= settings.max_concurrent_sync_channel_runs:
                logger.info("Synchronous channel run rejected: %d already running", _sync_channel_runs)
                return error_response(
                    message="The server is busy with other channel requests. Please retry shortly "
                            "or use a background job.",
                    status_code=429,
                    error_code="SERVER_BUSY",
                )
            _sync_channel_runs += 1
            try:
                return await func(*args, **kwargs)
            finally:
                _sync_channel_runs -= 1
        return wrapper
    return decorator


@app.get("/api/channel/{handle}/transcripts")
@_limit_sync_channel_runs()
async def api_channel_transcripts(
    handle: str = PathParam(pattern=_HANDLE_PATTERN),
    limit: int = Query(settings.max_videos_sync_export, ge=1, le=settings.max_videos_sync_export),
    concurrency: int = Query(5, ge=1, le=_MAX_CONCURRENCY),
    allow_whisper: bool = True,
    output_language: str = Query("original", pattern=_OUTPUT_MODE_PATTERN),
):
    """Fetch transcripts for a YouTube channel's newest eligible videos.

    Resolves the channel handle, discovers video IDs from the upload playlist,
    fetches video metadata for duration filtering, and fetches transcripts with
    caption-first retrieval and speech-to-text (Whisper) fallback.

    Returns channel info, statistics, and per-video results including metadata.
    ``limit`` counts eligible videos: CHANNEL_MIN_VIDEO_SECONDS <= duration <
    CHANNEL_MAX_VIDEO_SECONDS (default 3:00-30:00), no live/upcoming streams.
    """
    rid = current_request_id()
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

        # Steps 3-4: page the uploads playlist until `limit` eligible videos are found
        logger.info("[%s] Stage 3-4/5: Discovering eligible videos from upload playlist", rid)
        scan = await _to_thread(
            _scan_channel, video_svc, playlist_id, limit,
        )
        all_video_ids = [v.video_id for v in scan.scanned]
        videos_metadata = _scan_metadata(scan)
        eligible_videos = [v.video_id for v in scan.eligible]
        skipped_count = len(scan.scanned) - len(eligible_videos)
        logger.info(
            "[%s] Stage 3-4/5 complete: %d scanned, %d eligible, skipped %s",
            rid, len(all_video_ids), len(eligible_videos), dict(scan.skip_counts()) or "none",
        )

        # Step 5: Fetch transcripts in parallel
        logger.info(
            "[%s] Stage 5/5: Fetching transcripts for %d eligible videos (concurrency=%d)",
            rid, len(eligible_videos), concurrency,
        )
        from services.transcript_limiter import transcript_limiter
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
                    res = await _transcript_work(
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
                        w_res = await _transcript_work(
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
                "skipped_count": skipped_count,
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
    except Exception:
        logger.exception("[%s] Channel transcript failed for %s", rid, handle)
        return error_response(
            message=f"Failed to fetch channel transcripts (trace {rid}).",
            status_code=500,
        )


# --- Transcript Background Job endpoints ---


class TranscriptJobCreateRequest(BaseModel):
    max_videos: int = Field(0, ge=0, le=settings.max_videos_per_job)
    force_refresh: bool = False
    published_after: str | None = Field(None, max_length=40, pattern=_DATE_PATTERN)
    published_before: str | None = Field(None, max_length=40, pattern=_DATE_PATTERN)
    output_language: str = Field("en", pattern=_OUTPUT_MODE_PATTERN)


@app.post("/api/channel/{handle}/transcript-job")
async def api_start_channel_transcript_job(
    request: Request,
    handle: str = PathParam(pattern=_HANDLE_PATTERN),
    max_videos: int = Query(0, ge=0, le=settings.max_videos_per_job),
    force_refresh: bool = False,
    published_after: str | None = Query(None, max_length=40, pattern=_DATE_PATTERN),
    published_before: str | None = Query(None, max_length=40, pattern=_DATE_PATTERN),
    output_language: str = Query("en", pattern=_OUTPUT_MODE_PATTERN),
    req: TranscriptJobCreateRequest | None = None,
):
    """Launch asynchronous background job for channel transcripts."""
    from services.jobs.transcript_job_manager import JobLimitError, transcript_job_manager
    rid = current_request_id()
    principal = _principal(request)
    try:
        eff_max = req.max_videos if req and req.max_videos > 0 else max_videos
        # 0 means "all eligible videos", bounded by the configured per-job cap
        eff_max = min(eff_max or settings.max_videos_per_job, settings.max_videos_per_job)
        eff_refresh = req.force_refresh if req else force_refresh
        eff_pub_after = req.published_after if req and req.published_after else published_after
        eff_pub_before = req.published_before if req and req.published_before else published_before
        eff_out_lang = _normalize_output_mode(req.output_language if req else output_language, "en") or "en"

        progress = await transcript_job_manager.start_channel_job(
            channel_handle=handle,
            max_videos=eff_max,
            force_refresh=eff_refresh,
            published_after=eff_pub_after,
            published_before=eff_pub_before,
            output_language=eff_out_lang,
            owner=principal.subject if principal else None,
        )
        return success_response(
            data=progress.model_dump(),
            message=f"Transcript job {progress.job_id} queued for {progress.channel_title or handle}",
        )
    except JobLimitError as exc:
        logger.info("[%s] Job limit reached (%s) for %s", rid, exc.scope, principal.subject if principal else "-")
        message = (
            "You already have the maximum number of running jobs. Wait for one to finish or cancel it."
            if exc.scope == "user"
            else "The server is busy with other transcript jobs. Please try again shortly."
        )
        return error_response(message=message, status_code=429, error_code="JOB_LIMIT_REACHED")
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
    from services.jobs.transcript_job_manager import JobLimitError, transcript_job_manager
    existing = transcript_job_manager.get_job(job_id)
    if not existing or not _can_access_transcript_job(request, existing):
        return error_response(message=f"Job '{job_id}' not found", status_code=404)
    try:
        job = await transcript_job_manager.resume_job(job_id)
    except JobLimitError:
        return error_response(
            message="Too many running jobs to resume this one now. Please try again shortly.",
            status_code=429,
            error_code="JOB_LIMIT_REACHED",
        )
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
        writer.writerow(safe_csv_row(row))

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
@_limit_sync_channel_runs(applies=lambda call_kwargs: bool(call_kwargs["req"].channel_handle))
async def api_transcript_csv_export(req: TranscriptExportRequest):
    """Export transcripts as CSV for a single video or entire channel.

    Accepts either a video_url or a channel_handle. Returns a downloadable
    CSV file with one row per video. Never aborts on individual video failures.
    """
    rid = current_request_id()
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

            # Page the uploads playlist until max_videos eligible videos are found
            scan = await _to_thread(_scan_channel, video_svc, playlist_id, req.max_videos)
            videos_meta = _scan_metadata(scan)
            eligible = [v.video_id for v in scan.eligible]
            logger.info(
                "[%s] CSV export: %d scanned, %d eligible, skipped %s; fetching transcripts...",
                rid, len(scan.scanned), len(eligible), dict(scan.skip_counts()) or "none",
            )

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
                        transcript = await _transcript_work(
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
                transcript = await _transcript_work(
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

    except Exception:
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
