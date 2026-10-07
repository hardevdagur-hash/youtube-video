"""Deployment smoke tests, run against a live stack (never part of the default unit run).

    SMOKE_TEST_URL=https://transcripts.example.com pytest -m smoke tests/smoke

Optional: SMOKE_TEST_API_KEY (a key from API_KEYS) enables the authenticated checks.
SMOKE_TEST_INSECURE=1 skips TLS verification (self-signed staging certificates only).
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.smoke

BASE_URL = os.environ.get("SMOKE_TEST_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = float(os.environ.get("SMOKE_TEST_TIMEOUT", "30"))
API_KEY = os.environ.get("SMOKE_TEST_API_KEY", "")
VERIFY_TLS = os.environ.get("SMOKE_TEST_INSECURE", "") != "1"


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT, verify=VERIFY_TLS) as c:
        yield c


def test_health_is_public_and_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"] == {"status": "ok"}  # anonymous callers learn nothing else


def test_frontend_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<div id=\"root\">" in resp.text


def test_transcript_api_requires_authentication(client):
    resp = client.post("/api/transcript", json={"video_url": "dQw4w9WgXcQ", "output_language": "original"})
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHENTICATED"


def test_unknown_api_route_is_not_the_spa(client):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code in (401, 404)


def test_cross_origin_requests_not_allowed(client):
    resp = client.options("/api/health", headers={
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "GET",
    })
    assert resp.headers.get("access-control-allow-origin") not in ("*", "https://evil.example")


def test_security_headers_present(client):
    headers = client.get("/").headers
    assert headers.get("x-content-type-options") == "nosniff"
    assert "x-frame-options" in headers
    if BASE_URL.startswith("https://"):
        assert "max-age=" in headers.get("strict-transport-security", "")


@pytest.mark.skipif(not API_KEY, reason="SMOKE_TEST_API_KEY not set")
def test_authenticated_health_details(client):
    resp = client.get("/api/health", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["youtube_api_key_configured"] is True
    assert data["job_storage_writable"] is True
