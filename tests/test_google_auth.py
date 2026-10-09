# ruff: noqa: ARG001, S105, S106  (fixture-activation args; fixed test-only credentials)
"""Google sign-in: OAuth state/PKCE, ID-token verification, provisioning, roles, sessions.

Google is mocked at the network boundary only: ID tokens are real RS256 JWTs signed
with a test key and verified by the production code against a fake JWKS, so signature,
issuer, audience, expiry and nonce checks all genuinely run. No test calls Google.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import security.google_oauth as google_oauth
import webapp.main as web
from security.google_oauth import (
    STATE_COOKIE,
    GoogleAuthError,
    GoogleOAuthConfig,
    GoogleOIDCClient,
    OAuthStateStore,
)
from security.google_users import GoogleUserStore
from security.web_auth import AuthConfigError, AuthSettings
from tests.conftest import TEST_OTHER_USER_KEY, TEST_USER_PASSWORD, build_test_auth_settings

pytestmark = pytest.mark.security

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-value"
REDIRECT_URI = "http://testserver/api/auth/google/callback"
DOMAIN = "matrix.example"
ADMIN_SUB = "100000000000000000001"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    """Stands in for Google's JWKS endpoint (PyJWKClient interface)."""

    def __init__(self, public_key=None, error: Exception | None = None):
        self.public_key = public_key or _KEY.public_key()
        self.error = error

    def get_signing_key_from_jwt(self, token):
        if self.error:
            raise self.error
        return SimpleNamespace(key=self.public_key)


class FakeGoogle(GoogleOIDCClient):
    """Real authorization URL + real ID-token verification; the code exchange is scripted."""

    def __init__(self, config):
        super().__init__(config, jwks=FakeJWKS())
        self.next_claims: dict | None = None
        self.sign_with = _KEY
        self.exchange_error: Exception | None = None
        self.exchanges: list[tuple[str, str]] = []
        self.last_nonce = ""

    def authorization_url(self, state, pending):
        self.last_nonce = pending.nonce
        return super().authorization_url(state, pending)

    def exchange_code(self, code, code_verifier):
        self.exchanges.append((code, code_verifier))
        if self.exchange_error:
            raise self.exchange_error
        claims = {"nonce": self.last_nonce, **(self.next_claims or {})}
        return id_token(claims, key=self.sign_with)


def id_token(overrides: dict | None = None, key=_KEY, drop: tuple[str, ...] = ()) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com", "aud": CLIENT_ID, "azp": CLIENT_ID,
        "sub": "100000000000000000042", "email": f"asha@{DOMAIN}", "email_verified": True,
        "hd": DOMAIN, "name": "Asha", "iat": now, "exp": now + 3600, "nonce": "n",
    }
    claims.update(overrides or {})
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid"})


def google_config(**overrides) -> GoogleOAuthConfig:
    values = {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "redirect_uri": REDIRECT_URI,
        "allowed_domains": frozenset({DOMAIN}), "admin_subjects": frozenset({ADMIN_SUB}),
    }
    values.update(overrides)
    return GoogleOAuthConfig(**values)


@pytest.fixture
def google(monkeypatch, tmp_path):
    """Live app reconfigured with Google sign-in enabled and a fake Google."""
    original_config, original_store = web._authenticator.config, web._authenticator.google_users
    config = build_test_auth_settings(google=google_config())
    web._authenticator.google_users = GoogleUserStore(tmp_path / "users")
    web._authenticator.configure(config)
    fake = FakeGoogle(config.google)
    web._authenticator.google_client = fake
    monkeypatch.setattr(web, "_auth_settings", config)
    yield SimpleNamespace(fake=fake, config=config, store=web._authenticator.google_users, dir=tmp_path / "users")
    web._authenticator.google_users = original_store
    web._authenticator.configure(original_config)


def browser() -> TestClient:
    return TestClient(web.app, raise_server_exceptions=False, follow_redirects=False)


def start(client: TestClient) -> tuple[str, dict]:
    resp = client.get("/api/auth/google/start")
    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    return query["state"][0], query


def sign_in(client: TestClient, google, claims: dict | None = None, **params) -> httpx.Response:
    state, _ = start(client)
    google.fake.next_claims = claims
    return client.get("/api/auth/google/callback", params={"code": "auth-code-1", "state": state, **params})


