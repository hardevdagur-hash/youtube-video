"""Comprehensive tests verifying True Original Spoken script preservation and CSV export integrity."""

import csv
import io
from unittest.mock import MagicMock

from models.transcript import (
    TranscriptProviderName,
    TranscriptResult,
    TranscriptSegment,
    TranscriptSource,
)
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import TranscriptJobManager
from services.transcript_service import TranscriptService
from webapp.main import _build_csv_rows


class TestOriginalSpokenIntegrity:
    """Verifies that Original Spoken mode is strictly lossless and preserves source script."""

    def test_finalize_preserves_devanagari_script_in_original_mode(self):
        """Verifies that _finalize preserves Hindi Devanagari without transliterating to Roman characters."""
        service = TranscriptService(use_cache=False)
        hindi_raw = "मैं कहता हूं मैट्रिक्स जाओ। वो कहते हैं स्ट्रेंथ क्या है? मैं कहता हूं आप सक्सेस रेट देखो।"

        transcript = TranscriptResult(
            success=True,
            video_id="5HR9z5Gh26c",
            source=TranscriptSource.AUTO,
            provider=TranscriptProviderName.YOUTUBE_AUTO,
            language="hi",
            plain_text=hindi_raw,
            paragraph_text=hindi_raw,
            segments=[
                TranscriptSegment(start=0.0, end=3.0, duration=3.0, text="मैं कहता हूं मैट्रिक्स जाओ।"),
                TranscriptSegment(start=3.0, end=6.0, duration=3.0, text="वो कहते हैं स्ट्रेंथ क्या है?"),
            ],
            word_count=len(hindi_raw.split()),
            character_count=len(hindi_raw),
        )

        finalized = service._finalize(transcript, [], 0.0, output_format="original")

        # Invariant 1: raw_transcript is identical to source
        assert finalized.raw_transcript == hindi_raw
        # Invariant 2: plain_text is preserved in original Devanagari script (NO Romanization)
        assert finalized.plain_text == hindi_raw
        assert "मैं कहता हूं मैट्रिक्स जाओ" in finalized.plain_text
        assert "Main khta hoon" not in finalized.plain_text
        # Invariant 3: Source language is accurately documented
        assert finalized.language == "Hindi"
        assert finalized.source_language == "Hindi"
        assert finalized.source_language_code == "hi"

    def test_cache_healing_for_original_mode(self):
        """Verifies that cached items with old Romanized plain_text are healed to raw_transcript when requested in original mode."""
        service = TranscriptService(use_cache=False)
        hindi_raw = "एनटीए ने सुप्रीम कोर्ट में एक बात लिख कर दी है"
        romanized_legacy = "Entie ne suprim kort mein ek baat likh kar di hai"

        cached_mock = TranscriptResult(
            success=True,
            video_id="la-6erBNvE8",
            source=TranscriptSource.AUTO,
            provider=TranscriptProviderName.YOUTUBE_AUTO,
            language="English (India)",  # old corrupted metadata
            plain_text=romanized_legacy,  # old corrupted transliteration
            raw_transcript=hindi_raw,     # preserved source
            segments=[],
        )

        service._repository = MagicMock()
        service._repository.get.return_value = cached_mock
        service._use_cache = True

        result = service.get_transcript("la-6erBNvE8", allow_whisper=False, output_format="original")

        assert result.success is True
        assert result.plain_text == hindi_raw
        assert result.language == "Hindi"
        assert result.source_language == "Hindi"
        assert result.source_language_code == "hi"

    def test_job_manager_csv_export_contains_hindi_script_for_original_job(self):
        """Verifies that TranscriptJobManager.generate_csv exports Hindi Unicode script when output_language is 'original'."""
        manager = TranscriptJobManager()

        hindi_text = "मैं कहता हूं मैट्रिक्स जाओ। वो कहते हैं स्ट्रेंथ क्या है?"
        roman_text = "Main khta hoon maitriks jao. Woh kehte hain stremth kya hai?"

        job = TranscriptJobProgress(
            job_id="test_job_hindi_orig",
            channel_handle="matrixacademysikar",
            channel_id="UC_matrix",
            channel_title="Matrix Academy",
            status=JobStatus.COMPLETED,
            output_language="original",
            videos=[
                TranscriptVideoItem(
                    video_id="5HR9z5Gh26c",
                    video_url="https://www.youtube.com/watch?v=5HR9z5Gh26c",
                    title="1.5 महीने में NEET Rank का पूरा Game बदल दिया",
                    duration_seconds=300,
                    duration="5:00",
                    language="English (India)",  # legacy value
                    status="success",
                    transcript=roman_text,       # legacy value
                    raw_transcript=hindi_text,   # canonical source
                    source="youtube",
                    method="caption",
                )
            ],
        )
        manager._jobs[job.job_id] = job

        # 1. Standard CSV export
        csv_str = manager.generate_csv(job.job_id)
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)

        assert len(rows) == 1
        row = rows[0]
        assert row["video_id"] == "5HR9z5Gh26c"
        assert row["transcript"] == hindi_text
        assert "मैं कहता हूं मैट्रिक्स जाओ" in row["transcript"]
        assert "Main khta hoon" not in row["transcript"]
        assert row["language"] == "Hindi"

        # 2. Auditable CSV export with extra columns
        csv_audit = manager.generate_csv(job.job_id, include_audit_columns=True)
        audit_reader = csv.DictReader(io.StringIO(csv_audit))
        audit_rows = list(audit_reader)
        audit_row = audit_rows[0]

        assert audit_row["output_format"] == "original"
        assert audit_row["source_language"] == "Hindi"
        assert audit_row["source_language_code"] == "hi"

    def test_build_csv_rows_mode_separation(self):
        """Verifies that _build_csv_rows outputs correct representation for all 3 modes."""
        videos = [{
            "video_id": "test_modes_vid",
            "video_url": "https://www.youtube.com/watch?v=test_modes_vid",
            "channel_id": "c1",
            "channel_title": "Channel 1",
            "title": "NEET Success Story",
            "published_at": "2026-01-01T00:00:00Z",
            "duration_seconds": "180",
            "duration": "3:00",
            "language": "Hindi",
            "source_language": "Hindi",
            "source_language_code": "hi",
            "status": "success",
            "raw_transcript": "मैं कहता हूं मैट्रिक्स जाओ",
            "transcript": "Main khta hoon maitriks jao",
            "simple_english_transcript": "I advise students to join Matrix Academy.",
            "simple_hindi_transcript": "मैं कहता हूँ मैट्रिक्स जॉइन करो।",
            "source": "youtube",
            "method": "caption",
        }]

        # Mode 1: Original Spoken -> must output raw Devanagari Hindi
        csv_orig = _build_csv_rows(videos, output_language="original")
        r_orig = list(csv.DictReader(io.StringIO(csv_orig)))[0]
        assert r_orig["transcript"] == "मैं कहता हूं मैट्रिक्स जाओ"
        assert r_orig["language"] == "Hindi"

        # Mode 2: Simple English -> must output simplified English
        csv_en = _build_csv_rows(videos, output_language="en")
        r_en = list(csv.DictReader(io.StringIO(csv_en)))[0]
        assert r_en["transcript"] == "I advise students to join Matrix Academy."
        assert r_en["language"] == "Simple English"

        # Mode 3: Simple Hindi -> must output simplified Hindi
        csv_hi = _build_csv_rows(videos, output_language="hi")
        r_hi = list(csv.DictReader(io.StringIO(csv_hi)))[0]
        assert r_hi["transcript"] == "मैं कहता हूँ मैट्रिक्स जॉइन करो।"
        assert r_hi["language"] == "Simple Hindi"
