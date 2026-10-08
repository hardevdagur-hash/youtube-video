# Operations runbook

Single host, Docker Engine + compose plugin, repository checked out (e.g. `/srv/transcripts`).
All commands run from the repository root.

## First deployment

1. **DNS**: an `A`/`AAAA` record for your domain pointing at the host; open ports 80/443.
2. **Configuration**
   ```bash
   cp .env.example .env && chmod 600 .env
   # edit .env: YOUTUBE_API_KEY, GROQ_API_KEY
   python3 scripts/configure_env.py --app-env production --origin https://<domain>
   ```
   `configure_env.py` prompts for the admin password and writes `JWT_SECRET_KEY`,
   `AUTH_USERS` (scrypt hash) and `CORS_ORIGINS`; add `--api-key-name ops` for an API key.
   Delete the `.env.backup-*` it leaves once you have verified the new file.
3. **Bootstrap certificate**: `scripts/init-tls.sh self-signed <domain>`
4. **Deploy**: `scripts/deploy.sh`
5. **Real certificate**: `scripts/init-tls.sh letsencrypt <domain> <email>`
6. **Renewal** (crontab of the deploying user):
   ```cron
   17 3 * * * cd /srv/transcripts && scripts/init-tls.sh renew >> /var/log/transcripts-tls.log 2>&1
   ```
7. **Backups** (crontab):
   ```cron
   30 2 * * * cd /srv/transcripts && BACKUP_DIR=/srv/backups/transcripts scripts/backup.sh >> /var/log/transcripts-backup.log 2>&1
   ```
   Copy `/srv/backups/transcripts` off the host (object storage, another machine).

## Routine operations

| Task | Command |
| --- | --- |
| Deploy a new version | `git pull && scripts/deploy.sh` |
| Status | `docker compose ps` |
| Health (public) | `curl -fsS https://<domain>/api/health` |
| Health (details) | `curl -fsS -H "X-API-Key: <key>" https://<domain>/api/health` |
| Logs | `docker compose logs -f app` / `docker compose logs -f nginx` |
| Logs for one request | `docker compose logs app \| grep <X-Request-ID>` |
| Logs for one job | `docker compose logs app \| grep <job_id>` |
| Restart (jobs pause, then resume via UI/API) | `docker compose restart app` |
| Stop / start | `docker compose stop` / `docker compose start` |
| Shut down completely (keeps data) | `docker compose down` |
| Back up now | `scripts/backup.sh` |
| Restore | `scripts/restore.sh backups/transcript-data-<UTC>.tar.gz` |
| Roll back code | `scripts/rollback.sh` (or `scripts/rollback.sh <tag>`) |
| List images | `docker images transcript-app` |

Never run `docker compose down -v` or `docker volume rm`: that deletes the
`transcript_data` volume (all jobs and transcripts).

## What deploy.sh does

1. Preflight: docker/compose present, `.env` has `JWT_SECRET_KEY`, `YOUTUBE_API_KEY`
   and `AUTH_USERS` or `API_KEYS`, TLS files present, ≥ 2 GB free.
2. Builds `transcript-app:<git sha>`.
3. Backs up the data volume (unless `SKIP_BACKUP=1`).
4. `docker compose up -d`; waits for the container health check, then checks
   `https://127.0.0.1/api/health` through nginx.
5. Success: records the tag in `.deploy/current` (previous in `.deploy/previous`).
   Failure: prints app logs and starts the previous image again (data is never touched).

After a successful deploy, run the read-only smoke suite from any machine (it only
reads health/auth/static routes and creates nothing):

```bash
SMOKE_TEST_URL=https://<domain> SMOKE_TEST_API_KEY=<user key> pytest -o addopts="" -m smoke tests/smoke -q
```

## Secret rotation

