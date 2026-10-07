"""Transcript Service — multi-stage transcript retrieval orchestration (DEPRECATED).

DEPRECATED: Use ``transcript_reliability.TranscriptManager`` instead.
This module is kept for backward compatibility and delegates to TranscriptManager.

The new TranscriptManager provides:
- 6+ providers with automatic failover
- Retry engine with exponential backoff
- Circuit breaker for failing providers
- Provider health monitoring
- Multi-level caching (L1 memory, L2 Redis, L3 DB)
- Transcript validation (12-point checks)
- Duplicate removal (6 strategies)
- Silence detection (6 patterns)
- Quality scoring (8 dimensions)
- Version management (7 version types)

Migration::
    from transcript_reliability import TranscriptManager
    manager = TranscriptManager()
    result = manager.get_transcript(video_id="dQw4w9WgXcQ")
"""

import logging
import time
import warnings
from typing import Any

from models.transcript import (
    TranscriptResult,
    TranscriptSource,
    TranscriptProviderName,
    PipelineStep,
)
from interfaces.transcript_provider import TranscriptProvider
from providers.manual_transcript_provider import ManualTranscriptProvider
from providers.auto_transcript_provider import AutoTranscriptProvider
from providers.whisper_provider import WhisperProvider
from repositories.transcript_repository import TranscriptRepository
from exceptions.transcript_errors import (
    TranscriptUnavailableError,
    TranscriptDisabledError,
    TranscriptFetchError,
    AudioDownloadError,
    TranscriptionError,
    InvalidVideoIdError,
)
from clients.youtube_transcript_client import (
    NoTranscriptFoundError as ClientNoTranscriptFoundError,
    TranscriptsDisabledError as ClientTranscriptsDisabledError,
    VideoUnavailableError as ClientVideoUnavailableError,
    TooManyRequestsError as ClientTooManyRequestsError,
)
from utils.text_cleaner import TextCleaner
from utils.read_time import estimate_read_time
from services.transliteration import hinglish_normalizer
from services.english_converter import english_converter

logger = logging.getLogger(__name__)

warnings.warn(
    "TranscriptService is deprecated. Use TranscriptManager from transcript_reliability instead.",
    DeprecationWarning,
    stacklevel=2,
)


