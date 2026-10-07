# Security Policy

## Supported Versions

Use this section to tell people about which versions of your project are
currently being supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 5.1.x   | :white_check_mark: |
| 5.0.x   | :x:                |
| 4.0.x   | :white_check_mark: |
| < 4.0   | :x:                |

## Reporting a Vulnerability

Use this section to tell people how to report a vulnerability.

Tell them where to go, how often they can expect to get an update on a
reported vulnerability, what to expect if the vulnerability is accepted or
declined, etc.

## Authentication model

Every `/api/*` request is denied unless it carries valid credentials. The only
exceptions are `GET /api/health` (coarse status only) and `POST /api/auth/login|logout`.

| Client | Credential | How it is configured |
| ------ | ---------- | -------------------- |
| Browser (SPA) | `session` cookie (HttpOnly, SameSite=Strict, Secure in production), issued by `POST /api/auth/login` | `AUTH_USERS=username:role:scrypt-hash` |
| Scripts / services | `X-API-Key: ysk_...` header | `API_KEYS=name:role:sha256` |

- Roles: `admin` (operational endpoints such as `/api/metrics`, `/api/quota`,
  `/api/cache/stats`, and every user's jobs) and `user` (only the jobs they created).
- Only hashes are stored in configuration. Generate entries with
  `python scripts/hash_secret.py user|apikey|jwt-secret`.
- Removing a user from `AUTH_USERS` revokes their sessions on the next request.
  Rotating `JWT_SECRET_KEY` revokes all sessions.
- With `APP_ENV=production` the app refuses to start when `JWT_SECRET_KEY` is
  missing or weak, no credentials are configured, or `CORS_ORIGINS` contains `*`.
- Rate limits are per principal (`API_RATE_LIMIT_PER_MINUTE`, `COSTLY_RATE_LIMIT_PER_MINUTE`)
  and per IP for login. Repeated failed logins lock that username for 15 minutes.
  The limits are held in process memory: with several workers or replicas, each one
  enforces its own budget.

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
