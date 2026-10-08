# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately to the repository owner (do not open a
public issue). Include the affected endpoint or file, reproduction steps and impact.

## Controls at a glance

| Control | Where | Behaviour |
| --- | --- | --- |
| Authentication | `security/web_auth.py` (`AuthMiddleware`) | Deny by default on `/api/*`; session cookie or `X-API-Key` |
| Authorization | `webapp/main.py` (`can_access_owned`) | Jobs belong to their creator; admins see all; others get "not found" |
| CSRF | `WebAuthenticator.origin_allowed` | Cookie-authenticated unsafe methods need a trusted/same-host `Origin` |
| CORS | `cors_options`, `CORS_ORIGINS` | Explicit origins only; `*` refused in production; same-origin needs none |
| Rate limits | `AuthMiddleware`, nginx `limit_req` | Per user (`API_RATE_LIMIT_PER_MINUTE`, `COSTLY_RATE_LIMIT_PER_MINUTE`), per IP for login and at nginx |
| Resource caps | `config/settings.py` | `MAX_VIDEOS_PER_JOB`, `MAX_VIDEOS_SYNC_EXPORT`, `MAX_ACTIVE_JOBS[_PER_USER]`, `MAX_CONCURRENT_SYNC_CHANNEL_RUNS` |
| Input validation | Pydantic models / path patterns | Strict video/job/handle ids, output modes, ISO dates, body ≤ 64 KiB in the app (`infrastructure/validation.py`; chunked bodies without a length refused) and ≤ 1 MB at nginx |
| Path safety | `TranscriptRepository._file_for`, `TranscriptJobManager._job_path` | Files only from validated ids, resolved path must stay inside `DATA_DIR` |
| Error hygiene | `services/public_errors.py`, global handler | Fixed client messages + `trace_id`; details only in server logs |
| CSV injection | `services/csv_safety.py` | Cells starting with `= + - @` / tab / CR are prefixed with `'` |
| Secret redaction | `infrastructure/log_redaction.py` | API keys, tokens, cookies, passwords removed from every log line |
| Transport | `docker/nginx/nginx.conf` | TLS 1.2/1.3, HSTS, CSP, HTTP→HTTPS |
| Container | `Dockerfile`, `docker-compose.yml` | Non-root, `cap_drop: ALL`, `no-new-privileges`, app port not published |
## Authentication model

Every `/api/*` request is denied unless it carries valid credentials. The only
exceptions are `GET /api/health` (coarse status only) and `POST /api/auth/login|logout`.

| Client | Credential | How it is configured |
| ------ | ---------- | -------------------- |
| Browser (SPA) | `session` cookie (HttpOnly, SameSite=Strict, Secure in production), issued by `POST /api/auth/login` | `AUTH_USERS=username:role:scrypt-hash` |
| Scripts / services | `X-API-Key: ysk_...` header | `API_KEYS=name:role:sha256` |

- Roles: `admin` (operational endpoints `/api/transcript/metrics` and
  `/api/transcript/limiter/status`, and every user's jobs) and `user` (only the jobs they created).
- Only hashes are stored in configuration. To configure a production `.env` in one step
  (admin password prompt, generated `JWT_SECRET_KEY`, optional admin API key, explicit
  `CORS_ORIGINS`, `APP_ENV=production`, backup + validation against the startup checks):
  `python scripts/configure_env.py --origin https://your.domain [--api-key-name ops]`.
  Individual entries can be generated with `python scripts/hash_secret.py user|apikey|jwt-secret`.
  The `.env.backup-*` file it leaves contains the previous secrets; delete it once verified.
- Removing a user from `AUTH_USERS` revokes their sessions on the next request.
  Rotating `JWT_SECRET_KEY` revokes all sessions.
- `APP_ENV` must be exactly `development` (the default) or `production`; any other
  value (e.g. `prod`, `staging`) stops startup instead of silently getting development rules.
- With `APP_ENV=production` the app refuses to start when `JWT_SECRET_KEY` is
  missing or weak, no credentials are configured, or `CORS_ORIGINS` contains `*` or
  anything other than `https://host[:port]` origins (plain `http://`, including localhost,
  is rejected).

### Local development vs. production origins

| | `APP_ENV` | `CORS_ORIGINS` |
| --- | --- | --- |
| Local (Vite dev server, `frontend/vite.config.ts`) | `development` | `http://localhost:5173` |
| Production | `production` | your real `https://` origin(s), comma-separated |

The Vite dev server proxies `/api` to `http://localhost:8000`, so local HTTP is confined to
the loopback interface. When a real domain exists, re-run
`python scripts/configure_env.py --origin https://<real-domain>` (production is the default
mode); it replaces `CORS_ORIGINS` and refuses `*` or `http://` origins.
- Rate limits are per principal (`API_RATE_LIMIT_PER_MINUTE`, `COSTLY_RATE_LIMIT_PER_MINUTE`)
  and, for login, per client IP (`LOGIN_RATE_LIMIT_PER_MINUTE`) and per username across all
  IPs (`LOGIN_USER_RATE_LIMIT_PER_MINUTE`). Failed logins never lock an account for everyone:
  after 3 failures from one IP for one username, that IP must wait 2 s, 4 s, 8 s, ... (capped at
  15 minutes) before trying that username again, while the owner can still sign in from their
  own address. A successful login clears the backoff (`security/login_throttle.py`).
  The limits are held in process memory, which is correct for the supported single-instance,
  single-worker deployment (do not scale out without moving them to a shared store).

## Rotating third-party API keys

The YouTube Data API and Groq keys grant paid or quota-limited access. Rotate them
immediately if they were ever exposed (shared machine, screenshots, logs, images,
chat), and on any staff change.

1. **YouTube Data API key** — Google Cloud Console → *APIs & Services* → *Credentials*.
   1. Create a new API key.
   2. Under *API restrictions* allow only **YouTube Data API v3**.
   3. Under *Application restrictions* choose **IP addresses** and list the server's egress IPs.
   4. Put the new key in the secret store / `.env` as `YOUTUBE_API_KEY` and restart the app.
   5. Check `GET /api/health` (signed in) reports `youtube_api_key_configured: true`, then
      **delete** the old key and confirm a request with it now fails.
2. **Groq API key** — <https://console.groq.com/keys>.
   1. Create a new key, set it as `GROQ_API_KEY`, and restart.
   2. Run one transcript that needs speech-to-text to confirm it works.
   3. **Revoke** the old key.
3. **App credentials** — regenerate `JWT_SECRET_KEY` and any `API_KEYS` that may have
   leaked (`python scripts/hash_secret.py ...`), then hand the new raw API keys to their clients.

Never commit `.env` (it is git-ignored and excluded from Docker images via
`.dockerignore`). `.env.example` is the committed template and must only contain placeholders.
