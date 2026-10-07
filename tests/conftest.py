# ruff: noqa: ARG001, S105  (fixture-activation args; fixed test-only credentials)
import os

import pytest

os.environ.setdefault("YOUTUBE_API_KEY", "test_key_for_testing")


@pytest.fixture(autouse=True)
def isolated_metrics_registry(monkeypatch):
    """Fresh Prometheus registry per test (MetricsManager defaults to the global one)."""
    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    monkeypatch.setattr("observability.metrics.REGISTRY", registry)
    return registry


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
