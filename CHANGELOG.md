# Changelog

All notable changes to this project will be documented in this file.

---

## [Unreleased] - 2026-10-08

- **Login abuse**: failed logins no longer lock an account for everyone. Per-IP rate,
  per-(IP, username) exponential backoff (2 s doubling, 15 min cap, cleared on success)
  and a per-username cross-IP rate (`LOGIN_USER_RATE_LIMIT_PER_MINUTE`).
- **Request bodies**: API cap 64 KiB; chunked bodies without `Content-Length` refused (411).
- **Long videos**: audio over Groq's 25 MB limit is chunked with ffmpeg and merged
  (all-or-nothing); `STT_MAX_AUDIO_SECONDS` (default 2 h) checked before download;
  new `AUDIO_TOO_LONG` error. Both pipelines share one audio downloader (unique temp
  files under `DATA_DIR/tmp/audio`, cleaned at startup).
- **Channels**: `max_videos` now counts eligible videos (previously a channel whose newest
  uploads were Shorts or long lectures returned nothing); one shared discovery
  implementation; configurable window `CHANNEL_MIN/MAX_VIDEO_SECONDS` (default unchanged,
  3:00–30:00) and `CHANNEL_DISCOVERY_SCAN_CAP`; the UI shows the configured window.
- **Translation**: a part cut off at the model's output limit is split and retried
  instead of dropping the whole translation; gpt-oss models use `reasoning_effort=low`.
- **Fixes**: YouTube client read its API key at import time (rotated keys and tests
  silently used a stale key); lost checkpoints are logged as errors.
- **Verification**: live tests cover Groq Whisper (single upload vs forced chunks) and
  require real channel transcripts; stack test covers `down`/`up`, rollback and
  cross-user isolation through nginx.

## [v3.0.0] - 2026-10-07

### Transcript-only, single-instance production release

- **Scope**: the transcript module is the only product. Removed the channel metadata
  export (MVP 1) API and UI, legacy transcript routes, the unused observability /
  reliability / security frameworks, and their dependencies (pandas, redis,
  prometheus-client, Celery/Postgres stack, OpenTelemetry).
- **Security**: client-safe error codes everywhere, strict input validation, active-job
  and synchronous-run caps, path containment for all stored files, CSV formula-injection
  protection, PyJWT-only sessions, Groq misconfiguration no longer signs users out.
- **Architecture**: validated settings with `DATA_DIR`; jobs survive crashes/restarts
  (recovered as paused), finished jobs leave memory, retention cleanup, bounded caches;
  channel jobs use hosted Groq Whisper (the old local fallback never worked in
  production); chunked translation that rejects truncated output.
- **Deployment**: one Dockerfile (SPA + API, one worker), app + nginx compose stack with
  TLS, deploy with health gate and automatic rollback, verified backup/restore, CI that
  runs the real stack. Pinned dependencies (`requirements.lock`); `anyascii` was missing.
- **Observability**: request/job/user ids on every log line, JSON logs, access log, job
  outcome lines.
- **Fixes**: resuming rate-limited jobs, discovery restarts on resume, single-video cache
  isolation for Original Spoken, stale CSV export language in the UI.
- **Breaking**: `/api/export*`, `/api/metrics`, `/api/quota`, `/api/cache/stats`,
  `/api/video-metadata/*`, `/api/transcriptv2/*`, `/api/transcript/{id}[/…]` and
  `/api/process-transcript` are gone; job `caption_concurrency`/`whisper_concurrency`
  are ignored; default limits are 100 videos per job and 25 per synchronous request.

---

## [v2.1.0] - 2026-09-21

### Architectural Refactor: Complete Removal of MVP #3 (Video URL → AI Blog)

