#!/usr/bin/env bash
# Shared helpers for scripts/{deploy,rollback,restore,init-tls}.sh (sourced, never executed).
#
# Image versions: every deployed image is tagged transcript-app:<git sha> and the one in
# service is also tagged transcript-app:current (recorded in .deploy/current). Compose
# defaults IMAGE_TAG to "current", so a plain "docker compose up -d app" (secret rotation,
# restore, manual restarts) always starts the deployed version, never an unrelated build.

STATE_DIR="${STATE_DIR:-.deploy}"
ENV_FILE="${ENV_FILE:-.env}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
CURRENT_TAG="current"
NGINX_CONF="/etc/nginx/site/nginx.conf"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

env_value() {
    # Value of KEY in $ENV_FILE without sourcing it (never executes .env content).
    grep -E "^[[:space:]]*$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- \
        | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

google_auth_configured() {
    # Mirrors security.google_oauth.GoogleOAuthConfig.problems(): client, secret and redirect
    # URI, plus at least one account policy. The app itself re-validates everything on start.
    [ -n "$(env_value GOOGLE_CLIENT_ID)" ] || return 1
    [ -n "$(env_value GOOGLE_CLIENT_SECRET)" ] || return 1
    [ -n "$(env_value GOOGLE_REDIRECT_URI)" ] || return 1
    [ -n "$(env_value GOOGLE_ALLOWED_DOMAINS)" ] && return 0
    [ -n "$(env_value GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS)" ] && return 0
    is_true "$(env_value GOOGLE_ALLOW_ANY_ACCOUNT)"
}

auth_configured() {
    # Production must accept at least one kind of credential: password users, API keys
    # and/or Google sign-in (Google alone is enough).
    [ -n "$(env_value AUTH_USERS)" ] && return 0
    [ -n "$(env_value API_KEYS)" ] && return 0
    google_auth_configured
}

promote_image() {
    # Mark transcript-app:<tag> as the version in service.
    local tag="$1" previous
    previous="$(cat "$STATE_DIR/current" 2>/dev/null || true)"
    docker tag "transcript-app:${tag}" "transcript-app:${CURRENT_TAG}"
    mkdir -p "$STATE_DIR"
    if [ -n "$previous" ] && [ "$previous" != "$tag" ]; then
        printf '%s\n' "$previous" > "$STATE_DIR/previous"
    fi
    printf '%s\n' "$tag" > "$STATE_DIR/current"
}

deployed_tag() {
    # The recorded tag in service; fails if none was recorded or its image is gone.
    local tag
    tag="$(cat "$STATE_DIR/current" 2>/dev/null || true)"
    [ -n "$tag" ] || return 1
    docker image inspect "transcript-app:${tag}" >/dev/null 2>&1 || return 1
    printf '%s' "$tag"
}

wait_app_healthy() {
    local cid status="" waited=0
    cid="$(docker compose ps -q app)"
    [ -n "$cid" ] || return 1
    while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || echo missing)"
        case "$status" in
            healthy) return 0 ;;
            unhealthy|missing) return 1 ;;
        esac
        sleep 3
        waited=$((waited + 3))
    done
    return 1
}

reload_nginx() {
    # nginx loads $NGINX_CONF from a directory mount, so edits made on the host (git pull)
    # are visible inside the container and a reload applies them.
    # Returns non-zero (the running configuration is kept) when the new one is invalid.
    if [ -n "$(docker compose ps -q nginx 2>/dev/null)" ]; then
        if ! docker compose exec -T nginx nginx -t -c "$NGINX_CONF"; then
            log "nginx configuration test failed; the running configuration was kept"
            return 1
        fi
        docker compose exec -T nginx nginx -s reload -c "$NGINX_CONF" || return 1
        log "nginx configuration reloaded"
    else
        log "nginx is not running; its configuration is loaded on next start"
    fi
}
