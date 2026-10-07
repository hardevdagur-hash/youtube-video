"""Web authentication, authorization and abuse protection for the FastAPI app.

Request flow for every ``/api/*`` request (deny by default):

    Authentication (API key header or session cookie)
      -> Authorization (admin-only paths)
      -> CSRF origin check (cookie-authenticated unsafe methods)
      -> Rate limiting (per principal, stricter for cost-incurring routes)
      -> route handler (input validation via Pydantic / Query bounds)

Credentials are configured only through environment variables:

    APP_ENV              development | production (default: development)
    JWT_SECRET_KEY       >= 32 chars; required in production
    AUTH_USERS           comma list of  username:role:scrypt.N.r.p.salt.hash
    API_KEYS             comma list of  name:role:sha256hex
    SESSION_TTL_MINUTES  browser session lifetime (default 480)
    CORS_ORIGINS         comma list of allowed browser origins (default: none)
    TRUSTED_PROXIES      comma list of proxy IPs allowed to set X-Forwarded-For

Generate values with ``python scripts/hash_secret.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from infrastructure.rate_limiter import SlidingWindowRateLimiter
from security.jwt_service import JWTConfig, JWTService
from security.security_models import SecurityError

logger = logging.getLogger("security.web_auth")

ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_USER})

SESSION_COOKIE = "session"
API_KEY_HEADER = "x-api-key"

_INSECURE_JWT_DEFAULTS = frozenset({"", "change-me-in-production-32-chars!"})
_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
VALID_APP_ENVS = frozenset({"development", "production"})
# Production browser origins: https, host (or bracketed IPv6) and optional port; no path.
_HTTPS_ORIGIN_RE = re.compile(r"^https://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:\d{1,5})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# scrypt parameters for newly generated password hashes (~16 MB, ~50 ms).
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

# Paths reachable without credentials (method, path).
PUBLIC_ENDPOINTS = frozenset({
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
})

# Operational endpoints restricted to admins.
ADMIN_PATHS = frozenset({
    "/api/metrics",
    "/api/quota",
    "/api/cache/stats",
    "/api/active-exports",
    "/api/transcript/metrics",
    "/api/transcript/limiter/status",
    "/api/transcript-limiter/status",
})
ADMIN_PREFIXES = ("/api/export/jobs/",)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


class AuthConfigError(RuntimeError):
    """Raised when authentication configuration is unsafe for the environment."""


# ---------------------------------------------------------------------------
# Credential hashing
# ---------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """Return ``scrypt.N.r.p.salt.hash`` (dot-separated so it is safe in .env files)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM, dklen=32,
    )
    return f"scrypt.{SCRYPT_N}.{SCRYPT_R}.{SCRYPT_P}.{_b64e(salt)}.{_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split(".")
        if scheme != "scrypt":
            return False
        expected_bytes = _b64d(expected)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=_b64d(salt), n=int(n), r=int(r), p=int(p),
            maxmem=_SCRYPT_MAXMEM, dklen=len(expected_bytes),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected_bytes)


def generate_api_key() -> tuple[str, str]:
    """Return ``(raw_key, sha256_hex)``. Only the hash is stored in configuration."""
    raw = "ysk_" + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Verified against unknown usernames so login timing does not reveal valid accounts.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    auth_method: str  # "api_key" | "session"

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


