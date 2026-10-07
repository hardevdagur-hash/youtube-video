"""Transcript Service — caption-first transcript retrieval used by channel jobs and CSV export.

Pipeline: cache -> manual captions -> auto captions -> Whisper speech-to-text fallback.
Results are cached through ``TranscriptRepository``.
"""

import logging
import re
import time
from typing import Any

from clients.youtube_transcript_client import (
    NoTranscriptFoundError as ClientNoTranscriptFoundError,
)
from clients.youtube_transcript_client import (
    TooManyRequestsError as ClientTooManyRequestsError,
)
from clients.youtube_transcript_client import (
    TranscriptsDisabledError as ClientTranscriptsDisabledError,
)
from clients.youtube_transcript_client import (
    VideoUnavailableError as ClientVideoUnavailableError,
)
from exceptions.transcript_errors import (
    AudioDownloadError,
    InvalidVideoIdError,
    TranscriptDisabledError,
    TranscriptFetchError,
    TranscriptionError,
    TranscriptUnavailableError,
)
from interfaces.transcript_provider import TranscriptProvider
from models.transcript import (
    PipelineStep,
    TranscriptResult,
    TranscriptSource,
)
from providers.auto_transcript_provider import AutoTranscriptProvider
from providers.manual_transcript_provider import ManualTranscriptProvider
from providers.whisper_provider import WhisperProvider
from repositories.transcript_repository import TranscriptRepository
from services.english_converter import english_converter
from services.transliteration import hinglish_normalizer
from utils.read_time import estimate_read_time
from utils.text_cleaner import TextCleaner

logger = logging.getLogger(__name__)


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class SpeechToTextUnavailableError(RuntimeError):
    """Speech-to-text fallback is disabled or not configured on this server."""


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

    @staticmethod
    def _is_translated_whisper_result(result: TranscriptResult) -> bool:
        """True for Whisper results not produced by verbatim transcription (legacy task=translate)."""
        if result.source != TranscriptSource.WHISPER:
            return False
        info = getattr(result, "whisper_info", None)
        return getattr(info, "task", None) != "transcribe"

    def _get_whisper_provider(self) -> TranscriptProvider:
        """Lazy-load the speech-to-text fallback provider.

        STT_BACKEND=groq (default) uses hosted Groq Whisper; STT_BACKEND=local uses
        faster-whisper, which needs the optional local ML dependencies.

        Raises:
            SpeechToTextUnavailableError: STT is disabled or not configured.
            ImportError: the local backend's dependencies are not installed.
        """
        if self._whisper_provider is None:
            from config.settings import settings

            if not settings.whisper_enabled:
                raise SpeechToTextUnavailableError("speech-to-text is disabled (WHISPER_ENABLED=false)")
            if settings.stt_backend == "groq":
                if not settings.groq_api_key:
                    raise SpeechToTextUnavailableError("GROQ_API_KEY is not configured")
                from clients.groq_stt_client import GroqSpeechToTextClient

                self._whisper_provider = WhisperProvider(stt_client=GroqSpeechToTextClient())
            else:
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
                elif cached.success and self._is_translated_whisper_result(cached):
                    # Produced while Whisper ran with task="translate": its "raw" text is an
                    # English translation, not the spoken original. Recompute instead.
                    logger.info("Ignoring cached translated Whisper transcript for %s; re-transcribing verbatim", video_id)
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

        # Captions in the spoken language are the canonical source in every output mode.
        # (Whisper transcribes verbatim, so re-running it for non-English captions would
        # not yield English; English/Hindi output comes from the translation step.)
        if best_step:
            result = self._finalize(best_step["result"], pipeline_steps, start_time, video_title=video_title, channel_title=channel_title, output_format=output_format)
            self._repository.save(result)
            return result

        # Stage 3: Whisper (only if both manual AND auto failed, and NOT in rate limit)
        if allow_whisper and not is_rate_limited:
            try:
                whisper_provider = self._get_whisper_provider()
            except (ImportError, SpeechToTextUnavailableError) as exc:
                logger.warning("Speech-to-text fallback unavailable for %s: %s", video_id, exc)
                pipeline_steps.append({
                    "name": "Whisper STT",
                    "status": "skipped",
                    "detail": "Speech-to-text is not available on this server.",
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
                    from services.transcript_limiter import transcript_limiter
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
                    from services.transcript_limiter import transcript_limiter
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
        if not isinstance(video_id, str) or not _VIDEO_ID_RE.match(video_id):
            raise InvalidVideoIdError(
                "Invalid video ID: expected 11 characters from [A-Za-z0-9_-]."
            )

    def clear_cache(self) -> None:
        """Clear the transcript result cache."""
        self._repository.clear()
        logger.info("Transcript service cache cleared")
