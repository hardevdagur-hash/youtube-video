"""On-demand translation service using Groq LLM with strict pedagogical formatting."""

import logging
import re
import threading
import time
from typing import Any, Dict, List, Literal, Optional

from config.settings import get_settings
from repositories.transcript_repository import TranscriptRepository
from services.translation.english import SIMPLE_ENGLISH_SYSTEM_PROMPT
from services.translation.hindi import SIMPLE_HINDI_SYSTEM_PROMPT
from services.transcription.groq import GroqAuthError

logger = logging.getLogger(__name__)

# Transcripts are translated in chunks so no single completion can hit the output
# token limit (a 30-minute video is ~4,500 words). Chunks end at sentence
# boundaries where possible; caption text without punctuation is split by words.
_CHUNK_WORDS = 1200
_MAX_OUTPUT_TOKENS = 8000
# Devanagari output tokenises far less efficiently than English.
_TOKENS_PER_WORD = {"en": 3, "hi": 6}
_SENTENCE_END = re.compile(r"(?<=[.!?\u0964])\s+")


def _split_into_chunks(text: str, max_words: int = _CHUNK_WORDS) -> list[str]:
    """Split ``text`` into chunks of at most ``max_words`` words, preferring sentence ends."""
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in _SENTENCE_END.split(text.strip()):
        words = sentence.split()
        if not words:
            continue
        if count and count + len(words) > max_words:
            chunks.append(" ".join(current))
            current, count = [], 0
        while len(words) > max_words:  # one very long unpunctuated "sentence"
            chunks.append(" ".join(words[:max_words]))
            words = words[max_words:]
        current.extend(words)
        count += len(words)
    if current:
        chunks.append(" ".join(current))
    return chunks


