#!/usr/bin/env bash
# End-to-end deployment test against the running docker compose stack.
#
#   API_KEY=<raw key named "ci" in API_KEYS> OTHER_API_KEY=<raw key of another user> \
#       tests/deployment/stack_test.sh
#
# Verifies: nginx/TLS proxying, public health, auth enforcement, per-user job
# isolation, persistence of a job across an app restart and a full
# `docker compose down` / `up -d`, CSV export (with formula neutralisation),
# backup, restore and rollback. Needs no YouTube/Groq access: the job is seeded as
# a checkpoint. Used by CI (.github/workflows/ci.yml); safe on a fresh server, but
# step 10 restarts the stack and switches the running image tag.
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
BASE_URL="${BASE_URL:-https://127.0.0.1}"
API_KEY="${API_KEY:?set API_KEY to a raw API key (owner name must be ci)}"
OTHER_API_KEY="${OTHER_API_KEY:?set OTHER_API_KEY to the raw API key of a second, non-admin user}"
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

wait_https_ok() {
    # nginx may still be (re)connecting to the app right after it turns healthy.
    local waited=0
    while [ "$waited" -lt 40 ]; do
        [ "$(status_of "$BASE_URL/api/health")" = "200" ] && return 0
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

job_status() { status_of -H "X-API-Key: $1" "$BASE_URL/api/transcript/jobs/$JOB_ID"; }

log "0. Image contents: one worker, ffmpeg for long-audio chunking"
encoders="$(docker compose exec -T app ffmpeg -hide_banner -encoders 2>/dev/null)" \
    || fail "ffmpeg missing from the app image"
grep -q libopus <<< "$encoders" || fail "ffmpeg lacks libopus"
[ "$(docker compose ps -q app | wc -l)" = "1" ] || fail "exactly one app container expected"

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

log "5. Persistence across an app restart and a full down/up (volume kept)"
docker compose restart app >/dev/null
wait_app_healthy || fail "app not healthy after restart"
[ "$(job_status "$API_KEY")" = "200" ] || fail "job lost after restart"
docker compose down >/dev/null  # never -v: that would delete the data volume
docker compose up -d >/dev/null
wait_app_healthy || fail "app not healthy after down/up"
wait_https_ok || fail "nginx not serving after down/up"
[ "$(job_status "$API_KEY")" = "200" ] || fail "job lost after docker compose down/up"

log "6. CSV export (formula injection neutralised)"
csv="$("${CURL[@]}" -H "X-API-Key: $API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID/download")"
grep -q "hello world" <<< "$csv" || fail "CSV missing transcript"
grep -q "'=HYPERLINK" <<< "$csv" || fail "CSV formula not neutralised"

log "7. Ownership: anonymous and other users cannot see or touch the job"
[ "$(status_of "$BASE_URL/api/transcript/jobs/$JOB_ID")" = "401" ] || fail "job readable without auth"
[ "$(job_status "$OTHER_API_KEY")" = "404" ] || fail "another user can read the job"
[ "$(status_of -H "X-API-Key: $OTHER_API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID/download")" = "404" ] \
    || fail "another user can download the job"
[ "$(status_of -X POST -H "X-API-Key: $OTHER_API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID/cancel")" = "400" ] \
    || fail "another user's cancel must be refused like a missing job"
[ "$(status_of -X POST -H "X-API-Key: $OTHER_API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID/resume")" = "404" ] \
    || fail "another user can resume the job"
[ "$(status_of -H "X-API-Key: $API_KEY" "$BASE_URL/api/transcript/jobs/..%2F..%2Fetc%2Fpasswd")" != "200" ] \
    || fail "path traversal in job id accepted"

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
wait_https_ok || fail "nginx not serving after restore"
[ "$(job_status "$API_KEY")" = "200" ] || fail "job not back after restore"
"${CURL[@]}" -H "X-API-Key: $API_KEY" "$BASE_URL/api/transcript/jobs/$JOB_ID/download" | grep -q "hello world" \
    || fail "export of the restored job failed"

log "10. Rollback to an earlier image keeps the data"
current_tag="$(cat .deploy/current 2>/dev/null || true)"
[ -n "$current_tag" ] || fail "scripts/deploy.sh did not record the deployed tag"
docker tag "transcript-app:${current_tag}" "transcript-app:stacktest-previous"
scripts/rollback.sh stacktest-previous
wait_https_ok || fail "nginx not serving after rollback"
[ "$(cat .deploy/current)" = "stacktest-previous" ] || fail "rollback did not record the new current tag"
[ "$(job_status "$API_KEY")" = "200" ] || fail "job lost after rollback"
scripts/rollback.sh "$current_tag" >/dev/null
wait_https_ok || fail "nginx not serving after rolling forward again"

log "PASS: deployment stack test"
