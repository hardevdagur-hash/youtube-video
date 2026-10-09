# YouTube Transcript Service

Get transcripts for a single YouTube video or a whole channel, in the language
actually spoken or rewritten as Simple English / Simple Hindi, and export them to CSV.

- **Single video**: paste a URL or video ID and get the transcript.
- **Channel**: give a handle (`@channel`) and transcribe its eligible videos, either
  synchronously (small batches) or as a resumable background job with progress,
  cancel, resume and CSV download.
- **Output modes**: `original` (Original Spoken: verbatim captions/speech in the
  spoken language, never translated), `en` (Simple English), `hi` (Simple Hindi).
  `original` works for any spoken language YouTube or Whisper supports; rewriting is
  offered only into English and Hindi (other targets are rejected with 400/422).
- **Fallback**: videos without captions are transcribed from their audio with Groq
  Whisper (`whisper-large-v3`). Audio over Groq's 25 MB upload limit (roughly 45+
  minutes) is split into 10-minute chunks with ffmpeg and merged; videos longer than
  `STT_MAX_AUDIO_SECONDS` (2 h) are refused with `AUDIO_TOO_LONG` before any download.

It is a deliberately small system: one FastAPI process with one worker behind nginx,
with all state in one directory (`DATA_DIR`). No database, Redis or worker queue.

## Architecture

```text
Internet ──► nginx (TLS, HTTP→HTTPS, security headers, per-IP flood limit)
                │  only public entry point; app port is not published
                ▼
         FastAPI app — 1 container, 1 Uvicorn worker (webapp/main.py)
         ├─ auth middleware: session cookie or X-API-Key, roles, per-user rate limits
         ├─ React SPA (built into the image, served from frontend/dist)
         ├─ single video ── services/transcription/service.py
         │                    captions → (no captions) audio + Groq Whisper → clean → validate
         ├─ channels/jobs ── services/jobs/transcript_job_manager.py
         │                    YouTube Data API discovery → services/transcript_service.py
         │                    (manual captions → auto captions → Groq Whisper) per video
         ├─ translation ──── services/translation/service.py (Groq LLM, chunked, cached)
         └─ CSV export ───── formula-injection-safe writer
                ▼
         DATA_DIR (Docker volume transcript_data → /app/data)
           transcripts/      transcript + translation cache (JSON per video)
           transcript_jobs/  job checkpoints (JSON per job)  ← the source of truth for jobs
           users/            Google sign-in accounts (google_users.json, keyed by Google sub)
           tmp/audio/        short-lived audio downloads
```

Background jobs are asyncio tasks inside the app process, checkpointed to disk after
every video. That is why the service must run as **exactly one instance with one
worker**: a second worker would not see the first one's running jobs. See
[docs/TRANSCRIPT_MODULE.md](docs/TRANSCRIPT_MODULE.md) for the full pipeline.

| Path | What it is |
| --- | --- |
| `webapp/main.py` | FastAPI app: routes, middleware, error handling |
| `security/web_auth.py` | Authentication, authorization, CSRF, rate limits, CORS |
| `services/transcription/` | Single-video pipeline: captions → Groq Whisper → cleaning → validation |
| `services/transcript_service.py`, `providers/`, `clients/` | Channel/job pipeline: manual → auto captions → Whisper |
| `services/jobs/transcript_job_manager.py` | Background jobs: discovery, pacing, checkpoints, resume, retention |
| `services/translation/` | Simple English / Simple Hindi via Groq |
| `api/` | YouTube Data API (channel resolution, uploads playlist, video metadata) |
| `config/settings.py` | All configuration (validated environment variables) |
| `frontend/` | React + Vite SPA (`/transcript`) |
| `Dockerfile`, `docker-compose.yml`, `docker/nginx/` | Production stack |
| `scripts/` | deploy, backup, restore, rollback, TLS, credential setup |

## API