- **Permanently Eliminated MVP #3 Workflow**:
  - Completely removed all blog, SEO generation, prompt security, and content analysis modules.
  - Deleted 22 Category A files spanning models (`blog_export.py`, `blog_generation.py`, `blog_review.py`, `content_analysis.py`, `seo_package.py`), schemas (`analysis_response.py`), exceptions (`blog_errors.py`, `seo_errors.py`, `analysis_errors.py`), security guardrails (`prompt_security.py`), fixtures, and mocks (`golden_outputs.yaml`, `mock_llm_provider.py`).
  - Purged LLM exception hierarchy (`LLMProviderException`, `LLMAuthenticationException`, `LLMRateLimitException`, `LLMContextLengthException`, `LLMTimeoutException`).
  - Purged unused LLM provider keys (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`) from environment configuration; retained `OPENAI_API_KEY` strictly for Whisper STT audio fallback in MVP 2.
  - Cleaned user analytics metrics (removed `blogs_generated` counter and `record_blog_generated`).
  - Removed document export dependencies (`python-docx`, `fpdf2`) from `requirements-server.txt`.
- **Frontend Streamlining**:
  - Removed AI Blog workflow cards and navigation from React frontend (`Home.tsx`, `Docs.tsx`, `Footer.tsx`).
  - Updated home page to a clean, balanced 2-column layout focusing on MVP 1 (Channel Metadata Export) and MVP 2 (Video Transcript Extraction with Whisper STT fallback).
  - Cleaned TypeScript types in `frontend/src/types/index.ts` (removed Phase 6 content analysis and blog types).
  - Cleaned Nginx configuration (`api.conf`).
- **Retained & Verified Supported Workflows**:
  - **MVP 1**: `YouTube Channel Handle → Channel/Video Metadata`
  - **MVP 2**: `YouTube Video URL → Transcript` (with 8-Stage NLP & Hinglish Normalization)

---

## [v2.0.0] - 2026-06-30

### Major Features

#### Transcript Engine (3-Stage Fallback Pipeline)
- **Manual Transcript Provider** — fetches manually uploaded captions via youtube-transcript-api
- **Auto Transcript Provider** — falls back to auto-generated captions
- **Whisper STT Provider** — final fallback using yt-dlp + faster-whisper for audio transcription
- **Transcript Repository** — in-memory TTL cache + JSON file persistence
- **Text Cleaner** — Unicode normalization, punctuation fixes, paragraph detection
- **Language Detector** — langdetect library + heuristic fallback
- **Read Time Calculator** — WPM-based reading time estimation

#### YouTube API Layer Enhancements
- **SSL/TLS Resilience** — httplib2 patched with certifi CA bundle, stale connection cleanup, proper timeouts
- **SSL Retry Logic** — outer retry loop for SSL/connection errors in video_service
- **YouTube URL Parser** — robust parsing for all YouTube URL formats
- **Rich Metadata Service** — cached video metadata retrieval with 600s TTL

#### React SPA Frontend
- **Complete rewrite** from single-page to multi-page SPA with React Router v7
- **Home Page** — Clean workflow cards (Metadata Export, Transcript Engine)
- **Metadata Export Page** — channel input, progress polling, result display, CSV download
- **Transcript Page** — URL input, metadata display, pipeline visualization, transcript viewer
- **Dark Mode** — ThemeContext with localStorage persistence
- **Reusable UI Components** — Button, Card, Badge, Container

#### Web Application Enhancements
- New API endpoints: `/api/validate-url`, `/api/video-metadata/{id}`, `/api/transcript/{id}`
- SPA catch-all routing for React frontend
- JSON response support for all API endpoints

#### New Models
- `VideoURLResult` — parsed YouTube URL result
- `VideoMetadata` — rich video metadata with duration, thumbnails, stats
- `TranscriptResult` — transcript with pipeline steps, word count, language

#### New Utilities
- `ssl_config.py` — certifi SSL context configuration
- `http_client.py` — requests wrapper with retry/logging
- `cache.py` — generic TTL cache
- `date_formatter.py` — ISO to localized + relative date formatting
- `number_formatter.py` — human-readable number formatting (1.5K, 1.6M)
- `thumbnail.py` — thumbnail URL extraction by quality
- `url_helpers.py` — YouTube URL parsing/normalization helpers

### Dependencies & Configuration
- Updated `requirements.txt` with pinned compatible versions
- Added `node_modules/` to `.gitignore`
- Updated frontend `package.json` with react-router-dom v7
- Updated `vite.config.ts` for SPA build

### Testing
- 11 new test files covering transcript models, providers, repository, service, utils
- HTTP client, SSL config, YouTube client, URL parser, metadata tests

### Files Modified (17)
| File | Change |
|------|--------|
| `api/youtube_client.py` | SSL patching, certifi, exception hierarchy |
| `api/video_service.py` | SSL retry logic |
| `webapp/main.py` | New API endpoints, SPA routing |
| `frontend/src/App.tsx` | SPA rewrite with React Router |
| `frontend/src/types/index.ts` | Full TypeScript type definitions |
| `frontend/src/index.css` | Updated styles |
| `frontend/vite.config.ts` | SPA build config |
| `frontend/package.json` | Added react-router-dom |
| `frontend/index.html` | Updated for SPA |
| `frontend/package-lock.json` | Dependency lock update |
| `services/__init__.py` | Exports for new services |
| `models/__init__.py` | Exports for new models |
| `utils/__init__.py` | Exports for new utilities |
| `requirements.txt` | Pinned deps, new packages |
| `tests/test_video_discovery.py` | Minor test updates |
| `.gitignore` | Added node_modules/ |

### Files Added (87)
| Directory | Contents |
|-----------|----------|
| `clients/` | YouTube transcript client, Whisper client |
| `exceptions/` | 13 transcript error classes, YouTube error classes |
| `interfaces/` | TranscriptProvider ABC, SpeechToText ABC |
| `providers/` | Manual, Auto, Whisper transcript providers |
| `repositories/` | Transcript repository with caching |
| `schemas/` | Transcript request/response schemas |
| `services/` | Transcript service, URL parser, metadata service |
| `models/` | Transcript, VideoMetadata, VideoURL models |
| `utils/` | Cache, HTTP client, SSL config, text cleaner, etc. |
| `frontend/src/components/` | 20+ React components (blog, layout, metadata, transcript, ui, workflow) |
| `frontend/src/pages/` | 12 page components |
| `frontend/src/context/` | Workflow state context |
| `frontend/src/theme/` | Dark mode theme context |
| `tests/` | 11 test files |
| Root | `run_transcript.py`, `plan2.md`, `start_server.bat` |

---

## [v1.0.0] - Initial Release

### Features
- YouTube Data API v3 authentication with SSL-patched httplib2
- Channel handle resolution (with @ prefix normalization)
- Video discovery via uploads playlist with automatic pagination
- Batched metadata fetch (50 IDs/batch) with deduplication
- ISO 8601 duration parsing, Short/Video classification
- CSV export (batch + streaming for large channels)
- End-to-end pipeline (phases 3-6)
- FastAPI web application with Jinja2 templates
- Legacy dashboard with export progress and result views
