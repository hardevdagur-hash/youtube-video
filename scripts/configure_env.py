# ruff: noqa: T201  (CLI: printing is its output)
"""Write production authentication settings into a .env file.

Usage:
    python scripts/configure_env.py --origin https://transcripts.example.com
    python scripts/configure_env.py --origin https://a.example.com --origin https://b.example.com \
        --admin-user alice --api-key-name ops-script

What it does:
  * prompts (hidden) for the admin password and stores only its scrypt hash in AUTH_USERS
  * generates JWT_SECRET_KEY if it is missing or insecure (``--rotate-jwt`` forces a new one)
  * optionally generates an admin API key; the raw key is shown once, only its SHA-256 is stored
  * sets APP_ENV and replaces CORS_ORIGINS (wildcards are rejected)
  * backs up the existing file, writes atomically, then validates the result with the same
    checks the server runs at startup

Every other line of the file (comments, YouTube / Groq keys, tuning values) is left untouched.
Secret values are never echoed except the newly generated API key, which the operator must keep.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security.web_auth import (  # noqa: E402
    _INSECURE_JWT_DEFAULTS,
    ROLE_ADMIN,
    AuthConfigError,
    AuthSettings,
    generate_api_key,
    hash_password,
)

MIN_PASSWORD_LENGTH = 12
MIN_JWT_SECRET_LENGTH = 32
_KEY_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_ORIGIN_RE = re.compile(r"^(https?)://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:\d{1,5})?$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


class ConfigureError(ValueError):
    """Invalid input for the configuration tool."""


# ---------------------------------------------------------------------------
# .env line handling (format-preserving)
# ---------------------------------------------------------------------------


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    # Strip an inline comment only when it is clearly separated from the value.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def get_value(lines: list[str], key: str) -> str | None:
    """Return the last assignment of ``key`` (dotenv semantics: last one wins)."""
    found: str | None = None
    for line in lines:
        match = _KEY_LINE_RE.match(line)
        if match and match.group(1) == key:
            found = _unquote(match.group(2))
    return found


def upsert(lines: list[str], key: str, value: str) -> list[str]:
    """Set ``key=value``: rewrite the first assignment, drop duplicates, or append."""
    if "\n" in value or "\r" in value:
        raise ConfigureError(f"{key} value must be a single line")
    result: list[str] = []
    written = False
    for line in lines:
        match = _KEY_LINE_RE.match(line)
        if match and match.group(1) == key:
            if not written:
                result.append(f"{key}={value}")
                written = True
            continue
        result.append(line)
    if not written:
        result.append(f"{key}={value}")
    return result


def _merge_entry(existing: str | None, name: str, entry: str) -> str:
    """Replace the comma-separated ``name:...`` entry, or append it."""
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    kept = [p for p in parts if p.split(":", 1)[0] != name]
    kept.append(entry)
    return ",".join(kept)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def normalize_origin(origin: str, *, production: bool) -> str:
    value = origin.strip().rstrip("/")
    if "*" in value:
        raise ConfigureError("CORS origins must be explicit; '*' is not allowed")
    match = _ORIGIN_RE.match(value)
    if not match:
        raise ConfigureError(
            f"Invalid origin {value[:100]!r}: expected scheme://host[:port] with no path"
        )
    scheme, host = match.group(1), match.group(2).lower()
    if production and scheme != "https":
        raise ConfigureError(f"Production origin {value[:100]!r} must use https")
    if not production and scheme != "https" and host not in _LOCAL_HOSTS:
        raise ConfigureError(
            f"Development origin {value[:100]!r} must be https or a localhost/127.0.0.1 address"
        )
    if match.group(3) and not 1 <= int(match.group(3)[1:]) <= 65535:
        raise ConfigureError(f"Invalid port in origin {value[:100]!r}")
    return f"{scheme}://{host}{match.group(3) or ''}"


def validate_password(password: str, username: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ConfigureError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.strip() != password:
        raise ConfigureError("Password must not start or end with whitespace")
    if password.lower() == username.lower():
        raise ConfigureError("Password must not equal the username")


# ---------------------------------------------------------------------------
# Core transformation (pure; unit-tested)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigureResult:
    lines: list[str]
    raw_api_key: str | None
    jwt_generated: bool
    changed_keys: tuple[str, ...]


def build_config(
    lines: list[str],
    *,
    admin_user: str,
    password: str,
    origins: list[str],
    app_env: str = "production",
    rotate_jwt: bool = False,
    api_key_name: str | None = None,
) -> ConfigureResult:
    if app_env not in ("production", "development"):
        raise ConfigureError("APP_ENV must be 'production' or 'development'")
    production = app_env == "production"
    if not _NAME_RE.match(admin_user):
        raise ConfigureError("Admin username may only contain letters, digits and _ . @ -")
    if api_key_name is not None and not _NAME_RE.match(api_key_name):
        raise ConfigureError("API key name may only contain letters, digits and _ . @ -")
    validate_password(password, admin_user)
    if production and not origins:
        raise ConfigureError("At least one --origin is required for production")
    normalized = list(dict.fromkeys(normalize_origin(o, production=production) for o in origins))

    changed: list[str] = ["APP_ENV", "AUTH_USERS", "CORS_ORIGINS"]
    out = upsert(lines, "APP_ENV", app_env)

    current_jwt = get_value(out, "JWT_SECRET_KEY") or ""
    jwt_generated = (
        rotate_jwt
        or current_jwt in _INSECURE_JWT_DEFAULTS
        or len(current_jwt) < MIN_JWT_SECRET_LENGTH
    )
    if jwt_generated:
        out = upsert(out, "JWT_SECRET_KEY", secrets.token_urlsafe(48))
        changed.append("JWT_SECRET_KEY")

    user_entry = f"{admin_user}:{ROLE_ADMIN}:{hash_password(password)}"
    out = upsert(out, "AUTH_USERS", _merge_entry(get_value(out, "AUTH_USERS"), admin_user, user_entry))

    raw_key: str | None = None
    if api_key_name:
        raw_key, digest = generate_api_key()
        key_entry = f"{api_key_name}:{ROLE_ADMIN}:{digest}"
        out = upsert(out, "API_KEYS", _merge_entry(get_value(out, "API_KEYS"), api_key_name, key_entry))
        changed.append("API_KEYS")

    out = upsert(out, "CORS_ORIGINS", ",".join(normalized))
    return ConfigureResult(out, raw_key, jwt_generated, tuple(sorted(changed)))


def env_mapping(lines: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for line in lines:
        match = _KEY_LINE_RE.match(line)
        if match:
            mapping[match.group(1)] = _unquote(match.group(2))
    return mapping


def verify(lines: list[str]) -> list[str]:
    """Run the server's own startup checks against the new configuration."""
    try:
        settings = AuthSettings.from_env(env_mapping(lines))
    except AuthConfigError as exc:
        return [str(exc)]
    return settings.problems()


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def read_env(path: Path) -> tuple[list[str], str, bool]:
    """Return ``(lines, newline, had_bom)``; a missing file yields no lines."""
    if not path.exists():
        return [], os.linesep, False
    raw = path.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(), newline, had_bom