def auth_error(resp: httpx.Response) -> str | None:
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/transcript")  # always a same-origin, relative redirect
    return parse_qs(urlsplit(location).query).get("auth_error", [None])[0]


# ---------------------------------------------------------------------------
# Start endpoint
# ---------------------------------------------------------------------------


def test_start_redirects_to_google_with_minimal_scopes_state_nonce_and_pkce(google):
    client = browser()
    resp = client.get("/api/auth/google/start")
    assert resp.status_code == 302
    url = urlsplit(resp.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query["scope"] == "openid email profile"  # identity only: no Gmail/Drive/YouTube scopes
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID and query["redirect_uri"] == REDIRECT_URI
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert len(query["state"]) >= 40 and len(query["nonce"]) >= 40
    assert query["hd"] == DOMAIN
    assert CLIENT_SECRET not in resp.headers["location"]
    cookie = resp.headers["set-cookie"].lower()
    assert f"{STATE_COOKIE}={query['state'].lower()}" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/api/auth/google" in cookie
    assert resp.headers["cache-control"] == "no-store"


def test_each_start_gets_fresh_state(google):
    client = browser()
    assert start(client)[0] != start(client)[0]


def test_start_when_google_not_configured_is_unavailable(auth_config):
    resp = browser().get("/api/auth/google/start")
    assert auth_error(resp) == "unavailable"


def test_start_is_rate_limited_per_ip(google):
    web._authenticator.oauth_limiter = google_oauth_limiter(2)
    client = browser()
    assert client.get("/api/auth/google/start").status_code == 302
    assert client.get("/api/auth/google/start").status_code == 302
    assert auth_error(client.get("/api/auth/google/start")) == "rate_limited"


def google_oauth_limiter(limit):
    from infrastructure.rate_limiter import SlidingWindowRateLimiter

    return SlidingWindowRateLimiter(limit, 60.0)


def test_providers_endpoint_reports_enabled_methods(google, auth_config):
    # auth_config re-applies settings without Google; check both states.
    assert browser().get("/api/auth/providers").json()["providers"] == {"password": True, "google": False}


def test_providers_endpoint_with_google(google):
    resp = browser().get("/api/auth/providers")
    assert resp.status_code == 200
    assert resp.json()["providers"] == {"password": True, "google": True}


# ---------------------------------------------------------------------------
# Successful sign-in, provisioning and roles
# ---------------------------------------------------------------------------


def test_new_google_user_is_provisioned_as_user_and_gets_the_existing_session(google):
    client = browser()
    resp = sign_in(client, google)
    assert auth_error(resp) is None and resp.headers["location"] == "/transcript"
    session_cookie = [c for c in resp.headers.get_list("set-cookie") if c.startswith("session=")][0].lower()
    assert "httponly" in session_cookie and "samesite=strict" in session_cookie  # same cookie as password login
    assert f"{STATE_COOKIE}=" in "".join(resp.headers.get_list("set-cookie"))  # state cookie cleared
    # Code exchange happened server-side with the PKCE verifier from /start.
    assert google.fake.exchanges[0][0] == "auth-code-1" and len(google.fake.exchanges[0][1]) >= 43

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"] == {
        "username": f"asha@{DOMAIN}", "role": "user", "auth_method": "session", "provider": "google",
    }
    record = json.loads((google.dir / "google_users.json").read_text(encoding="utf-8"))["users"]
    assert record["100000000000000000042"]["user_id"] == "google:100000000000000000042"
    assert record["100000000000000000042"]["auth_provider"] == "google"
    assert "role" not in record["100000000000000000042"]  # roles are never stored client-influenced


def test_google_session_is_a_regular_application_jwt(google):
    client = browser()
    sign_in(client, google)
    token = client.cookies.get("session")
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == "google:100000000000000000042" and claims["type"] == "access"
    assert claims["iss"] == "youtube-export" and claims["aud"] == "youtube-export-web"


def test_existing_google_user_signs_in_to_the_same_account(google):
    sign_in(browser(), google)
    first = google.store.get("100000000000000000042")
    sign_in(browser(), google, {"email": f"asha.k@{DOMAIN}", "name": "Asha K"})
    second = google.store.get("100000000000000000042")
    assert len(google.store) == 1
    assert second.created_at == first.created_at
    assert second.email == f"asha.k@{DOMAIN}"  # profile refreshed, identity (sub) unchanged