class TranslationError(Exception):
    """Raised when on-demand translation fails."""

    def __init__(self, message: str, error_code: str = "TRANSLATION_FAILED", retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.retryable = retryable


class TranslationService:
    """Handles on-demand Simple English and Simple Hindi translation with multi-tier caching."""

    def __init__(
        self,
        repository: Optional[TranscriptRepository] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        settings = get_settings()
        self.repository = repository or TranscriptRepository(persist_dir=str(settings.transcript_cache_dir))
        self.api_key = api_key or getattr(settings, "groq_api_key", "")
        self.model = model or getattr(settings, "groq_translation_model", "openai/gpt-oss-120b")
        self._client = None

    def _get_client(self):
        if not self.api_key:
            raise GroqAuthError(
                "Groq API key is not configured for translation. Please set GROQ_API_KEY in your .env file."
            )
        if self._client is None:
            from groq import Groq
            self._client = Groq(api_key=self.api_key, timeout=60)
        return self._client

    def translate(
        self,
        video_id: str,
        original_text: str,
        target_language: Literal["en", "hi"],
        source_language: str = "en",
        original_segments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Translate canonical original transcript into Simple English or Simple Hindi.

        Args:
            video_id: 11-character YouTube video ID.
            original_text: Canonical transcript text.
            target_language: "en" for Simple English, "hi" for Simple Hindi.
            source_language: Original source language of video.
            original_segments: Optional original timestamped segments.

        Returns:
            Dict with translated transcript, word count, cache status.
        """
        cache_sub_key = f"{target_language}:simple"
        logger.info("Translation requested for %s -> %s", video_id, target_language)

        # 1. Check Translation Cache: transcript:{video_id}:{target_language}:simple
        cached_translation = self.repository.get_translation(video_id, cache_sub_key)
        if cached_translation and cached_translation.plain_text:
            logger.info("Translation cache HIT for %s -> %s", video_id, target_language)
            return {
                "video_id": video_id,
                "source_language": source_language,
                "output_language": target_language,
                "provider": "cache",
                "transcript": cached_translation.plain_text,
                "segments": [
                    {
                        "start": s.start,
                        "end": s.end,
                        "duration": s.duration,
                        "text": s.text,
                    }
                    for s in cached_translation.segments
                ],
                "word_count": len(cached_translation.plain_text.split()),
                "from_cache": True,
            }

        logger.info("Translation cache MISS for %s -> %s. Invoking Groq LLM...", video_id, target_language)

        # 2. Select Prompt Specification
        if target_language == "en":
            system_prompt = SIMPLE_ENGLISH_SYSTEM_PROMPT
        elif target_language == "hi":
            system_prompt = SIMPLE_HINDI_SYSTEM_PROMPT
        else:
            raise TranslationError(f"Unsupported target language '{target_language}'. Allowed: 'en', 'hi'.")

        client = self._get_client()

        start_time = time.time()
        chunks = _split_into_chunks(original_text)
        if not chunks:
            raise TranslationError("Nothing to translate.", error_code="TRANSLATION_FAILED", retryable=False)
        instruction = "Simple English" if target_language == "en" else "Simple Hindi"
        translated_parts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            part_note = f" (part {index} of {len(chunks)}; output only this part)" if len(chunks) > 1 else ""
            max_output_tokens = min(
                _MAX_OUTPUT_TOKENS, len(chunk.split()) * _TOKENS_PER_WORD[target_language] + 200
            )
            try:
                chat_completion = client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": f"Convert the following transcript into {instruction}{part_note}:\n\n{chunk}",
                        },
                    ],
                    model=self.model,
                    temperature=0.2,
                    max_tokens=max_output_tokens,
                )
                choice = chat_completion.choices[0]
                content = (choice.message.content or "").strip()
            except Exception as exc:
                logger.error("Groq translation failed for %s (part %d/%d): %s", video_id, index, len(chunks), exc)
                err_str = str(exc).lower()
                if "authentication" in err_str or "api_key" in err_str:
                    raise GroqAuthError(f"Groq authentication failed during translation: {exc}") from exc
                raise TranslationError(f"Translation failed: {exc}", error_code="TRANSLATION_FAILED") from exc
            if getattr(choice, "finish_reason", None) == "length" or not content:
                # Never return or cache a silently truncated translation.
                logger.error(
                    "Groq translation for %s part %d/%d incomplete (finish_reason=%s)",
                    video_id, index, len(chunks), getattr(choice, "finish_reason", None),
                )
                raise TranslationError("Translation was incomplete.", error_code="TRANSLATION_FAILED")
            translated_parts.append(content)

        translated_text = "\n\n".join(translated_parts)
        logger.info(
            "Groq translation of %s -> %s completed in %.2fs (%d part(s))",
            video_id, target_language, time.time() - start_time, len(chunks),
        )

        # 3. Save to Derived Translation Cache
        try:
            from models.transcript import TranscriptResult as ModelTranscriptResult
            save_model = ModelTranscriptResult(
                video_id=video_id,
                language=target_language,
                plain_text=translated_text,
                segments=[],
                provider=f"groq_{self.model}",
                status="completed",
            )
            self.repository.save_translation(save_model, cache_sub_key)
            logger.info("Cached derived translation for %s -> %s", video_id, target_language)
        except Exception as exc:
            logger.warning("Failed to persist translation cache for %s: %s", video_id, exc)

        return {
            "video_id": video_id,
            "source_language": source_language,
            "output_language": target_language,
            "provider": f"groq_{self.model}",
            "transcript": translated_text,
            "segments": [],
            "word_count": len(translated_text.split()),
            "from_cache": False,
            "processing_time_seconds": round(time.time() - start_time, 2),
        }


_shared_service: TranslationService | None = None
_shared_lock = threading.Lock()


def get_translation_service() -> TranslationService:
    """Process-wide translation service, so every caller shares one translation cache."""
    global _shared_service
    with _shared_lock:
        if _shared_service is None:
            _shared_service = TranslationService()
        return _shared_service


def reset_translation_service() -> None:
    """Drop the shared instance (configuration reload and test isolation)."""
    global _shared_service
    with _shared_lock:
        _shared_service = None