All `/api/*` routes require authentication except `GET /api/health`,
`POST /api/auth/login|logout` and the Google sign-in routes below. Send a session cookie
(browser; obtained with a password or with Google) or `X-API-Key`.

| Method & path | Purpose |
| --- | --- |
| `GET /api/health` | Health: 200 `ok` or 503 `unhealthy` (signed-in callers get details) |
| `POST /api/auth/login` / `logout`, `GET /api/auth/me` | Session cookie auth; `me` also returns server limits |
| `GET /api/auth/providers`, `/api/auth/google/start`, `/api/auth/google/callback` | Optional Google sign-in (same session afterwards) |
| `POST /api/transcript` | `{video_url, output_language: original\|en\|hi}` → transcript |
| `GET /api/channel/{handle}/transcripts` | Synchronous channel run (≤ `MAX_VIDEOS_SYNC_EXPORT`) |
| `POST /api/channel/{handle}/transcript-job` | Start a background job (≤ `MAX_VIDEOS_PER_JOB`) |
| `GET /api/transcript/jobs/{id}` | Job status, progress and per-video results |
| `POST /api/transcript/jobs/{id}/cancel` / `resume` | Cancel / resume a job |
| `GET /api/transcript/jobs/{id}/download` | Job CSV |
| `POST /api/transcript/export` | Synchronous CSV for a video or a small channel run |
| `GET /api/validate-url` | Parse and validate a YouTube URL |
| `GET /api/transcript/metrics`, `/api/transcript/limiter/status` | Admin only |

Errors are `{"success": false, "error_code", "message", "trace_id"}` with fixed,
client-safe messages; the `trace_id` equals the `X-Request-ID` response header and
appears in every server log line for that request.

## Configuration

Everything is an environment variable; [`.env.example`](.env.example) documents
each one. The essentials:

| Variable | Required | Notes |
| --- | --- | --- |
| `APP_ENV` | yes | `development` or `production` (production refuses unsafe config) |
| `YOUTUBE_API_KEY` | yes | YouTube Data API v3 key (channel discovery, titles) |
| `GROQ_API_KEY` | for STT / `en` / `hi` | Empty: only captioned videos and `original` mode work |
| `JWT_SECRET_KEY` | production | ≥ 32 random chars; signs session cookies |
| `AUTH_USERS` / `API_KEYS` | production (one of these or Google) | `user:role:scrypt-hash` / `name:role:sha256` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `GOOGLE_ALLOWED_DOMAINS` (Workspace, `hd` checked) / `GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS` / `GOOGLE_ALLOW_ANY_ACCOUNT`, `GOOGLE_ADMIN_SUBJECTS` | no | Optional "Continue with Google"; see [docs/GOOGLE_SIGN_IN.md](docs/GOOGLE_SIGN_IN.md) |
| `WHISPER_MAX_CONCURRENCY`, `STT_QUEUE_TIMEOUT_SECONDS`, `TRANSCRIPT_WORKER_THREADS` | no | Server-wide speech-to-text limit and the dedicated transcript worker pool |
| `YOUTUBE_PROXY_URL`, `YTDLP_COOKIES_FILE` | no | Optional mitigations when YouTube blocks the server IP (`BOT_BLOCKED`); see docs/OPERATIONS.md |
| `CORS_ORIGINS` | no | Only for cross-origin browser clients; never `*` |
| `DATA_DIR` | no | Persistent data (`./data`; `/app/data` in Docker) |
| `MAX_VIDEOS_PER_JOB`, `MAX_VIDEOS_SYNC_EXPORT`, `MAX_ACTIVE_JOBS[_PER_USER]` | no | Abuse/cost limits (100, 25, 4/2) |
| `CHANNEL_MIN_VIDEO_SECONDS`, `CHANNEL_MAX_VIDEO_SECONDS` | no | Channel eligibility window (180, 1800 = 3:00–30:00) |
| `CHANNEL_DISCOVERY_SCAN_CAP` | no | Uploads examined to find `max_videos` eligible ones (1000; YouTube quota bound) |
| `STT_MAX_AUDIO_SECONDS`, `STT_CHUNK_SECONDS` | no | Longest video sent to speech-to-text (7200); chunk length (600) |
| `LOGIN_RATE_LIMIT_PER_MINUTE`, `LOGIN_USER_RATE_LIMIT_PER_MINUTE` | no | Login attempts per IP (5) and per username across IPs (30) |
| `JOB_RETENTION_DAYS` | no | Finished jobs are deleted after this (30) |
| `LOG_LEVEL`, `LOG_FORMAT` | no | `INFO`; `text` or `json` (the image uses `json`) |