def test_identity_is_the_google_sub_not_the_email(google):
    sign_in(browser(), google, {"sub": "100000000000000000077", "email": f"shared@{DOMAIN}"})
    sign_in(browser(), google, {"sub": "100000000000000000078", "email": f"shared@{DOMAIN}"})
    assert len(google.store) == 2  # same email, different Google accounts => different users


def test_google_user_cannot_self_assign_admin(google):
    client = browser()
    resp = sign_in(client, google, {"role": "admin", "roles": ["admin"], "is_admin": True}, role="admin")
    assert auth_error(resp) is None
    assert client.get("/api/auth/me").json()["user"]["role"] == "user"
    assert client.get("/api/transcript/metrics").status_code == 403


def test_configured_admin_subject_gets_admin_role(google):
    client = browser()
    sign_in(client, google, {"sub": ADMIN_SUB, "email": f"boss@{DOMAIN}"})
    assert client.get("/api/auth/me").json()["user"]["role"] == "admin"
    assert client.get("/api/transcript/metrics").status_code == 200


def test_admin_rights_follow_server_configuration_not_the_session(google):
    client = browser()
    sign_in(client, google, {"sub": ADMIN_SUB, "email": f"boss@{DOMAIN}"})
    config = build_test_auth_settings(google=google_config(admin_subjects=frozenset()))
    web._authenticator.configure(config)
    web._authenticator.google_client = google.fake
    assert client.get("/api/auth/me").json()["user"]["role"] == "user"  # demoted without a new login


def test_google_session_works_on_protected_endpoints_and_owns_its_jobs(google):
    from models.transcript_job import JobStatus, TranscriptJobProgress
    from services.jobs.transcript_job_manager import transcript_job_manager

    client = browser()
    sign_in(client, google)
    job = TranscriptJobProgress(
        job_id="9a9a9a9a9a9a", channel_handle="chan", channel_id="", channel_title="chan",
        status=JobStatus.COMPLETED, total_discovered=0, eligible_videos=0, skipped_videos=0,
        remaining=0, videos=[], owner="google:100000000000000000042",
    )
    transcript_job_manager._jobs[job.job_id] = job
    try:
        assert client.get(f"/api/transcript/jobs/{job.job_id}").status_code == 200
        other = TestClient(web.app, headers={"X-API-Key": TEST_OTHER_USER_KEY})
        assert other.get(f"/api/transcript/jobs/{job.job_id}").status_code == 404
    finally:
        transcript_job_manager._jobs.pop(job.job_id, None)


