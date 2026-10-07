"""Tests for scripts/configure_env.py (production .env auth configuration)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from security.web_auth import AuthSettings, hash_api_key, verify_password

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "configure_env.py"
_spec = importlib.util.spec_from_file_location("configure_env", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
configure_env = importlib.util.module_from_spec(_spec)
sys.modules["configure_env"] = configure_env
_spec.loader.exec_module(configure_env)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105 - test fixture
EXISTING = [
    "# YouTube settings",
    "YOUTUBE_API_KEY=AIzaPLACEHOLDERPLACEHOLDERPLACEHOLDER00",
    "CORS_ORIGINS=*",
    "LOG_LEVEL=INFO",
    "",
]


def _build(lines=None, **overrides):
    kwargs = {
        "admin_user": "admin",
        "password": PASSWORD,
        "origins": ["https://transcripts.example.com"],
    }
    kwargs.update(overrides)
    return configure_env.build_config(list(EXISTING if lines is None else lines), **kwargs)


class TestBuildConfig:
    def test_produces_config_that_passes_production_startup_checks(self):
        result = _build()
        settings = AuthSettings.from_env(configure_env.env_mapping(result.lines))
        assert settings.is_production
        assert settings.problems() == []
        assert configure_env.verify(result.lines) == []

    def test_unrelated_lines_are_preserved_verbatim_and_in_order(self):
        result = _build()
        kept = [line for line in result.lines if line in EXISTING]
        assert kept == [line for line in EXISTING if not line.startswith("CORS_ORIGINS")]

    def test_wildcard_cors_is_replaced_with_explicit_origin(self):
        result = _build()
        assert configure_env.get_value(result.lines, "CORS_ORIGINS") == "https://transcripts.example.com"
        assert sum(1 for line in result.lines if line.startswith("CORS_ORIGINS=")) == 1

    def test_password_stored_only_as_verifiable_scrypt_hash(self):
        result = _build()
        assert all(PASSWORD not in line for line in result.lines)
        name, role, encoded = configure_env.get_value(result.lines, "AUTH_USERS").split(":")
        assert (name, role) == ("admin", "admin")
        assert verify_password(PASSWORD, encoded)

    def test_jwt_secret_generated_when_missing(self):
        result = _build()
        secret = configure_env.get_value(result.lines, "JWT_SECRET_KEY")
        assert result.jwt_generated
        assert len(secret) >= 32

    def test_strong_existing_jwt_secret_is_kept(self):
        strong = "s" * 64
        result = _build([*EXISTING, f"JWT_SECRET_KEY={strong}"])
        assert not result.jwt_generated
        assert configure_env.get_value(result.lines, "JWT_SECRET_KEY") == strong

    @pytest.mark.parametrize("weak", ["", "short", "change-me-in-production-32-chars!"])
    def test_weak_existing_jwt_secret_is_replaced(self, weak):
        result = _build([*EXISTING, f"JWT_SECRET_KEY={weak}"])
        assert result.jwt_generated
        assert configure_env.get_value(result.lines, "JWT_SECRET_KEY") != weak

    def test_rotate_jwt_replaces_strong_secret(self):
        strong = "s" * 64
        result = _build([*EXISTING, f"JWT_SECRET_KEY={strong}"], rotate_jwt=True)
        assert configure_env.get_value(result.lines, "JWT_SECRET_KEY") != strong

    def test_api_key_raw_value_shown_once_and_only_hash_stored(self):
        result = _build(api_key_name="ops")
        assert result.raw_api_key is not None
        assert result.raw_api_key.startswith("ysk_")
        assert all(result.raw_api_key not in line for line in result.lines)
        assert configure_env.get_value(result.lines, "API_KEYS") == f"ops:admin:{hash_api_key(result.raw_api_key)}"

    def test_no_api_key_unless_requested(self):
        result = _build()
        assert result.raw_api_key is None
        assert configure_env.get_value(result.lines, "API_KEYS") is None

    def test_other_users_and_keys_are_kept_and_same_name_replaced(self):
        other_user = "bob:user:scrypt.16384.8.1.c2FsdA.aGFzaA"
        other_key = "ci:user:" + "a" * 64
        lines = [*EXISTING, f"AUTH_USERS={other_user},admin:admin:scrypt.1.1.1.old.old", f"API_KEYS={other_key}"]
        result = _build(lines, api_key_name="ops")
        users = configure_env.get_value(result.lines, "AUTH_USERS").split(",")
        assert len(users) == 2
        assert users[0] == other_user
        assert "old" not in users[1]
        keys = configure_env.get_value(result.lines, "API_KEYS").split(",")
        assert keys[0] == other_key
        assert keys[1].startswith("ops:admin:")

    def test_duplicate_assignments_collapse_to_one(self):
        result = _build([*EXISTING, "CORS_ORIGINS=http://x", "export APP_ENV=development"])
        assert sum(1 for line in result.lines if "CORS_ORIGINS=" in line) == 1
        assert configure_env.get_value(result.lines, "APP_ENV") == "production"

    def test_origins_are_normalized_and_deduplicated(self):
        result = _build(origins=["https://A.example.com/", "https://a.example.com", "https://b.example.com:8443"])
        assert configure_env.get_value(result.lines, "CORS_ORIGINS") == (
            "https://a.example.com,https://b.example.com:8443"
        )


class TestValidation:
    @pytest.mark.parametrize(
        "origin",
        ["*", "https://*.example.com", "https://example.com/path", "example.com", "ftp://example.com",
         "https://example.com:99999", "javascript:alert(1)"],
    )
    def test_bad_origins_rejected(self, origin):
        with pytest.raises(configure_env.ConfigureError):
            _build(origins=[origin])

    @pytest.mark.parametrize(
        "origin", ["http://transcripts.example.com", "http://localhost:5173", "http://127.0.0.1:8000"]
    )
    def test_production_rejects_every_http_origin(self, origin):
        with pytest.raises(configure_env.ConfigureError, match="https"):
            _build(origins=[origin])

    def test_production_accepts_real_https_origin(self):
        result = _build(origins=["https://real-domain.example"])
        assert configure_env.verify(result.lines) == []

    @pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
    def test_development_allows_http_localhost(self, origin):
        result = _build(origins=[origin], app_env="development")
        assert configure_env.get_value(result.lines, "APP_ENV") == "development"
        assert configure_env.get_value(result.lines, "CORS_ORIGINS") == origin
        assert configure_env.verify(result.lines) == []

    def test_development_rejects_http_non_local_host(self):
        with pytest.raises(configure_env.ConfigureError, match="localhost"):
            _build(origins=["http://dev.internal:5173"], app_env="development")

    def test_production_requires_an_origin(self):
        with pytest.raises(configure_env.ConfigureError, match="origin"):
            _build(origins=[])

    @pytest.mark.parametrize("password", ["short", "eleven-char", " leading-space-pass", "trailing-space-pass "])
    def test_weak_passwords_rejected(self, password):
        with pytest.raises(configure_env.ConfigureError):
            _build(password=password)

    def test_password_equal_to_username_rejected(self):
        with pytest.raises(configure_env.ConfigureError, match="username"):
            _build(admin_user="administrator1", password="Administrator1")  # noqa: S106 - test fixture

    @pytest.mark.parametrize("name", ["bad name", "a:b", "x" * 65, ""])
    def test_invalid_names_rejected(self, name):
        with pytest.raises(configure_env.ConfigureError):
            _build(admin_user=name)
        with pytest.raises(configure_env.ConfigureError):
            _build(api_key_name=name or " ")

    def test_multiline_value_rejected(self):
        with pytest.raises(configure_env.ConfigureError):
            configure_env.upsert([], "X", "a\nb")


class TestMain:
    def _prompt(self, *answers):
        replies = iter(answers)
        return lambda _msg: next(replies)

    def test_writes_file_with_backup_and_preserves_crlf_and_bom(self, tmp_path, capsys):
        env = tmp_path / ".env"
        env.write_bytes(b"\xef\xbb\xbf" + "\r\n".join(EXISTING).encode() + b"\r\n")
        code = configure_env.main(
            ["--env-file", str(env), "--origin", "https://t.example.com", "--api-key-name", "ops"],
            prompt=self._prompt(PASSWORD, PASSWORD),
        )
        assert code == 0
        raw = env.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
        backups = list(tmp_path.glob(".env.backup-*"))
        assert len(backups) == 1
        assert b"CORS_ORIGINS=*" in backups[0].read_bytes()
        lines, _, _ = configure_env.read_env(env)
        assert configure_env.verify(lines) == []

        out = capsys.readouterr().out
        secret = configure_env.get_value(lines, "JWT_SECRET_KEY")
        assert PASSWORD not in out
        assert secret not in out
        assert configure_env.get_value(lines, "AUTH_USERS").split(":")[2] not in out
        assert "ysk_" in out  # the one value the operator must keep
        assert len(list(tmp_path.iterdir())) == 2  # .env + one backup, no leftover temp file

    def test_creates_missing_file_without_backup(self, tmp_path):
        env = tmp_path / ".env"
        code = configure_env.main(
            ["--env-file", str(env), "--origin", "https://t.example.com"],
            prompt=self._prompt(PASSWORD, PASSWORD),
        )
        assert code == 0
        assert env.exists()
        assert not list(tmp_path.glob(".env.backup-*"))

    def test_mismatched_passwords_leave_file_untouched(self, tmp_path, capsys):
        env = tmp_path / ".env"
        env.write_text("\n".join(EXISTING), encoding="utf-8")
        before = env.read_bytes()
        code = configure_env.main(
            ["--env-file", str(env), "--origin", "https://t.example.com"],
            prompt=self._prompt(PASSWORD, PASSWORD + "x"),
        )
        assert code == 1
        assert env.read_bytes() == before
        assert "do not match" in capsys.readouterr().err
        assert not list(tmp_path.glob(".env.backup-*"))

    def test_invalid_origin_leaves_file_untouched(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("\n".join(EXISTING), encoding="utf-8")
        before = env.read_bytes()
        code = configure_env.main(
            ["--env-file", str(env), "--origin", "*"], prompt=self._prompt(PASSWORD, PASSWORD)
        )
        assert code == 1
        assert env.read_bytes() == before

    def test_existing_invalid_auth_entry_blocks_write(self, tmp_path, capsys):
        env = tmp_path / ".env"
        env.write_text("\n".join([*EXISTING, "API_KEYS=broken-entry"]), encoding="utf-8")
        before = env.read_bytes()
        code = configure_env.main(
            ["--env-file", str(env), "--origin", "https://t.example.com"],
            prompt=self._prompt(PASSWORD, PASSWORD),
        )
        assert code == 2
        assert env.read_bytes() == before
        assert "API_KEYS" in capsys.readouterr().err
