#!/usr/bin/env bash
# Build and (re)deploy the single-instance stack, then verify it; on failure the
# previously deployed image is started again automatically.
#
#   scripts/deploy.sh             build from the current checkout and deploy
#   SKIP_BACKUP=1 scripts/deploy.sh   skip the pre-deploy data backup
#
# Requires: docker with the compose plugin, ./.env, docker/nginx/certs/*.pem.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/common.sh
. scripts/lib/common.sh

preflight() {
    command -v docker >/dev/null || die "docker is not installed"
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed"
    docker info >/dev/null 2>&1 || die "the docker daemon is not reachable"
    [ -f "$ENV_FILE" ] || die ".env not found: copy .env.example to .env and fill it in (see README)"

    local missing=()
    for key in JWT_SECRET_KEY YOUTUBE_API_KEY; do
        [ -n "$(env_value "$key")" ] || missing+=("$key")
    done
    auth_configured \
        || missing+=("a sign-in method: AUTH_USERS, API_KEYS and/or complete Google sign-in (GOOGLE_*)")
    [ ${#missing[@]} -eq 0 ] || die "missing required settings in .env: ${missing[*]}"
    [ -n "$(env_value GROQ_API_KEY)" ] \
        || log "WARNING: GROQ_API_KEY is empty: speech-to-text fallback and translation are disabled"

    if [ ! -s docker/nginx/certs/fullchain.pem ] || [ ! -s docker/nginx/certs/privkey.pem ]; then
        die "TLS certificate missing: run 'scripts/init-tls.sh self-signed <host>' (bootstrap) or 'scripts/init-tls.sh letsencrypt <domain> <email>'"
    fi

    local free_kb
    free_kb="$(df -Pk . | awk 'NR==2 {print $4}')"
    [ "${free_kb:-0}" -ge 2097152 ] || die "less than 2 GB free disk space"
}

image_tag() {
    local tag
    tag="$(git rev-parse --short=12 HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)"
    if git rev-parse --git-dir >/dev/null 2>&1 && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        tag="${tag}-dirty-$(date -u +%Y%m%d%H%M%S)"
    fi
    printf '%s' "$tag"
}

wait_healthy() {
    wait_app_healthy || return 1
    # End to end through nginx/TLS (-k: also valid for the bootstrap self-signed cert).
    if command -v curl >/dev/null; then
        local attempt
        for attempt in 1 2 3 4 5; do
            curl -fsSk --max-time 10 https://127.0.0.1/api/health >/dev/null && return 0
            sleep 3
        done
        log "app is healthy but https://127.0.0.1/api/health through nginx failed (attempt $attempt)"
        return 1
    fi
    log "WARNING: curl not installed; skipped the end-to-end check through nginx"
    return 0
}

main() {
    preflight
    local tag previous
    tag="$(image_tag)"
    previous="$(deployed_tag || true)"

    log "Building transcript-app:${tag}"
    # --pull: base images (python, node) are refreshed so their security fixes are included.
    IMAGE_TAG="$tag" docker compose build --pull app

    if [ -z "${SKIP_BACKUP:-}" ] && [ -n "$(docker compose ps -q app 2>/dev/null)" ]; then
        log "Backing up data before deploying"
        scripts/backup.sh || die "pre-deploy backup failed (set SKIP_BACKUP=1 to deploy anyway)"
    fi

    log "Starting transcript-app:${tag}"
    IMAGE_TAG="$tag" docker compose up -d --remove-orphans

    # A running nginx keeps its old configuration until reloaded (git pull edits the file).
    if reload_nginx && wait_healthy; then
        promote_image "$tag"
        log "Deployed transcript-app:${tag} (now transcript-app:${CURRENT_TAG})"
        docker compose ps
        return 0
    fi

    log "Deployment of ${tag} failed health checks; recent app logs:"
    docker compose logs --tail 60 app || true
    if [ -n "$previous" ]; then
        log "Rolling back to transcript-app:${previous}"
        IMAGE_TAG="$previous" docker compose up -d --remove-orphans
        if wait_healthy; then
            log "Rollback to ${previous} is healthy"
        else
            log "Rollback to ${previous} is NOT healthy"
        fi
    else
        log "No previous image to roll back to"
    fi
    die "deployment failed"
}

main "$@"