def test_google_session_is_csrf_checked_like_password_sessions(google):
    client = browser()
    sign_in(client, google)
    resp = client.post("/api/transcript/jobs/0123456789ab/cancel", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# State / CSRF
# ---------------------------------------------------------------------------


def test_wrong_state_is_refused(google):
    client = browser()
    start(client)
    resp = client.get("/api/auth/google/callback", params={"code": "c", "state": "x" * 43})
    assert auth_error(resp) == "failed"
    assert "session" not in client.cookies
    assert google.fake.exchanges == []  # nothing was sent to Google


def test_state_from_another_browser_is_refused(google):
    attacker = browser()
    state, _ = start(attacker)
    victim = browser()  # no state cookie: login CSRF attempt
    resp = victim.get("/api/auth/google/callback", params={"code": "attacker-code", "state": state})
    assert auth_error(resp) == "failed"
    assert google.fake.exchanges == []


def test_state_is_single_use(google):
    client = browser()
    state, _ = start(client)
    google.fake.next_claims = None
    cookie = client.cookies.get(STATE_COOKIE)
    assert auth_error(client.get("/api/auth/google/callback", params={"code": "c", "state": state})) is None
    client.cookies.set(STATE_COOKIE, cookie, path="/api/auth/google")
    replay = client.get("/api/auth/google/callback", params={"code": "c", "state": state})
    assert auth_error(replay) == "failed"


def test_expired_state_is_refused(google, monkeypatch):
    client = browser()
    state, _ = start(client)
    real_time = time.time
    monkeypatch.setattr(google_oauth.time, "time", lambda: real_time() + google_oauth.STATE_TTL_SECONDS + 1)
    resp = client.get("/api/auth/google/callback", params={"code": "c", "state": state})
    assert auth_error(resp) == "failed"
    assert google.fake.exchanges == []


def test_missing_code_is_refused(google):
    client = browser()
    state, _ = start(client)
    assert auth_error(client.get("/api/auth/google/callback", params={"state": state})) == "failed"


def test_user_cancelling_at_google_is_reported_as_cancelled(google):
    client = browser()
    state, _ = start(client)
    resp = client.get("/api/auth/google/callback", params={"error": "access_denied", "state": state})
    assert auth_error(resp) == "cancelled"
    # The state was consumed: it cannot be completed afterwards.
    assert auth_error(client.get("/api/auth/google/callback", params={"code": "c", "state": state})) == "failed"


def test_state_store_is_bounded():
    store = OAuthStateStore(max_entries=5)
    states = [store.create()[0] for _ in range(20)]
    assert len(store) == 5
    assert store.consume(states[0]) is None and store.consume(states[-1]) is not None


# ---------------------------------------------------------------------------
# Code exchange and ID-token verification failures
# ---------------------------------------------------------------------------


def test_invalid_authorization_code_is_refused(google):
    google.fake.exchange_error = GoogleAuthError("exchange_failed", "code exchange rejected: HTTP 400 invalid_grant")
    assert auth_error(sign_in(browser(), google)) == "failed"


def test_google_outage_is_reported_as_unavailable(google):
    google.fake.exchange_error = GoogleAuthError("unavailable", "token endpoint unreachable")
    assert auth_error(sign_in(browser(), google)) == "unavailable"


@pytest.mark.parametrize(("claims", "expected"), [
    ({"iss": "https://evil.example"}, "failed"),                          # wrong issuer
    ({"aud": "someone-else.apps.googleusercontent.com"}, "failed"),       # wrong audience
    ({"azp": "someone-else.apps.googleusercontent.com"}, "failed"),       # wrong authorized party
    ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "failed"),  # expired
    ({"nonce": "not-the-nonce-we-sent"}, "failed"),                       # replayed / foreign token
    ({"email_verified": False}, "not_allowed"),                           # unverified email
    ({"hd": "", "email": "asha@other.example"}, "not_allowed"),           # outside the allowlist
    ({"hd": "other.example", "email": f"asha@{DOMAIN}"}, "not_allowed"),  # Workspace domain decides
    ({"sub": "../../etc/passwd"}, "failed"),                              # malformed subject
])
def test_rejected_id_tokens(google, claims, expected):
    client = browser()
    assert auth_error(sign_in(client, google, claims)) == expected
    assert client.get("/api/auth/me").status_code == 401
    assert len(google.store) == 0


# ---------------------------------------------------------------------------
# Workspace (hd) vs personal accounts
# ---------------------------------------------------------------------------

PERSONAL_SUB = "100000000000000000055"


def _use_policy(google, **overrides) -> None:
    """Reconfigure the live app's Google account policy, keeping the fake Google."""
    config = build_test_auth_settings(google=google_config(**overrides))
    web._authenticator.configure(config)
    web._authenticator.google_client = google.fake
    google.fake.config = config.google


@pytest.mark.parametrize(("claims", "expected_error"), [
    pytest.param({"hd": DOMAIN, "email": f"asha@{DOMAIN}"}, None, id="correct-hd"),
    pytest.param({"hd": DOMAIN, "email": "asha@alias.example"}, None, id="correct-hd-alias-email-domain"),
    pytest.param({"hd": "", "email": f"asha@{DOMAIN}"}, "not_allowed", id="missing-hd-company-email"),
    pytest.param({"hd": "other.example", "email": f"asha@{DOMAIN}"}, "not_allowed", id="wrong-hd"),
    pytest.param({"hd": "other.example", "email": "asha@other.example"}, "not_allowed", id="other-workspace"),
    pytest.param({"hd": "", "email": "asha@gmail.com"}, "not_allowed", id="personal-gmail"),
])
def test_workspace_policy_requires_the_hd_claim(google, claims, expected_error):
    client = browser()
    resp = sign_in(client, google, {"sub": PERSONAL_SUB, "email_verified": True, **claims})
    assert auth_error(resp) == expected_error
    assert (client.get("/api/auth/me").status_code == 200) is (expected_error is None)
    assert (google.store.get(PERSONAL_SUB) is not None) is (expected_error is None)