| Secret | How | Effect |
| --- | --- | --- |
| `JWT_SECRET_KEY` | `python3 scripts/configure_env.py --rotate-jwt --origin https://<domain>`, then `docker compose up -d app` | All browser sessions end; users sign in again |
| Admin password | `python3 scripts/configure_env.py --origin https://<domain>` (prompts), then `docker compose up -d app` | Old password stops working; existing sessions of removed users are revoked |
| API key | `python3 scripts/hash_secret.py` → replace the entry in `API_KEYS`, restart | Old key rejected immediately |
| `YOUTUBE_API_KEY`, `GROQ_API_KEY` | Create the new key in Google Cloud / Groq console, update `.env`, `docker compose up -d app`, then revoke the old key | See SECURITY.md "Rotating third-party API keys" |

`docker compose up -d app` recreates the container so it reads the new `.env`
(`restart` does not). Running jobs are checkpointed and come back `paused`.

## Incidents

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `/api/health` 503 | `YOUTUBE_API_KEY` missing or data volume not writable | Check `.env`; `docker compose logs app`; disk space (`df -h`) |
| Channel requests fail, logs show `API key not valid` | YouTube key revoked/restricted | Create a new key (see SECURITY.md), update `.env`, `docker compose up -d app` |
| Single videos without captions fail with `STT_UNAVAILABLE` | `GROQ_API_KEY` missing/invalid | Fix the key, restart the app |
| Jobs end `paused` with "persistent YouTube rate limiting" | YouTube throttles this server's IP | Wait (hours), then resume; consider a larger `TRANSCRIPT_REQUEST_INTERVAL` |
| Many `CAPTIONS_UNAVAILABLE` for videos that have captions | YouTube blocks caption scraping from the host IP | Speech-to-text fallback (Groq) covers it; otherwise run from another egress IP |
| `AUDIO_EXTRACTION_FAILED`; logs show "Sign in to confirm you're not a bot" | YouTube challenges yt-dlp audio downloads from this IP | Not fixable in the app; retry later or use another egress IP. Captioned videos are unaffected |
| `AUDIO_TOO_LONG` | Uncaptioned video longer than `STT_MAX_AUDIO_SECONDS` | Expected cost guard; raise the setting only with Groq budget for it |
| `STT_RATE_LIMITED` on long videos | Groq audio-seconds-per-hour limit (7,200 on the free tier) | Wait an hour or upgrade the Groq plan |
| Logs show `Login throttled (backoff/ip_rate/user_rate)` | Repeated failed logins | Attacker IPs are slowed, accounts are not locked; if `user_rate` persists, someone is guessing one account from many IPs: consider firewalling |
| Channel job completes with few/zero videos | Uploads outside `CHANNEL_MIN/MAX_VIDEO_SECONDS` within `CHANNEL_DISCOVERY_SCAN_CAP` | Check the job's `skipped_videos`; adjust the window if the product rule allows |
| `Failed to save transcript job checkpoint` in logs | Disk full / volume read-only | Free space (`df -h`); the last good checkpoint is intact and the next save catches up |
| 429 `JOB_LIMIT_REACHED` / `SERVER_BUSY` | Configured limits reached | Expected; raise `MAX_ACTIVE_JOBS*` / `MAX_CONCURRENT_SYNC_CHANNEL_RUNS` only if the host has capacity |
| nginx does not start | Missing/invalid certificate | `scripts/init-tls.sh self-signed <domain>`; `docker compose logs nginx` |
| Users logged out after a restart | `JWT_SECRET_KEY` changed | Expected when rotating; keep it stable otherwise |

## Restore drill (do this before go-live and periodically)

```bash
scripts/backup.sh
scripts/restore.sh "$(ls -1 backups/transcript-data-*.tar.gz | sort | tail -n 1)" --yes
curl -fsS -H "X-API-Key: <key>" https://<domain>/api/transcript/jobs/<a known job id>
```

`restore.sh` verifies the checksum and archive paths, takes a safety backup first,
replaces the volume contents, waits for health and checks the restored job count.
`tests/deployment/stack_test.sh` automates this drill (used by CI).
