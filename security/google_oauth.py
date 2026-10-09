"""Google sign-in (OAuth 2.0 authorization-code flow + OpenID Connect).

Only the identity scopes ``openid email profile`` are requested. The flow:

    GET /api/auth/google/start
        new state + nonce + PKCE verifier kept server-side (single use, 10 min);
        the state is also set in an HttpOnly cookie that binds the flow to this browser;
        302 to Google's consent screen.
    GET /api/auth/google/callback?code=...&state=...
        state must match the cookie AND an unused server-side entry;
        the code is exchanged server-side (client secret + PKCE verifier);
        the ID token's signature (Google JWKS), issuer, audience, expiry, issue time
        and nonce are verified, and the email must be verified by Google;
        the account must pass the configured domain policy.

The verified Google ``sub`` (never the email) identifies the user. The caller then
provisions/looks up the local user and issues the application's existing session.

Configuration (environment, parsed by ``security.web_auth.AuthSettings``):

    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
    GOOGLE_ALLOWED_DOMAINS                 comma list of Google Workspace domains: the ID token's
                                           ``hd`` claim must equal one of them (the email domain
                                           alone is never enough)
    GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS  comma list of email domains (e.g. gmail.com) whose
                                           personal accounts (no ``hd`` claim) are accepted
    GOOGLE_ALLOW_ANY_ACCOUNT               "true" to accept any verified Google account
    GOOGLE_ADMIN_SUBJECTS                  comma list of Google ``sub`` ids granted the admin role
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

logger = logging.getLogger("security.google_oauth")

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL, not a secret)
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPES = "openid email profile"

CALLBACK_PATH = "/api/auth/google/callback"
STATE_COOKIE = "google_oauth_state"
STATE_COOKIE_PATH = "/api/auth/google"
STATE_TTL_SECONDS = 600
MAX_PENDING_LOGINS = 10_000
_CLOCK_SKEW_SECONDS = 60
_HTTP_TIMEOUT_SECONDS = 10.0

SUBJECT_PREFIX = "google:"
_SUB_RE = re.compile(r"^[0-9A-Za-z_-]{1,255}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


# ---------------------------------------------------------------------------
# Errors: ``code`` is internal (logs); ``public_code`` is shown to the browser
# ---------------------------------------------------------------------------

_PUBLIC_CODES = {
    "cancelled": "cancelled",
    "not_allowed": "not_allowed",
    "disabled": "not_allowed",
    "email_unverified": "not_allowed",
    "unavailable": "unavailable",
    "rate_limited": "rate_limited",
}


class GoogleAuthError(Exception):
    """A Google sign-in attempt that must be refused. Messages are for server logs only."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code

    @property
    def public_code(self) -> str:
        """Coarse, client-safe reason: cancelled | not_allowed | unavailable | rate_limited | failed."""
        return _PUBLIC_CODES.get(self.code, "failed")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    allowed_domains: frozenset[str] = frozenset()  # Workspace domains, matched against "hd"
    personal_email_domains: frozenset[str] = frozenset()  # personal accounts (no "hd") by email domain
    allow_any_account: bool = False
    admin_subjects: frozenset[str] = frozenset()

    @property
    def configured(self) -> bool:
        """Any Google setting present (then it must be complete and valid)."""
        return bool(self.client_id or self.client_secret or self.redirect_uri)

    def problems(self, production: bool) -> list[str]:
        if not self.configured:
            return []
        issues: list[str] = []
        if not (self.client_id and self.client_secret and self.redirect_uri):
            issues.append("Google sign-in needs GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI")
        if self.redirect_uri:
            parts = urlsplit(self.redirect_uri)
            if parts.path != CALLBACK_PATH or parts.query or parts.fragment or not parts.netloc:
                issues.append(f"GOOGLE_REDIRECT_URI must be <scheme>://<host>{CALLBACK_PATH}")
            elif production and parts.scheme != "https":
                issues.append("GOOGLE_REDIRECT_URI must use https in production")
            elif parts.scheme not in ("http", "https"):
                issues.append("GOOGLE_REDIRECT_URI must be an http(s) URL")
        if not self.allowed_domains and not self.personal_email_domains and not self.allow_any_account:
            issues.append(
                "Google sign-in needs GOOGLE_ALLOWED_DOMAINS (Workspace) and/or "
                "GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS, or GOOGLE_ALLOW_ANY_ACCOUNT=true to accept any "
                "Google account (anyone could then sign up and use the YouTube/Groq quota)"
            )
        for key, domains in (
            ("GOOGLE_ALLOWED_DOMAINS", self.allowed_domains),
            ("GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS", self.personal_email_domains),
        ):
            bad_domains = [d for d in domains if not _DOMAIN_RE.match(d)]
            if bad_domains:
                issues.append(f"{key} has invalid entries: {', '.join(sorted(bad_domains))[:200]}")
        overlap = self.allowed_domains & self.personal_email_domains
        if overlap:
            # A personal account on a Workspace domain would bypass the hd check.
            issues.append(
                "a domain cannot be both a Workspace domain and a personal email domain: "
                f"{', '.join(sorted(overlap))[:200]}"
            )
        if any(not _SUB_RE.match(s) for s in self.admin_subjects):
            issues.append("GOOGLE_ADMIN_SUBJECTS must contain Google account ids (the 'sub' claim)")
        return issues

    @classmethod
    def from_env(cls, env: dict[str, str]) -> GoogleOAuthConfig:
        def split(key: str) -> list[str]:
            return [p.strip() for p in env.get(key, "").split(",") if p.strip()]

        return cls(
            client_id=env.get("GOOGLE_CLIENT_ID", "").strip(),
            client_secret=env.get("GOOGLE_CLIENT_SECRET", "").strip(),
            redirect_uri=env.get("GOOGLE_REDIRECT_URI", "").strip(),
            allowed_domains=frozenset(d.lower().lstrip("@") for d in split("GOOGLE_ALLOWED_DOMAINS")),
            personal_email_domains=frozenset(
                d.lower().lstrip("@") for d in split("GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS")
            ),
            allow_any_account=env.get("GOOGLE_ALLOW_ANY_ACCOUNT", "").strip().lower() in ("1", "true", "yes", "on"),
            admin_subjects=frozenset(split("GOOGLE_ADMIN_SUBJECTS")),
        )