def test_verified_email_without_hd_is_not_a_workspace_identity(google):
    # A personal Google account created on the company address: Google verified the email,
    # but it is not managed by the company's Workspace, so it must not get in.
    resp = sign_in(browser(), google, {"sub": PERSONAL_SUB, "email": f"former.staff@{DOMAIN}",
                                       "email_verified": True, "hd": ""})
    assert auth_error(resp) == "not_allowed"
    assert len(google.store) == 0


def test_explicitly_allowed_personal_accounts(google):
    _use_policy(google, personal_email_domains=frozenset({"gmail.com"}))
    client = browser()
    resp = sign_in(client, google, {"sub": PERSONAL_SUB, "email": "asha@gmail.com", "hd": ""})
    assert auth_error(resp) is None
    assert client.get("/api/auth/me").json()["user"]["role"] == "user"
    stored = google.store.get(PERSONAL_SUB)
    assert stored is not None and stored.hosted_domain == ""
    # Still no back door: a personal account on the Workspace domain stays refused...
    other = sign_in(browser(), google, {"sub": "100000000000000000056", "email": f"x@{DOMAIN}", "hd": ""})
    assert auth_error(other) == "not_allowed"
    # ...and Workspace members still sign in.
    assert auth_error(sign_in(browser(), google)) is None


def test_personal_domains_alone_do_not_admit_workspace_accounts(google):
    _use_policy(google, allowed_domains=frozenset(), personal_email_domains=frozenset({"gmail.com"}))
    assert auth_error(sign_in(browser(), google)) == "not_allowed"  # hd=matrix.example, not listed


def test_workspace_hd_is_stored_and_rechecked_per_request(google):
    client = browser()
    sign_in(client, google)
    assert google.store.get("100000000000000000042").hosted_domain == DOMAIN
    _use_policy(google, allowed_domains=frozenset(), personal_email_domains=frozenset({DOMAIN.replace("matrix", "p")}))
    assert client.get("/api/auth/me").status_code == 401  # the session follows the current policy


def test_legacy_account_record_without_hd_is_treated_as_personal(google):
    google.dir.mkdir(parents=True, exist_ok=True)
    (google.dir / "google_users.json").write_text(json.dumps({"version": 1, "users": {
        "100000000000000000042": {"user_id": "google:100000000000000000042", "email": f"asha@{DOMAIN}",
                                  "display_name": "Asha", "created_at": "2026-10-01T00:00:00+00:00",
                                  "last_login_at": "2026-10-01T00:00:00+00:00", "account_domain": DOMAIN,
                                  "disabled": False},
    }}), encoding="utf-8")
    web._authenticator.google_users = GoogleUserStore(google.dir)
    session = web._authenticator.issue_session(
        web._authenticator.google_principal(web._authenticator.google_users.get("100000000000000000042")),
    )
    client = browser()
    client.cookies.set("session", session)
    assert client.get("/api/auth/me").status_code == 401  # no verified hd on record
    # Signing in again records the hd from a fresh, verified ID token.
    assert auth_error(sign_in(client, google)) is None
    assert client.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize(("overrides", "problem"), [
    pytest.param({"personal_email_domains": frozenset({DOMAIN})}, "both a Workspace domain and a personal", id="overlap"),
    pytest.param({"personal_email_domains": frozenset({"not a domain"})}, "GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS",
                 id="invalid-personal"),
])
def test_personal_domain_misconfiguration_is_refused(overrides, problem):
    settings = build_test_auth_settings(google=google_config(**overrides))
    assert any(problem in p for p in settings.problems())
    assert settings.google_enabled is False


def test_personal_domains_parsed_from_environment():
    settings = AuthSettings.from_env({
        "APP_ENV": "development", "GOOGLE_CLIENT_ID": CLIENT_ID, "GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "GOOGLE_REDIRECT_URI": REDIRECT_URI, "GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS": "@Gmail.com",
    })
    assert settings.google.personal_email_domains == frozenset({"gmail.com"})
    assert settings.google.allowed_domains == frozenset()
    assert settings.google_enabled


