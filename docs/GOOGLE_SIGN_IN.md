# Google sign-in ("Continue with Google")

Optional second way to sign in, next to username/password. It is **off** until the
`GOOGLE_*` settings below are complete; the login page only shows the Google button
when the server reports it enabled (`GET /api/auth/providers`).

## How it works

```text
Browser ── click "Continue with Google" ──► GET /api/auth/google/start
   server: new state + nonce + PKCE verifier (single use, 10 min, kept server-side)
           state also set in an HttpOnly cookie (binds the attempt to this browser)
   302 ──► accounts.google.com (scopes: openid email profile — nothing else)
Google ── user picks account / consents ──► GET /api/auth/google/callback?code=…&state=…
   server: state = cookie = unused server entry            (CSRF / replay)
           code → token exchange with client secret + PKCE (server-side only)
           ID token: RS256 signature (Google JWKS), iss, aud, azp, exp, iat, nonce
           email_verified, domain policy, account not disabled
           find-or-create local account by Google "sub"     (DATA_DIR/users/google_users.json)
           role = admin only if sub ∈ GOOGLE_ADMIN_SUBJECTS, else user
           existing session JWT in the existing HttpOnly cookie
   303 ──► /transcript  (failure: /transcript?auth_error=<cancelled|not_allowed|unavailable|rate_limited|failed>)
```

After that the request path is identical to a password login: same JWT, cookie,
expiry (`SESSION_TTL_MINUTES`), CSRF checks, rate limits, job ownership and admin
checks. Google access/refresh tokens are never stored or sent to the browser.

Google users are identified by Google's stable `sub`, never by email. Their local id
(the owner of their jobs) is `google:<sub>`.

## Routes

| Route | Purpose |
| --- | --- |
| `GET /api/auth/providers` | Public: `{"password": bool, "google": bool}` for the login page |
| `GET /api/auth/google/start` | Public: begins the flow (302 to Google) |
| `GET /api/auth/google/callback` | Public: **the OAuth redirect URI path** |

## Redirect URIs

The redirect URI must be registered in Google **and** set in `GOOGLE_REDIRECT_URI`,
byte for byte, on the **same host the browser uses** (the session cookie belongs to
that host). Open the app on exactly that host (`localhost`, not `127.0.0.1`).

| Where | App URL in the browser | `GOOGLE_REDIRECT_URI` |
| --- | --- | --- |
| Local, Vite dev server (`make dev-web` + `make dev-api`, or `start.ps1`; Vite proxies `/api` to `:8000`) | `http://localhost:5173` | `http://localhost:5173/api/auth/google/callback` |
| Local, API serving the built SPA (`uvicorn webapp.main:app --port 8000`) | `http://localhost:8000` | `http://localhost:8000/api/auth/google/callback` |
| Production (nginx → app, see `docker/nginx/nginx.conf`) | `https://<your-domain>` | `https://<your-domain>/api/auth/google/callback` |

Local http works only with `APP_ENV=development` (production requires https and sets
`Secure` cookies). `<your-domain>` is the real domain once chosen; it is not configured
anywhere in this repository yet.

## Google Cloud console setup

1. **Project**: <https://console.cloud.google.com/> → select or create a project
   (if your organisation uses Google Workspace, create it inside that organisation).
2. **Google Auth Platform** → *Get started*: app name (e.g. "YouTube Transcripts"), user
   support email, then **Audience**:
   - **Internal** — only accounts of your Workspace organisation can sign in (available
     only for projects in a Workspace organisation). Recommended for an internal tool.
   - **External** — any Google account *that also passes this app's domain policy*.
     While the publishing status is *Testing*, only listed **test users** (up to 100)
     can sign in; add them under *Audience → Test users*. Publish to *In production*
     for general use (the identity-only scopes are non-sensitive, so no Google security
     review is needed; Google may ask to verify the brand).
3. **Branding**: app name, support email, logo (optional), application home page,
   privacy policy and terms links (needed to publish an External app), and under
   **Authorized domains** add your production domain (e.g. `example.com` for
   `transcripts.example.com`). `localhost` needs no entry.
4. **Data Access**: add only the scopes `openid`, `.../auth/userinfo.email` and
   `.../auth/userinfo.profile`. Do **not** add Gmail, Drive, YouTube, Calendar,
   Contacts or any other scope; the app requests only `openid email profile`.
5. **Clients** → *Create client* → type **Web application**:
   - *Authorized JavaScript origins*: not needed (the flow is server-side).
   - *Authorized redirect URIs*: add each URI from the table above that you use, e.g.
     `http://localhost:5173/api/auth/google/callback` and
     `https://<your-domain>/api/auth/google/callback`.
   - Create, then copy the **Client ID** and **Client secret** (the secret is shown
     once; store it in your password manager).
6. Put the values in `.env` (never in git; `.env` is git-ignored) and restart the app.

## Configuration

