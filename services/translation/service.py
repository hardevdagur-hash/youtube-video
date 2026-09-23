"""On-demand translation service using Groq LLM with strict pedagogical formatting."""

import logging
import time
from typing import Any, Dict, List, Literal, Optional

from config.settings import get_settings
from repositories.transcript_repository import TranscriptRepository
from services.translation.english import SIMPLE_ENGLISH_SYSTEM_PROMPT
from services.translation.hindi import SIMPLE_HINDI_SYSTEM_PROMPT
from services.transcription.groq import GroqAuthError

logger = logging.getLogger(__name__)


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
        self.repository = repository or TranscriptRepository(
            persist_dir=str(getattr(settings, "transcript_cache_dir", "data/transcripts"))
        )
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
            user_instruction = f"Convert the following transcript into Simple English:\n\n{original_text}"
        elif target_language == "hi":
            system_prompt = SIMPLE_HINDI_SYSTEM_PROMPT
            user_instruction = f"Convert the following transcript into Simple Hindi:\n\n{original_text}"
        else:
            raise TranslationError(f"Unsupported target language '{target_language}'. Allowed: 'en', 'hi'.")

        client = self._get_client()

        start_time = time.time()
        max_output_tokens = min(4096, max(300, len(original_text.split()) * 3 + 100))
        try:
            chat_completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_instruction},
                ],
                model=self.model,
                temperature=0.2,
                max_tokens=max_output_tokens,
            )
            translated_text = chat_completion.choices[0].message.content.strip()
            elapsed = time.time() - start_time
            logger.info("Groq translation completed in %.2fs", elapsed)
        except Exception as exc:
            logger.error("Groq translation failed for %s: %s", video_id, exc)
            err_str = str(exc).lower()
            if "authentication" in err_str or "api_key" in err_str:
                raise GroqAuthError(f"Groq authentication failed during translation: {exc}") from exc
            raise TranslationError(f"Translation failed: {exc}", error_code="TRANSLATION_FAILED") from exc

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