Generate the security values with
`python scripts/configure_env.py --app-env production --origin https://<your-domain>`
(prompts for the admin password, writes hashes only, backs up the old `.env`).

## Local development

Requirements: Python 3.12, Node 20.

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows
npm --prefix frontend ci
cp .env.example .env        # set YOUTUBE_API_KEY (and GROQ_API_KEY)
python scripts/configure_env.py --app-env development --origin http://localhost:5173
make dev-api                # API on http://127.0.0.1:8000 (auto-reload)
make dev-web                # SPA on http://127.0.0.1:5173, proxies /api
```

On Windows without `make`, `start.ps1` starts both.

## Testing

```bash
make check                                     # ruff (read-only) + pytest + frontend typecheck/tests/build
pytest -o log_cli=false -q                     # backend: unit, security, reliability, integration
RUN_LIVE_TESTS=1 pytest -m live tests/live     # real YouTube/Groq calls (uses .env keys; costs quota)
SMOKE_TEST_URL=https://host SMOKE_TEST_INSECURE=1 pytest -m smoke tests/smoke   # against a deployment
API_KEY=<raw key named "ci"> OTHER_API_KEY=<another user's key> tests/deployment/stack_test.sh
                                               # on a running stack: isolation, restart, down/up, backup, restore, rollback
