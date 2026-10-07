# ruff: noqa: ARG001, S105, S106, S603  (fixture-activation args; fake test-only credentials; trusted subprocess)
"""Phase 1 security regression: behaviour through the real request path.

Complements tests/test_web_auth.py with jobs created via the API, session expiry,
limit boundaries, lockout timing, server-side error logging and real production
startup in a subprocess.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import webapp.main as web
from config.settings import settings
from security.jwt_service import JWTConfig, JWTService
from tests.conftest import (
    TEST_ADMIN_KEY,
    TEST_OTHER_USER_KEY,
    TEST_USER_KEY,
    TEST_USER_PASSWORD,
    build_test_auth_settings,
)

pytestmark = pytest.mark.security

ROOT = Path(__file__).resolve().parent.parent
USER_A, USER_B = TEST_USER_KEY, TEST_OTHER_USER_KEY


def _client(api_key: str | None = None) -> TestClient:
    headers = {"X-API-Key": api_key} if api_key else {}
    return TestClient(web.app, raise_server_exceptions=False, headers=headers)


def _reconfigure(monkeypatch, **overrides):
    config = build_test_auth_settings(**overrides)
    original = web._authenticator.config
    web._authenticator.configure(config)
    monkeypatch.setattr(web, "_auth_settings", config)
    return original


# ---------------------------------------------------------------------------
# Fixtures: create real jobs through the API without touching YouTube
# ---------------------------------------------------------------------------


@pytest.fixture
def transcript_jobs(auth_config, tmp_path, monkeypatch):
    """Transcript jobs are created by the real route; only background discovery is stubbed."""
    from services.jobs.transcript_job_manager import transcript_job_manager

    calls: list[dict] = []

    async def no_discovery(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(transcript_job_manager, "_discover_and_run", no_discovery)
    monkeypatch.setattr(transcript_job_manager, "_jobs_dir", tmp_path)  # never touch data/
    before = set(transcript_job_manager._jobs)
    yield calls
    for job_id in set(transcript_job_manager._jobs) - before:
        transcript_job_manager._jobs.pop(job_id, None)
        transcript_job_manager._tasks.pop(job_id, None)


def _create_transcript_job(api_key: str, handle: str, **query) -> str:
    resp = _client(api_key).post(f"/api/channel/{handle}/transcript-job", params=query or None)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["job_id"]


@pytest.fixture
def export_jobs(auth_config, tmp_path, monkeypatch):
    """Export jobs are created by the real route; only the pipeline run is stubbed."""
    started: list = []

    async def fake_pipeline(job_id, request):
        started.append((job_id, request))

    monkeypatch.setattr(web, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(web, "_run_async_pipeline", fake_pipeline)
    monkeypatch.setattr(web, "is_youtube_api_key_valid", lambda: (True, ""))
    web.rate_limiter.reset("export:testclient")
    return started


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _session_token(subject: str, minutes: int) -> str:
    cfg = web._authenticator.config
    service = JWTService(JWTConfig(
        secret_key=cfg.jwt_secret, access_token_expire_minutes=minutes,
        issuer="youtube-export", audience="youtube-export-web",
    ))
    return service.create_access_token(SimpleNamespace(id=subject, email="", role="user", organization_id=""))


def test_valid_session_cookie_authenticates(auth_config):
    client = _client()
    client.cookies.set("session", _session_token("alice", 30))
    assert client.get("/api/auth/me").json()["user"]["username"] == "alice"


def test_expired_session_rejected(auth_config):
    client = _client()
    client.cookies.set("session", _session_token("alice", -5))
    assert client.get("/api/auth/me").status_code == 401


def test_session_signed_with_other_secret_rejected(auth_config):
    service = JWTService(JWTConfig(secret_key="attacker-secret-" + "y" * 40, issuer="youtube-export",
                                   audience="youtube-export-web"))
    token = service.create_access_token(SimpleNamespace(id="root", email="", role="admin", organization_id=""))
    client = _client()
    client.cookies.set("session", token)
    assert client.get("/api/auth/me").status_code == 401


def test_session_for_removed_user_rejected(auth_config, monkeypatch):
    token = _session_token("alice", 30)
    _reconfigure(monkeypatch, users={})  # alice removed from AUTH_USERS
    client = _client()
    client.cookies.set("session", token)
    assert client.get("/api/auth/me").status_code == 401


def test_api_key_cannot_be_smuggled_via_query_or_bearer(auth_config):
    client = _client()
    assert client.get("/api/auth/me", params={"api_key": TEST_USER_KEY}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {TEST_USER_KEY}"}).status_code == 401


# ---------------------------------------------------------------------------
# Authorization + job ownership (jobs created through the API)
# ---------------------------------------------------------------------------


def test_user_endpoint_allowed_admin_endpoint_denied(auth_config):
    assert _client(USER_A).get("/api/auth/me").status_code == 200
    assert _client(USER_A).get("/api/transcript/limiter/status").status_code == 403
    assert _client(TEST_ADMIN_KEY).get("/api/transcript/limiter/status").status_code == 200


def test_transcript_job_isolation_between_users(transcript_jobs):
    job_a = _create_transcript_job(USER_A, "channela")
    job_b = _create_transcript_job(USER_B, "channelb")
    a, b, admin = _client(USER_A), _client(USER_B), _client(TEST_ADMIN_KEY)

    assert a.get(f"/api/transcript/jobs/{job_a}").status_code == 200
    assert b.get(f"/api/transcript/jobs/{job_b}").status_code == 200
    assert a.get(f"/api/transcript/jobs/{job_b}").status_code == 404
    assert b.get(f"/api/transcript/jobs/{job_a}").status_code == 404
    assert admin.get(f"/api/transcript/jobs/{job_a}").status_code == 200
    assert admin.get(f"/api/transcript/jobs/{job_b}").status_code == 200

    # A cannot cancel, resume or download B's job
    assert a.post(f"/api/transcript/jobs/{job_b}/cancel").status_code == 400
    assert a.post(f"/api/transcript/jobs/{job_b}/resume").status_code == 404
    assert a.get(f"/api/transcript/jobs/{job_b}/download").status_code == 404
    from services.jobs.transcript_job_manager import transcript_job_manager
    assert transcript_job_manager.get_job(job_b).status.value != "cancelled"

    # Owners can cancel and download their own job
    assert b.post(f"/api/transcript/jobs/{job_b}/cancel").status_code == 200
    dl = b.get(f"/api/transcript/jobs/{job_b}/download")
    assert dl.status_code == 200
    assert "text/csv" in dl.headers["content-type"]


def test_owner_is_recorded_from_authenticated_principal(transcript_jobs):
    from services.jobs.transcript_job_manager import transcript_job_manager

    job_id = _create_transcript_job(USER_A, "channela")
    assert transcript_job_manager.get_job(job_id).owner == "key:user-key"
    persisted = json.loads((transcript_job_manager._jobs_dir / f"{job_id}.json").read_text(encoding="utf-8"))
    assert persisted["owner"] == "key:user-key"


def test_export_job_isolation_between_users(export_jobs):
    a, b, admin = _client(USER_A), _client(USER_B), _client(TEST_ADMIN_KEY)
    job_a = a.post("/api/export", json={"channel": "@channela", "limit": 5}).json()["job_id"]
    job_b = b.post("/api/export", json={"channel": "@channelb", "limit": 5}).json()["job_id"]
    (web.RUNS_DIR / job_b / "videos.csv").write_text("video_id\nabc\n", encoding="utf-8")
    (web.RUNS_DIR / job_b / "progress.json").write_text('{"status": "completed"}', encoding="utf-8")

    assert b.get(f"/api/export/{job_b}/progress").json()["success"] is True
    assert a.get(f"/api/export/{job_b}/progress").json()["success"] is False
    assert a.get(f"/api/export/{job_b}/download").status_code == 404
    assert b.get(f"/api/export/{job_b}/download").status_code == 200
    assert a.post(f"/api/export/{job_b}/cancel").json()["success"] is False
    assert a.delete(f"/api/export/{job_b}").status_code == 404
    assert (web.RUNS_DIR / job_b).exists()
    assert admin.get(f"/api/export/{job_b}/progress").json()["success"] is True
    assert (web.RUNS_DIR / job_a / "owner").read_text(encoding="utf-8") == "key:user-key"


# ---------------------------------------------------------------------------
# Input validation / unsafe filenames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/transcript/jobs/..%2F..%2Fetc",
    "/api/transcript/jobs/%2e%2e%5c%2e%2e%5cwin.ini",
    "/api/transcript/jobs/0123456789ab%00",
    "/api/export/..%5C..%5Cx/download",
])
def test_path_traversal_rejected(authed_client, path):
    resp = authed_client.get(path)
    assert resp.status_code in (400, 404, 422)
    assert "root:" not in resp.text
    assert "[fonts]" not in resp.text


def test_invalid_language_identifier_rejected(authed_client):
    for bad in ("x", "e%3Cscript%3E", "english-language-long", "en%20us", "..%5C.."):
        resp = authed_client.get(f"/api/transcript/dQw4w9WgXcQ/translate/{bad}")
        assert resp.status_code == 422, bad


def test_unsafe_download_filename_sanitized(auth_config, tmp_path, monkeypatch):
    from models.transcript_job import JobStatus, TranscriptJobProgress
    from services.jobs.transcript_job_manager import transcript_job_manager

    job = TranscriptJobProgress(
        job_id="ee11ee11ee11", channel_handle='evil"\r\nSet-Cookie: x=1;.csv', channel_id="", channel_title="x",
        status=JobStatus.COMPLETED, total_discovered=0, eligible_videos=0, skipped_videos=0, remaining=0,
        videos=[], owner="key:user-key",
    )
    transcript_job_manager._jobs[job.job_id] = job
    try:
        resp = _client(USER_A).get(f"/api/transcript/jobs/{job.job_id}/download")
        assert resp.status_code == 200
        disposition = resp.headers["content-disposition"]
        assert re.fullmatch(r'attachment; filename="[A-Za-z0-9._-]+"', disposition), disposition
        assert "set-cookie" not in {k.lower() for k in resp.headers}  # no header injection
    finally:
        transcript_job_manager._jobs.pop(job.job_id, None)


# ---------------------------------------------------------------------------
# Resource abuse limits
# ---------------------------------------------------------------------------


def test_max_videos_per_job_boundary(transcript_jobs):
    cap = settings.max_videos_per_job
    _create_transcript_job(USER_A, "channela", max_videos=cap)
    assert transcript_jobs[-1]["max_videos"] == cap
    resp = _client(USER_A).post("/api/channel/channela/transcript-job", params={"max_videos": cap + 1})
    assert resp.status_code == 422


def test_max_videos_zero_means_configured_cap(transcript_jobs):
    _create_transcript_job(USER_A, "channela", max_videos=0)
    assert transcript_jobs[-1]["max_videos"] == settings.max_videos_per_job


def test_export_limit_zero_means_configured_cap(export_jobs):
    resp = _client(USER_A).post("/api/export", json={"channel": "@chan", "limit": 0})
    assert resp.status_code == 200
    _, request = export_jobs[-1]
    assert request.limit == settings.max_videos_per_job


def test_max_videos_sync_export_boundary(authed_client):
    cap = settings.max_videos_sync_export
    resp = authed_client.post("/api/transcript/export", json={"channel_handle": "chan", "max_videos": cap + 1})
    assert resp.status_code == 422
    resp = authed_client.post("/api/transcript/export", json={"channel_handle": "chan", "max_videos": 0})
    assert resp.status_code == 422


def test_concurrency_limit_is_ten(transcript_jobs):
    ok = _client(USER_A).post("/api/channel/channela/transcript-job",
                              json={"caption_concurrency": 10, "whisper_concurrency": 10})
    assert ok.status_code == 200
    assert transcript_jobs[-1]["caption_concurrency"] == 10
    for field in ("caption_concurrency", "whisper_concurrency"):
        resp = _client(USER_A).post("/api/channel/channela/transcript-job", json={field: 11})
        assert resp.status_code == 422
        assert _client(USER_A).post("/api/channel/channela/transcript-job", json={field: 0}).status_code == 422
    assert _client(USER_A).get("/api/channel/chan/transcripts", params={"concurrency": 11}).status_code == 422


# ---------------------------------------------------------------------------
# Login protection: lockout duration
# ---------------------------------------------------------------------------


def test_login_lockout_lasts_fifteen_minutes(monkeypatch):
    import infrastructure.rate_limiter as rl

    now = [1_000_000.0]
    monkeypatch.setattr(rl, "time", SimpleNamespace(time=lambda: now[0]))
    original = _reconfigure(monkeypatch, login_rate_per_minute=1000)
    try:
        assert web._auth_settings.login_max_failures == 10
        assert web._auth_settings.login_lockout_seconds == 15 * 60
        client = _client()
        for _ in range(10):
            assert client.post("/api/auth/login", json={"username": "alice", "password": "bad"}).status_code == 401
        good = {"username": "alice", "password": TEST_USER_PASSWORD}
        assert client.post("/api/auth/login", json=good).status_code == 429
        now[0] += 15 * 60 - 5  # still inside the window
        assert client.post("/api/auth/login", json=good).status_code == 429
        now[0] += 10  # window elapsed
        assert client.post("/api/auth/login", json=good).status_code == 200
        # Another account was never locked
        assert client.post("/api/auth/login", json={"username": "root", "password": TEST_USER_PASSWORD}).status_code == 200
    finally:
        web._authenticator.configure(original)


# ---------------------------------------------------------------------------
# Error handling: generic to client, detailed in server logs
# ---------------------------------------------------------------------------


def test_internal_error_generic_to_client_detailed_in_logs(auth_config, monkeypatch, caplog):
    detail = "boom-detail-7781 in internal/module.py"

    def boom():
        raise RuntimeError(detail)

    monkeypatch.setattr(web.quota_tracker, "usage", boom)
    caplog.set_level(logging.ERROR, logger="webapp")
    resp = _client(TEST_ADMIN_KEY).get("/api/quota")

    assert resp.status_code == 500
    body = resp.json()
    assert body == {"success": False, "error": "Internal server error.", "error_code": "INTERNAL_ERROR",
                    "trace_id": body["trace_id"]}
    assert len(body["trace_id"]) == 8
    assert resp.headers["x-request-id"] == body["trace_id"]
    assert detail not in resp.text

    logged = [r for r in caplog.records if body["trace_id"] in r.getMessage()]
    assert logged, "server log must carry the trace id"
    assert detail in logged[0].getMessage() or detail in (logged[0].exc_text or "")
    assert "RuntimeError" in (logged[0].exc_text or "")


# ---------------------------------------------------------------------------
# Production startup validation: real import in a clean subprocess
# ---------------------------------------------------------------------------

_SAFE_PROD_ENV = {
    "APP_ENV": "production",
    "JWT_SECRET_KEY": "prod-test-secret-" + "q" * 40,
    "API_KEYS": "ops:admin:" + "a" * 64,
    "CORS_ORIGINS": "https://app.example.com",
    "YOUTUBE_API_KEY": "fake-test-youtube-key",
    "OTEL_ENABLED": "false",
}

_BOOT = textwrap.dedent("""
    import json, sys
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False  # ignore the developer's .env
    try:
        import webapp.main as web
    except Exception as exc:
        print(json.dumps({"started": False, "error": type(exc).__name__, "message": str(exc)}))
        sys.exit(0)
    from fastapi.testclient import TestClient
    c = TestClient(web.app, raise_server_exceptions=False)
    docs = c.get("/docs")
    print(json.dumps({
        "started": True,
        "openapi": c.get("/openapi.json").status_code,
        "redoc": c.get("/redoc").status_code,
        "docs_is_swagger": "swagger-ui" in docs.text.lower(),
        "docs_oauth_redirect": c.get("/docs/oauth2-redirect").status_code,
    }))
