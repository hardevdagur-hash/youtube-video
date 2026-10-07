"""Unit tests for TranscriptRepository."""

from pathlib import Path

import pytest

from models.transcript import TranscriptResult, TranscriptSource, TranscriptProviderName
from repositories.transcript_repository import TranscriptRepository
from utils.cache import TTLCache


@pytest.fixture
def sample_transcript():
    return TranscriptResult(
        video_id="dQw4w9WgXcQ",
        source=TranscriptSource.MANUAL,
        provider=TranscriptProviderName.YOUTUBE_MANUAL,
        language="en",
        segments=[],
        plain_text="Hello world test transcript.",
        word_count=4,
        character_count=28,
        estimated_read_time="< 1 min",
    )


class TestTranscriptRepository:
    def test_save_and_get(self, sample_transcript):
        repo = TranscriptRepository(cache=TTLCache(ttl_seconds=3600))
        repo.save(sample_transcript)

        result = repo.get("dQw4w9WgXcQ")
        assert result is not None
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.success is True
        assert result.plain_text == sample_transcript.plain_text

    def test_get_missing(self):
        repo = TranscriptRepository()
        assert repo.get("nonexistent12345") is None

    def test_get_expired(self):
        import time
        cache = TTLCache(ttl_seconds=0.001)
        repo = TranscriptRepository(cache=cache)
        repo.save(TranscriptResult(video_id="test12345678"))
        time.sleep(0.05)  # > Windows monotonic clock resolution (~15.6 ms)
        assert repo.get("test12345678") is None

    def test_delete(self, sample_transcript):
        repo = TranscriptRepository(cache=TTLCache(ttl_seconds=3600))
        repo.save(sample_transcript)
        repo.delete("dQw4w9WgXcQ")
        assert repo.get("dQw4w9WgXcQ") is None

    def test_clear(self, sample_transcript):
        repo = TranscriptRepository(cache=TTLCache(ttl_seconds=3600))
        repo.save(sample_transcript)
        repo.clear()
        assert repo.get("dQw4w9WgXcQ") is None

    def test_cached_count(self, sample_transcript):
        repo = TranscriptRepository(cache=TTLCache(ttl_seconds=3600))
        assert repo.cached_count == 0
        repo.save(sample_transcript)
        assert repo.cached_count == 1

    def test_persist_to_disk(self, sample_transcript, tmp_path):
        repo = TranscriptRepository(
            cache=TTLCache(ttl_seconds=3600),
            persist_dir=str(tmp_path),
        )
        repo.save(sample_transcript)
        assert (tmp_path / "dQw4w9WgXcQ.json").exists()

        # Reload from file
        repo2 = TranscriptRepository(
            cache=TTLCache(ttl_seconds=3600),
            persist_dir=str(tmp_path),
        )
        result = repo2.get("dQw4w9WgXcQ")
        assert result is not None
        assert result.plain_text == sample_transcript.plain_text

    def test_malformed_cache_data(self):
        cache = TTLCache[dict](ttl_seconds=3600)
        cache.set("transcript:bad", "not_a_dict")  # non-dict data
        repo = TranscriptRepository(cache=cache)
        result = repo.get("bad")
        assert result is None
