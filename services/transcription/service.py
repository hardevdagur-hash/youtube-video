"""Canonical transcript acquisition service.

Implements the exact 8-step pipeline:
1. YouTube URL -> Validate URL -> Extract video_id
2. Check Redis/in-memory cache
3. Try YouTube captions (free, zero-cost)
4. If captions unavailable (CAPTIONS_DISABLED / UNAVAILABLE) -> Extract audio via yt-dlp -> Groq Whisper Large V3
5. Normalize transcript via 8-stage NLP cleaning
6. Validate transcript quality and check for ASR hallucination loops
7. Store canonical transcript in repository
8. Return structured response
"""

import logging
import threading
import time
import zlib
from typing import Any

from config.settings import get_settings
from repositories.transcript_repository import TranscriptRepository
from services.transcript_limiter import transcript_limiter
from services.transcription.cleaner import TranscriptCleaner
from services.transcription.groq import (
    GroqWhisperProvider,
)
from services.transcription.provider import TranscriptionProvider
from services.transcription.stt_gate import stt_gate
from services.transcription.validator import (
    TranscriptValidator,
)
from services.youtube.audio import (
    YouTubeAudioExtractor,
)
from services.youtube.captions import (
    CaptionsUnavailableError,
    YouTubeCaptionsService,
)
from services.youtube.resolver import YouTubeResolver

logger = logging.getLogger(__name__)

# Concurrent requests for the same uncached video wait for one acquisition instead of
# paying for captions/STT twice. A fixed pool of striped locks bounds memory (a lock
# per video id would grow forever); unrelated videos rarely share a stripe.
_LOCK_STRIPES = 64
_video_lock_stripes = tuple(threading.Lock() for _ in range(_LOCK_STRIPES))


#: Caption failures after which speech-to-text must not be attempted.
_NO_STT_FALLBACK_CODES = frozenset({"CAPTIONS_RATE_LIMITED", "BOT_BLOCKED", "VIDEO_UNAVAILABLE"})


def _get_video_lock(video_id: str) -> threading.Lock:
    return _video_lock_stripes[zlib.crc32(video_id.encode("utf-8")) % _LOCK_STRIPES]


