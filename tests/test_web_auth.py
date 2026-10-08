# ruff: noqa: ARG001, S105, S106  (fixture-activation args; fixed test-only credentials)
"""Security tests for web authentication, authorization, validation and error hygiene."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

import webapp.main as web
from security.web_auth import (
    PUBLIC_ENDPOINTS,
    AuthConfigError,
    AuthSettings,
    cors_options,
    hash_password,
    is_admin_path,
    verify_password,
)
from tests.conftest import (
    TEST_ADMIN_KEY,
    TEST_OTHER_USER_KEY,
    TEST_USER_KEY,
    TEST_USER_PASSWORD,
    build_test_auth_settings,
)

pytestmark = pytest.mark.security

_PARAM_VALUES = {
    "video_id": "dQw4w9WgXcQ",
    "job_id": "0123456789ab",
    "handle": "somechannel",
    "target_language": "en",
}


def _client(api_key: str | None = None, **kwargs) -> TestClient:
    headers = {"X-API-Key": api_key} if api_key else {}
    return TestClient(web.app, raise_server_exceptions=False, headers=headers, **kwargs)


def _api_routes():
    for route in web.app.routes:
        if isinstance(route, Route) and route.path.startswith("/api/"):
            path = re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: _PARAM_VALUES.get(m.group(1), "x"), route.path)
            for method in sorted(route.methods - {"HEAD"}):
                yield method, route.path, path


# ---------------------------------------------------------------------------
# Authentication: deny by default
# ---------------------------------------------------------------------------


def test_every_api_route_requires_authentication(auth_config):
    client = _client()
    checked = 0
    for method, template, path in _api_routes():
        if (method, template) in PUBLIC_ENDPOINTS:
            continue
        resp = client.request(method, path, json={} if method in ("POST", "PUT", "PATCH") else None)
        assert resp.status_code == 401, f"{method} {template} returned {resp.status_code} without credentials"
        assert resp.json()["error_code"] == "UNAUTHENTICATED"
        checked += 1
    assert checked >= 10  # guards against the route enumeration silently matching nothing


def test_invalid_api_key_rejected(auth_config):
    resp = _client("ysk_not-a-real-key").get("/api/auth/me")
    assert resp.status_code == 401


def test_valid_api_key_accepted(auth_config):
    resp = _client(TEST_USER_KEY).get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["user"] == {"username": "key:user-key", "role": "user", "auth_method": "api_key"}


def test_anonymous_health_reveals_only_status(auth_config):
    resp = _client().get("/api/health")
    assert resp.status_code == 200
    assert set(resp.json()["data"]) == {"status"}


def test_authenticated_health_has_details(auth_config, monkeypatch):
    resp = _client(TEST_USER_KEY).get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["data"]["job_storage_writable"] is True


# ---------------------------------------------------------------------------
# Login / sessions
# ---------------------------------------------------------------------------


def test_login_sets_hardened_session_cookie(auth_config):
    client = _client()
    resp = client.post("/api/auth/login", json={"username": "alice", "password": TEST_USER_PASSWORD})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "alice"
    assert me.json()["user"]["auth_method"] == "session"


def test_login_cookie_is_secure_in_production(monkeypatch):
    config = build_test_auth_settings(app_env="production")
    original = web._authenticator.config
    web._authenticator.configure(config)
    monkeypatch.setattr(web, "_auth_settings", config)
    try:
        resp = _client().post("/api/auth/login", json={"username": "alice", "password": TEST_USER_PASSWORD})
        assert resp.status_code == 200
        assert "secure" in resp.headers["set-cookie"].lower()
    finally:
        web._authenticator.configure(original)


def test_wrong_password_rejected_without_detail(auth_config):
    resp = _client().post("/api/auth/login", json={"username": "alice", "password": "wrong-password"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid username or password."
    unknown = _client().post("/api/auth/login", json={"username": "nobody", "password": "wrong-password"})
    assert unknown.status_code == 401
    assert unknown.json()["error"] == resp.json()["error"]


def test_failed_logins_throttle_the_attacker_not_the_account(monkeypatch):
    # The TestClient peer is "testclient"; trusting it lets X-Forwarded-For pick the client IP.
    config = build_test_auth_settings(login_rate_per_minute=1000, trusted_proxies=frozenset({"testclient"}))
    original = web._authenticator.config
    web._authenticator.configure(config)
    monkeypatch.setattr(web, "_auth_settings", config)
    try:
        attacker, victim = _client(), _client()
        attacker.headers["X-Forwarded-For"] = "203.0.113.9"
        victim.headers["X-Forwarded-For"] = "198.51.100.7"
        for _ in range(3):
            assert attacker.post("/api/auth/login", json={"username": "alice", "password": "bad"}).status_code == 401
        # The attacker is now backed off, even with the right password...
        throttled = attacker.post("/api/auth/login", json={"username": "alice", "password": TEST_USER_PASSWORD})
        assert throttled.status_code == 429
        assert int(throttled.headers["Retry-After"]) >= 1
        # ...but the account owner, from another address, is not locked out.
        assert victim.post("/api/auth/login", json={"username": "alice", "password": TEST_USER_PASSWORD}).status_code == 200
    finally:
        web._authenticator.configure(original)


def test_login_rate_limited_per_ip(auth_config):
    client = _client()
    codes = [client.post("/api/auth/login", json={"username": f"u{i}", "password": "x"}).status_code for i in range(7)]
    assert codes[-1] == 429
    assert 429 in codes


def test_tampered_session_cookie_rejected(auth_config):
    client = _client()
    client.cookies.set("session", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyb290In0.forged")
    assert client.get("/api/auth/me").status_code == 401


def test_logout_clears_session(auth_config):
    client = _client()
    client.post("/api/auth/login", json={"username": "alice", "password": TEST_USER_PASSWORD})
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert 'session=""' in resp.headers["set-cookie"] or "max-age=0" in resp.headers["set-cookie"].lower()


# ---------------------------------------------------------------------------
# CSRF / CORS
# ---------------------------------------------------------------------------


def test_cookie_auth_rejects_cross_site_unsafe_request(auth_config):
    client = _client()
    client.post("/api/auth/login", json={"username": "alice", "password": TEST_USER_PASSWORD})
    evil = client.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert evil.json()["error_code"] == "CSRF_REJECTED"
    same_site = client.post("/api/auth/logout", headers={"Origin": "http://testserver"})
    assert same_site.status_code == 200


def test_login_rejects_cross_site_origin(auth_config):
    resp = _client().post(
        "/api/auth/login",
        json={"username": "alice", "password": TEST_USER_PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_api_key_requests_not_subject_to_origin_check(auth_config):
    resp = _client(TEST_USER_KEY).post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200


def test_cors_options_explicit_origins_allow_credentials():
    opts = cors_options(build_test_auth_settings(cors_origins=["https://app.example.com"]))
    assert opts["allow_origins"] == ["https://app.example.com"]
    assert opts["allow_credentials"] is True
    assert "*" not in opts["allow_methods"]
    assert "*" not in opts["allow_headers"]


def test_cors_options_default_is_same_origin_only():
    opts = cors_options(build_test_auth_settings(cors_origins=[]))
    assert opts["allow_origins"] == []
    assert opts["allow_credentials"] is False


def test_cors_wildcard_never_allows_credentials():
    opts = cors_options(build_test_auth_settings(cors_origins=["*"]))
    assert opts["allow_credentials"] is False


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/transcript/metrics", "/api/transcript/limiter/status"])
def test_admin_only_routes(auth_config, path):
    assert is_admin_path(path)
    assert _client(TEST_USER_KEY).get(path).status_code == 403
    assert _client(TEST_ADMIN_KEY).get(path).status_code == 200


def test_transcript_job_visible_only_to_owner_and_admin(auth_config):
    from models.transcript_job import JobStatus, TranscriptJobProgress
    from services.jobs.transcript_job_manager import transcript_job_manager

    job = TranscriptJobProgress(
        job_id="abcdef012345", channel_handle="chan", channel_id="", channel_title="chan",
        status=JobStatus.COMPLETED, total_discovered=0, eligible_videos=0, skipped_videos=0,
        remaining=0, videos=[], owner="key:user-key",
    )
    transcript_job_manager._jobs[job.job_id] = job
    try:
        assert _client(TEST_USER_KEY).get(f"/api/transcript/jobs/{job.job_id}").status_code == 200
        assert _client(TEST_ADMIN_KEY).get(f"/api/transcript/jobs/{job.job_id}").status_code == 200
        other = _client(TEST_OTHER_USER_KEY)
        assert other.get(f"/api/transcript/jobs/{job.job_id}").status_code == 404
        assert other.get(f"/api/transcript/jobs/{job.job_id}/download").status_code == 404
        assert other.post(f"/api/transcript/jobs/{job.job_id}/cancel").status_code == 400
        assert other.post(f"/api/transcript/jobs/{job.job_id}/resume").status_code == 404
        assert job.status == JobStatus.COMPLETED  # untouched by the other user
    finally:
        transcript_job_manager._jobs.pop(job.job_id, None)


def test_legacy_transcript_job_without_owner_is_admin_only(auth_config):
    from models.transcript_job import JobStatus, TranscriptJobProgress
    from services.jobs.transcript_job_manager import transcript_job_manager

    job = TranscriptJobProgress(
        job_id="fedcba543210", channel_handle="chan", channel_id="", channel_title="chan",
        status=JobStatus.COMPLETED, total_discovered=0, eligible_videos=0, skipped_videos=0,
        remaining=0, videos=[],
    )
    transcript_job_manager._jobs[job.job_id] = job
    try:
        assert _client(TEST_USER_KEY).get(f"/api/transcript/jobs/{job.job_id}").status_code == 404
        assert _client(TEST_ADMIN_KEY).get(f"/api/transcript/jobs/{job.job_id}").status_code == 200
    finally:
        transcript_job_manager._jobs.pop(job.job_id, None)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(monkeypatch):
    config = build_test_auth_settings(api_rate_per_minute=3)
    original = web._authenticator.config
    web._authenticator.configure(config)
    monkeypatch.setattr(web, "_auth_settings", config)
    try:
        client = _client(TEST_USER_KEY)
        codes = [client.get("/api/auth/me").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        resp = client.get("/api/auth/me")
        assert resp.headers["retry-after"] == "60"
        # Limits are per principal: another key is unaffected
        assert _client(TEST_OTHER_USER_KEY).get("/api/auth/me").status_code == 200
    finally:
        web._authenticator.configure(original)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/transcript/jobs/..%5C..%5Csecrets",
    "/api/transcript/jobs/not-a-job",
    "/api/transcript/jobs/0123456789AB",
    "/api/channel/bad%20handle/transcripts",
    "/api/channel/..%5C..%5Cwindows/transcripts",
])
def test_malformed_identifiers_rejected(authed_client, path):
    assert authed_client.get(path).status_code == 422


def test_channel_handle_validated(authed_client):
    resp = authed_client.post("/api/channel/bad%20handle%3Cscript%3E/transcript-job")
    assert resp.status_code == 422


@pytest.mark.parametrize("max_videos", [-1, 10_000_000])
def test_transcript_job_max_videos_bounded(authed_client, max_videos):
    resp = authed_client.post(f"/api/channel/somechannel/transcript-job?max_videos={max_videos}")
    assert resp.status_code == 422


def test_channel_transcripts_limit_bounded(authed_client):
    assert authed_client.get("/api/channel/somechannel/transcripts?limit=999999").status_code == 422
    assert authed_client.get("/api/channel/somechannel/transcripts?concurrency=1000").status_code == 422


def test_sync_export_max_videos_bounded(authed_client):
    resp = authed_client.post("/api/transcript/export", json={"channel_handle": "chan", "max_videos": 10_000_000})
    assert resp.status_code == 422


def test_filename_sanitised():
    assert web._safe_filename_part('evil"; filename=x.exe\r\n') == "evil___filename_x.exe__"


# ---------------------------------------------------------------------------
# Error hygiene
# ---------------------------------------------------------------------------


def test_unhandled_errors_do_not_leak_details(auth_config, monkeypatch):
    def boom():
        raise RuntimeError("db password=hunter2 at C:\\internal\\path")

    from services.transcript_limiter import transcript_limiter

    monkeypatch.setattr(transcript_limiter, "get_status", boom)
    resp = _client(TEST_ADMIN_KEY).get("/api/transcript/limiter/status")
    assert resp.status_code == 500
    body = resp.text
    assert "hunter2" not in body
    assert "path" not in body
    assert "RuntimeError" not in body
    data = resp.json()
    assert data["error"] == "Internal server error."
    assert data["trace_id"] == resp.headers["x-request-id"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_password_hash_roundtrip():
    encoded = hash_password("s3cret-passphrase")
    assert encoded.startswith("scrypt.")
    assert "$" not in encoded  # safe inside .env / compose files
    assert ":" not in encoded  # safe inside AUTH_USERS entries
    assert verify_password("s3cret-passphrase", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("s3cret-passphrase", "garbage")


def test_production_refuses_weak_secret():
    with pytest.raises(AuthConfigError, match="JWT_SECRET_KEY"):
        build_test_auth_settings(app_env="production", jwt_secret="short").validate()


def test_production_refuses_default_secret():
    cfg = AuthSettings.from_env({
        "APP_ENV": "production",
        "JWT_SECRET_KEY": "change-me-in-production-32-chars!",
        "API_KEYS": "k:user:" + "0" * 64,
    })
    with pytest.raises(AuthConfigError, match="JWT_SECRET_KEY"):
        cfg.validate()


def test_production_refuses_wildcard_cors():
    with pytest.raises(AuthConfigError, match="CORS_ORIGINS"):
        build_test_auth_settings(app_env="production", cors_origins=["*"]).validate()


def test_production_refuses_missing_credentials():
    with pytest.raises(AuthConfigError, match="no credentials"):
        build_test_auth_settings(app_env="production", users={}, api_keys=[]).validate()


def test_production_accepts_safe_config():
    build_test_auth_settings(app_env="production").validate()


def test_development_generates_ephemeral_secret():
    cfg = AuthSettings.from_env({"APP_ENV": "development"})
    assert cfg.jwt_secret_ephemeral
    assert len(cfg.jwt_secret) >= 32


@pytest.mark.parametrize("env", [
    {"AUTH_USERS": "alice:superuser:scrypt.1.2.3.a.b"},
    {"AUTH_USERS": "alice:user:plaintextpassword"},
    {"AUTH_USERS": "missing-fields"},
    {"API_KEYS": "k:user:not-a-sha256"},
    {"SESSION_TTL_MINUTES": "0"},
])
def test_malformed_credential_config_rejected(env):
    with pytest.raises(AuthConfigError):
        AuthSettings.from_env(env)
