#!/usr/bin/env bash
# Back up the persistent transcript data (job checkpoints + transcript cache).
#
#   scripts/backup.sh                     -> ./backups/transcript-data-<UTC>.tar.gz (+ .sha256)
#   BACKUP_DIR=/srv/backups BACKUP_KEEP=30 scripts/backup.sh
#
# Not included on purpose: .env (secrets: back them up in your secret store),
# logs, and DATA_DIR/tmp (short-lived audio). Checkpoints are written atomically
# (write + rename), so an online backup captures complete files.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is not installed"
if ! [[ "$BACKUP_KEEP" =~ ^[0-9]+$ ]] || [ "$BACKUP_KEEP" -lt 1 ]; then
    die "BACKUP_KEEP must be a positive integer"
fi

umask 077
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$BACKUP_DIR/transcript-data-${stamp}.tar.gz"
partial="${archive}.partial"
trap 'rm -f "$partial"' EXIT

tar_args=(tar -C /app/data --exclude=./tmp -czf - .)
set +e
if [ -n "$(docker compose ps -q app 2>/dev/null)" ]; then
    docker compose exec -T app "${tar_args[@]}" > "$partial"
else
    # App stopped: read the volume with a throwaway, network-less container of the same image.
    cid="$(docker compose ps -aq app 2>/dev/null)"
    [ -n "$cid" ] || die "no app container found; nothing has been deployed yet"
    image="$(docker inspect -f '{{.Config.Image}}' "$cid")"
    volume="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' "$cid")"
    docker run --rm -i --network none -v "${volume}:/app/data:ro" --entrypoint tar "$image" "${tar_args[@]:1}" > "$partial"
fi
rc=$?
set -e
# GNU tar exits 1 when a file changed while being read (a job checkpoint was
# replaced mid-backup); the archived copy is still a complete earlier version.
[ "$rc" -eq 0 ] || [ "$rc" -eq 1 ] || die "tar failed with exit code $rc"
[ "$rc" -eq 0 ] || log "WARNING: some files changed during the backup; archived versions are complete"

tar -tzf "$partial" > /dev/null || die "archive verification failed"
mv "$partial" "$archive"
trap - EXIT
( cd "$BACKUP_DIR" && sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256" )

jobs="$(tar -tzf "$archive" | grep -c '^\./transcript_jobs/[0-9a-f]\{12\}\.json$' || true)"
log "Backup written: $archive ($(du -h "$archive" | cut -f1), ${jobs} job checkpoint(s))"

# Keep the newest BACKUP_KEEP archives (UTC timestamps in the names sort chronologically).
mapfile -t old < <(find "$BACKUP_DIR" -maxdepth 1 -name 'transcript-data-*.tar.gz' -printf '%f\n' \
    | sort -r | tail -n +"$((BACKUP_KEEP + 1))")
for name in "${old[@]}"; do
    rm -f -- "$BACKUP_DIR/$name" "$BACKUP_DIR/$name.sha256"
    log "Pruned old backup $name"
done