class TranscriptService:
    """Orchestrates YouTube transcript acquisition with Groq Whisper Large V3 fallback."""

    def __init__(
        self,
        resolver: YouTubeResolver | None = None,
        captions_service: YouTubeCaptionsService | None = None,
        audio_extractor: YouTubeAudioExtractor | None = None,
        transcription_provider: TranscriptionProvider | None = None,
        cleaner: TranscriptCleaner | None = None,
        validator: TranscriptValidator | None = None,
        repository: TranscriptRepository | None = None,
    ) -> None:
        settings = get_settings()
        self.resolver = resolver or YouTubeResolver()
        self.captions_service = captions_service or YouTubeCaptionsService()
        self.audio_extractor = audio_extractor or YouTubeAudioExtractor(
            temp_dir=getattr(settings, "audio_temp_dir", None)
        )
        self.transcription_provider = transcription_provider or GroqWhisperProvider()
        self.cleaner = cleaner or TranscriptCleaner()
        self.validator = validator or TranscriptValidator()
        # Own namespace: the channel pipeline (services.transcript_service) caches
        # mode-dependent results under the same video ids in the parent directory.
        self.repository = repository or TranscriptRepository(
            persist_dir=str(settings.transcript_cache_dir / "canonical")
        )

    @staticmethod
    def _build_cache_response(video_id: str, cached_result) -> dict[str, Any]:
        logger.info("[Step 2] Cache HIT for canonical transcript %s", video_id)
        segments = [
            {
                "start": s.start,
                "end": s.end,
                "duration": s.duration,
                "text": s.text,
            }
            for s in cached_result.segments
        ]
        return {
            "video_id": video_id,
            "source_language": cached_result.language or "en",
            "provider": "cache",
            "original_provider": cached_result.provider or "youtube_captions",
            "transcript": cached_result.plain_text,
            "segments": segments,
            "word_count": len(cached_result.plain_text.split()),
            "duration_seconds": getattr(cached_result, "duration_seconds", None) or getattr(cached_result, "duration", 0.0),
            "confidence": 1.0,
            "from_cache": True,
        }

    def get_canonical_transcript(
        self,
        video_url_or_id: str,
        preferred_languages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch or generate canonical original transcript for a YouTube video.

        Follows Step 1 through Step 8 strictly.

        Returns:
            Dict containing canonical transcript payload:
            - video_id
            - source_language
            - provider ("youtube_captions" | "groq_whisper_large_v3" | "cache")
            - transcript (clean text)
            - segments
            - word_count
            - duration_seconds
            - confidence
            - from_cache (bool)
        """
        start_time = time.time()

        # STEP 1: Validate URL & extract video_id
        video_id = self.resolver.resolve_video_id(video_url_or_id)
        logger.info("[Step 1] Resolved video_id: %s", video_id)

        # STEP 2: Check Cache (Fast-path)
        cached_result = self.repository.get(video_id)
        if cached_result and cached_result.plain_text:
            return self._build_cache_response(video_id, cached_result)

        # Acquire lock to deduplicate simultaneous requests for the same uncached video
        with _get_video_lock(video_id):
            # Double-check cache under lock
            cached_result = self.repository.get(video_id)
            if cached_result and cached_result.plain_text:
                return self._build_cache_response(video_id, cached_result)

            logger.info("[Step 2] Cache MISS for %s", video_id)
            return self._acquire_and_persist(video_id, start_time, preferred_languages)

    def _acquire_and_persist(
        self,
        video_id: str,
        start_time: float,
        preferred_languages: list[str] | None = None,
    ) -> dict[str, Any]:
        raw_segments: list[dict[str, Any]] = []
        raw_text: str = ""
        source_language: str = "en"
        source_provider: str = ""
        duration_seconds: float | None = None

        # While YouTube is rate limiting / bot-checking this server, do not contact it at
        # all: neither captions nor an audio download would succeed, and both prolong the block.
        if transcript_limiter.is_in_cooldown():
            raise CaptionsUnavailableError(
                f"YouTube cooldown active ({transcript_limiter.remaining_cooldown_seconds():.0f}s left)",
                error_code="CAPTIONS_RATE_LIMITED",
            )

        # STEP 3: Try YouTube Captions
        captions_available = False
        try:
            logger.info("[Step 3] Trying YouTube Captions for %s", video_id)
            cap_segments, cap_lang, is_manual = self.captions_service.fetch_captions(
                video_id=video_id,
                preferred_languages=preferred_languages or ["en", "hi"],
            )
            raw_segments = cap_segments
            source_language = cap_lang
            source_provider = "youtube_captions"
            captions_available = True
            transcript_limiter.record_success(video_id)
            logger.info(
                "[Step 3] YouTube captions available for %s (%d segments, lang=%s)",
                video_id,
                len(raw_segments),
                source_language,
            )
        except CaptionsUnavailableError as cap_err:
            if cap_err.error_code in _NO_STT_FALLBACK_CODES:
                # Rate limit / bot check: more YouTube traffic (audio download) and Groq
                # spend would not help. Unavailable video: there is no audio to transcribe.
                if cap_err.error_code in ("CAPTIONS_RATE_LIMITED", "BOT_BLOCKED"):
                    transcript_limiter.record_rate_limit(video_id)
                logger.warning(
                    "[Step 3] Captions failed for %s (%s); not falling back to STT",
                    video_id, cap_err.error_code,
                )
                raise
            logger.info(
                "[Step 4] YouTube captions unavailable for %s (%s). Falling back to STT.",
                video_id,
                cap_err.message,
            )

        # STEP 4: Fallback to Audio Extraction + Groq Whisper Large V3 (one process-wide slot)
        if not captions_available:
            logger.info("[Step 4] Starting audio acquisition and Groq Whisper Large V3...")
            with stt_gate.slot(video_id), self.audio_extractor.audio_context(video_id) as audio_path:
                logger.info("[Step 4] Audio ready at %s. Sending to Groq Whisper...", audio_path)
                stt_result = self.transcription_provider.transcribe(audio_path)
                raw_text = stt_result.text
                source_language = stt_result.language
                duration_seconds = stt_result.duration
                source_provider = "groq_whisper_large_v3"
                raw_segments = [s.to_dict() for s in stt_result.segments]
                logger.info(
                    "[Step 4] Groq transcription successful for %s (words=%d, lang=%s)",
                    video_id,
                    len(raw_text.split()),
                    source_language,
                )

        # STEP 5: Normalize Transcript
        logger.info("[Step 5] Normalizing transcript for %s...", video_id)
        cleaned = self.cleaner.clean(
            raw_segments=raw_segments,
            video_id=video_id,
            raw_text=raw_text,
        )
        clean_text = cleaned["text"]
        clean_segments = cleaned["segments"]
        word_count = cleaned["word_count"]

        # Calculate duration from segments if not provided
        if duration_seconds is None or duration_seconds == 0.0:
            if clean_segments:
                last_seg = clean_segments[-1]
                duration_seconds = float(last_seg.get("end", last_seg.get("start", 0.0)))
            else:
                duration_seconds = 0.0

        # STEP 6: Validate Transcript Quality
        logger.info("[Step 6] Running quality validation for %s...", video_id)
        validation_report = self.validator.validate(
            text=clean_text,
            segments=clean_segments,
            duration_seconds=duration_seconds,
        )

        # STEP 7: Store Canonical Transcript
        logger.info("[Step 7] Storing canonical transcript for %s...", video_id)
        try:
            from models.transcript import TranscriptResult as ModelTranscriptResult
            from models.transcript import TranscriptSegment as ModelTranscriptSegment

            model_segments = [
                ModelTranscriptSegment(
                    start=s["start"],
                    end=s["end"],
                    duration=s["duration"],
                    text=s["text"],
                )
                for s in clean_segments
            ]
            save_model = ModelTranscriptResult(
                video_id=video_id,
                language=source_language,
                plain_text=clean_text,
                segments=model_segments,
                provider=source_provider,
                duration_seconds=duration_seconds or 0.0,
                status="completed",
            )
            self.repository.save(save_model)
            logger.info("[Step 7] Canonical transcript saved for %s", video_id)
        except Exception as e:
            logger.warning("Failed to persist canonical transcript for %s: %s", video_id, e)

        # STEP 8: Return Response
        total_time = round(time.time() - start_time, 2)
        logger.info("[Step 8] Transcript acquisition finished for %s in %.2fs", video_id, total_time)

        return {
            "video_id": video_id,
            "source_language": source_language,
            "provider": source_provider,
            "transcript": clean_text,
            "segments": clean_segments,
            "word_count": word_count,
            "duration_seconds": duration_seconds,
            "confidence": validation_report.confidence,
            "from_cache": False,
            "processing_time_seconds": total_time,
        }