@dataclass
class AuthSettings:
    app_env: str = "development"
    jwt_secret: str = ""
    jwt_secret_ephemeral: bool = False
    users: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (role, hash)
    api_keys: list[tuple[str, str, str]] = field(default_factory=list)  # (name, role, sha256)
    session_ttl_minutes: int = 480
    cors_origins: list[str] = field(default_factory=list)
    trusted_proxies: frozenset[str] = frozenset()
    api_rate_per_minute: int = 300
    costly_rate_per_minute: int = 20
    login_rate_per_minute: int = 5
    login_max_failures: int = 10
    login_lockout_seconds: int = 900

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def trusted_origins(self) -> frozenset[str]:
        origins = {o for o in self.cors_origins if o != "*"}
        if not self.is_production:
            origins.update(_DEV_ORIGINS)
        return frozenset(origins)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AuthSettings:
        env = dict(os.environ) if env is None else env
        app_env = env.get("APP_ENV", "development").strip().lower() or "development"
        if app_env not in VALID_APP_ENVS:
            # Fail closed: a typo such as "prod" must not silently get development rules.
            raise AuthConfigError(
                f"APP_ENV must be one of {sorted(VALID_APP_ENVS)}, got {app_env[:32]!r}"
            )

        secret = env.get("JWT_SECRET_KEY", "").strip()
        ephemeral = False
        if secret in _INSECURE_JWT_DEFAULTS and app_env != "production":
            # Development convenience: sessions simply reset on restart.
            secret = secrets.token_urlsafe(48)
            ephemeral = True

        return cls(
            app_env=app_env,
            jwt_secret=secret,
            jwt_secret_ephemeral=ephemeral,
            users=_parse_users(env.get("AUTH_USERS", "")),
            api_keys=_parse_api_keys(env.get("API_KEYS", "")),
            session_ttl_minutes=_int(env, "SESSION_TTL_MINUTES", 480, 5, 7 * 24 * 60),
            cors_origins=_split(env.get("CORS_ORIGINS", "")),
            trusted_proxies=frozenset(_split(env.get("TRUSTED_PROXIES", ""))),
            api_rate_per_minute=_int(env, "API_RATE_LIMIT_PER_MINUTE", 300, 1, 100_000),
            costly_rate_per_minute=_int(env, "COSTLY_RATE_LIMIT_PER_MINUTE", 20, 1, 10_000),
            login_rate_per_minute=_int(env, "LOGIN_RATE_LIMIT_PER_MINUTE", 5, 1, 1_000),
        )

    def problems(self) -> list[str]:
        """Configuration problems that make the deployment unsafe."""
        issues: list[str] = []
        if self.is_production:
            if self.jwt_secret in _INSECURE_JWT_DEFAULTS or len(self.jwt_secret) < 32:
                issues.append("JWT_SECRET_KEY must be set to a random value of at least 32 characters")
            if "*" in self.cors_origins:
                issues.append("CORS_ORIGINS must list explicit origins, not '*'")
            insecure = [o for o in self.cors_origins if o != "*" and not _valid_https_origin(o)]
            if insecure:
                shown = ", ".join(repr(o[:100]) for o in insecure[:5])
                issues.append(
                    f"CORS_ORIGINS must contain only https://host[:port] origins in production: {shown}"
                )
        if not self.users and not self.api_keys:
            issues.append("no credentials configured: set AUTH_USERS and/or API_KEYS")
        return issues

    def validate(self) -> None:
        """Fail fast in production; warn loudly in development."""
        issues = self.problems()
        if self.is_production and issues:
            raise AuthConfigError("Refusing to start: " + "; ".join(issues))
        for issue in issues:
            logger.warning("Auth configuration: %s", issue)
        if self.jwt_secret_ephemeral:
            logger.warning("JWT_SECRET_KEY not set; using an ephemeral development secret")
        if "*" in self.cors_origins:
            logger.warning("CORS_ORIGINS='*' is only tolerated in development")


def _valid_https_origin(origin: str) -> bool:
    match = _HTTPS_ORIGIN_RE.match(origin)
    if not match:
        return False
    port = match.group(2)
    return port is None or 1 <= int(port[1:]) <= 65535