""")


def _boot(overrides: dict[str, str | None]) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SAFE_PROD_ENV and k not in ("AUTH_USERS", "API_KEYS")}
    env.update(_SAFE_PROD_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    proc = subprocess.run([sys.executable, "-c", _BOOT], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=120)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"no result from subprocess: {proc.stderr[-2000:]}"
    return json.loads(lines[-1])


@pytest.mark.parametrize(("overrides", "expected"), [
    ({"JWT_SECRET_KEY": "short"}, "JWT_SECRET_KEY"),
    ({"JWT_SECRET_KEY": None}, "JWT_SECRET_KEY"),
    ({"JWT_SECRET_KEY": "change-me-in-production-32-chars!"}, "JWT_SECRET_KEY"),
    ({"API_KEYS": None}, "no credentials"),
    ({"CORS_ORIGINS": "*"}, "CORS_ORIGINS"),
    ({"CORS_ORIGINS": "https://app.example.com,*"}, "CORS_ORIGINS"),
    ({"CORS_ORIGINS": "http://app.example.com"}, "https://"),
    ({"CORS_ORIGINS": "http://localhost:5173"}, "https://"),
    ({"CORS_ORIGINS": "https://app.example.com,http://other.example.com"}, "https://"),
    ({"CORS_ORIGINS": "https://app.example.com/path"}, "https://"),
    ({"APP_ENV": "prod"}, "APP_ENV"),
    ({"APP_ENV": "staging"}, "APP_ENV"),
])
def test_production_refuses_to_start(overrides, expected):
    result = _boot(overrides)
    assert result["started"] is False
    assert result["error"] == "AuthConfigError"
    assert expected in result["message"]


@pytest.mark.parametrize("origins", [
    "https://REAL-DOMAIN.example",
    "https://real-domain.example,https://www.real-domain.example:8443",
])
def test_production_accepts_explicit_https_origins(origins):
    assert _boot({"CORS_ORIGINS": origins})["started"] is True


def test_development_accepts_local_vite_origin():
    result = _boot({"APP_ENV": "development", "CORS_ORIGINS": "http://localhost:5173",
                    "JWT_SECRET_KEY": None})
    assert result["started"] is True


def test_production_starts_with_safe_config_and_hides_api_docs():
    result = _boot({})
    assert result["started"] is True
    assert result["openapi"] == 404
    assert result["redoc"] == 404
    assert result["docs_is_swagger"] is False
    assert result["docs_oauth_redirect"] == 404