def test_hd_hint_only_when_a_single_workspace_domain_is_the_whole_policy():
    pending = OAuthStateStore().create()[1]
    workspace_only = GoogleOIDCClient(google_config(), jwks=FakeJWKS()).authorization_url("s" * 43, pending)
    assert "hd=" in workspace_only
    mixed = GoogleOIDCClient(
        google_config(personal_email_domains=frozenset({"gmail.com"})), jwks=FakeJWKS(),
    ).authorization_url("s" * 43, pending)
    assert "hd=" not in mixed  # the hint would hide personal accounts in Google's picker


# ---------------------------------------------------------------------------
# Logout ends the session server-side
# ---------------------------------------------------------------------------


def test_logout_revokes_the_session_token_itself(google):
    client = browser()
    sign_in(client, google)
    token = client.cookies.get("session")
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    replay = browser()
    replay.cookies.set("session", token)  # a copy of the cookie taken before logout
    assert replay.get("/api/auth/me").status_code == 401
    # A new sign-in gets a new, working session.
    assert auth_error(sign_in(client, google)) is None
    assert client.get("/api/auth/me").status_code == 200


def test_logout_revokes_password_sessions_too(google):
    client = browser()
    client.post("/api/auth/login", json={"username": "alice", "password": TEST_USER_PASSWORD})
    token = client.cookies.get("session")
    client.post("/api/auth/logout")
    replay = browser()
    replay.cookies.set("session", token)
    assert replay.get("/api/auth/me").status_code == 401


def test_logout_without_or_with_garbage_cookie_is_harmless(google):
    assert browser().post("/api/auth/logout").status_code == 200
    client = browser()
    client.cookies.set("session", "not-a-jwt")
    assert client.post("/api/auth/logout").status_code == 200


def test_session_tokens_have_unique_ids(google):
    sub = SimpleNamespace(id="alice", role="user")
    first = jwt.decode(web._authenticator._jwt.create_access_token(sub), options={"verify_signature": False})
    second = jwt.decode(web._authenticator._jwt.create_access_token(sub), options={"verify_signature": False})
    assert first["jti"] and first["jti"] != second["jti"]


def test_revocation_list_is_bounded_and_expires_entries(monkeypatch):
    from security.web_auth import RevokedSessions

    revoked = RevokedSessions(max_entries=3)
    now = time.time()
    for i in range(5):
        revoked.revoke(f"j{i}", now + 100 + i)
    assert len(revoked) == 3
    assert not revoked.is_revoked("j0") and revoked.is_revoked("j4")  # soonest-expiring dropped first
    revoked.revoke("expired", now - 1)
    assert not revoked.is_revoked("expired")
    monkeypatch.setattr("security.web_auth.time.time", lambda: now + 1000)
    assert not revoked.is_revoked("j4")  # past the token's own expiry


def test_oauth_code_and_state_redacted_from_any_logged_url():
    from infrastructure.log_redaction import redact

    line = "GET /api/auth/google/callback?code=4/0AbCdEf-secret&state=Zm9vYmFyYmF6&scope=email HTTP/1.1"
    out = redact(line)
    assert "4/0AbCdEf-secret" not in out and "Zm9vYmFyYmF6" not in out
    assert "scope=email" in out


def test_id_token_signed_by_another_key_is_refused(google):
    google.fake.sign_with = _OTHER_KEY
    assert auth_error(sign_in(browser(), google)) == "failed"
    assert len(google.store) == 0


def test_verify_requires_core_claims():
    client = GoogleOIDCClient(google_config(), jwks=FakeJWKS())
    for missing in ("exp", "iat", "sub", "aud", "iss"):
        with pytest.raises(GoogleAuthError):
            client.verify_id_token(id_token(drop=(missing,)), "n")


def test_verify_accepts_both_google_issuer_spellings():
    client = GoogleOIDCClient(google_config(), jwks=FakeJWKS())
    for issuer in ("https://accounts.google.com", "accounts.google.com"):
        assert client.verify_id_token(id_token({"iss": issuer}), "n").sub == "100000000000000000042"


def test_unsigned_token_is_refused():
    client = GoogleOIDCClient(google_config(), jwks=FakeJWKS())
    unsigned = jwt.encode({"sub": "1", "aud": CLIENT_ID, "iss": "accounts.google.com"}, key=None, algorithm="none")
    with pytest.raises(GoogleAuthError):
        client.verify_id_token(unsigned, "n")


