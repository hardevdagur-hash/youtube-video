# ruff: noqa: S105, S106  (fake test-only secrets)
"""Regression tests: credentials must never reach log output (stdout, files, other handlers)."""

from __future__ import annotations

import logging

import httpx
import pytest

from infrastructure import log_redaction
from infrastructure.log_redaction import (
    REDACTED,
    install_secret_redaction,
    redact,
    refresh_known_secrets,
)

pytestmark = pytest.mark.security

# Fake secrets only. Shaped like the real formats so the pattern rules are exercised.
FAKE_YOUTUBE_KEY = "AIza" + "FAKEfakeFAKEfakeFAKEfakeFAKEfake123"  # 39 chars like a real key
FAKE_GROQ_KEY = "gsk_" + "FakeGroqKeyForTests" * 3
FAKE_BEARER = "FakeBearerTokenValue123456"
FAKE_APP_KEY = "ysk_FakeAppApiKeyValueForTests_0123456789"
FAKE_ODD_SECRET = "odd-format-secret-value-42"  # no recognisable pattern; caught via env value

_MANAGED_LOGGERS = ("", "uvicorn", "googleapiclient", "httplib2", *log_redaction.NOISY_HTTP_LOGGERS)


@pytest.fixture
def fake_secrets_env(monkeypatch):
    """Expose fake secrets through the environment, never the developer's real .env values."""
    monkeypatch.setenv("YOUTUBE_API_KEY", FAKE_YOUTUBE_KEY)
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("JWT_SECRET_KEY", FAKE_ODD_SECRET)
    install_secret_redaction()
    refresh_known_secrets()
    yield
    monkeypatch.undo()
    refresh_known_secrets()


@pytest.fixture
def isolated_logging():
    """Snapshot and restore logger handlers/levels touched by setup_logging()."""
    saved = {}
    for name in _MANAGED_LOGGERS:
        lg = logging.getLogger(name)
        saved[name] = (lg.level, list(lg.handlers), lg.propagate)
    yield
    for name, (level, handlers, propagate) in saved.items():
        lg = logging.getLogger(name)
        for h in lg.handlers:
            if h not in handlers:
                h.close()
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


def _assert_no_fake_secret(text: str) -> None:
    for secret in (FAKE_YOUTUBE_KEY, FAKE_GROQ_KEY, FAKE_BEARER, FAKE_APP_KEY, FAKE_ODD_SECRET):
        assert secret not in text


# ---------------------------------------------------------------------------
# 1 + 2. YouTube requests still authenticate; the key never reaches logs
# ---------------------------------------------------------------------------


def test_youtube_key_absent_from_googleapiclient_debug_logs(fake_secrets_env, caplog):
    # googleapiclient logs the full request URL (including ?key=) at DEBUG level.
    caplog.set_level(logging.DEBUG, logger="googleapiclient.discovery")
    url = f"https://youtube.googleapis.com/youtube/v3/channels?part=id&forHandle=x&key={FAKE_YOUTUBE_KEY}&alt=json"
    logging.getLogger("googleapiclient.discovery").debug("URL being requested: GET %s", url)
    assert "key=" + REDACTED in caplog.text
    _assert_no_fake_secret(caplog.text)


def test_httpx_debug_logging_redacts_credentials(fake_secrets_env, caplog):
    # Worst case for the Groq SDK (httpx based): someone turns httpx logging up to DEBUG.
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        resp = client.get("https://api.example.com/v1/x", params={"key": FAKE_YOUTUBE_KEY},
                          headers={"Authorization": f"Bearer {FAKE_GROQ_KEY}"})
    assert resp.status_code == 200
    assert seen[0].url.params["key"] == FAKE_YOUTUBE_KEY  # the upstream still receives the credential
    httpx_records = [r for r in caplog.records if r.name == "httpx"]
    assert httpx_records, "httpx should have logged the request at DEBUG level"
    assert "key=" + REDACTED in httpx_records[0].getMessage()
    _assert_no_fake_secret(caplog.text)

def test_http_client_request_logging_suppressed_by_default(fake_secrets_env, isolated_logging, tmp_path, monkeypatch):
    from config.settings import settings
    from infrastructure.logging import setup_logging

    monkeypatch.setattr(settings, "logs_dir", tmp_path)
    setup_logging()
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        assert not lg.isEnabledFor(logging.INFO), f"{name} must not log request URLs at INFO"
        assert lg.isEnabledFor(logging.WARNING)
    # Application loggers keep normal levels
    assert logging.getLogger("webapp").isEnabledFor(logging.INFO)


# ---------------------------------------------------------------------------
# 3-5. Pattern redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    f"GET https://www.googleapis.com/youtube/v3/channels?key={FAKE_YOUTUBE_KEY}",
    "GET https://www.googleapis.com/youtube/v3/videos?part=id&key=SECRETVALUE1&maxResults=50",
    "GET https://example.com/x?api_key=SECRETVALUE1",
    "https://example.com/cb?access_token=SECRETVALUE1&state=ok",
])
def test_query_parameter_credentials_redacted(text):
    out = redact(text)
    assert "SECRETVALUE1" not in out
    assert FAKE_YOUTUBE_KEY not in out
    assert REDACTED in out


