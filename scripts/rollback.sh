#!/usr/bin/env bash
# Start a previously deployed image again (code rollback; data is not touched).
#
#   scripts/rollback.sh            -> the image deployed before the current one
#   scripts/rollback.sh <tag>      -> a specific transcript-app:<tag>
#
# The target becomes transcript-app:current, so later "docker compose up -d" runs keep it.
# To also roll back data, restore a backup with scripts/restore.sh.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/common.sh
. scripts/lib/common.sh

target="${1:-$(cat "$STATE_DIR/previous" 2>/dev/null || true)}"
current="$(cat "$STATE_DIR/current" 2>/dev/null || true)"
[ -n "$target" ] || die "no previous deployment recorded; pass an image tag (docker images transcript-app)"
[ "$target" != "$CURRENT_TAG" ] || die "pass the version tag itself (docker images transcript-app), not '${CURRENT_TAG}'"
docker image inspect "transcript-app:${target}" >/dev/null 2>&1 || die "image transcript-app:${target} not found"

log "Rolling back from ${current:-unknown} to ${target}"
IMAGE_TAG="$target" docker compose up -d --remove-orphans

wait_app_healthy || die "transcript-app:${target} is not healthy; check 'docker compose logs app'"

promote_image "$target"
log "Rolled back to transcript-app:${target} (now transcript-app:${CURRENT_TAG})"
