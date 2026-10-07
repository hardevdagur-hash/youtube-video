"""Unit tests for HinglishNormalizer and Hinglish pipeline integration."""

import pytest
from services.transliteration.hinglish_normalizer import HinglishNormalizer, hinglish_normalizer
from models.transcript import TranscriptResult, TranscriptSegment, TranscriptSource, TranscriptProviderName


class TestHinglishNormalizer:
    """Test suite for Hinglish / Roman Hindi transliteration."""

    def test_devanagari_sample_1(self):
        input_text = "आपको इस question को solve करना है और फिर इसका answer निकालना है।"
        expected = "Aapko is question ko solve karna hai aur phir iska answer nikalna hai."
        result = hinglish_normalizer.normalize(input_text)
        assert result == expected
        assert not hinglish_normalizer.contains_non_roman_script(result)

    def test_devanagari_sample_2(self):
        input_text = "Newton's second law को यहाँ apply करेंगे।"
        expected = "Newton's second law ko yahan apply karenge."
        result = hinglish_normalizer.normalize(input_text)
        assert result == expected
        assert not hinglish_normalizer.contains_non_roman_script(result)

    def test_devanagari_sample_3(self):
        input_text = "आज हम friction के बारे में पढ़ेंगे।"
        expected = "Aaj hum friction ke baare mein padhenge."
        result = hinglish_normalizer.normalize(input_text)
        assert result == expected
        assert not hinglish_normalizer.contains_non_roman_script(result)

    def test_english_text_remains_unchanged(self):
        input_text = "Newton's second law of motion"
        result = hinglish_normalizer.normalize(input_text)
        assert result == input_text
        assert not hinglish_normalizer.contains_non_roman_script(result)

    def test_urdu_script_normalization(self):
        input_text = "آپ اپنی کمانڈ بنا لیتے ہیں..."
        expected = "Aap apni command bana lete hain..."
        result = hinglish_normalizer.normalize(input_text)
        assert result == expected
        assert not hinglish_normalizer.contains_non_roman_script(result)

    def test_urdu_compound_verb(self):
        input_text = "ہم یہاں پر acceleration نکالیں گے"
        expected = "Hum yahan par acceleration nikalenge"
        result = hinglish_normalizer.normalize(input_text)
        assert result == expected
        assert not hinglish_normalizer.contains_non_roman_script(result)

    def test_hindi_verb_conjugation(self):
        input_text = "हम यहाँ पर acceleration निकालेंगे"
        expected = "Hum yahan par acceleration nikalenge"
        result = hinglish_normalizer.normalize(input_text)
        assert result == expected
        assert not hinglish_normalizer.contains_non_roman_script(result)

    def test_contains_non_roman_script(self):
        assert hinglish_normalizer.contains_non_roman_script("आपको") is True
        assert hinglish_normalizer.contains_non_roman_script("آپ") is True
        assert hinglish_normalizer.contains_non_roman_script("Newton's second law") is False
        assert hinglish_normalizer.contains_non_roman_script("Aapko is question ko solve karna hai.") is False

    def test_is_hinglish_or_hindi(self):
        assert hinglish_normalizer.is_hinglish_or_hindi("आपको इस question को solve करना है") is True
        assert hinglish_normalizer.is_hinglish_or_hindi("آپ اپنی کمانڈ بنا لیتے ہیں") is True
        assert hinglish_normalizer.is_hinglish_or_hindi("is video mein hum concepts padhenge aur solve karenge") is True
        assert hinglish_normalizer.is_hinglish_or_hindi("In this video we will discuss mechanics and thermodynamics") is False

    def test_empty_and_whitespace(self):
        assert hinglish_normalizer.normalize("") == ""
        assert hinglish_normalizer.normalize("   ") == ""

    def test_normalize_segments(self):
        segments = [
            TranscriptSegment(start=0.0, end=2.5, duration=2.5, text="आज हम friction के बारे में पढ़ेंगे।"),
            TranscriptSegment(start=2.5, end=5.0, duration=2.5, text="Newton's second law apply करेंगे।"),
        ]
        normalized = hinglish_normalizer.normalize_segments(segments)
        assert normalized[0].text == "Aaj hum friction ke baare mein padhenge."
        assert normalized[1].text == "Newton's second law apply karenge."
        assert not hinglish_normalizer.contains_non_roman_script(normalized[0].text)
        assert not hinglish_normalizer.contains_non_roman_script(normalized[1].text)


