# Operations runbook

Single host, Docker Engine + compose plugin, repository checked out (e.g. `/srv/transcripts`).
All commands run from the repository root.

## Production domain

The app runs on its own host name (for example a `transcripts.` subdomain of the main
site), separate from any other application. **No domain is configured in this
repository**; choose it, then set it in every place below (same value everywhere):

| Where | Setting |
| --- | --- |
| DNS | `A`/`AAAA` record for `<domain>` → this host; ports 80 and 443 open |
| TLS | `scripts/init-tls.sh letsencrypt <domain> <email>` (certificate for exactly that name) |
| `.env` `CORS_ORIGINS` | `https://<domain>` (written by `configure_env.py --origin https://<domain>`) |
| `.env` `GOOGLE_REDIRECT_URI` | `https://<domain>/api/auth/google/callback` |
| Google Cloud OAuth client | Authorized redirect URI `https://<domain>/api/auth/google/callback`; authorized domain = the registrable domain |
| nginx | Nothing: `server_name _` serves whatever name DNS points here; HTTP redirects to `https://$host` |
| Cookies | Nothing: session cookies are host-only (no `Domain=`), so other subdomains never receive them |
| Frontend | Nothing: the SPA calls the API on its own origin (`VITE_API_BASE` stays empty) |

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
   For Google sign-in add the `GOOGLE_*` settings by hand (docs/GOOGLE_SIGN_IN.md).
   Google alone is a valid sign-in method; `deploy.sh` refuses only a configuration with
   no sign-in method at all.
   Delete the `.env.backup-*` it leaves once you have verified the new file.
3. **Bootstrap certificate**: `scripts/init-tls.sh self-signed <domain>`
4. **Deploy**: `scripts/deploy.sh`
5. **Real certificate**: `scripts/init-tls.sh letsencrypt <domain> <email>`
6. **Renewal** (crontab of any user allowed to run docker; root is not required because
   the root-owned certbot files are copied by a short-lived container). `renew` exits
   non-zero on any failure, so let cron mail it:
   ```cron
   MAILTO=ops@example.com
   17 3 * * * cd /srv/transcripts && scripts/init-tls.sh renew >> /var/log/transcripts-tls.log 2>&1 || echo "TLS renewal failed: see /var/log/transcripts-tls.log"
   ```
   Also monitor certificate expiry externally (any uptime service with TLS-expiry alerts);
   nothing in this repository alerts on its own.
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

**Image versions.** Every deploy builds `transcript-app:<git sha>` and, once healthy,
also tags it `transcript-app:current`; compose starts `:current` unless `IMAGE_TAG` is
set. So every plain `docker compose up -d` / `restart` (secret rotation, restore, manual
restarts) starts the deployed version, never an unrelated build. `.deploy/current` and
`.deploy/previous` record the version tags. Never run `docker compose build` by hand:
use `scripts/deploy.sh`.

## What deploy.sh does

1. Preflight: docker/compose present; `.env` has `JWT_SECRET_KEY`, `YOUTUBE_API_KEY`
   and at least one sign-in method (`AUTH_USERS`, `API_KEYS` and/or complete Google
   sign-in); TLS files present; ≥ 2 GB free.
2. Builds `transcript-app:<git sha>` (`--pull`: base images get their security fixes).
3. Backs up the data volume (unless `SKIP_BACKUP=1`).
4. `docker compose up -d`; tests and reloads the nginx configuration (it is
   mounted as a directory, so a `git pull` change takes effect), waits for the
   container health check, then checks `https://127.0.0.1/api/health` through nginx.
5. Success: tags the image `transcript-app:current` and records the tag in
   `.deploy/current` (previous in `.deploy/previous`).
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
| `GOOGLE_CLIENT_SECRET` | Google Auth Platform → Clients → add a new secret, update `.env`, `docker compose up -d app`, then disable/delete the old secret | Sign-ins in progress fail once; existing sessions are unaffected |
| Google admin / disabled accounts | `GOOGLE_ADMIN_SUBJECTS` in `.env`; `"disabled": true` in `users/google_users.json` (see docs/GOOGLE_SIGN_IN.md) | Applied on restart; affected sessions end immediately |
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
| `BOT_BLOCKED` (503, retryable); logs show "Sign in to confirm you're not a bot" or "blocking requests from your IP" | YouTube refuses this server's IP (common on cloud/datacenter IPs) | Not fixable in code. The app stops contacting YouTube for the cooldown, never caches it as "no captions", and keeps job items resumable. Options: retry later, set `YOUTUBE_PROXY_URL` (egress proxy) and/or `YTDLP_COOKIES_FILE`, or move to another egress IP. **Test real videos from the production server before go-live** |
| `STT_BUSY` (503, retryable) | All `WHISPER_MAX_CONCURRENCY` speech-to-text slots busy longer than `STT_QUEUE_TIMEOUT_SECONDS` | Expected under load; raise the concurrency only with Groq budget and bandwidth for it |
| `AUDIO_EXTRACTION_FAILED` | yt-dlp could not fetch audio (other than a bot check) | Check `docker compose logs app` for the yt-dlp message; retry |
| `AUDIO_TOO_LONG` | Uncaptioned video longer than `STT_MAX_AUDIO_SECONDS` | Expected cost guard; raise the setting only with Groq budget for it |
| `STT_RATE_LIMITED` on long videos | Groq audio-seconds-per-hour limit (7,200 on the free tier) | Wait an hour or upgrade the Groq plan |
| Logs show `Login throttled (backoff/ip_rate/user_rate)` | Repeated failed logins | Attacker IPs are slowed, accounts are not locked; if `user_rate` persists, someone is guessing one account from many IPs: consider firewalling |
| Channel job completes with few/zero videos | Uploads outside `CHANNEL_MIN/MAX_VIDEO_SECONDS` within `CHANNEL_DISCOVERY_SCAN_CAP` | Check the job's `skipped_videos`; adjust the window if the product rule allows |
| Login page shows no Google button | Google settings incomplete/invalid (startup log names the problem; production refuses to start) or `users/google_users.json` unreadable | Fix `.env` per docs/GOOGLE_SIGN_IN.md; never delete the account file, restore it from backup |
| Google sign-in returns `auth_error=failed` | `redirect_uri_mismatch`, clock skew, expired attempt (>10 min) or wrong client secret; log line `Google sign-in refused (<reason>)` | Compare `GOOGLE_REDIRECT_URI` with the client's registered URIs; check the reason in the log |
| Google sign-in returns `auth_error=not_allowed` | Workspace account whose `hd` is not in `GOOGLE_ALLOWED_DOMAINS`, a personal account (no `hd`, even on the company's email domain) not covered by `GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS`, unverified email, or disabled | Expected; the log line names the reason. Adjust the policy only if intended |
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
replaces the volume contents, starts the deployed version recorded in
`.deploy/current`, waits for health and checks the restored job count.
`tests/deployment/stack_test.sh` automates this drill (used by CI), including checks
that restore, rollback and a plain `docker compose up -d app` keep the deployed image.

## Monitoring (not provided by this repository)

Docker restarts a container whose process exits, but not one that is merely
`unhealthy`, and nothing here sends alerts. Before go-live set up, outside this host:
an uptime check on `https://<domain>/api/health`, TLS-expiry alerts, disk-usage alerts,
and mail/alerting for the backup and renewal cron jobs.