# ---------------------------------------------------------------------------
# Pending logins (state -> nonce + PKCE verifier), in process, single use
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingLogin:
    nonce: str
    code_verifier: str
    created: float


class OAuthStateStore:
    """Short-lived, single-use login attempts. In memory: correct for the single-instance
    deployment (a restart only invalidates logins that were in progress)."""

    def __init__(self, ttl_seconds: float = STATE_TTL_SECONDS, max_entries: int = MAX_PENDING_LOGINS) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._pending: OrderedDict[str, PendingLogin] = OrderedDict()
        self._lock = Lock()

    def create(self) -> tuple[str, PendingLogin]:
        state = secrets.token_urlsafe(32)
        pending = PendingLogin(
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),  # 86 chars: within PKCE's 43-128
            created=time.time(),
        )
        with self._lock:
            self._purge(time.time())
            self._pending[state] = pending
            while len(self._pending) > self._max:
                self._pending.popitem(last=False)  # oldest attempts are abandoned first
        return state, pending

    def consume(self, state: str) -> PendingLogin | None:
        """Remove and return the attempt for ``state`` if it exists and has not expired."""
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or time.time() - pending.created > self._ttl:
            return None
        return pending

    def _purge(self, now: float) -> None:
        for state in [s for s, p in self._pending.items() if now - p.created > self._ttl]:
            del self._pending[state]

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


def valid_state_format(state: str | None) -> bool:
    return bool(state) and bool(_STATE_RE.match(state))


def pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def states_match(cookie_value: str | None, state: str | None) -> bool:
    return bool(cookie_value) and bool(state) and hmac.compare_digest(cookie_value, state)


# ---------------------------------------------------------------------------
# Verified identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool
    name: str = ""
    hosted_domain: str = ""  # the "hd" claim: set only for Google Workspace accounts

    @property
    def email_domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower() if "@" in self.email else ""


def domain_policy_violation(hosted_domain: str, email_domain: str, config: GoogleOAuthConfig) -> str | None:
    """Why an account may not sign in under the domain policy, or None if it may.

    Workspace accounts (``hd`` present) are accepted only when ``hd`` is a configured Workspace
    domain; the email domain is ignored for them. Personal accounts (no ``hd``) are accepted only
    when their email domain is explicitly listed in GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS, so a
    personal account created on a company address never passes as a member of that Workspace.
    """
    if config.allow_any_account:
        return None
    hosted_domain = hosted_domain.lower()
    email_domain = email_domain.lower()
    if hosted_domain:
        if hosted_domain in config.allowed_domains:
            return None
        return f"Workspace domain {hosted_domain} is not allowed"
    if email_domain and email_domain in config.personal_email_domains:
        return None
    if email_domain in config.allowed_domains:
        return f"personal account on Workspace domain {email_domain} (no hd claim) is not allowed"
    return f"personal account domain {email_domain or '-'} is not allowed"


