"""Deployment script and compose guards.

Static checks pin the production-safety properties of the deployment files (image tags,
nginx config propagation, TLS renewal); the bash checks run ``scripts/lib/common.sh``
itself (skipped where no working bash exists, e.g. Windows without WSL; CI runs them).
The full behaviour (deploy, restore, rollback, reload) is exercised against real
containers by ``tests/deployment/stack_test.sh`` in CI.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "lib" / "common.sh"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Image tags: production recovery never starts an unrelated :latest image
# ---------------------------------------------------------------------------


def test_compose_defaults_to_the_deployed_image_not_latest():
    compose = _read("docker-compose.yml")
    assert "image: transcript-app:${IMAGE_TAG:-current}" in compose
    assert ":-latest" not in compose


@pytest.mark.parametrize("script", ["scripts/deploy.sh", "scripts/rollback.sh"])
def test_deploy_and_rollback_promote_the_started_image(script):
    assert "promote_image" in _read(script)


def test_promote_image_tags_current_and_records_versions():
    common = _read("scripts/lib/common.sh")
    body = common.split("promote_image() {", 1)[1].split("\n}\n", 1)[0]
    assert 'docker tag "transcript-app:${tag}" "transcript-app:${CURRENT_TAG}"' in body
    assert '"$STATE_DIR/current"' in body and '"$STATE_DIR/previous"' in body
    assert 'CURRENT_TAG="current"' in common


def test_restore_starts_the_recorded_deployed_version():
    restore = _read("scripts/restore.sh")
    assert 'tag="$(deployed_tag)"' in restore
    assert 'IMAGE_TAG="$tag" docker compose up -d app' in restore
    # The old behaviour (whatever image the stopped container had, or a bare up) is gone.
    assert "{{.Config.Image}}" not in restore
    assert re.search(r"^docker compose up -d app\s*$", restore, re.MULTILINE) is None


@pytest.mark.parametrize("rel", ["scripts", "docs/OPERATIONS.md", "README.md", "docker-compose.yml"])
def test_no_latest_image_in_deployment_paths(rel):
    paths = sorted((ROOT / rel).rglob("*.sh")) if (ROOT / rel).is_dir() else [ROOT / rel]
    for path in paths:
        assert "transcript-app:latest" not in path.read_text(encoding="utf-8"), path


def test_deploy_pulls_fresh_base_images():
    assert "docker compose build --pull app" in _read("scripts/deploy.sh")


# ---------------------------------------------------------------------------
# nginx: configuration edits reach the running container
# ---------------------------------------------------------------------------


def test_nginx_config_is_directory_mounted_and_loaded_explicitly():
    compose = _read("docker-compose.yml")
    assert "./docker/nginx:/etc/nginx/site:ro" in compose
    assert "./docker/nginx/nginx.conf:/etc/nginx/nginx.conf" not in compose  # single-file mount
    assert '"-c", "/etc/nginx/site/nginx.conf"' in compose
    assert 'NGINX_CONF="/etc/nginx/site/nginx.conf"' in _read("scripts/lib/common.sh")


def test_deploy_reloads_nginx_before_the_health_gate():
    deploy = _read("scripts/deploy.sh")
    assert "if reload_nginx && wait_healthy; then" in deploy


def test_nginx_keeps_security_headers_and_adds_noindex():
    conf = _read("docker/nginx/nginx.conf")
    for header in ("Strict-Transport-Security", "Content-Security-Policy", "Permissions-Policy"):
        assert f"add_header {header}" in conf
    assert 'add_header X-Robots-Tag "noindex, nofollow" always;' in conf
    assert "proxy_hide_header X-Robots-Tag;" in conf


def test_oauth_callback_location_never_logs_the_code():
    conf = _read("docker/nginx/nginx.conf")
    callback = conf.split("location = /api/auth/google/callback {", 1)[1].split("}", 1)[0]
    assert "access_log /dev/stdout no_query;" in callback
    assert "error_log /dev/stderr crit;" in callback
    no_query = conf.split("log_format no_query", 1)[1].split(";", 1)[0]
    assert "$request " not in no_query and "$request_uri" not in no_query and "$args" not in no_query


# ---------------------------------------------------------------------------
# TLS renewal works for a non-root cron user
# ---------------------------------------------------------------------------


def test_letsencrypt_files_are_copied_inside_a_container():
    tls = _read("scripts/init-tls.sh")
    install = tls.split("install_letsencrypt_cert() {", 1)[1].split("\n}\n", 1)[0]
    assert "docker run --rm" in install and "/etc/letsencrypt:ro" in install
    assert 'chown "$2:$3"' in install
    # Renewal failures must surface (cron MAILTO) instead of being swallowed.
    assert "certbot renew failed" in tls


# ---------------------------------------------------------------------------
# Production authentication preflight (scripts/lib/common.sh), run with bash
# ---------------------------------------------------------------------------


@cache
def _bash() -> str | None:
    bash = shutil.which("bash")
    if not bash:
        return None
    try:
        probe = subprocess.run(  # noqa: S603 (fixed argv)
            [bash, "-c", "echo ok"], capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bash if probe.returncode == 0 and probe.stdout.strip() == "ok" else None


def _auth_configured(tmp_path: Path, env_lines: list[str]) -> bool:
    bash = _bash()
    if bash is None:
        pytest.skip("no working bash (the CI runner has one)")
    env_file = tmp_path / "test.env"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8", newline="\n")
    common = COMMON.as_posix()
    result = subprocess.run(  # noqa: S603 (fixed script from this repository + a temp .env path)
        [bash, "-c", f'set -Eeuo pipefail; ENV_FILE="$1"; . "{common}"; auth_configured', "_", env_file.as_posix()],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


GOOGLE_CORE = [
    "GOOGLE_CLIENT_ID=cid.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET=s3cret-value",
    "GOOGLE_REDIRECT_URI=https://transcripts.example.test/api/auth/google/callback",
]


@pytest.mark.parametrize(("env_lines", "expected"), [
    pytest.param([*GOOGLE_CORE, "GOOGLE_ALLOWED_DOMAINS=company.example"], True, id="google-only-workspace"),
    pytest.param([*GOOGLE_CORE, "GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS=gmail.com"], True, id="google-only-personal"),
    pytest.param([*GOOGLE_CORE, "GOOGLE_ALLOW_ANY_ACCOUNT=true"], True, id="google-only-any-account"),
    pytest.param(["AUTH_USERS=admin:admin:scrypt.x"], True, id="password"),
    pytest.param(["API_KEYS=ci:user:abc"], True, id="api-key"),
    pytest.param(["AUTH_USERS=admin:admin:scrypt.x", *GOOGLE_CORE, "GOOGLE_ALLOWED_DOMAINS=c.example"], True,
                 id="google-plus-password"),
    pytest.param([], False, id="nothing"),
    pytest.param(["AUTH_USERS=", "API_KEYS="], False, id="empty-values"),
    pytest.param(GOOGLE_CORE, False, id="google-without-account-policy"),
    pytest.param([*GOOGLE_CORE[:2], "GOOGLE_ALLOWED_DOMAINS=company.example"], False, id="google-missing-redirect"),
    pytest.param([*GOOGLE_CORE, "GOOGLE_ALLOW_ANY_ACCOUNT=false"], False, id="google-any-account-false"),
])
def test_deploy_requires_some_authentication_but_google_alone_is_enough(tmp_path, env_lines, expected):
    assert _auth_configured(tmp_path, env_lines) is expected