```

Tests never touch real data or credentials: they run with a temporary `DATA_DIR` and
blank API keys. `ruff check .` never modifies files; fixing is an explicit
`make lint-fix`. CI (`.github/workflows/ci.yml`) runs all of the above and additionally
builds the image and runs the real compose stack (nginx + TLS + volume).

## Production deployment

Single Linux host with Docker Engine and the compose plugin; DNS for your domain
pointing at it; ports 80 and 443 open. Full runbook: [docs/OPERATIONS.md](docs/OPERATIONS.md).

```bash
git clone <repo> transcripts && cd transcripts
cp .env.example .env && chmod 600 .env       # fill in keys (see Configuration)
python3 scripts/configure_env.py --app-env production --origin https://transcripts.example.com
scripts/init-tls.sh self-signed transcripts.example.com   # bootstrap certificate
scripts/deploy.sh                                          # build, start, health-gate, auto-rollback
scripts/init-tls.sh letsencrypt transcripts.example.com you@example.com
curl -fsS https://transcripts.example.com/api/health
```

- **Persistence**: everything lives in the `transcript_data` volume; restarts and
  redeploys keep all jobs and transcripts. Jobs that were running when the app stopped
  come back as `paused`; resume them from the UI or API.
- **Backup / restore**: `scripts/backup.sh` (verified, checksummed tarball under
  `./backups`, keeps 14), `scripts/restore.sh <archive>`. `.env` is not included:
  keep secrets in your password manager / secret store.
- **Rollback**: `scripts/rollback.sh` starts the previously deployed image
  (`deploy.sh` does this automatically when a new image fails its health checks).
- **Logs**: `docker compose logs -f app` (JSON, one line per event, with
  `request_id`, `job_id` and `user`).

## Security

Deny-by-default authentication on `/api/*`; per-job ownership (users see only their
own jobs, admins see all; another user's job is indistinguishable from a missing one); CSRF origin checks for cookie
sessions; per-user and per-IP rate limits; job and synchronous-run caps; strict
validation of every identifier and path; formula-injection-safe CSV; generic client
errors with server-side detail; secrets redacted from all logs; non-root container
without published app port. Details and key-rotation steps: [SECURITY.md](SECURITY.md).

## Known limitations

- **One instance only.** Jobs, rate limits and caches live in one process. Scaling out
  would need a shared job store and queue; that is intentionally out of scope.
- **Sequential channel jobs.** Videos are processed one at a time with global pacing
  (`TRANSCRIPT_REQUEST_INTERVAL`) to stay under YouTube's caption rate limits; large
  channels take time and can pause on persistent rate limiting (resume later).
- **Channel eligibility differs from single videos.** Channel runs take only non-live
  uploads inside `CHANNEL_MIN_VIDEO_SECONDS`–`CHANNEL_MAX_VIDEO_SECONDS` (default
  3:00–30:00, a product rule); a single-video request has no window, so a 45-minute
  lecture works on its own but is skipped by a channel run unless the maximum is raised.
  `max_videos` counts eligible videos; discovery examines at most
  `CHANNEL_DISCOVERY_SCAN_CAP` uploads to find them.
- **YouTube may block requests from some server IPs** (cloud ranges). Caption fetching
  can be rate limited, and audio download for speech-to-text (yt-dlp) can be answered
  with "Sign in to confirm you're not a bot" (observed intermittently, per video, during
  verification). Affected videos then fail with `AUDIO_EXTRACTION_FAILED`; there is no
  cookie-based workaround configured.
- **Two transcript pipelines.** Single videos and channel jobs use different (but
  equivalent-mode) pipelines and separate caches; output text can differ slightly in
  cleaning. Both share the audio download, the duration cap and Groq Whisper.
- **Speech-to-text cost/limits**: videos longer than `STT_MAX_AUDIO_SECONDS` are refused;
  Groq's free tier allows 7,200 audio-seconds per hour, so several long videos in a row
  can hit `STT_RATE_LIMITED`. Chunk boundaries are cut by time, so a word spanning a
  boundary may be transcribed imperfectly.
- **Translation of very long transcripts** is done in parts; a part the model cuts off is
  split and retried, and if it still cannot complete, the original-language transcript
  is returned clearly labelled (`fallback_to_original` / source language), never a
  partial translation.
- The `/api/transcript/metrics` counters are in-memory and reset on restart.

## Troubleshooting

| Symptom | Likely cause / action |
| --- | --- |
| App container restarts, log says `Refusing to start` | Unsafe production config (weak `JWT_SECRET_KEY`, `*` or `http://` in `CORS_ORIGINS`, no `AUTH_USERS`/`API_KEYS`); fix `.env`, `scripts/deploy.sh` |
| `/api/health` 503 | `YOUTUBE_API_KEY` missing/placeholder or `DATA_DIR` not writable (full disk / read-only volume) |
| Channel lookups fail, log shows `API key not valid` | YouTube key revoked or restricted (check HTTP referrer/IP restrictions in Google Cloud console) |
| `STT_UNAVAILABLE` | `GROQ_API_KEY` empty or rejected |
| `AUDIO_EXTRACTION_FAILED` | yt-dlp blocked by YouTube from this IP, video private/region-locked, or live |
| `AUDIO_TOO_LONG` | Video longer than `STT_MAX_AUDIO_SECONDS` and has no captions |
| Login returns 429 | Throttled for this IP/username; wait `Retry-After` seconds (other users and IPs are unaffected) |
| Channel job completes with 0 videos | No uploads in the eligibility window within `CHANNEL_DISCOVERY_SCAN_CAP`; check `skipped_videos`, adjust the window |
| Job shows `paused` after a restart | Expected: it was running when the app stopped; resume it |
