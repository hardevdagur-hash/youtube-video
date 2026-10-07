# ruff: noqa: ARG001, S105  (fixture-activation args; fixed test-only credentials)
import atexit
import os
import shutil
import tempfile

import pytest

# Isolate every test run from the developer's real data and logs. This must run before
# config.settings is imported (it reads DATA_DIR / LOG_DIR once), and it overrides any
# value from the shell or .env on purpose.
_TEST_ROOT = tempfile.mkdtemp(prefix="transcript-tests-")
atexit.register(shutil.rmtree, _TEST_ROOT, ignore_errors=True)
os.environ["DATA_DIR"] = os.path.join(_TEST_ROOT, "data")
os.environ["LOG_DIR"] = os.path.join(_TEST_ROOT, "logs")

# Never let tests use real credentials from the shell or .env (that would make real,
# billable YouTube/Groq calls). Empty values win over .env because it never overrides.
os.environ["YOUTUBE_API_KEY"] = "test_key_for_testing"
os.environ["GROQ_API_KEY"] = ""
os.environ["JWT_SECRET_KEY"] = "test-only-jwt-secret-" + "x" * 32


@pytest.fixture(autouse=True)
def _fresh_translation_service():
    """The translation service is a process-wide singleton; tests patch its class."""
    from services.translation import service as translation_service

    translation_service.reset_translation_service()
    yield
    translation_service.reset_translation_service()


# ---------------------------------------------------------------------------
# Web authentication fixtures
# ---------------------------------------------------------------------------

TEST_ADMIN_KEY = "ysk_test-admin-key-0000000000000000000000000"
TEST_USER_KEY = "ysk_test-user-key-11111111111111111111111111"
TEST_OTHER_USER_KEY = "ysk_test-other-key-2222222222222222222222222"
TEST_USER_PASSWORD = "correct horse battery staple"

_PASSWORD_HASH_CACHE: dict[str, str] = {}


def build_test_auth_settings(**overrides):
    """AuthSettings with known test credentials (never read from the developer's .env)."""
    from security.web_auth import AuthSettings, hash_api_key, hash_password

    if "pw" not in _PASSWORD_HASH_CACHE:
        _PASSWORD_HASH_CACHE["pw"] = hash_password(TEST_USER_PASSWORD)
    values = {
        "app_env": "development",
        "jwt_secret": "test-secret-" + "x" * 40,
        "users": {
            "alice": ("user", _PASSWORD_HASH_CACHE["pw"]),
            "root": ("admin", _PASSWORD_HASH_CACHE["pw"]),
        },
        "api_keys": [
            ("admin-key", "admin", hash_api_key(TEST_ADMIN_KEY)),
            ("user-key", "user", hash_api_key(TEST_USER_KEY)),
            ("other-key", "user", hash_api_key(TEST_OTHER_USER_KEY)),
        ],
        "cors_origins": ["https://app.example.com"],
    }
    values.update(overrides)
    return AuthSettings(**values)


@pytest.fixture
def auth_config(monkeypatch):
    """Reconfigure the live app's authenticator with test credentials for one test."""
    import webapp.main as web

    original = web._authenticator.config
    config = build_test_auth_settings()
    web._authenticator.configure(config)
    monkeypatch.setattr(web, "_auth_settings", config)
    yield config
    web._authenticator.configure(original)


@pytest.fixture
def authed_client(auth_config):
    """TestClient authenticated as a regular user via API key."""
    from fastapi.testclient import TestClient

    from webapp.main import app

    return TestClient(app, raise_server_exceptions=False, headers={"X-API-Key": TEST_USER_KEY})