def write_env_atomic(path: Path, lines: list[str], newline: str, had_bom: bool) -> Path | None:
    """Back up ``path`` (if present) and atomically replace it. Returns the backup path."""
    backup: Path | None = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.backup-{stamp}")
        shutil.copy2(path, backup)
    payload = newline.join(lines) + newline
    data = (b"\xef\xbb\xbf" if had_bom else b"") + payload.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError as cleanup_exc:
            print(f"warning: could not remove temp file {tmp_name}: {cleanup_exc}", file=sys.stderr)
        raise
    return backup


def _prompt_password(username: str, prompt: Callable[[str], str]) -> str:
    password = prompt(f"Password for admin user '{username}': ")
    validate_password(password, username)
    if prompt("Repeat password: ") != password:
        raise ConfigureError("Passwords do not match")
    return password


def main(argv: list[str] | None = None, prompt: Callable[[str], str] = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--origin", action="append", default=[], help="allowed browser origin (repeatable)")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--api-key-name", default=None, help="also create an admin API key with this name")
    parser.add_argument("--app-env", choices=("production", "development"), default="production")
    parser.add_argument("--rotate-jwt", action="store_true", help="replace an existing JWT_SECRET_KEY")
    args = parser.parse_args(argv)

    try:
        lines, newline, had_bom = read_env(args.env_file)
        password = _prompt_password(args.admin_user, prompt)
        result = build_config(
            lines,
            admin_user=args.admin_user,
            password=password,
            origins=args.origin,
            app_env=args.app_env,
            rotate_jwt=args.rotate_jwt,
            api_key_name=args.api_key_name,
        )
        problems = verify(result.lines)
        if problems:
            print("Not written; configuration would still be unsafe:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 2
        backup = write_env_atomic(args.env_file, result.lines, newline, had_bom)
    except ConfigureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not update {args.env_file}: {exc.strerror or exc}", file=sys.stderr)
        return 1

    print(f"Updated {args.env_file}: {', '.join(result.changed_keys)}")
    if backup:
        print(f"Previous file saved as {backup.name} (contains secrets; delete once verified)")
    if result.jwt_generated:
        print("A new JWT_SECRET_KEY was generated; existing browser sessions are invalidated.")
    if result.raw_api_key:
        print(f"\nAdmin API key '{args.api_key_name}' (shown once, store it securely):\n{result.raw_api_key}")
    print("Startup validation: OK. Restart the server to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
