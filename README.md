# YouTube Transcript Service

Get transcripts for a single YouTube video or a whole channel, in the language
actually spoken or rewritten as Simple English / Simple Hindi, and export them to CSV.

- **Single video**: paste a URL or video ID and get the transcript.
- **Channel**: give a handle (`@channel`) and transcribe its eligible videos, either
  synchronously (small batches) or as a resumable background job with progress,
  cancel, resume and CSV download.
- **Output modes**: `original` (Original Spoken: verbatim captions/speech in the
  spoken language, never translated), `en` (Simple English), `hi` (Simple Hindi).
- **Fallback**: videos without captions are transcribed from their audio with Groq
  Whisper (`whisper-large-v3`).

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

All `/api/*` routes require authentication except `GET /api/health` and
`POST /api/auth/login|logout`. Send a session cookie (browser) or `X-API-Key`.

| Method & path | Purpose |
| --- | --- |
| `GET /api/health` | Health: 200 `ok` or 503 `unhealthy` (signed-in callers get details) |
| `POST /api/auth/login` / `logout`, `GET /api/auth/me` | Session cookie auth; `me` also returns server limits |
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
| `AUTH_USERS` / `API_KEYS` | production (one of) | `user:role:scrypt-hash` / `name:role:sha256` |
| `CORS_ORIGINS` | no | Only for cross-origin browser clients; never `*` |
| `DATA_DIR` | no | Persistent data (`./data`; `/app/data` in Docker) |
| `MAX_VIDEOS_PER_JOB`, `MAX_VIDEOS_SYNC_EXPORT`, `MAX_ACTIVE_JOBS[_PER_USER]` | no | Abuse/cost limits (100, 25, 4/2) |
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
API_KEY=<raw key named "ci"> tests/deployment/stack_test.sh                    # restart/backup/restore on a stack
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
- **Channel eligibility**: only 3–30 minute, non-live videos are transcribed.
- **YouTube may block caption fetching** from some server IPs (cloud ranges); videos
  then fall back to speech-to-text if `GROQ_API_KEY` is set.
- **Two transcript pipelines.** Single videos and channel jobs use different (but
  equivalent-mode) pipelines and separate caches; output text can differ slightly in
  cleaning.
- **Speech-to-text limits**: audio over Groq's 25 MB upload limit is not downloaded, so
  speech-to-text fails for videos longer than roughly 45–60 minutes.
- The `/api/transcript/metrics` counters are in-memory and reset on restart.