def test_url_structure_preserved_after_redaction():
    out = redact("GET https://www.googleapis.com/youtube/v3/videos?part=id&key=SECRETVALUE1&maxResults=50")
    assert out == f"GET https://www.googleapis.com/youtube/v3/videos?part=id&key={REDACTED}&maxResults=50"


@pytest.mark.parametrize("text", [
    f"Authorization: Bearer {FAKE_BEARER}",
    f"headers={{'Authorization': 'Bearer {FAKE_BEARER}'}}",
    f'"authorization": "bearer {FAKE_BEARER}"',
])
def test_bearer_tokens_redacted(text):
    out = redact(text)
    assert FAKE_BEARER not in out
    assert REDACTED in out


@pytest.mark.parametrize("text", [
    f"X-API-Key: {FAKE_APP_KEY}",
    f"headers={{'X-API-Key': '{FAKE_APP_KEY}'}}",
    f"x-api-key={FAKE_APP_KEY}",
    f"client sent {FAKE_APP_KEY}",
])
def test_api_key_headers_redacted(text):
    assert FAKE_APP_KEY not in redact(text)


@pytest.mark.parametrize("text", [
    "Set-Cookie: session=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2lnbmF0dXJlMTIz; HttpOnly",
    "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2lnbmF0dXJlMTIz",
    "password=hunter2hunter2",
    '{"password": "hunter2hunter2"}',
    "GROQ_API_KEY=" + FAKE_GROQ_KEY,
    "Groq error with " + FAKE_GROQ_KEY,
    "AUTH_USERS=alice:admin:scrypt.16384.8.1.c2FsdHNhbHRzYWx0.aGFzaGhhc2hoYXNoaGFzaGhhc2g",
])
def test_session_jwt_password_and_provider_keys_redacted(text):
    out = redact(text)
    for fragment in ("eyJzdWIiOiJhbGljZSJ9", "hunter2hunter2", FAKE_GROQ_KEY, "aGFzaGhhc2hoYXNoaGFzaGhhc2g"):
        assert fragment not in out


def test_configured_secret_values_redacted_even_without_known_format(fake_secrets_env):
    assert FAKE_ODD_SECRET not in redact(f"loaded signing secret {FAKE_ODD_SECRET} from env")


def test_ordinary_text_untouched():
    text = "Invalid username or password. Fetched 12 transcripts; token count 532; key points: a, b"
    assert redact(text) == text


# ---------------------------------------------------------------------------
# 6. Redaction applies to stdout and file handlers configured by setup_logging()
# ---------------------------------------------------------------------------


def test_stdout_and_file_output_redacted(fake_secrets_env, isolated_logging, tmp_path, monkeypatch, capsys):
    from config.settings import settings
    from infrastructure.logging import setup_logging

    monkeypatch.setattr(settings, "logs_dir", tmp_path)
    setup_logging()
    log = logging.getLogger("tests.redaction")
    log.setLevel(logging.INFO)
    url = f"https://www.googleapis.com/youtube/v3/channels?part=id&key={FAKE_YOUTUBE_KEY}"
    log.info("Requesting %s", url)
    # StructuredFormatter writes `extra` fields for records that have format args
    log.warning("Upstream rejected %s", "request", extra={"request_url": url, "auth": f"Bearer {FAKE_BEARER}"})
    try:
        raise RuntimeError(f"connect failed for {url} with X-API-Key: {FAKE_APP_KEY}")
    except RuntimeError:
        log.exception("YouTube call failed")
    for h in logging.getLogger().handlers:
        h.flush()

    stdout = capsys.readouterr().out
    file_text = (tmp_path / "transcript-service.log").read_text(encoding="utf-8")
    errors_text = (tmp_path / "errors.log").read_text(encoding="utf-8")

    for output in (stdout, file_text, errors_text):
        _assert_no_fake_secret(output)
    assert "Requesting https://www.googleapis.com/youtube/v3/channels" in stdout
    assert "Requesting https://www.googleapis.com/youtube/v3/channels" in file_text
    assert "request_url" in file_text  # structured extras kept, only the secret removed


# ---------------------------------------------------------------------------
# 7. Useful error information survives redaction
# ---------------------------------------------------------------------------


def test_error_messages_remain_useful(fake_secrets_env, caplog):
    caplog.set_level(logging.INFO, logger="tests.errors")
    log = logging.getLogger("tests.errors")
    try:
        raise ValueError(
            f"HTTP 403 quotaExceeded for https://www.googleapis.com/youtube/v3/search?q=x&key={FAKE_YOUTUBE_KEY}"
        )
    except ValueError:
        log.exception("YouTube search failed for channel %s", "physicsgalaxyworld")

    record = caplog.records[-1]
    message = record.getMessage()
    assert message == "YouTube search failed for channel physicsgalaxyworld"
    assert "ValueError" in record.exc_text
    assert "HTTP 403 quotaExceeded" in record.exc_text
    assert "/youtube/v3/search?q=x&key=" + REDACTED in record.exc_text
    assert "test_log_redaction.py" in record.exc_text  # traceback frames kept
    _assert_no_fake_secret(caplog.text)


def test_malformed_log_args_do_not_crash():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "value %s %s", ("only-one",), None)
    out = log_redaction._redact_record(record)
    assert out.args is None
    assert "value" in out.msg