class TranscriptService:
    """Orchestrates the transcript retrieval pipeline.

    Uses a multi-stage fallback pipeline to retrieve the highest-quality
    transcript available. Results are cached via ``TranscriptRepository``.

    Usage::

        service = TranscriptService()
        result = service.get_transcript("dQw4w9WgXcQ")
        if result.success:
            print(result.plain_text[:200])
    """

    def __init__(
        self,
        manual_provider: TranscriptProvider | None = None,
        auto_provider: TranscriptProvider | None = None,
        whisper_provider: TranscriptProvider | None = None,
        repository: TranscriptRepository | None = None,
        text_cleaner: TextCleaner | None = None,
        use_cache: bool = True,
    ) -> None:
        self._manual_provider = manual_provider or ManualTranscriptProvider()
        self._auto_provider = auto_provider or AutoTranscriptProvider()
        self._whisper_provider = whisper_provider
        if repository is not None:
            self._repository = repository
        else:
            try:
                from config.settings import settings
                p_dir = str(getattr(settings, "transcript_cache_dir", "data/transcripts"))
            except Exception:
                p_dir = "data/transcripts"
            self._repository = TranscriptRepository(persist_dir=p_dir)
        self._text_cleaner = text_cleaner or TextCleaner()
        self._use_cache = use_cache

    def _get_whisper_provider(self) -> TranscriptProvider:
        """Lazy-load the Whisper provider (may fail if deps missing)."""
        if self._whisper_provider is None:
            self._whisper_provider = WhisperProvider()
        return self._whisper_provider

    def get_transcript(
        self,
        video_id: str,
        language: str | None = None,
        force_refresh: bool = False,
        allow_whisper: bool = True,
        video_title: str | None = None,
        channel_title: str | None = None,
        output_format: str = "original",
    ) -> TranscriptResult:
        """Retrieve the best available transcript for a video.

        Implements the three-stage fallback pipeline:
        1. Manual transcript (highest quality)
        2. Auto-generated transcript
        3. Whisper speech-to-text (last resort)

        Args:
            video_id: 11-character YouTube video ID.
            language: Preferred language code (e.g. "en", "es").
            force_refresh: If True, bypass cache.
            allow_whisper: If False, skip Stage 3 (Whisper fallback).
            video_title: Optional video title for domain entity biasing.
            channel_title: Optional channel name for series/channel biasing.
            output_format: 'original' (canonical source) | 'en' | 'hi'

        Returns:
            ``TranscriptResult`` with transcript data and pipeline metadata.

        Raises:
            InvalidVideoIdError: If video_id is malformed.
        """
        import sys
        self._validate_video_id(video_id)

        pipeline_steps: list[dict[str, Any]] = []
        start_time = time.time()

        # Check cache
        if self._use_cache and not force_refresh:
            cached = self._repository.get(video_id)
            if cached is not None:
                if not cached.success and getattr(cached, "error_code", None) in ("RATE_LIMITED", "NETWORK_ERROR", "TIMEOUT"):
                    logger.info("Ignoring transient cached failure for %s (code=%s)", video_id, getattr(cached, "error_code", None))
                elif not cached.success and allow_whisper and not any(
                    (getattr(s, "name", "") == "Whisper STT" if hasattr(s, "name") else (s.get("name") == "Whisper STT" if isinstance(s, dict) else False))
                    for s in (cached.pipeline_steps or [])
                ):
                    logger.info("Ignoring cached caption failure for %s because allow_whisper=True and Whisper not yet attempted", video_id)
                elif cached.success and allow_whisper and output_format != "original" and (
                    str(cached.language).lower() in ("hi", "hindi", "ur", "urdu", "hinglish")
                    or english_converter.contains_non_roman_script(cached.plain_text or "")
                    or (cached.source != TranscriptSource.WHISPER and hinglish_normalizer.is_hinglish_or_hindi(cached.plain_text or ""))
                ):
                    logger.info("Ignoring cached non-English caption for %s because allow_whisper=True and output_format != 'original'", video_id)
                else:
                    logger.info("Returning cached transcript for %s (source=%s)", video_id, cached.source)
                    # Canonical healing: If Original Spoken requested and raw_transcript exists, ensure authentic verbatim text
                    if output_format in ("original", "original_spoken") and getattr(cached, "raw_transcript", None):
                        cached.plain_text = cached.raw_transcript
                        if getattr(cached, "source_language", None):
                            cached.language = cached.source_language
                        elif english_converter.contains_non_roman_script(cached.raw_transcript):
                            cached.language = "Hindi"
                            cached.source_language = "Hindi"
                            cached.source_language_code = "hi"
                    return cached

        # Stage 1: Manual transcript (NEVER throws - trapped as skipped)
        step_manual = self._execute_stage(
            "Manual Transcript",
            video_id,
            language,
            self._manual_provider,
            pipeline_steps,
            video_title=video_title,
            channel_title=channel_title,
        )

        is_rate_limited = (
            step_manual is not None
            and step_manual.get("error_type") == "RATE_LIMITED"
        )

        # Stage 2: Auto transcript (SKIP if Stage 1 was RATE_LIMITED to protect YouTube IP)
        if is_rate_limited:
            logger.warning(
                "Manual transcript hit RATE_LIMITED for %s. Skipping Auto transcript to prevent request storm.",
                video_id,
            )
            step_auto = None
            pipeline_steps.append({
                "name": "Auto Transcript",
                "status": "skipped",
                "detail": "Skipped due to YouTube rate limiting",
                "error_type": "RATE_LIMITED",
            })
        else:
            step_auto = self._execute_stage(
                "Auto Transcript",
                video_id,
                language,
                self._auto_provider,
                pipeline_steps,
                video_title=video_title,
                channel_title=channel_title,
            )
            if step_auto and step_auto.get("error_type") == "RATE_LIMITED":
                is_rate_limited = True

        # Determine best result - prefer manual over auto
        best_step = None
        if step_manual and step_manual.get("status") == "ok":
            best_step = step_manual
            logger.debug("Using MANUAL transcript for %s", video_id)
        elif step_auto and step_auto.get("status") == "ok":
            best_step = step_auto
            logger.debug("Using AUTO transcript for %s (manual unavailable)", video_id)

        # If caption candidate is in Hindi/non-English and allow_whisper is True,
        # fallback to Whisper STT ONLY IF output_format is not 'original'
        if best_step and allow_whisper and output_format not in ("original", "original_spoken"):
            cand_res = best_step.get("result")
            if cand_res:
                cand_lang = (getattr(cand_res, "language", "") or "").lower()
                cand_text = getattr(cand_res, "plain_text", "") or ""
                if cand_lang in ("hi", "hindi", "ur", "urdu", "hinglish") or english_converter.contains_non_roman_script(cand_text):
                    logger.info("Caption candidate for %s is in %s (non-English). Falling back to Whisper STT for English translation.", video_id, cand_lang)
                    best_step = None

        if best_step:
            result = self._finalize(best_step["result"], pipeline_steps, start_time, video_title=video_title, channel_title=channel_title, output_format=output_format)
            self._repository.save(result)
            return result

        # Stage 3: Whisper (only if both manual AND auto failed, and NOT in rate limit)
        if allow_whisper and not is_rate_limited:
            try:
                whisper_provider = self._get_whisper_provider()
            except ImportError as exc:
                pipeline_steps.append({
                    "name": "Whisper STT",
                    "status": "skipped",
                    "detail": f"Dependencies not available: {exc}",
                })
                whisper_provider = None

            if whisper_provider:
                step_whisper = self._execute_stage(
                    "Whisper STT",
                    video_id,
                    language,
                    whisper_provider,
                    pipeline_steps,
                    video_title=video_title,
                    channel_title=channel_title,
                )
            else:
                step_whisper = None
            if step_whisper and step_whisper.get("status") == "ok":
                result = self._finalize(step_whisper["result"], pipeline_steps, start_time, video_title=video_title, channel_title=channel_title, output_format=output_format)
                self._repository.save(result)
                return result
        elif allow_whisper and is_rate_limited:
            pipeline_steps.append({
                "name": "Whisper STT",
                "status": "skipped",
                "detail": "Skipped to avoid secondary requests during YouTube rate limit cooldown",
                "error_type": "RATE_LIMITED",
            })

        # All stages failed
        pipeline_steps.append({
            "name": "Error",
            "status": "error",
            "detail": "No transcript available from any source.",
        })
        elapsed = round(time.time() - start_time, 2)
        error_result = self._build_error_result(
            video_id,
            pipeline_steps,
            elapsed,
        )
        if getattr(error_result, "error_code", None) not in ("RATE_LIMITED", "NETWORK_ERROR", "TIMEOUT"):
            self._repository.save(error_result)
        logger.debug("All transcript stages failed for %s", video_id)
        return error_result

    def get_all_transcripts(self, video_id: str) -> dict[str, Any]:
        """Retrieve ALL available transcripts separately: manual, auto, translated.

        Never raises exceptions for missing transcripts.
        Returns null for any transcript that is not available.

        Returns:
            Dict with:
                success: bool
                video_id: str
                manual: TranscriptResult | None
                auto: TranscriptResult | None
                pipeline_steps: list[PipelineStep]
                available_languages: list[dict]
        """
        self._validate_video_id(video_id)

        pipeline_steps: list[dict[str, Any]] = []
        start_time = time.time()

        result: dict[str, Any] = {
            "success": True,
            "video_id": video_id,
            "manual": None,
            "auto": None,
            "whisper": None,
            "pipeline_steps": [],
            "available_languages": [],
        }

        # Manual
        step_manual = self._execute_stage(
            "Manual Transcript", video_id, None, self._manual_provider, pipeline_steps,
        )
        if step_manual and step_manual.get("status") == "ok":
            result["manual"] = step_manual["result"]
            logger.debug("get_all: manual available for %s", video_id)
        else:
            logger.debug("get_all: manual not available for %s", video_id)

        # Auto
        step_auto = self._execute_stage(
            "Auto Transcript", video_id, None, self._auto_provider, pipeline_steps,
        )
        if step_auto and step_auto.get("status") == "ok":
            result["auto"] = step_auto["result"]
            logger.debug("get_all: auto available for %s", video_id)
        else:
            logger.debug("get_all: auto not available for %s", video_id)

        # Whisper fallback when captions are unavailable.
        if result["manual"] is None and result["auto"] is None:
            try:
                whisper_provider = self._get_whisper_provider()
            except ImportError as exc:
                whisper_provider = None
                pipeline_steps.append({
                    "name": "Whisper STT",
                    "status": "skipped",
                    "detail": f"Dependencies not available: {exc}",
                })

            if whisper_provider is not None:
                step_whisper = self._execute_stage(
                    "Whisper STT", video_id, None, whisper_provider, pipeline_steps,
                )
                if step_whisper and step_whisper.get("status") == "ok":
                    result["whisper"] = step_whisper["result"]
                    logger.debug("get_all: whisper available for %s", video_id)
                else:
                    logger.debug("get_all: whisper not available for %s", video_id)

        # If every source failed, report overall failure.
        if result["manual"] is None and result["auto"] is None and result["whisper"] is None:
            result["success"] = False
            pipeline_steps.append({
                "name": "Error",
                "status": "error",
                "detail": "No transcript available from any source.",
            })

        # Available languages from whichever succeeded
        best = result["manual"] or result["auto"] or result["whisper"]
        if best:
            result["available_languages"] = best.available_languages or []
            result["pipeline_steps"] = pipeline_steps
            # Add finalizing steps
            elapsed = round(time.time() - start_time, 2)
            pipeline_steps.append({
                "name": "Cleaning Transcript",
                "status": "ok",
                "detail": f"{best.word_count} words, {best.character_count} chars",
            })
            pipeline_steps.append({
                "name": "Ready",
                "status": "ok",
                "detail": f"Retrieved in {elapsed}s",
            })
        else:
            result["pipeline_steps"] = pipeline_steps

        logger.debug("get_all returning for %s: manual=%s, auto=%s",
                     video_id, 'YES' if result['manual'] else 'NULL', 'YES' if result['auto'] else 'NULL')
        return result

    def get_transcript_status(self, video_id: str) -> dict[str, Any]:
        """Check transcript availability without full retrieval.

        Enumerates ALL available transcripts via the YouTube API and
        returns structured data about each one.

        Args:
            video_id: 11-character YouTube video ID.

        Returns:
            Dict with availability info including every available transcript
            with language, language_code, is_generated, is_translatable.
        """
        result: dict[str, Any] = {
            "video_id": video_id,
            "cached": False,
            "available_transcripts": [],
            "whisper_possible": True,
        }

        cached = self._repository.get(video_id)
        if cached is not None:
            result["cached"] = True
            result["source"] = cached.source

        try:
            available = self._manual_provider._client.list_all_transcripts(video_id)
            result["available_transcripts"] = [
                {
                    "language": t["language"],
                    "language_code": t["language_code"],
                    "is_generated": t["is_generated"],
                    "is_translatable": t["is_translatable"],
                }
                for t in available
            ]
        except Exception as exc:
            logger.warning("Could not enumerate transcripts for %s: %s", video_id, exc)

        return result

    def _execute_stage(
        self,
        stage_name: str,
        video_id: str,
        language: str | None,
        provider: TranscriptProvider,
        pipeline_steps: list[dict[str, Any]],
        video_title: str | None = None,
        channel_title: str | None = None,
    ) -> dict[str, Any] | None:
        step: dict[str, Any] = {
            "name": stage_name,
            "status": "running",
            "detail": "",
        }
        pipeline_steps.append(step)

        try:
            try:
                transcript = provider.get_transcript(
                    video_id,
                    language=language,
                    title=video_title,
                    channel_title=channel_title,
                )
            except TypeError:
                transcript = provider.get_transcript(video_id, language=language)
            if transcript.success and transcript.segments:
                try:
                    from transcript_reliability.transcript_limiter import transcript_limiter
                    transcript_limiter.record_success(video_id)
                except Exception:
                    pass
                step["status"] = "ok"
                step["detail"] = f"{transcript.source.value} ({transcript.language}, {transcript.word_count} words)"
                step["result"] = transcript
                return step

            step["status"] = "error"
            step["detail"] = transcript.error or "No segments returned"
            return step

        except Exception as exc:
            exc_type = type(exc).__name__

            if isinstance(exc, (TranscriptDisabledError, ClientTranscriptsDisabledError)):
                step["status"] = "skipped"
                step["detail"] = str(exc)
                step["error_type"] = "CAPTIONS_DISABLED"
                return step

            if isinstance(exc, (ClientTooManyRequestsError,)) or "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower() or "429" in str(exc):
                try:
                    from transcript_reliability.transcript_limiter import transcript_limiter
                    transcript_limiter.record_rate_limit(video_id)
                except Exception:
                    pass
                step["status"] = "skipped"
                step["detail"] = str(exc)
                step["error_type"] = "RATE_LIMITED"
                return step

            if isinstance(exc, (ClientVideoUnavailableError,)):
                step["status"] = "skipped"
                step["detail"] = str(exc)
                step["error_type"] = "VIDEO_UNAVAILABLE"
                return step

            if isinstance(exc, (TranscriptUnavailableError, ClientNoTranscriptFoundError)):
                step["status"] = "skipped"
                step["detail"] = str(exc)
                step["error_type"] = "NO_CAPTIONS"
                return step

            if isinstance(exc, (TranscriptFetchError,)):
                step["status"] = "skipped"
                step["detail"] = str(exc)
                step["error_type"] = "REQUEST_FAILED"
                return step

            if isinstance(exc, (AudioDownloadError, TranscriptionError)):
                step["status"] = "error"
                step["detail"] = f"{exc_type}: {exc}"
                step["error_type"] = "LIBRARY_ERROR"
                return step

            logger.exception("Unexpected error in stage '%s' for %s", stage_name, video_id)
            step["status"] = "error"
            step["detail"] = f"Unexpected error: {exc}"
            step["error_type"] = "UNKNOWN_ERROR"
            return step

    def _finalize(
        self,
        transcript: TranscriptResult,
        pipeline_steps: list[dict[str, Any]],
        start_time: float,
        video_title: str | None = None,
        channel_title: str | None = None,
        output_format: str = "original",
    ) -> TranscriptResult:
        """Finalize transcript result with pipeline metadata and strict canonical preservation."""
        elapsed = round(time.time() - start_time, 2)

        raw_text = transcript.plain_text or transcript.paragraph_text or ""
        if not getattr(transcript, "raw_transcript", None):
            transcript.raw_transcript = raw_text

        # Detect source language accurately
        lang_lower = (transcript.language or "").lower()
        is_hindi = (
            lang_lower in ("hi", "ur", "hindi", "urdu", "hinglish")
            or english_converter.contains_non_roman_script(raw_text)
            or hinglish_normalizer.is_hinglish_or_hindi(raw_text)
        )

        # Document canonical source language metadata
        if not getattr(transcript, "source_language", None):
            if is_hindi:
                transcript.source_language = "Hindi"
                transcript.source_language_code = "hi"
            elif lang_lower.startswith("en"):
                transcript.source_language = "English"
                transcript.source_language_code = "en"
            else:
                transcript.source_language = transcript.language or "Unknown"
                transcript.source_language_code = lang_lower or "auto"

        transcript.output_format = output_format

        # In Original Spoken mode, strictly preserve authentic verbatim source representation
        if output_format in ("original", "original_spoken"):
            transcript.plain_text = transcript.raw_transcript
            transcript.language = transcript.source_language or ("Hindi" if is_hindi else transcript.language)
            transcript.word_count = len(transcript.plain_text.split()) if transcript.plain_text else 0
            transcript.character_count = len(transcript.plain_text) if transcript.plain_text else 0
            transcript.estimated_read_time = estimate_read_time(transcript.word_count)
        else:
            # Transliteration or conversion requested for non-original mode
            if (
                is_hindi
                or transcript.source == TranscriptSource.WHISPER
            ):
                if transcript.segments:
                    english_converter.convert_segments(transcript.segments, title=video_title, channel=channel_title)
                if transcript.plain_text:
                    transcript.plain_text = english_converter.convert(transcript.plain_text, title=video_title, channel=channel_title)
                if transcript.paragraph_text:
                    transcript.paragraph_text = self._text_cleaner.build_paragraphs(transcript.segments) if transcript.segments else english_converter.convert(transcript.paragraph_text, title=video_title, channel=channel_title)
                transcript.language = "English (India)"
                transcript.word_count = len(transcript.plain_text.split()) if transcript.plain_text else 0
                transcript.character_count = len(transcript.plain_text) if transcript.plain_text else 0
                transcript.estimated_read_time = estimate_read_time(transcript.word_count)

        pipeline_steps.append({
            "name": "Cleaning Transcript",
            "status": "ok",
            "detail": f"{transcript.word_count} words, {transcript.character_count} chars",
        })
        pipeline_steps.append({
            "name": "Ready",
            "status": "ok",
            "detail": f"Retrieved in {elapsed}s from {transcript.source.value}",
        })

        transcript.pipeline_steps = [
            PipelineStep(**s) if isinstance(s, dict) else s
            for s in pipeline_steps
        ]

        return transcript

    def _build_error_result(
        self,
        video_id: str,
        pipeline_steps: list[dict[str, Any]],
        elapsed: float,
    ) -> TranscriptResult:
        """Build a failed TranscriptResult when all stages fail."""
        error_code = "NO_CAPTIONS"
        error_message = "No transcript/caption track is available for this video."

        # Scan pipeline steps for specific error type
        error_types = [
            s.get("error_type") for s in pipeline_steps
            if isinstance(s, dict) and s.get("error_type")
        ]
        step_details = " ".join(
            str(s.get("detail", "")) for s in pipeline_steps
            if isinstance(s, dict)
        ).lower()

        if "RATE_LIMITED" in error_types or "too many requests" in step_details or "rate limited" in step_details:
            error_code = "RATE_LIMITED"
            error_message = "Rate limited by YouTube. Please retry later."
        elif "VIDEO_UNAVAILABLE" in error_types or "unavailable" in step_details:
            error_code = "VIDEO_UNAVAILABLE"
            error_message = "This video is unavailable, private, or deleted."
        elif "CAPTIONS_DISABLED" in error_types or "subtitles are disabled" in step_details or "transcripts disabled" in step_details:
            error_code = "CAPTIONS_DISABLED"
            error_message = "Subtitles/transcripts are disabled or not available for this video on YouTube."
        elif "timeout" in step_details or "timed out" in step_details:
            error_code = "TIMEOUT"
            error_message = "Connection to YouTube timed out."
        elif "ssl" in step_details or "connection error" in step_details:
            error_code = "NETWORK_ERROR"
            error_message = "Network error connecting to YouTube."
        elif "NO_CAPTIONS" in error_types:
            error_code = "NO_CAPTIONS"
            error_message = "No transcript/caption track is available for this video."

        whisper_attempted = any(
            (getattr(s, "name", "") == "Whisper STT" if hasattr(s, "name") else (s.get("name") == "Whisper STT" if isinstance(s, dict) else False))
            and (getattr(s, "status", "") != "skipped" if hasattr(s, "status") else (s.get("status") != "skipped" if isinstance(s, dict) else False))
            for s in pipeline_steps
        )
        return TranscriptResult(
            success=False,
            video_id=video_id,
            source=TranscriptSource.WHISPER if whisper_attempted else TranscriptSource.MANUAL,
            error=error_message,
            error_code=error_code,
            method="speech_to_text" if whisper_attempted else "youtube_transcript",
            pipeline_steps=[
                PipelineStep(**s) if isinstance(s, dict) else s
                for s in pipeline_steps
            ],
        )

    @staticmethod
    def _validate_video_id(video_id: str) -> None:
        """Validate YouTube video ID format."""
        if not video_id or not isinstance(video_id, str):
            raise InvalidVideoIdError("Video ID must be a non-empty string.")
        if len(video_id) != 11:
            raise InvalidVideoIdError(
                f"Invalid video ID '{video_id}'. Must be exactly 11 characters."
            )

    def translate_transcript(
        self,
        video_id: str,
        target_language: str,
    ) -> TranscriptResult:
        """Get the transcript in a specific language, translating if necessary.

        Uses YouTube's built-in translation to convert the best available
        transcript into the target language. Results are cached per language.

        Args:
            video_id: 11-character YouTube video ID.
            target_language: Language code to translate to (e.g. "en", "hi").

        Returns:
            ``TranscriptResult`` with translated segments and updated language.
        """
        # Check translation cache first
        cached = self._repository.get_translation(video_id, target_language)
        if cached is not None:
            return cached

        # Get original to ensure transcript exists
        original = self.get_transcript(video_id)
        if not original.success:
            return original

        # If already in target language, return as-is
        if original.language == target_language:
            return original

        # Use auto provider's client for translation
        try:
            client = self._auto_provider._client
            available = client.list_all_transcripts(video_id)

            # Look for exact match in target language
            for t_info in available:
                t_obj = t_info.get("_transcript")
                if t_obj and t_obj.language_code == target_language:
                    raw = client._to_dicts(t_obj.fetch())
                    segments = client.parse_segments(raw)
                    segments = self._text_cleaner.clean_segments(segments)
                    plain_text = " ".join(s.text for s in segments)
                    word_count = len(plain_text.split())
                    char_count = len(plain_text)
                    result = TranscriptResult(
                        success=True,
                        video_id=video_id,
                        source=original.source,
                        provider=original.provider,
                        language=target_language,
                        segments=segments,
                        plain_text=plain_text,
                        paragraph_text="\n".join(s.text for s in segments),
                        word_count=word_count,
                        character_count=char_count,
                        estimated_read_time=estimate_read_time(word_count),
                        available_languages=original.available_languages,
                    )
                    self._repository.save_translation(result, target_language)
                    return result

            # Look for translatable transcript
            for t_info in available:
                t_obj = t_info.get("_transcript")
                if t_obj and t_info.get("is_translatable"):
                    translated = t_obj.translate(target_language)
                    raw = client._to_dicts(translated.fetch())
                    segments = client.parse_segments(raw)
                    segments = self._text_cleaner.clean_segments(segments)
                    plain_text = " ".join(s.text for s in segments)
                    word_count = len(plain_text.split())
                    char_count = len(plain_text)
                    result = TranscriptResult(
                        success=True,
                        video_id=video_id,
                        source=original.source,
                        provider=original.provider,
                        language=target_language,
                        translation_source=t_obj.language_code,
                        segments=segments,
                        plain_text=plain_text,
                        paragraph_text="\n".join(s.text for s in segments),
                        word_count=word_count,
                        character_count=char_count,
                        estimated_read_time=estimate_read_time(word_count),
                        available_languages=original.available_languages,
                    )
                    self._repository.save_translation(result, target_language)
                    return result

            # Fallback: return original
            logger.warning("No translatable transcript found for %s to %s", video_id, target_language)
            return original

        except Exception as exc:
            logger.exception("Translation failed for %s to %s: %s", video_id, target_language, exc)
            return original

    def clear_cache(self) -> None:
        """Clear the transcript result cache."""
        self._repository.clear()
        logger.info("Transcript service cache cleared")
