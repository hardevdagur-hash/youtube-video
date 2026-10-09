"""Local accounts for users who sign in with Google.

Password users and API keys are configured in the environment (``AUTH_USERS``,
``API_KEYS``); Google users sign themselves up, so their mapping must persist. It
uses the application's existing persistence: one JSON file in ``DATA_DIR`` written
atomically (write + rename), included in ``scripts/backup.sh`` archives.

    DATA_DIR/users/google_users.json
    {"version": 1, "users": {"<google sub>": {
        "user_id": "google:<sub>", "email": ..., "display_name": ...,
        "auth_provider": "google", "created_at": ..., "last_login_at": ...,
        "account_domain": ..., "hosted_domain": "<hd claim or empty>",
        "disabled": false}}}

Keyed by Google's stable ``sub``, never by email. Roles are NOT stored: every Google
user is ``user`` unless their ``sub`` is listed in ``GOOGLE_ADMIN_SUBJECTS`` (server
configuration), so admin rights can never come from this file or the browser.
Operators can block an account by setting ``"disabled": true`` and restarting.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger("security.google_users")

_SUB_RE = re.compile(r"^[0-9A-Za-z_-]{1,255}$")
_FILE_NAME = "google_users.json"
_VERSION = 1


class GoogleUserStoreError(RuntimeError):
    """The account file cannot be read or written; Google sign-in must fail closed."""


@dataclass(frozen=True)
class GoogleUser:
    user_id: str
    email: str
    display_name: str
    created_at: str
    last_login_at: str
    disabled: bool = False
    auth_provider: str = "google"
    account_domain: str = ""  # Workspace "hd", else the email domain (as verified at sign-in)
    # The verified "hd" claim at the last sign-in; empty for personal accounts. Records written
    # before this field existed load as "" and are therefore treated as personal accounts.
    hosted_domain: str = ""

    @property
    def sub(self) -> str:
        return self.user_id.split(":", 1)[1]

    @property
    def email_domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower() if "@" in self.email else ""


class GoogleUserStore:
    """Thread-safe, file-backed map of Google ``sub`` -> local account (single instance)."""

    def __init__(self, directory: Path) -> None:
        self._path = Path(directory) / _FILE_NAME
        self._lock = Lock()
        self._users: dict[str, GoogleUser] = {}
        self._load_error: str | None = None
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            users = raw["users"]
            if not isinstance(users, dict):
                raise TypeError("users must be an object")
            loaded = {}
            for sub, record in users.items():
                if not _SUB_RE.match(sub) or not isinstance(record, dict):
                    raise ValueError("invalid account entry")
                loaded[sub] = GoogleUser(
                    user_id=f"google:{sub}",
                    email=str(record.get("email", ""))[:254],
                    display_name=str(record.get("display_name", ""))[:200],
                    created_at=str(record.get("created_at", "")),
                    last_login_at=str(record.get("last_login_at", "")),
                    disabled=record.get("disabled") is True,
                    account_domain=str(record.get("account_domain", "")).lower()[:253],
                    hosted_domain=str(record.get("hosted_domain", "")).lower()[:253],
                )
            self._users = loaded
            logger.info("Loaded %d Google account(s)", len(loaded))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Never overwrite a file we could not understand: that would delete accounts.
            self._load_error = f"{type(exc).__name__}: {exc}"
            logger.error("Google account file %s is unreadable (%s); Google sign-in is disabled until it is fixed",
                         self._path.name, self._load_error)

    @property
    def healthy(self) -> bool:
        return self._load_error is None

    def get(self, sub: str) -> GoogleUser | None:
        if self._load_error is not None:
            return None
        with self._lock:
            return self._users.get(sub)

    def __len__(self) -> int:
        with self._lock:
            return len(self._users)

    def record_login(
        self, sub: str, email: str, display_name: str, account_domain: str = "", hosted_domain: str = "",
    ) -> tuple[GoogleUser, bool]:
        """Create the account on first sign-in or refresh its profile; returns ``(user, created)``."""
        if self._load_error is not None:
            raise GoogleUserStoreError(f"account file unreadable: {self._load_error}")
        if not _SUB_RE.match(sub):
            raise GoogleUserStoreError("invalid Google subject")
        now = datetime.now(UTC).isoformat()
        with self._lock:
            existing = self._users.get(sub)
            created = existing is None
            user = GoogleUser(
                user_id=f"google:{sub}",
                email=email[:254],
                display_name=display_name[:200],
                created_at=existing.created_at if existing else now,
                last_login_at=now,
                disabled=existing.disabled if existing else False,
                account_domain=account_domain.lower()[:253],
                hosted_domain=hosted_domain.lower()[:253],
            )
            updated = {**self._users, sub: user}
            self._write(updated)
            self._users = updated
        return user, created

    def _write(self, users: dict[str, GoogleUser]) -> None:
        payload = {
            "version": _VERSION,
            "users": {
                sub: {k: v for k, v in asdict(u).items() if k != "user_id"} | {"user_id": u.user_id}
                for sub, u in users.items()
            },
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".google_users.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self._path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise GoogleUserStoreError(f"could not save Google accounts: {exc}") from exc