| Variable | Notes |
| --- | --- |
| `GOOGLE_CLIENT_ID` | From step 5 |
| `GOOGLE_CLIENT_SECRET` | From step 5. Secret: `.env` / secret store only |
| `GOOGLE_REDIRECT_URI` | Exactly one of the registered redirect URIs (table above) |
| `GOOGLE_ALLOWED_DOMAINS` | Comma list of **Google Workspace** domains. The ID token's verified `hd` claim must equal one of them. The email domain alone is never enough: a personal Google account created on `alice@<company>` has no `hd` and is refused |
| `GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS` | Optional comma list of email domains whose **personal** Google accounts (no `hd`) may sign in, e.g. `gmail.com`. Must not repeat a Workspace domain (refused at startup). Empty = personal accounts refused |
| `GOOGLE_ALLOW_ANY_ACCOUNT` | `true` to accept any verified Google account instead (public sign-up: anyone could spend your YouTube/Groq quota) |
| `GOOGLE_ADMIN_SUBJECTS` | Comma list of Google `sub` ids that get the admin role |
| `GOOGLE_AUTH_RATE_LIMIT_PER_MINUTE` | Starts + callbacks per client IP (default 20) |

At least one of `GOOGLE_ALLOWED_DOMAINS`, `GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS` or
`GOOGLE_ALLOW_ANY_ACCOUNT=true` is required: there is no silent "anyone can sign up" default.

Account policy, decided on every sign-in and re-checked on every request:

| Account | `hd` claim | Accepted when |
| --- | --- | --- |
| Workspace member `alice@company.com` | `company.com` | `company.com` is in `GOOGLE_ALLOWED_DOMAINS` |
| Workspace member of another organisation | `other.com` | never (unless `GOOGLE_ALLOW_ANY_ACCOUNT=true`) |
| Personal account on a company address `alice@company.com` | none | never: not a member of the Workspace, even though Google verified the email |
| Personal account `bob@gmail.com` | none | `gmail.com` is in `GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS` |

Every Google account, Workspace or personal, gets the `user` role; only
`GOOGLE_ADMIN_SUBJECTS` grants admin. Incomplete or invalid settings disable Google
sign-in with a warning in development and **refuse to start** in production
(the log names the problem, never the values).

Local example (`.env`, values elided):

```dotenv
APP_ENV=development
GOOGLE_CLIENT_ID=<client id>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<client secret>
GOOGLE_REDIRECT_URI=http://localhost:5173/api/auth/google/callback
GOOGLE_ALLOWED_DOMAINS=<your-company-domain>
```

## Accounts and roles

- **First sign-in** creates the account (`role` = `user`). Nothing from the browser can
  change the role: not a query parameter, not a token claim.
- **Admins**: sign in once with Google, find your `sub` in
  `DATA_DIR/users/google_users.json` (or the log line
  `User google:<sub> signed in with Google ...`), add it to `GOOGLE_ADMIN_SUBJECTS`,
  recreate the app (`docker compose up -d app`). Removing it demotes the account on its
  next request; no new sign-in is needed. The existing password admin (`AUTH_USERS`) is
  unaffected and keeps working.
- **Disable an account**: set `"disabled": true` for its `sub` in `google_users.json`
  while the app is stopped (it rewrites the file on every Google sign-in):
  ```bash
  docker compose stop app
  docker compose run --rm --no-deps --entrypoint python app - <<'PY'
  import json; p = "/app/data/users/google_users.json"
  d = json.load(open(p)); d["users"]["<sub>"]["disabled"] = True
  json.dump(d, open(p, "w"), indent=2)
  PY
  docker compose start app
  ```
  Its sessions stop working immediately after the restart.
- Narrowing `GOOGLE_ALLOWED_DOMAINS` / `GOOGLE_ALLOWED_PERSONAL_EMAIL_DOMAINS`, or
  turning Google sign-in off, also ends the affected Google sessions on their next
  request. Accounts recorded before the `hosted_domain` field existed count as personal
  until they sign in again.
- **Sign out** ends the session on the server too: a copy of the cookie stops working
  immediately (until the next app restart, which clears the in-memory sign-out list;
  sessions still expire after `SESSION_TTL_MINUTES`).
- The account file is in the data volume, so `scripts/backup.sh` / `restore.sh` include it.

## Security summary

State (256-bit, single use, 10-minute expiry, bound to an HttpOnly `SameSite=Lax` cookie
scoped to `/api/auth/google`), PKCE S256, nonce, server-side code exchange, ID-token
signature/issuer/audience/authorized-party/expiry/issued-at checks, verified email,
domain policy, per-IP rate limit (plus nginx's per-IP limit), fixed client-visible
error codes, no tokens/codes/secrets in logs (nginx logs the callback without its
query string). The resulting session is the existing one; no second token system.

## Status

| Item | State |
| --- | --- |
| Code + automated tests (Google mocked) | Ready |
| Local sign-in with a real Google client | Needs your OAuth client (steps above) |
| Google Cloud configuration | Required (manual) |
| Production domain + https redirect URI | Required (domain not chosen yet) |
| Real browser test against Google | Required before go-live |
