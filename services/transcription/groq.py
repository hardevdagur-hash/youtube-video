"""Groq Whisper Large V3 Speech-to-Text provider."""

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from config.settings import get_settings
from services.transcription.audio_chunking import AudioChunk, AudioChunkingError, split_audio
from services.transcription.provider import (
    TranscriptionProvider,
    TranscriptionResult,
    TranscriptionSegment,
)

logger = logging.getLogger(__name__)

# Groq rejects uploads over 25 MB; stay a little below it for multipart overhead.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024


class GroqTranscriptionError(Exception):
    """Base exception for Groq transcription failures."""

    def __init__(
        self,
        message: str,
        error_code: str = "TRANSCRIPTION_FAILED",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable


class GroqAuthError(GroqTranscriptionError):
    def __init__(self, message: str = "Groq API key is missing or invalid.") -> None:
        super().__init__(message, error_code="GROQ_AUTH_ERROR", retryable=False)


class GroqRateLimitError(GroqTranscriptionError):
    def __init__(self, message: str = "Groq API rate limit reached. Please try again.") -> None:
        super().__init__(message, error_code="GROQ_RATE_LIMIT", retryable=True)


class GroqTimeoutError(GroqTranscriptionError):
    def __init__(self, message: str = "Groq transcription request timed out.") -> None:
        super().__init__(message, error_code="GROQ_TIMEOUT", retryable=True)


class GroqWhisperProvider(TranscriptionProvider):
    """Speech-to-text provider using Groq's Whisper Large V3 API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else getattr(settings, "groq_api_key", "")
        self.model = model or getattr(settings, "groq_whisper_model", "whisper-large-v3")
        self.timeout = timeout or getattr(settings, "groq_timeout_seconds", 120)
        self.max_retries = max_retries or getattr(settings, "groq_max_retries", 3)

        self._client = None
        if self.api_key:
            try:
                from groq import Groq
                self._client = Groq(api_key=self.api_key, timeout=self.timeout)
            except Exception as e:
                logger.warning("Failed to instantiate Groq client: %s", e)

    def _get_client(self):
        if not self.api_key:
            raise GroqAuthError(
                "Groq API key is not configured. Please set GROQ_API_KEY in your environment or .env file."
            )
        if self._client is None:
            from groq import Groq
            self._client = Groq(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def transcribe(
        self, audio_path: Path, language: str | None = None
    ) -> TranscriptionResult:
        """Transcribe an audio file using Groq Whisper Large V3.

        Files within the upload limit are sent as-is; larger files are split into
        chunks (see ``audio_chunking``) and the results merged.

        Args:
            audio_path: Path to local audio file.
            language: Optional language hint (e.g. 'en', 'hi').

        Returns:
            TranscriptionResult with segments, full text, and detected language.

        Raises:
            GroqAuthError: If API key is missing or unauthorized.
            GroqRateLimitError: If rate limits are exceeded after retries.
            GroqTimeoutError: If request times out.
            GroqTranscriptionError: For other transcription failures, including audio
                longer than STT_MAX_AUDIO_SECONDS (error_code AUDIO_TOO_LONG).
        """
        self._get_client()  # fail fast on a missing key, before any audio work

        if not audio_path.exists() or audio_path.stat().st_size == 0:
            raise GroqTranscriptionError(
                f"Audio file not found or empty: {audio_path}",
                error_code="AUDIO_EXTRACTION_FAILED",
                retryable=False,
            )

        if audio_path.stat().st_size <= MAX_UPLOAD_BYTES:
            return self._transcribe_file(audio_path, language)
        return self._transcribe_chunked(audio_path, language)

    def _transcribe_chunked(self, audio_path: Path, language: str | None) -> TranscriptionResult:
        """Transcribe audio over the upload limit as consecutive chunks, then merge.

        All-or-nothing: if any chunk fails the whole transcription fails, so a partial
        transcript is never returned as if it were complete.
        """
        settings = get_settings()
        chunk_seconds = int(getattr(settings, "stt_chunk_seconds", 600))
        max_seconds = int(getattr(settings, "stt_max_audio_seconds", 7200))
        with tempfile.TemporaryDirectory(prefix=f"{audio_path.stem}_chunks_", dir=audio_path.parent) as tmp:
            try:
                chunks = split_audio(audio_path, Path(tmp), chunk_seconds)
            except AudioChunkingError as exc:
                raise GroqTranscriptionError(
                    f"Could not split long audio for transcription: {exc}",
                    error_code="STT_FAILED",
                    retryable=False,
                ) from exc
            total = chunks[-1].end
            if total > max_seconds:
                # Duration was unknown before download; enforce the cap before spending credits.
                raise GroqTranscriptionError(
                    f"Audio is {total:.0f}s long; the speech-to-text limit is {max_seconds}s.",
                    error_code="AUDIO_TOO_LONG",
                    retryable=False,
                )
            logger.info(
                "Audio %s exceeds the %d MB upload limit; transcribing %d chunk(s)",
                audio_path.name, MAX_UPLOAD_BYTES // (1024 * 1024), len(chunks),
            )
            parts: list[tuple[AudioChunk, TranscriptionResult]] = []
            for index, chunk in enumerate(chunks, start=1):
                logger.info("Transcribing chunk %d/%d (%.0fs-%.0fs)", index, len(chunks), chunk.start, chunk.end)
                parts.append((chunk, self._transcribe_file(chunk.path, language)))
        return self._merge_chunks(parts)

    @staticmethod
    def _merge_chunks(parts: list[tuple[AudioChunk, TranscriptionResult]]) -> TranscriptionResult:
        """Concatenate chunk results with segment times shifted to the original timeline."""
        segments: list[TranscriptionSegment] = []
        texts: list[str] = []
        seconds_by_language: dict[str, float] = {}
        for chunk, result in parts:
            if result.text:
                texts.append(result.text)
            for seg in result.segments:
                segments.append(TranscriptionSegment(
                    start=round(seg.start + chunk.start, 3),
                    end=round(seg.end + chunk.start, 3),
                    text=seg.text,
                    duration=seg.duration,
                ))
            if result.text:
                seconds_by_language[result.language] = (
                    seconds_by_language.get(result.language, 0.0) + (chunk.end - chunk.start)
                )
        # The language spoken for most of the audio (chunks of silence or music do not vote).
        language = max(seconds_by_language, key=seconds_by_language.__getitem__) if seconds_by_language else "en"
        return TranscriptionResult(
            text=" ".join(texts).strip(),
            segments=segments,
            language=language,
            duration=float(parts[-1][0].end) if parts else 0.0,
            confidence=0.95,
            provider="groq_whisper_large_v3",
        )

    def _transcribe_file(self, audio_path: Path, language: str | None) -> TranscriptionResult:
        """One upload (within the size limit) with retries for transient failures."""
        client = self._get_client()
        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        logger.info(
            "Sending %s (%.2f MB) to Groq %s (lang=%s)",
            audio_path.name,
            file_size_mb,
            self.model,
            language or "auto",
        )

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with open(audio_path, "rb") as audio_file:
                    kwargs: dict[str, Any] = {
                        "file": (audio_path.name, audio_file),
                        "model": self.model,
                        "response_format": "verbose_json",
                        "temperature": 0.0,
                    }
                    if language:
                        kwargs["language"] = language

                    start_time = time.time()
                    response = client.audio.transcriptions.create(**kwargs)
                    elapsed = time.time() - start_time
                    logger.info(
                        "Groq Whisper transcription completed in %.2fs (attempt %d)",
                        elapsed,
                        attempt,
                    )

                return self._parse_response(response)

            except Exception as exc:
                last_exc = exc
                error_str = str(exc).lower()
                status_code = getattr(exc, "status_code", None)

                logger.warning(
                    "Groq Whisper attempt %d/%d failed: %s (status=%s)",
                    attempt,
                    self.max_retries,
                    exc,
                    status_code,
                )

                if status_code == 401 or "authentication" in error_str or "api_key" in error_str:
                    raise GroqAuthError(f"Groq authentication failed: {exc}") from exc

                if status_code == 429 or "rate_limit" in error_str:
                    if attempt == self.max_retries:
                        raise GroqRateLimitError(f"Groq rate limit exceeded: {exc}") from exc
                    backoff = 2 ** attempt
                    logger.info("Rate limited by Groq. Backing off for %ds...", backoff)
                    time.sleep(backoff)
                    continue

                if "timeout" in error_str:
                    if attempt == self.max_retries:
                        raise GroqTimeoutError(f"Groq request timed out after {self.timeout}s: {exc}") from exc
                    time.sleep(1)
                    continue

                # Unrecoverable error
                if attempt == self.max_retries:
                    break
                time.sleep(1)

        raise GroqTranscriptionError(
            f"Groq transcription failed after {self.max_retries} attempts: {last_exc}",
            error_code="TRANSCRIPTION_FAILED",
            retryable=True,
        ) from last_exc

    def _parse_response(self, response: Any) -> TranscriptionResult:
        """Parse verbose_json response from Groq Whisper."""
        # Check if response is dict or object
        if hasattr(response, "text"):
            raw_text = response.text or ""
            raw_segments = getattr(response, "segments", []) or []
            detected_lang = getattr(response, "language", "en") or "en"
            duration = getattr(response, "duration", 0.0) or 0.0
        elif isinstance(response, dict):
            raw_text = response.get("text", "")
            raw_segments = response.get("segments", [])
            detected_lang = response.get("language", "en")
            duration = response.get("duration", 0.0)
        else:
            raw_text = str(response)
            raw_segments = []
            detected_lang = "en"
            duration = 0.0

        segments: list[TranscriptionSegment] = []
        for s in raw_segments:
            if isinstance(s, dict):
                s_start = float(s.get("start", 0.0))
                s_end = float(s.get("end", s_start))
                s_text = str(s.get("text", "")).strip()
            else:
                s_start = float(getattr(s, "start", 0.0))
                s_end = float(getattr(s, "end", s_start))
                s_text = str(getattr(s, "text", "")).strip()

            if s_text:
                segments.append(
                    TranscriptionSegment(
                        start=s_start,
                        end=s_end,
                        text=s_text,
                        duration=max(0.0, s_end - s_start),
                    )
                )

        # Standardize language codes
        lang_code = self._normalize_language_code(detected_lang)

        return TranscriptionResult(
            text=raw_text.strip(),
            segments=segments,
            language=lang_code,
            duration=float(duration),
            confidence=0.95,
            provider="groq_whisper_large_v3",
        )

    @staticmethod
    def _normalize_language_code(lang: str) -> str:
        code = (lang or "en").lower().strip()
        lang_map = {
            "english": "en",
            "hindi": "hi",
            "spanish": "es",
            "french": "fr",
            "german": "de",
            "japanese": "ja",
            "chinese": "zh",
            "russian": "ru",
        }
        return lang_map.get(code, code[:2] if len(code) >= 2 else "en")