def test_jwks_outage_is_unavailable():
    client = GoogleOIDCClient(google_config(), jwks=FakeJWKS(error=jwt.PyJWKClientConnectionError("down")))
    with pytest.raises(GoogleAuthError) as err:
        client.verify_id_token(id_token(), "n")
    assert err.value.public_code == "unavailable"


@pytest.mark.parametrize(("status", "body", "code"), [
    (400, {"error": "invalid_grant", "error_description": "Bad Request"}, "exchange_failed"),
    (401, {"error": "invalid_client"}, "exchange_failed"),
    (503, {}, "unavailable"),
    (200, {"access_token": "x"}, "exchange_failed"),  # no id_token
])
def test_code_exchange_errors(monkeypatch, status, body, code):
    def fake_post(url, data, headers, timeout):
        assert url == google_oauth.TOKEN_ENDPOINT
        assert data["grant_type"] == "authorization_code" and data["code_verifier"] == "verifier"
        return httpx.Response(status, json=body)

    monkeypatch.setattr(google_oauth.httpx, "post", fake_post)
    with pytest.raises(GoogleAuthError) as err:
        GoogleOIDCClient(google_config(), jwks=FakeJWKS()).exchange_code("code", "verifier")
    assert err.value.code == code


def test_code_exchange_network_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(google_oauth.httpx, "post", boom)
    with pytest.raises(GoogleAuthError) as err:
        GoogleOIDCClient(google_config(), jwks=FakeJWKS()).exchange_code("code", "verifier")
    assert err.value.public_code == "unavailable"


# ---------------------------------------------------------------------------
# Accounts: disabled, store failures, unexpected errors, revocation
# ---------------------------------------------------------------------------


def _disable(google, sub: str) -> None:
    path = google.dir / "google_users.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["users"][sub]["disabled"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    web._authenticator.google_users = GoogleUserStore(google.dir)  # what a restart does


def test_disabled_account_cannot_sign_in_and_loses_its_session(google):
    client = browser()
    sign_in(client, google)
    _disable(google, "100000000000000000042")
    assert client.get("/api/auth/me").status_code == 401
    assert auth_error(sign_in(browser(), google)) == "not_allowed"


def test_turning_google_off_revokes_google_sessions(google):
    client = browser()
    sign_in(client, google)
    web._authenticator.configure(build_test_auth_settings())
    assert client.get("/api/auth/me").status_code == 401


def test_narrowing_the_domain_allowlist_revokes_sessions(google):
    client = browser()
    sign_in(client, google)
    web._authenticator.configure(build_test_auth_settings(google=google_config(allowed_domains=frozenset({"x.example"}))))
    web._authenticator.google_client = google.fake
    assert client.get("/api/auth/me").status_code == 401


def test_corrupt_account_file_disables_google_without_overwriting_it(google):
    google.dir.mkdir(parents=True, exist_ok=True)
    (google.dir / "google_users.json").write_text("{not json", encoding="utf-8")
    web._authenticator.google_users = GoogleUserStore(google.dir)
    assert web._authenticator.google_enabled is False
    assert auth_error(browser().get("/api/auth/google/start")) == "unavailable"
    assert (google.dir / "google_users.json").read_text(encoding="utf-8") == "{not json"


def test_provisioning_failure_is_reported_safely(google, monkeypatch):
    from security.google_users import GoogleUserStoreError

    def fail(*args, **kwargs):
        raise GoogleUserStoreError("could not save Google accounts: [Errno 28] No space left on device: /app/data")

    monkeypatch.setattr(google.store, "record_login", fail)
    client = browser()
    resp = sign_in(client, google)
    assert auth_error(resp) == "failed"
    assert "/app/data" not in resp.headers["location"] and "session" not in client.cookies


def test_unexpected_error_is_generic_and_logged(google, caplog):
    google.fake.exchange_error = RuntimeError("internal detail /srv/secret/path")
    with caplog.at_level(logging.ERROR, logger="webapp"):
        resp = sign_in(browser(), google)
    assert auth_error(resp) == "failed"
    assert "/srv/secret/path" not in resp.text + resp.headers["location"]
    assert "Google sign-in failed unexpectedly" in caplog.text