def check_account_policy(identity: GoogleIdentity, config: GoogleOAuthConfig) -> None:
    """Raise unless the verified account may use this application."""
    if not identity.email_verified:
        raise GoogleAuthError("email_unverified", "Google reports the email address as unverified")
    violation = domain_policy_violation(identity.hosted_domain, identity.email_domain, config)
    if violation:
        raise GoogleAuthError("not_allowed", violation)


# ---------------------------------------------------------------------------
# Google endpoints
# ---------------------------------------------------------------------------


class SigningKeyResolver(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


@dataclass
class GoogleOIDCClient:
    """Builds the consent URL, exchanges codes and verifies ID tokens."""

    config: GoogleOAuthConfig
    jwks: SigningKeyResolver = field(default_factory=lambda: jwt.PyJWKClient(
        JWKS_URI, cache_keys=True, lifespan=3600, timeout=int(_HTTP_TIMEOUT_SECONDS),
    ))

    def authorization_url(self, state: str, pending: PendingLogin) -> str:
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "nonce": pending.nonce,
            "code_challenge": pkce_challenge(pending.code_verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
            "access_type": "online",
        }
        if (
            len(self.config.allowed_domains) == 1
            and not self.config.personal_email_domains
            and not self.config.allow_any_account
        ):
            # UI hint only (preselects the Workspace domain); enforced again after verification.
            params["hd"] = next(iter(self.config.allowed_domains))
        return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def exchange_code(self, code: str, code_verifier: str) -> str:
        """Server-side code exchange; returns the raw ID token. Never logs codes or tokens."""
        try:
            response = httpx.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "redirect_uri": self.config.redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise GoogleAuthError("unavailable", f"token endpoint unreachable: {type(exc).__name__}") from None
        if response.status_code >= 500:
            raise GoogleAuthError("unavailable", f"token endpoint returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise GoogleAuthError("exchange_failed", "token endpoint returned non-JSON") from None
        if response.status_code != 200:
            # Google's error code (e.g. invalid_grant) is safe to log; the description is not needed.
            error = str(body.get("error", "unknown"))[:64] if isinstance(body, dict) else "unknown"
            raise GoogleAuthError("exchange_failed", f"code exchange rejected: HTTP {response.status_code} {error}")
        id_token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(id_token, str) or not id_token:
            raise GoogleAuthError("exchange_failed", "token response has no id_token")
        return id_token

    def verify_id_token(self, id_token: str, expected_nonce: str) -> GoogleIdentity:
        """Verify signature (Google JWKS, RS256), iss, aud, exp, iat, nonce; return the identity."""
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(id_token)
        except jwt.PyJWKClientConnectionError:
            raise GoogleAuthError("unavailable", "Google signing keys unavailable") from None
        except jwt.PyJWTError as exc:
            raise GoogleAuthError("invalid_token", f"no usable signing key: {type(exc).__name__}") from None
        try:
            claims = jwt.decode(
                id_token,
                key=getattr(signing_key, "key", signing_key),
                algorithms=["RS256"],
                audience=self.config.client_id,
                issuer=GOOGLE_ISSUERS,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "aud", "sub", "exp", "iat"]},
            )
        except jwt.ExpiredSignatureError:
            raise GoogleAuthError("invalid_token", "ID token expired") from None
        except jwt.PyJWTError as exc:
            raise GoogleAuthError("invalid_token", f"ID token rejected: {type(exc).__name__}") from None

        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
            raise GoogleAuthError("invalid_token", "ID token nonce mismatch")
        azp = claims.get("azp")
        if azp is not None and azp != self.config.client_id:
            raise GoogleAuthError("invalid_token", "ID token authorized party mismatch")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not _SUB_RE.match(sub):
            raise GoogleAuthError("invalid_token", "ID token has an invalid subject")
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email or len(email) > 254:
            raise GoogleAuthError("invalid_token", "ID token has no usable email")
        verified = claims.get("email_verified")
        return GoogleIdentity(
            sub=sub,
            email=email,
            email_verified=verified is True or verified == "true",
            name=str(claims.get("name") or "")[:200],
            hosted_domain=str(claims.get("hd") or "").lower()[:253],
        )
