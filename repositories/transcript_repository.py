"""Transcript repository — caching and persistence layer.

Provides a clean abstraction over transcript storage,
allowing future migration to Redis, PostgreSQL, or S3
without changing service-layer code.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from models.transcript import TranscriptResult
from utils.cache import TTLCache

logger = logging.getLogger(__name__)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_:-]{1,32}$")


class TranscriptRepository:
    """Repository for transcript CRUD operations.

    Current implementation uses an in-memory TTL cache with optional
    JSON file persistence. Designed for easy Redis replacement.
    """

    def __init__(
        self,
        cache: TTLCache[dict[str, Any]] | None = None,
        persist_dir: str | None = None,
        cache_ttl: float = 3600,
    ) -> None:
        self._cache = cache or TTLCache[dict[str, Any]](ttl_seconds=cache_ttl)
        self._persist_dir = Path(persist_dir) if persist_dir else None

        if self._persist_dir:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            self._persist_root = self._persist_dir.resolve()

    def _file_for(self, video_id: str, language: str | None = None) -> Path | None:
        """Cache file for a video (and optional language), or None if the key is unsafe.

        Keys come from YouTube and request input, so they are validated and the final
        path must stay inside the cache directory (no traversal via ``..`` or separators).
        """
        if not self._persist_dir or not isinstance(video_id, str) or not _VIDEO_ID_RE.match(video_id):
            if self._persist_dir:
                logger.warning("Refusing transcript cache path for invalid video id %r", str(video_id)[:32])
            return None
        name = video_id
        if language is not None:
            if not _LANGUAGE_RE.match(language):
                logger.warning("Refusing transcript cache path for invalid language %r", language[:32])
                return None
            name = f"{video_id}_{language.replace(':', '_')}"
        path = (self._persist_dir / f"{name}.json").resolve()
        if not path.is_relative_to(self._persist_root):
            logger.warning("Refusing transcript cache path outside %s", self._persist_root)
            return None
        return path

    @staticmethod
    def _write_atomic(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, video_id: str) -> TranscriptResult | None:
        """Retrieve a cached transcript by video ID.

        Args:
            video_id: 11-character YouTube video ID.

        Returns:
            ``TranscriptResult`` if found, else None.
        """
        cache_key = f"transcript:{video_id}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Transcript cache HIT for %s", video_id)
            try:
                return TranscriptResult(**cached)
            except Exception:
                logger.exception("Failed to deserialize cached transcript for %s", video_id)
                self._cache.delete(cache_key)

        # Check file persistence
        file_path = self._file_for(video_id)
        if file_path is not None:
            if file_path.exists():
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    self._cache.set(cache_key, data)
                    logger.debug("Transcript file cache HIT for %s", video_id)
                    return TranscriptResult(**data)
                except Exception as exc:
                    logger.exception("Failed to read persisted transcript for %s: %s", video_id, exc)

        logger.debug("Transcript cache MISS for %s", video_id)
        return None

    def save(self, transcript: TranscriptResult) -> None:
        """Save a transcript result.

        Args:
            transcript: The transcript to cache/persist.
        """
        cache_key = f"transcript:{transcript.video_id}"
        data = transcript.model_dump()

        self._cache.set(cache_key, data)

        file_path = self._file_for(transcript.video_id)
        if file_path is not None:
            try:
                self._write_atomic(file_path, data)
                logger.debug("Transcript persisted for %s", transcript.video_id)
            except Exception as exc:
                logger.warning("Failed to persist transcript for %s: %s", transcript.video_id, exc)

    def delete(self, video_id: str) -> None:
        """Remove a cached transcript.

        Args:
            video_id: 11-character YouTube video ID.
        """
        cache_key = f"transcript:{video_id}"
        self._cache.delete(cache_key)

        file_path = self._file_for(video_id)
        if file_path is not None:
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception as exc:
                logger.exception("Failed to delete persisted transcript for %s: %s", video_id, exc)

    def get_translation(self, video_id: str, language: str) -> TranscriptResult | None:
        """Retrieve a cached translation by video ID and language code.

        Args:
            video_id: 11-character YouTube video ID.
            language: Target language code (e.g. 'en', 'hi', 'en:simple').

        Returns:
            ``TranscriptResult`` if found, else None.
        """
        cache_key = f"transcript:{video_id}:{language}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Translation cache HIT for %s/%s", video_id, language)
            try:
                return TranscriptResult(**cached)
            except Exception:
                logger.exception("Failed to deserialize cached translation for %s/%s", video_id, language)
                self._cache.delete(cache_key)

        # Check disk persistence
        file_path = self._file_for(video_id, language)
        if file_path is not None:
            if file_path.exists():
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    self._cache.set(cache_key, data)
                    logger.debug("Translation disk cache HIT for %s/%s", video_id, language)
                    return TranscriptResult(**data)
                except Exception as exc:
                    logger.exception("Failed to read persisted translation for %s/%s: %s", video_id, language, exc)

        logger.debug("Translation cache MISS for %s/%s", video_id, language)
        return None

    def save_translation(self, transcript: TranscriptResult, language: str) -> None:
        """Save a translated transcript result.

        Args:
            transcript: The translated transcript to cache.
            language: The target language code.
        """
        cache_key = f"transcript:{transcript.video_id}:{language}"
        data = transcript.model_dump()
        self._cache.set(cache_key, data)

        file_path = self._file_for(transcript.video_id, language)
        if file_path is not None:
            try:
                self._write_atomic(file_path, data)
                logger.debug("Translation persisted for %s/%s", transcript.video_id, language)
            except Exception as exc:
                logger.warning("Failed to persist translation for %s/%s: %s",
                               transcript.video_id, language, exc)

    def clear(self) -> None:
        """Clear all cached transcripts."""
        self._cache.clear()
        logger.info("Transcript repository cache cleared")

    @property
    def cached_count(self) -> int:
        """Number of transcripts currently cached in memory."""
        return self._cache.size
