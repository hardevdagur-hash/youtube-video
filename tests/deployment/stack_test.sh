#!/usr/bin/env bash
# End-to-end deployment test against the running docker compose stack.
#
#   API_KEY=<raw key whose hash is in API_KEYS as name "ci"> tests/deployment/stack_test.sh
#
# Verifies: nginx/TLS proxying, public health, auth enforcement, persistence of a
# job across an app restart, CSV export (with formula neutralisation), backup and
# restore. Needs no YouTube/Groq access: the job is seeded as a checkpoint.
# Used by CI (.github/workflows/ci.yml); also safe to run on a fresh server.
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
BASE_URL="${BASE_URL:-https://127.0.0.1}"
API_KEY="${API_KEY:?set API_KEY to a raw API key (owner name must be ci)}"
JOB_ID="c1c1c1c1c1c1"
CURL=(curl -sS -k --max-time 20)

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { log "FAIL: $*" >&2; docker compose logs --tail 80 app >&2 || true; exit 1; }

status_of() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@"; }

wait_app_healthy() {
    local cid status waited=0
    cid="$(docker compose ps -q app)"
    while [ "$waited" -lt 120 ]; do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
        [ "$status" = "healthy" ] && return 0
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

log "1. TLS + health through nginx"
[ "$(status_of "$BASE_URL/api/health")" = "200" ] || fail "GET /api/health via nginx"
"${CURL[@]}" "$BASE_URL/api/health" | grep -q '"status":"ok"' || fail "health body"
[ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1/api/health)" = "301" ] \
    || fail "plain HTTP must redirect to HTTPS"
"${CURL[@]}" -D - -o /dev/null "$BASE_URL/" | grep -qi '^strict-transport-security:' || fail "HSTS header"
"${CURL[@]}" -D - -o /dev/null "$BASE_URL/" | grep -qi '^content-security-policy:' || fail "CSP header"

log "2. Frontend served"
"${CURL[@]}" "$BASE_URL/transcript" | grep -q '<div id="root">' || fail "SPA shell"

log "3. Authentication enforced"
[ "$(status_of -X POST -H 'Content-Type: application/json' -d '{"video_url":"dQw4w9WgXcQ"}' "$BASE_URL/api/transcript")" = "401" ] \
    || fail "unauthenticated transcript request must be 401"
[ "$(status_of -H "X-API-Key: ysk_wrong" "$BASE_URL/api/auth/me")" = "401" ] || fail "invalid key must be 401"
[ "$(status_of -H "X-API-Key: $API_KEY" "$BASE_URL/api/auth/me")" = "200" ] || fail "valid key must be 200"

log "4. Seed a completed job checkpoint"
docker compose exec -T app python - <<'PY'
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import TranscriptJobManager

TranscriptJobManager()._save_checkpoint(TranscriptJobProgress(
    job_id="c1c1c1c1c1c1", channel_handle="@ci", channel_id="UCci", channel_title="CI",
    status=JobStatus.COMPLETED, owner="key:ci", output_language="original",
    total_discovered=1, eligible_videos=1,
    videos=[TranscriptVideoItem(
        video_id="dQw4w9WgXcQ", video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="=HYPERLINK(\"http://evil.example\")", status="success", raw_transcript="hello world",
        transcript="hello world", source="youtube", method="caption",
    )],
))
PY
[ "$(status_of -H "X-API-Key: $API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID")" = "200" ] || fail "seeded job not visible"

log "5. Persistence across an app restart"
docker compose restart app >/dev/null
wait_app_healthy || fail "app not healthy after restart"
[ "$(status_of -H "X-API-Key: $API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID")" = "200" ] || fail "job lost after restart"

log "6. CSV export (formula injection neutralised)"
csv="$("${CURL[@]}" -H "X-API-Key: $API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID/download")"
grep -q "hello world" <<< "$csv" || fail "CSV missing transcript"
grep -q "'=HYPERLINK" <<< "$csv" || fail "CSV formula not neutralised"

log "7. Ownership: another key cannot see the job"
# (only checks the unauthenticated case here; per-user isolation is unit tested)
[ "$(status_of "$BASE_URL/api/transcript/jobs/$JOB_ID")" = "401" ] || fail "job readable without auth"

log "8. Backup"
BACKUP_DIR="$(mktemp -d)"
export BACKUP_DIR
scripts/backup.sh
archive="$(find "$BACKUP_DIR" -name 'transcript-data-*.tar.gz' | sort | tail -n 1)"
[ -n "$archive" ] || fail "no backup archive written"
tar -tzf "$archive" | grep -q "transcript_jobs/$JOB_ID.json" || fail "job missing from backup"

log "9. Restore"
docker compose exec -T app rm -f "/app/data/transcript_jobs/$JOB_ID.json"
docker compose restart app >/dev/null
wait_app_healthy || fail "app not healthy after deleting the job"
[ "$(status_of -H "X-API-Key: $API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID")" = "404" ] || fail "job should be gone before restore"
scripts/restore.sh "$archive" --yes
[ "$(status_of -H "X-API-Key: $API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID")" = "200" ] || fail "job not back after restore"

log "PASS: deployment stack test"
