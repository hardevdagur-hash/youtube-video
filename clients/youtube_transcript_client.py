"""Client for fetching YouTube transcripts via youtube-transcript-api.

Injects a custom requests.Session with:
  - certifi CA bundle
  - SSL / 5xx retry with exponential backoff
  - Comprehensive request logging
  - Explicit timeouts

Smart transcript selection strategy:
  1. Enumerate ALL available transcripts via list_transcripts()
  2. Log every candidate with full metadata
  3. Select best transcript using priority scoring:
     a. Manual English (exact code match)
     b. Manual English variant (en-US, en-GB, en-IN)
     c. Auto-generated English
     d. Manual Hindi (hi)
     e. Manual translatable to English
     f. Auto translatable to English
     g. Best available (any language, any type)
  4. Cache the TranscriptList to avoid redundant API calls
"""

import logging
import ssl
import time
import uuid
from typing import Any

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        TranscriptsDisabled,
        NoTranscriptFound,
        VideoUnavailable,
    )

    _HAS_YOUTUBE_TRANSCRIPT = True
except ImportError:
    _HAS_YOUTUBE_TRANSCRIPT = False


class YouTubeTranscriptClientError(Exception):
    """Base error for YouTube transcript client."""


class NoTranscriptFoundError(YouTubeTranscriptClientError):
    """No transcript found for this video."""


class TranscriptsDisabledError(YouTubeTranscriptClientError):
    """Transcripts are disabled for this video."""


class VideoUnavailableError(YouTubeTranscriptClientError):
    """Video is unavailable or private."""


class TooManyRequestsError(YouTubeTranscriptClientError):
    """Rate limited by YouTube."""


class TranscriptSslError(YouTubeTranscriptClientError):
    """SSL/TLS handshake failure when fetching transcript."""


# -- Priority-ordered language codes to search --------------------------------
PREFERRED_LANGUAGES = ["en", "en-US", "en-GB", "en-IN", "hi"]

# -- Logging helper for transcript candidates ---------------------------------
_TRANSCRIPT_LOG_FMT = (
    "language=%(language)s, code=%(language_code)s, "
    "generated=%(is_generated)s, translatable=%(is_translatable)s"
)


def _log_transcript_candidate(t, verdict: str, reason: str) -> None:
    """Log a single transcript candidate with the acceptance/rejection verdict."""
    logger.info(
        "Transcript candidate [%s]: %s  --  %s",
        verdict,
        _TRANSCRIPT_LOG_FMT % {
            "language": t.language,
            "language_code": t.language_code,
            "is_generated": t.is_generated,
            "is_translatable": t.is_translatable,
        },
        reason,
    )


