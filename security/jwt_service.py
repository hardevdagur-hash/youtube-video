"""HS256 session tokens (PyJWT).

Used by ``security.web_auth`` for the browser session cookie. Tokens carry the
subject, role, type, issue/expiry times, issuer and audience; verification enforces
signature, expiry, issuer, audience and the expected token type.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt.exceptions import ExpiredSignatureError, PyJWTError

from security.security_models import InvalidTokenError, TokenExpiredError

ALGORITHM = "HS256"
_MIN_SECRET_LENGTH = 32


def _secret_from_env() -> str:
    return os.getenv("JWT_SECRET_KEY", "")


@dataclass
class JWTConfig:
    secret_key: str = field(default_factory=_secret_from_env)
    algorithm: str = ALGORITHM
    access_token_expire_minutes: int = field(
        default_factory=lambda: int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
    )
    issuer: str = "youtube-export"
    audience: str = "youtube-export-web"


class JWTService:
    def __init__(self, config: JWTConfig | None = None) -> None:
        self._config = config or JWTConfig()
        if len(self._config.secret_key) < _MIN_SECRET_LENGTH:
            raise ValueError(f"JWT secret must be at least {_MIN_SECRET_LENGTH} characters")
        if self._config.algorithm != ALGORITHM:
            raise ValueError(f"Only {ALGORITHM} tokens are supported")

    def create_access_token(self, user: Any, extra_claims: dict[str, Any] | None = None) -> str:
        """Sign an access token for ``user`` (any object with ``id``/``uuid`` and ``role``)."""
        now = datetime.now(UTC)
        role = getattr(user, "role", "user")
        payload: dict[str, Any] = {
            "sub": str(getattr(user, "uuid", None) or getattr(user, "id", "")),
            "role": getattr(role, "value", role),
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=self._config.access_token_expire_minutes),
            "iss": self._config.issuer,
            "aud": self._config.audience,
        }
        if extra_claims:
            payload.update(extra_claims)
        return jwt.encode(payload, self._config.secret_key, algorithm=self._config.algorithm)

    def verify_token(self, token: str, expected_type: str = "access") -> dict[str, Any]:
        """Return the claims of a valid token; raise TokenExpiredError / InvalidTokenError otherwise."""
        try:
            payload = jwt.decode(
                token,
                self._config.secret_key,
                algorithms=[self._config.algorithm],
                audience=self._config.audience,
                issuer=self._config.issuer,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except ExpiredSignatureError:
            raise TokenExpiredError("Token has expired") from None
        except PyJWTError:
            raise InvalidTokenError("Invalid token") from None
        if payload.get("type") != expected_type:
            raise InvalidTokenError("Invalid token type")
        return payload
