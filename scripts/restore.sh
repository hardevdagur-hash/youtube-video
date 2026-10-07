#!/usr/bin/env bash
# Restore transcript data from a backup archive created by scripts/backup.sh.
#
#   scripts/restore.sh backups/transcript-data-20261007T120000Z.tar.gz [--yes]
#
# Steps: verify checksum and archive contents -> safety backup of the current data
# -> stop the app -> replace the volume contents -> start the app -> verify.
# Restoring replaces ALL current jobs and cached transcripts.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

archive="${1:-}"
assume_yes="${2:-}"
[ -n "$archive" ] || die "usage: $0 <backup.tar.gz> [--yes]"
[ -f "$archive" ] || die "archive not found: $archive"
command -v docker >/dev/null || die "docker is not installed"

if [ -f "${archive}.sha256" ]; then
    ( cd "$(dirname "$archive")" && sha256sum -c "$(basename "$archive").sha256" ) >/dev/null \
        || die "checksum mismatch for $archive"
    log "Checksum verified"
else
    log "WARNING: no ${archive}.sha256 next to the archive; skipping checksum verification"
fi

listing="$(tar -tzf "$archive")" || die "archive is corrupt or not a gzip tarball"
if grep -Eq '(^/|(^|/)\.\.(/|$))' <<< "$listing"; then
    die "archive contains absolute or parent-directory paths; refusing to restore"
fi
expected_jobs="$(grep -c '^\./transcript_jobs/[0-9a-f]\{12\}\.json$' <<< "$listing" || true)"
log "Archive contains ${expected_jobs} job checkpoint(s)"

if [ "$assume_yes" != "--yes" ]; then
    read -r -p "This replaces all current transcript data. Type RESTORE to continue: " answer
    [ "$answer" = "RESTORE" ] || die "aborted"
fi

log "Taking a safety backup of the current data"
scripts/backup.sh || die "safety backup failed; nothing was changed"

cid="$(docker compose ps -aq app)"
[ -n "$cid" ] || die "no app container found; deploy once (scripts/deploy.sh) before restoring"
image="$(docker inspect -f '{{.Config.Image}}' "$cid")"
volume="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' "$cid")"
[ -n "$volume" ] || die "could not determine the data volume of the app container"

log "Stopping the app"
docker compose stop app

log "Restoring $archive into volume $volume"
docker run --rm -i --network none -v "${volume}:/app/data" --entrypoint sh "$image" -c \
    'find /app/data -mindepth 1 -delete && tar -xzf - -C /app/data' < "$archive" \
    || die "restore failed; the safety backup can be restored the same way"

log "Starting the app"
docker compose up -d app

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
[ "$status" = "healthy" ] || die "app is not healthy after restore (status: ${status:-unknown}); check 'docker compose logs app'"

restored_jobs="$(docker compose exec -T app sh -c 'ls /app/data/transcript_jobs 2>/dev/null | grep -c "^[0-9a-f]\{12\}\.json$" || true')"
[ "$restored_jobs" = "$expected_jobs" ] \
    || die "restored ${restored_jobs} job checkpoint(s), expected ${expected_jobs}"
log "Restore complete: ${restored_jobs} job checkpoint(s), app healthy"