def _split(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def _int(env: dict[str, str], key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(env.get(key, default))
    except (TypeError, ValueError):
        raise AuthConfigError(f"{key} must be an integer") from None
    if not lo <= value <= hi:
        raise AuthConfigError(f"{key} must be between {lo} and {hi}")
    return value


def _parse_users(raw: str) -> dict[str, tuple[str, str]]:
    users: dict[str, tuple[str, str]] = {}
    for entry in _split(raw):
        parts = entry.split(":")
        if len(parts) != 3:
            raise AuthConfigError("AUTH_USERS entries must be username:role:hash")
        name, role, pw_hash = parts
        if not _NAME_RE.match(name) or role not in VALID_ROLES or not pw_hash.startswith("scrypt."):
            raise AuthConfigError(f"Invalid AUTH_USERS entry for user '{name[:64]}'")
        users[name] = (role, pw_hash)
    return users


def _parse_api_keys(raw: str) -> list[tuple[str, str, str]]:
    keys: list[tuple[str, str, str]] = []
    for entry in _split(raw):
        parts = entry.split(":")
        if len(parts) != 3:
            raise AuthConfigError("API_KEYS entries must be name:role:sha256hex")
        name, role, digest = parts
        if not _NAME_RE.match(name) or role not in VALID_ROLES or not _SHA256_RE.match(digest):
            raise AuthConfigError(f"Invalid API_KEYS entry '{name[:64]}'")
        keys.append((name, role, digest))
    return keys


# ---------------------------------------------------------------------------
# Authenticator
# ---------------------------------------------------------------------------


class WebAuthenticator:
    """Verifies credentials and issues session tokens."""

    def __init__(self, config: AuthSettings) -> None:
        self.configure(config)

    def configure(self, config: AuthSettings) -> None:
        """(Re)build credential stores, token service and limiters from ``config``."""
        self.config = config
        self._jwt = JWTService(JWTConfig(
            secret_key=config.jwt_secret,
            access_token_expire_minutes=config.session_ttl_minutes,
            issuer="youtube-export",
            audience="youtube-export-web",
        ))
        self.api_limiter = SlidingWindowRateLimiter(config.api_rate_per_minute, 60.0)
        self.costly_limiter = SlidingWindowRateLimiter(config.costly_rate_per_minute, 60.0)
        self.login_limiter = SlidingWindowRateLimiter(config.login_rate_per_minute, 60.0)
        self.login_failures = SlidingWindowRateLimiter(
            config.login_max_failures, float(config.login_lockout_seconds),
        )

    # -- API keys -----------------------------------------------------------

    def authenticate_api_key(self, raw_key: str) -> Principal | None:
        if not raw_key or len(raw_key) > 256:
            return None
        candidate = hash_api_key(raw_key)
        match: Principal | None = None
        for name, role, digest in self.config.api_keys:  # no early exit: constant work
            if hmac.compare_digest(candidate, digest):
                match = Principal(subject=f"key:{name}", role=role, auth_method="api_key")
        return match

    # -- Passwords and sessions --------------------------------------------

    def is_locked_out(self, username: str) -> bool:
        return self.login_failures.remaining(f"user:{username.lower()}") == 0

    def authenticate_password(self, username: str, password: str) -> Principal | None:
        record = self.config.users.get(username)
        encoded = record[1] if record else _DUMMY_PASSWORD_HASH
        valid = verify_password(password, encoded) and record is not None
        if not valid:
            self.login_failures.allow(f"user:{username.lower()}")
            return None
        return Principal(subject=username, role=record[0], auth_method="session")

    def issue_session(self, principal: Principal) -> str:
        user = SimpleNamespace(id=principal.subject, email="", role=principal.role, organization_id="")
        return self._jwt.create_access_token(user)

    def authenticate_session(self, token: str) -> Principal | None:
        if not token or len(token) > 4096:
            return None
        try:
            claims = self._jwt.verify_token(token, expected_type="access")
        except SecurityError:
            return None
        except Exception:  # malformed tokens from any JWT backend
            return None
        subject = claims.get("sub")
        record = self.config.users.get(subject) if isinstance(subject, str) else None
        if record is None:  # user removed from configuration => session revoked
            return None
        return Principal(subject=subject, role=record[0], auth_method="session")

    def authenticate_request(self, request: Request) -> Principal | None:
        api_key = request.headers.get(API_KEY_HEADER)
        if api_key:
            return self.authenticate_api_key(api_key)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            return self.authenticate_session(token)
        return None

    # -- Request helpers ----------------------------------------------------

    def client_ip(self, request: Request) -> str:
        peer = getattr(request.client, "host", None) or "unknown"
        if peer in self.config.trusted_proxies:
            forwarded = request.headers.get("x-forwarded-for", "")
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            # Walk from the right, skipping our own proxies.
            for hop in reversed(hops):
                if hop not in self.config.trusted_proxies:
                    return hop
        return peer

    def origin_allowed(self, request: Request) -> bool:
        """CSRF defence: if the browser reports an origin it must be trusted."""
        origin = request.headers.get("origin")
        if not origin:
            referer = request.headers.get("referer")
            if not referer:
                return True  # non-browser client; SameSite=Strict covers browsers
            parts = urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}"
        if origin == "null":
            return False
        if origin in self.config.trusted_origins:
            return True
        host = request.headers.get("host", "")
        return bool(host) and urlsplit(origin).netloc == host


# ---------------------------------------------------------------------------
# Route classification
# ---------------------------------------------------------------------------

_TRANSCRIPT_NON_COSTLY = ("metrics", "limiter", "jobs")


def is_admin_path(path: str) -> bool:
    return path in ADMIN_PATHS or path.startswith(ADMIN_PREFIXES)


def is_costly(method: str, path: str) -> bool:
    """Routes that spend YouTube quota, Groq/Whisper credits or heavy CPU."""
    if method == "POST" and path in ("/api/export", "/api/transcript", "/api/transcript/export",
                                     "/api/process-transcript"):
        return True
    if path.startswith("/api/channel/"):
        return True
    if path.startswith("/api/transcriptv2/"):
        return True
    if method == "POST" and path.startswith("/api/transcript/jobs/") and path.endswith("/resume"):
        return True
    if path.startswith("/api/transcript/"):
        segment = path.split("/")[3] if path.count("/") >= 3 else ""
        return segment not in _TRANSCRIPT_NON_COSTLY
    return False


def _deny(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "error": message, "error_code": code},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """Deny-by-default authentication, authorization and rate limiting for /api/*."""

    def __init__(self, app, authenticator: WebAuthenticator) -> None:
        super().__init__(app)
        self.auth = authenticator

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        method = request.method.upper()
        request.state.principal = None

        if not (path == "/api" or path.startswith("/api/")) or method == "OPTIONS":
            return await call_next(request)

        principal = self.auth.authenticate_request(request)
        request.state.principal = principal

        if principal is None and (method, path) not in PUBLIC_ENDPOINTS:
            return _deny(401, "UNAUTHENTICATED", "Authentication required.")

        if principal is not None:
            if principal.auth_method == "session" and method in _UNSAFE_METHODS \
                    and not self.auth.origin_allowed(request):
                return _deny(403, "CSRF_REJECTED", "Cross-site request rejected.")
            if is_admin_path(path) and not principal.is_admin:
                return _deny(403, "FORBIDDEN", "Administrator access required.")

            limiter = self.auth.costly_limiter if is_costly(method, path) else self.auth.api_limiter
            if not limiter.allow(principal.subject):
                return _deny(429, "RATE_LIMITED", "Too many requests.", {"Retry-After": "60"})

        return await call_next(request)


def cors_options(config: AuthSettings) -> dict:
    """CORSMiddleware options: explicit origins only; '*' (dev only) never with credentials."""
    wildcard = "*" in config.cors_origins
    origins = [o for o in config.cors_origins if o != "*"]
    return {
        "allow_origins": ["*"] if wildcard else origins,
        "allow_credentials": not wildcard and bool(origins),
        "allow_methods": ["GET", "POST", "DELETE"],
        "allow_headers": ["Content-Type", "X-API-Key"],
    }


def can_access_owned(principal: Principal | None, owner: str | None) -> bool:
    """Ownership rule for jobs: admins see everything; users only their own jobs."""
    if principal is None:
        return False
    if principal.is_admin:
        return True
    return owner is not None and owner == principal.subject