def test_tokens_codes_and_secrets_never_logged(google, caplog):
    with caplog.at_level(logging.DEBUG):
        client = browser()
        state, query = start(client)
        client.get("/api/auth/google/callback", params={"code": "auth-code-SECRET123", "state": state})
        google.fake.next_claims = {"nonce": "wrong"}
        state2, _ = start(client)
        client.get("/api/auth/google/callback", params={"code": "auth-code-SECRET456", "state": state2})
    # Server-side loggers only: "httpx2" is the in-process test client logging its own
    # request URL (the browser side). In production nginx logs this route without its
    # query string (docker/nginx/nginx.conf, location = /api/auth/google/callback).
    text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    token = client.cookies.get("session") or ""
    for secret in (CLIENT_SECRET, "auth-code-SECRET123", "auth-code-SECRET456", state, query["nonce"][0]):
        assert secret not in text
    assert not token or token not in text


# ---------------------------------------------------------------------------
# Existing authentication is unchanged
# ---------------------------------------------------------------------------


def test_password_login_still_works_with_google_enabled(google):
    client = browser()
    resp = client.post("/api/auth/login", json={"username": "alice", "password": TEST_USER_PASSWORD})
    assert resp.status_code == 200
    assert client.get("/api/auth/me").json()["user"]["provider"] == "password"


def test_unauthenticated_requests_still_rejected(google):
    assert browser().get("/api/auth/me").status_code == 401
    assert browser().post("/api/transcript", json={"video_url": "dQw4w9WgXcQ"}).status_code == 401


def test_forged_google_session_is_rejected(google):
    forged = jwt.encode(
        {"sub": f"google:{ADMIN_SUB}", "type": "access", "iss": "youtube-export", "aud": "youtube-export-web",
         "iat": int(time.time()), "exp": int(time.time()) + 600, "role": "admin"},
        "not-the-server-secret-" + "x" * 32, algorithm="HS256",
    )
    client = browser()
    client.cookies.set("session", forged)
    assert client.get("/api/auth/me").status_code == 401


def test_session_for_unknown_google_account_is_rejected(google):
    token = web._authenticator._jwt.create_access_token(SimpleNamespace(id="google:100000000000000000999", role="admin"))
    client = browser()
    client.cookies.set("session", token)
    assert client.get("/api/auth/me").status_code == 401  # never provisioned => no access


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_google_settings_parsed_from_environment():
    settings = AuthSettings.from_env({
        "APP_ENV": "development", "GOOGLE_CLIENT_ID": CLIENT_ID, "GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "GOOGLE_REDIRECT_URI": REDIRECT_URI, "GOOGLE_ALLOWED_DOMAINS": f"@{DOMAIN.upper()}, b.example",
        "GOOGLE_ADMIN_SUBJECTS": ADMIN_SUB,
    })
    assert settings.google.allowed_domains == frozenset({DOMAIN, "b.example"})
    assert settings.google_enabled


@pytest.mark.parametrize(("overrides", "problem"), [
    ({"client_secret": ""}, "GOOGLE_CLIENT_SECRET"),
    ({"redirect_uri": "http://testserver/other"}, "GOOGLE_REDIRECT_URI"),
    ({"allowed_domains": frozenset()}, "GOOGLE_ALLOWED_DOMAINS"),
    ({"allowed_domains": frozenset({"not a domain"})}, "invalid entries"),
    ({"admin_subjects": frozenset({"bad sub!"})}, "GOOGLE_ADMIN_SUBJECTS"),
])
def test_incomplete_google_config_is_reported_and_disabled(overrides, problem):
    settings = build_test_auth_settings(google=google_config(**overrides))
    assert any(problem in p for p in settings.problems())
    assert settings.google_enabled is False


def test_any_account_mode_must_be_explicit():
    settings = build_test_auth_settings(google=google_config(allowed_domains=frozenset(), allow_any_account=True))
    assert settings.google_enabled


def test_production_refuses_insecure_or_incomplete_google_config():
    with pytest.raises(AuthConfigError, match="https"):
        build_test_auth_settings(app_env="production", google=google_config()).validate()
    with pytest.raises(AuthConfigError, match="GOOGLE_CLIENT_SECRET"):
        build_test_auth_settings(app_env="production", google=google_config(
            client_secret="", redirect_uri="https://t.example/api/auth/google/callback")).validate()
    build_test_auth_settings(app_env="production", google=google_config(
        redirect_uri="https://t.example/api/auth/google/callback")).validate()
