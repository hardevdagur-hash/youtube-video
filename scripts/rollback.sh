#!/usr/bin/env bash
# Start a previously deployed image again (code rollback; data is not touched).
#
#   scripts/rollback.sh            -> the image deployed before the current one
#   scripts/rollback.sh <tag>      -> a specific transcript-app:<tag>
#
# To also roll back data, restore a backup with scripts/restore.sh.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
STATE_DIR=".deploy"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

target="${1:-$(cat "$STATE_DIR/previous" 2>/dev/null || true)}"
current="$(cat "$STATE_DIR/current" 2>/dev/null || true)"
[ -n "$target" ] || die "no previous deployment recorded; pass an image tag (docker images transcript-app)"
docker image inspect "transcript-app:${target}" >/dev/null 2>&1 || die "image transcript-app:${target} not found"

log "Rolling back from ${current:-unknown} to ${target}"
IMAGE_TAG="$target" docker compose up -d --remove-orphans

cid="$(docker compose ps -q app)"
waited=0
status=""
while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    [ "$status" = "healthy" ] && break
    [ "$status" = "unhealthy" ] && break
    sleep 3
    waited=$((waited + 3))
done
[ "$status" = "healthy" ] || die "transcript-app:${target} is not healthy (status: ${status:-unknown})"

mkdir -p "$STATE_DIR"
[ -n "$current" ] && printf '%s\n' "$current" > "$STATE_DIR/previous"
printf '%s\n' "$target" > "$STATE_DIR/current"
log "Rolled back to transcript-app:${target}"