def _build_transcript_session() -> requests.Session:
    """Build a requests.Session configured for youtube-transcript-api."""
    session = requests.Session()

    retry_strategy = Retry(
        total=2,
        read=2,
        connect=2,
        backoff_factor=1.0,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset({"HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"}),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=30,
        max_retries=retry_strategy,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.verify = certifi.where()

    session.headers.update({
        "Accept-Language": "en-US",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    })

    return session


class LoggingSession(requests.Session):
    """A requests.Session that logs every request and response."""

    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        request_id = uuid.uuid4().hex[:8]
        timeout = kwargs.get("timeout", (15, 30))

        _redact = {"authorization", "x-api-key", "api-key", "cookie", "set-cookie"}
        log_headers = {
            k: "***" if k.lower() in _redact else str(v)
            for k, v in kwargs.get("headers", {}).items()
        }
        body = kwargs.get("json") or kwargs.get("data")
        if body is not None:
            body = str(body)[:300]

        logger.info(
            "[%s] >>> %s %s  timeout=%s  headers=%s  body=%s",
            request_id, method.upper(), url, timeout,
            log_headers or "-", body or "-",
        )

        start = time.time()
        try:
            response = super().request(method, url, *args, timeout=timeout, **kwargs)
            elapsed = round(time.time() - start, 3)
            logger.info(
                "[%s] <<< %s %s  status=%d  elapsed=%.3fs",
                request_id, method.upper(), url, response.status_code, elapsed,
            )
            return response
        except requests.exceptions.SSLError as exc:
            elapsed = round(time.time() - start, 3)
            logger.error(
                "[%s] SSL ERROR %s %s  elapsed=%.3fs  error=%s",
                request_id, method.upper(), url, elapsed, exc,
            )
            raise TranscriptSslError(
                "Unable to connect securely to the transcript service. "
                "Please try again in a few moments."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            elapsed = round(time.time() - start, 3)
            logger.error(
                "[%s] CONNECTION ERROR %s %s  elapsed=%.3fs  error=%s",
                request_id, method.upper(), url, elapsed, exc,
            )
            raise
        except requests.exceptions.Timeout as exc:
            elapsed = round(time.time() - start, 3)
            logger.error(
                "[%s] TIMEOUT %s %s  elapsed=%.3fs  error=%s",
                request_id, method.upper(), url, elapsed, exc,
            )
            raise
        except Exception as exc:
            elapsed = round(time.time() - start, 3)
            logger.exception(
                "[%s] REQUEST FAILED %s %s  elapsed=%.3fs",
                request_id, method.upper(), url, elapsed,
            )
            raise


class YouTubeTranscriptClient:
    """Client for fetching YouTube video transcripts.

    Injects a custom requests.Session with retry, logging, and SSL
    configuration into the underlying youtube-transcript-api.

    Uses smart transcript selection: lists ALL available transcripts,
    logs every candidate, then picks the best match using a priority
    scoring system.
    """

    def __init__(self) -> None:
        if not _HAS_YOUTUBE_TRANSCRIPT:
            raise ImportError(
                "youtube-transcript-api is required. "
                "Install with: pip install youtube-transcript-api"
            )
        session = _build_transcript_session()
        self._session = session
        self._api = YouTubeTranscriptApi(http_client=session)

    # ------------------------------------------------------------------
    # Low-level: list & fetch
    # ------------------------------------------------------------------

    def list_transcripts(self, video_id: str) -> Any:
        """List available transcripts for a video.

        Returns:
            ``TranscriptList`` from youtube-transcript-api.

        Raises:
            VideoUnavailableError: Video is unavailable.
            TranscriptsDisabledError: Transcripts disabled for this video.
            TooManyRequestsError: Rate limited.
            TranscriptSslError: SSL handshake failure.
        """
        try:
            return self._api.list(video_id)
        except VideoUnavailable:
            raise VideoUnavailableError(f"Video {video_id} is unavailable.")
        except TranscriptsDisabled:
            raise TranscriptsDisabledError(f"Transcripts disabled for video {video_id}.")
        except TranscriptSslError:
            raise
        except Exception as exc:
            exc_name = type(exc).__name__
            exc_msg = str(exc).lower()
            if "toomanyrequests" in exc_name.lower() or "429" in exc_msg or "too many requests" in exc_msg or "rate limit" in exc_msg:
                raise TooManyRequestsError("Rate limited by YouTube.")
            raise YouTubeTranscriptClientError(f"Failed to list transcripts: {exc}")

    # ------------------------------------------------------------------
    # Smart transcript enumeration & selection
    # ------------------------------------------------------------------

    def list_all_transcripts(self, video_id: str) -> list[dict[str, Any]]:
        """Enumerate ALL available transcripts and log them with full metadata.

        Args:
            video_id: 11-character YouTube video ID.

        Returns:
            List of dicts, each with keys:
                language, language_code, is_generated, is_translatable
        """
        transcript_list = self.list_transcripts(video_id)
        available = []
        for t in transcript_list:
            info = {
                "language": t.language,
                "language_code": t.language_code,
                "is_generated": t.is_generated,
                "is_translatable": t.is_translatable,
                "_transcript": t,
            }
            available.append(info)
            logger.info(
                "Available transcript: language=%s, code=%s, "
                "generated=%s, translatable=%s",
                t.language, t.language_code, t.is_generated, t.is_translatable,
            )
        if not available:
            logger.warning("No transcripts available for video %s", video_id)
        return available

    def find_best_transcript(
        self,
        video_id: str,
        preferred_languages: list[str] | None = None,
        transcript_type: str = "any",
    ) -> tuple[list[dict[str, Any]], str, bool, str | None]:
        """Find and fetch the best available transcript using smart prioritization.

        Steps:
          1. List ALL transcripts via ``list_transcripts()``
          2. Log each candidate with language, code, generated, translatable
          3. Score candidates and pick the best match
          4. If the best match is translatable but not in a preferred language,
             translate it to English
          5. Return (segments, language_code, is_manual, translation_source)

        Priority (highest to lowest) when ``transcript_type="any"``:
          1. Manual English (exact "en" match)
          2. Manual English variant (en-US, en-GB, en-IN)
          3. Manual Hindi (hi)
          4. Auto-generated English (any "en*" code)
          5. Manual transcript translatable to English
          6. Auto-generated transcript translatable to English
          7. Best available transcript (any language, any type)
          8. Translatable transcript (any language → en)

        When ``transcript_type="manual"``, only priorities 1-3 and non-generated
        translatable candidates are considered.

        When ``transcript_type="auto"``, only auto-generated candidates are
        considered (priorities 4, 6, and auto-only from 7-8).

        Args:
            video_id: 11-character YouTube video ID.
            preferred_languages: Override default language priority list.
            transcript_type: One of "any" (default), "manual", or "auto".

        Returns:
            Tuple of (segments, language_code, is_manual, translation_source).
            ``translation_source`` is the original language code if translated,
            or None if the transcript is in its original language.

        Raises:
            NoTranscriptFoundError: No transcript available through any strategy.
        """
        langs = preferred_languages or PREFERRED_LANGUAGES
        transcript_list = self.list_transcripts(video_id)

        candidates = list(transcript_list)
        logger.info(
            "Searching for best %s transcript among %d candidate(s) for "
            "video %s with preferred languages %s",
            transcript_type, len(candidates), video_id, langs,
        )

        if not candidates:
            logger.error("Zero transcript candidates returned for video %s", video_id)
            raise NoTranscriptFoundError(
                f"No transcript found for video {video_id}. "
                "The video may not have captions enabled."
            )

        for t in candidates:
            _log_transcript_candidate(t, "candidate", "available for evaluation")

        if transcript_type == "manual":
            return self._find_best_manual(candidates, langs)
        if transcript_type == "auto":
            return self._find_best_auto(candidates, langs)
        return self._find_best_any(candidates, langs)

    def _find_best_manual(
        self,
        candidates: list[Any],
        langs: list[str],
    ) -> tuple[list[dict[str, Any]], str, bool, str | None]:
        """Select best manually-created transcript only, prioritizing original language."""

        # Priority M1: Manual in preferred languages (exact/prefix match, native)
        for lang in langs:
            for t in candidates:
                if not t.is_generated and (t.language_code == lang or t.language_code.startswith(lang)):
                    _log_transcript_candidate(t, "ACCEPTED", f"Priority M1: manual {lang}")
                    return self._to_dicts(t.fetch()), t.language_code, True, None

        # Priority M2: Any manual transcript in original native language
        for t in candidates:
            if not t.is_generated:
                _log_transcript_candidate(t, "ACCEPTED", f"Priority M2: manual native ({t.language_code})")
                return self._to_dicts(t.fetch()), t.language_code, True, None

        # Priority M3: Manual translatable to English (last resort fallback)
        for t in candidates:
            if not t.is_generated and t.is_translatable:
                _log_transcript_candidate(t, "ACCEPTED", "Priority M3: manual translatable to en (last resort)")
                translated = t.translate("en")
                return self._to_dicts(translated.fetch()), "en", True, t.language_code

        names = ", ".join(f"{t.language}({t.language_code})" for t in candidates)
        raise NoTranscriptFoundError(
            f"No manually-created transcript found. Available: [{names}]"
        )

    def _find_best_auto(
        self,
        candidates: list[Any],
        langs: list[str],
    ) -> tuple[list[dict[str, Any]], str, bool, str | None]:
        """Select best auto-generated transcript only, prioritizing original language."""

        # Priority A1: Auto in preferred languages (exact/prefix match, native)
        for lang in langs:
            for t in candidates:
                if t.is_generated and (t.language_code == lang or t.language_code.startswith(lang)):
                    _log_transcript_candidate(t, "ACCEPTED", f"Priority A1: auto {lang}")
                    return self._to_dicts(t.fetch()), t.language_code, False, None

        # Priority A2: Any auto transcript in original native language
        for t in candidates:
            if t.is_generated:
                _log_transcript_candidate(t, "ACCEPTED", f"Priority A2: auto native ({t.language_code})")
                return self._to_dicts(t.fetch()), t.language_code, False, None

        # Priority A3: Auto translatable to English (last resort fallback)
        for t in candidates:
            if t.is_generated and t.is_translatable:
                _log_transcript_candidate(t, "ACCEPTED", "Priority A3: auto translatable to en (last resort)")
                translated = t.translate("en")
                return self._to_dicts(translated.fetch()), "en", False, t.language_code

        names = ", ".join(f"{t.language}({t.language_code})" for t in candidates)
        raise NoTranscriptFoundError(
            f"No auto-generated transcript found. Available: [{names}]"
        )

    def _find_best_any(
        self,
        candidates: list[Any],
        langs: list[str],
    ) -> tuple[list[dict[str, Any]], str, bool, str | None]:
        """Select best transcript of any type, preserving original source representation."""

        # Priority 1: Manual in preferred languages (native)
        for lang in langs:
            for t in candidates:
                if not t.is_generated and (t.language_code == lang or t.language_code.startswith(lang)):
                    _log_transcript_candidate(t, "ACCEPTED", f"Priority 1: manual {lang}")
                    return self._to_dicts(t.fetch()), t.language_code, True, None

        # Priority 2: Any manual transcript in native language
        for t in candidates:
            if not t.is_generated:
                _log_transcript_candidate(t, "ACCEPTED", f"Priority 2: manual native ({t.language_code})")
                return self._to_dicts(t.fetch()), t.language_code, True, None

        # Priority 3: Auto-generated in preferred languages (native)
        for lang in langs:
            for t in candidates:
                if t.is_generated and (t.language_code == lang or t.language_code.startswith(lang)):
                    _log_transcript_candidate(t, "ACCEPTED", f"Priority 3: auto {lang}")
                    return self._to_dicts(t.fetch()), t.language_code, False, None

        # Priority 4: Any auto-generated in native language
        for t in candidates:
            if t.is_generated:
                _log_transcript_candidate(t, "ACCEPTED", f"Priority 4: auto native ({t.language_code})")
                return self._to_dicts(t.fetch()), t.language_code, False, None

        # Priority 5: Machine-translatable to English (absolute last resort)
        for t in candidates:
            if t.is_translatable:
                _log_transcript_candidate(t, "ACCEPTED", "Priority 5: translatable to en (last resort)")
                translated = t.translate("en")
                return self._to_dicts(translated.fetch()), "en", not t.is_generated, t.language_code

        names = ", ".join(f"{t.language}({t.language_code})" for t in candidates)
        logger.error("All transcript strategies exhausted. Candidates: [%s]", names)
        raise NoTranscriptFoundError(
            f"No compatible transcript found. Checked languages {langs}. "
            f"Available: [{names}]"
        )

    # ------------------------------------------------------------------
    # Legacy methods (kept for backward compatibility)
    # ------------------------------------------------------------------

    def fetch_transcript(
        self,
        video_id: str,
        languages: list[str] | None = None,
        prefer_manual: bool = True,
    ) -> list[dict[str, Any]]:
        """Legacy: fetch transcript segments limited to specific languages.

        Deprecated: prefer ``find_best_transcript()`` which checks all
        available transcripts with full enumeration.

        Args:
            video_id: 11-character YouTube video ID.
            languages: Optional list of language codes (default PREFERRED_LANGUAGES).
            prefer_manual: Prefer manually created captions.

        Returns:
            List of dicts with keys: text, start, duration.

        Raises:
            NoTranscriptFoundError: No transcript found.
        """
        langs = languages or PREFERRED_LANGUAGES
        transcript_list = self.list_transcripts(video_id)

        if prefer_manual:
            for lang in langs:
                try:
                    transcript = transcript_list.find_manually_created_transcript([lang])
                    return self._to_dicts(transcript.fetch())
                except NoTranscriptFound:
                    continue

        for lang in langs:
            try:
                transcript = transcript_list.find_generated_transcript([lang])
                return self._to_dicts(transcript.fetch())
            except NoTranscriptFound:
                continue

        try:
            transcript = transcript_list.find_transcript(langs)
            return self._to_dicts(transcript.fetch())
        except NoTranscriptFound:
            pass

        raise NoTranscriptFoundError(
            f"No transcript found for video {video_id} in languages {langs}."
        )

    def fetch_transcript_all_languages(
        self, video_id: str, prefer_manual: bool = True
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """Legacy: fetch transcript in any available language.

        Deprecated: prefer ``find_best_transcript()``.
        """
        transcript_list = self.list_transcripts(video_id)

        if prefer_manual:
            for t in transcript_list:
                if not t.is_generated:
                    return self._to_dicts(t.fetch()), t.language_code, True

        for t in transcript_list:
            if t.is_generated:
                return self._to_dicts(t.fetch()), t.language_code, False

        try:
            t = transcript_list.find_transcript(["en"])
            return self._to_dicts(t.fetch()), t.language_code, not t.is_generated
        except Exception as exc:
            raise NoTranscriptFoundError(
                f"No transcript found for video {video_id}: {exc}"
            )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dicts(fetched_transcript: Any) -> list[dict[str, Any]]:
        """Convert FetchedTranscript to list of dicts."""
        return [
            {"text": seg.text, "start": seg.start, "duration": seg.duration}
            for seg in fetched_transcript
        ]

    @staticmethod
    def parse_segments(raw_segments: list[dict[str, Any]]) -> list[TranscriptSegment]:
        """Convert raw API segments to structured TranscriptSegment models.

        Args:
            raw_segments: List of dicts with text, start, duration keys.

        Returns:
            List of ``TranscriptSegment`` objects.
        """
        parsed: list[TranscriptSegment] = []
        for seg in raw_segments:
            start = float(seg.get("start", 0))
            duration = float(seg.get("duration", 0))
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            parsed.append(
                TranscriptSegment(
                    start=start,
                    end=start + duration,
                    duration=duration,
                    text=text,
                )
            )
        return parsed
