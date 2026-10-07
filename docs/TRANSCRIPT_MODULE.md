# Transcript module

How a transcript is produced, how the output modes work, and how background jobs
behave. Everything here describes the code as it is; file references are the source
of truth.

## Output modes

| `output_language` | Name in the UI | Result |
| --- | --- | --- |
| `original` (alias `original_spoken`) | Original Spoken | The caption/speech text **in the spoken language**, verbatim. Never translated, never transliterated. |
| `en` | Simple English | The original text rewritten into simple English by the Groq LLM. |
| `hi` | Simple Hindi | The original text rewritten into simple Hindi (Devanagari) by the Groq LLM. |

Rules that hold in every path:

1. The original text is always acquired first and kept (`raw_transcript` in API
   responses and job items); `en`/`hi` are derived from it by an explicit translation
   step (`services/translation/service.py`).
2. If translation fails, the original text is returned **labelled with its real
   language** and `fallback_to_original: true` (single video) or the item keeps
   `transcript == raw_transcript` (jobs). It is never mislabelled as English.
3. Caption selection prefers the **spoken** language: YouTube's auto-generated (ASR)
   track reveals what is spoken, and manual tracks in other languages are treated as
   translations (`clients/youtube_transcript_client.py`, `_spoken_language_code`).
4. Speech-to-text always *transcribes* (never Whisper's `translate` task), so it also
   yields the spoken language.

Verified live on 2026-10-07 (`tests/live`): an English and a Hindi video in `original`
returned verbatim captions (Hindi stayed in Devanagari); `en` and `hi` produced
Simple English / Simple Hindi from the Hindi source.

## Single video: `POST /api/transcript`

`services/transcription/service.py` (`TranscriptService.get_canonical_transcript`):

1. Resolve the video id from the URL/ID (`services/youtube/resolver.py`).
2. Cache lookup in `DATA_DIR/transcripts/canonical/<video_id>.json`. Concurrent
   requests for the same uncached video wait for one acquisition (striped locks).
3. YouTube captions (`services/youtube/captions.py`).
4. No captions → download audio with yt-dlp (≈50–70 kbps, ≤ 25 MB) → Groq Whisper
   (`services/transcription/groq.py`).
5. Clean/normalise (`services/transcription/cleaner.py` → `pipeline/`), validate
   quality (`validator.py`), cache.
6. `en`/`hi`: translate (cached per video and mode).

Errors map to fixed public codes (`services/public_errors.py`), e.g.
`INVALID_YOUTUBE_URL` (400), `CAPTIONS_DISABLED` (404), `RATE_LIMITED` (429),
`STT_UNAVAILABLE` (503, Groq key missing/invalid), `STT_TIMEOUT` (504),
`TRANSLATION_FAILED` (502). Provider exception text never reaches clients.

## Channels

### Discovery (both channel paths)

YouTube Data API: resolve the handle → uploads playlist → page through video ids →
fetch metadata in batches of 50 → keep videos of **3–30 minutes that are not live**
(`services/duration_filter.py`), optionally within `published_after/before`.

### Synchronous: `GET /api/channel/{handle}/transcripts`, `POST /api/transcript/export`

Bounded by `MAX_VIDEOS_SYNC_EXPORT` (default 25) and by
`MAX_CONCURRENT_SYNC_CHANNEL_RUNS` (default 2, server-wide, else 429). Each video goes
through `services/transcript_service.py` (below), then the output mode is applied.
Use background jobs for anything larger.

### Background jobs: `POST /api/channel/{handle}/transcript-job`

`services/jobs/transcript_job_manager.py`.

Per video: manual captions → auto captions → (if no captions and STT enabled) audio +
Groq Whisper via `providers/whisper_provider.py` with `clients/groq_stt_client.py`
(`STT_BACKEND=groq`; `local` uses faster-whisper and needs extra packages). Then the
output mode is applied and the job is checkpointed.

Videos are processed **sequentially** with global pacing (`TRANSCRIPT_REQUEST_INTERVAL`,
default 2.5 s) and adaptive cooldowns when YouTube rate-limits caption requests
(`services/transcript_limiter.py`). After 4 consecutive rate limits the job pauses.

Limits: `MAX_VIDEOS_PER_JOB` (100), `MAX_ACTIVE_JOBS` (4 server-wide),
`MAX_ACTIVE_JOBS_PER_USER` (2). Exceeding them returns 429 `JOB_LIMIT_REACHED`.

#### Lifecycle

```text
queued ─► running ◄─► cooldown ─► completed | failed | cancelled
             │
             └─► paused   (persistent rate limiting, server shutdown, or crash)
```

| Event | Effect |
| --- | --- |
| Every video processed | Checkpoint written atomically (`DATA_DIR/transcript_jobs/<job_id>.json`) |
| `POST …/cancel` | Status `cancelled`; the running task stops after the current video |
| `POST …/resume` | Retries `pending`, `processing` (interrupted), `rate_limited` and `temporary_error` items with a fresh retry budget; re-runs discovery if it never finished |
| Graceful shutdown (deploy, `docker compose stop`) | Running jobs checkpointed as `paused` |
| Crash / kill | On startup, jobs left `queued`/`running`/`cooldown` become `paused` |
| Job finished | Leaves the in-memory active set; a small LRU (`JOB_MEMORY_CACHE_SIZE`) keeps recent jobs, others are read from disk |
| Not updated for `JOB_RETENTION_DAYS` (30) | Checkpoint deleted (startup + hourly). Running jobs are never deleted |

Ownership: every job records the authenticated principal that created it. Only that
user (or an admin) can read, cancel, resume or download it; for anyone else it does not
exist (no way to probe other users' job ids).

## CSV export

Columns: `video_id, video_url, channel_id, channel_title, title, published_at,
duration_seconds, duration, language, status, transcript, source, method, error_code,
error_message` (+ `output_format, source_language, source_language_code` with
`audit=true` / `include_audit_columns`). Files are UTF-8 with BOM (Excel friendly).
Every cell starting with `= + - @`, tab or CR is prefixed with `'`
(`services/csv_safety.py`), so titles like `=HYPERLINK(...)` cannot execute in
spreadsheets.

## Storage

```text
DATA_DIR/
  transcripts/                 channel/job pipeline cache: <video_id>.json,
                               translations: <video_id>_<mode>_simple.json
  transcripts/canonical/       single-video pipeline cache
  transcript_jobs/             <job_id>.json checkpoints (12 hex chars, validated)
  tmp/audio/                   audio downloads, deleted after transcription
```

All file names are built only from validated ids and must resolve inside `DATA_DIR`;
writes are atomic (temp file + rename).