class TestTranscriptPipelineHinglishIntegration:
    """Test integration of Hinglish normalizer with TranscriptService and WhisperProvider."""

    def test_transcript_service_finalize_normalizes_hindi(self):
        from services.transcript_service import TranscriptService

        service = TranscriptService(use_cache=False)
        raw_text = "आज हम friction के बारे में पढ़ेंगे। Newton's second law को यहाँ apply करेंगे।"
        transcript = TranscriptResult(
            success=True,
            video_id="test_vid_123",
            source=TranscriptSource.AUTO,
            provider=TranscriptProviderName.YOUTUBE_AUTO,
            language="hi",
            plain_text=raw_text,
            paragraph_text=raw_text,
            segments=[
                TranscriptSegment(start=0.0, end=3.0, duration=3.0, text="आज हम friction के बारे में पढ़ेंगे।"),
                TranscriptSegment(start=3.0, end=6.0, duration=3.0, text="Newton's second law को यहाँ apply करेंगे।"),
            ],
            word_count=len(raw_text.split()),
            character_count=len(raw_text),
        )

        finalized = service._finalize(transcript, [], 0.0, output_format="hinglish")

        assert finalized.language in ("English (India)", "Hinglish")
        assert finalized.raw_transcript == raw_text
        assert "Aaj hum friction ke baare mein padhenge." in finalized.plain_text
        assert "Newton's second law ko yahan apply karenge." in finalized.plain_text
        assert not hinglish_normalizer.contains_non_roman_script(finalized.plain_text)

    def test_whisper_provider_hinglish_output(self):
        from providers.whisper_provider import WhisperProvider
        from interfaces.speech_to_text import SpeechToTextClient, TranscriptionResult, TranscriptionSegment

        class MockHindiSTTClient(SpeechToTextClient):
            def transcribe(self, audio_path: str, language: str | None = None, initial_prompt: str | None = None) -> TranscriptionResult:
                return TranscriptionResult(
                    segments=[
                        TranscriptionSegment(start=0.0, end=3.0, text="آپ اپنی کمانڈ بنا لیتے ہیں"),
                        TranscriptionSegment(start=3.0, end=6.0, text="اور acceleration نکالیں گے"),
                    ],
                    language="ur",
                    language_confidence=0.92,
                    duration_seconds=6.0,
                    processing_time_seconds=0.5,
                )

            def model_name(self) -> str:
                return "mock_hindi_whisper"

        class MockAudioService:
            def __init__(self, temp_dir=None):
                pass
            def download_audio(self, video_id: str):
                return "mock_audio.m4a"
            def cleanup(self, path):
                pass

        provider = WhisperProvider(stt_client=MockHindiSTTClient())
        provider._audio_service = MockAudioService()

        result = provider.get_transcript("test_vid_456")

        assert result.success is True
        assert result.language in ("English (India)", "Hinglish")
        assert "Aap apni command bana lete hain" in result.plain_text
        assert "nikalenge" in result.plain_text
        assert not hinglish_normalizer.contains_non_roman_script(result.plain_text)
        assert result.raw_transcript != ""

    def test_job_manager_csv_export_hinglish(self):
        import csv
        import io
        from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
        from services.jobs.transcript_job_manager import TranscriptJobManager

        manager = TranscriptJobManager()
        job = TranscriptJobProgress(
            job_id="job_hinglish_1",
            channel_handle="@physicsgalaxyworld",
            channel_id="UC_test",
            channel_title="Physics Galaxy",
            status=JobStatus.COMPLETED,
            eligible_videos=1,
            successful=1,
            videos=[
                TranscriptVideoItem(
                    video_id="KxzI2CqkD6g",
                    video_url="https://www.youtube.com/watch?v=KxzI2CqkD6g",
                    channel_id="UC_test",
                    channel_title="Physics Galaxy",
                    title="1 Que = IIT Selection | Episode 4 for JEE Advanced Mechanics",
                    published_at="2024-01-01T00:00:00Z",
                    duration_seconds=707,
                    duration="11:47",
                    language="Hinglish",
                    status="success",
                    transcript="Aap apni command bana lete hain to JEE Advanced paper will be a cake walk for you.",
                    raw_transcript="آپ اپنی کمانڈ بنا لیتے ہیں تو JEE Advanced paper will be a cake walk for you.",
                    source="whisper",
                    method="speech_to_text",
                )
            ],
        )
        manager._jobs[job.job_id] = job

        csv_content = manager.generate_csv(job.job_id)
        assert csv_content != ""

        reader = csv.DictReader(io.StringIO(csv_content))
        rows = list(reader)
        assert len(rows) == 1
        row = rows[0]

        # 15 columns verified
        assert len(row.keys()) == 15
        assert row["video_id"] == "KxzI2CqkD6g"
        assert row["language"] == "Hinglish"
        assert row["method"] == "speech_to_text"
        assert row["source"] == "whisper"
        assert "Aap apni command bana lete hain" in row["transcript"]
        assert "cake walk for you" in row["transcript"]
        assert not hinglish_normalizer.contains_non_roman_script(row["transcript"])

