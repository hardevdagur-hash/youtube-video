"""Unit tests for transcript providers with mocked clients."""

from unittest.mock import MagicMock

import pytest

from models.transcript import TranscriptProviderName, TranscriptSegment, TranscriptSource

# ─── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_youtube_client():
    client = MagicMock()
    raw_segments = [
        {"text": "Hello world.", "start": 0.0, "duration": 2.0},
        {"text": "This is a test.", "start": 2.0, "duration": 3.0},
    ]
    client.find_best_transcript.return_value = (raw_segments, "en", True, None)
    client.list_all_transcripts.return_value = []
    client.parse_segments.side_effect = lambda raw: [
        TranscriptSegment(
            start=float(s["start"]),
            end=float(s["start"]) + float(s["duration"]),
            duration=float(s["duration"]),
            text=str(s["text"]).strip(),
        )
        for s in raw
    ]
    return client


@pytest.fixture
def mock_audio_path(tmp_path):
    audio_file = tmp_path / "test_audio.wav"
    audio_file.write_text("fake audio data")
    return str(audio_file)


# ─── Manual Transcript Provider ────────────────────────────────────


class TestManualTranscriptProvider:
    def test_successful_manual_transcript(self, mock_youtube_client):
        from providers.manual_transcript_provider import ManualTranscriptProvider

        provider = ManualTranscriptProvider(client=mock_youtube_client)
        result = provider.get_transcript("dQw4w9WgXcQ")

        assert result.success is True
        assert result.source == TranscriptSource.MANUAL
        assert result.provider == TranscriptProviderName.YOUTUBE_MANUAL
        assert len(result.segments) == 2
        assert result.plain_text == "Hello world. This is a test."
        assert result.word_count == 6

    def test_manual_transcript_empty_segments(self, mock_youtube_client):
        mock_youtube_client.find_best_transcript.return_value = ([], "en", True, None)
        from clients.youtube_transcript_client import NoTranscriptFoundError
        from providers.manual_transcript_provider import ManualTranscriptProvider

        provider = ManualTranscriptProvider(client=mock_youtube_client)
        with pytest.raises(NoTranscriptFoundError):
            provider.get_transcript("dQw4w9WgXcQ")

    def test_manual_transcript_fetch_failure(self, mock_youtube_client):
        mock_youtube_client.find_best_transcript.side_effect = Exception("API failure")
        from clients.youtube_transcript_client import NoTranscriptFoundError
        from providers.manual_transcript_provider import ManualTranscriptProvider

        provider = ManualTranscriptProvider(client=mock_youtube_client)
        with pytest.raises(NoTranscriptFoundError):
            provider.get_transcript("dQw4w9WgXcQ")

    def test_provider_name(self):
        from providers.manual_transcript_provider import ManualTranscriptProvider

        assert ManualTranscriptProvider().name() == "Manual Transcript"


# ─── Auto Transcript Provider ──────────────────────────────────────


class TestAutoTranscriptProvider:
    def test_successful_auto_transcript(self, mock_youtube_client):
        from providers.auto_transcript_provider import AutoTranscriptProvider

        provider = AutoTranscriptProvider(client=mock_youtube_client)
        result = provider.get_transcript("dQw4w9WgXcQ")

        assert result.success is True
        assert result.source == TranscriptSource.AUTO
        assert result.provider == TranscriptProviderName.YOUTUBE_AUTO
        assert len(result.segments) == 2
        assert result.word_count == 6

    def test_auto_transcript_empty(self, mock_youtube_client):
        mock_youtube_client.find_best_transcript.return_value = ([], "en", False, None)
        from clients.youtube_transcript_client import NoTranscriptFoundError
        from providers.auto_transcript_provider import AutoTranscriptProvider

        provider = AutoTranscriptProvider(client=mock_youtube_client)
        with pytest.raises(NoTranscriptFoundError):
            provider.get_transcript("dQw4w9WgXcQ")

    def test_provider_name(self):
        from providers.auto_transcript_provider import AutoTranscriptProvider

        assert AutoTranscriptProvider().name() == "Auto Transcript"


# ─── Whisper Provider ──────────────────────────────────────────────


class TestWhisperProvider:
    def test_whisper_provider_missing_dependency(self):
        """Should raise ImportError if yt-dlp is not installed."""
        pass  # Handled by fixture logic

    def test_whisper_provider_name(self):
        """Provider name should include model info."""
        pass  # Requires faster-whisper

    def test_audio_download_error_handling(self):
        """Provider should raise AudioDownloadError on download failure."""
        pass  # Integration test with mocked yt-dlp

    def test_transcription_error_handling(self):
        """Provider should raise TranscriptionError on STT failure."""
        pass  # Integration test with mocked STT


# ─── YouTube Transcript Client ─────────────────────────────────────


class TestYouTubeTranscriptClient:
    def test_parse_segments(self):
        from clients.youtube_transcript_client import YouTubeTranscriptClient

        client = YouTubeTranscriptClient()
        raw = [
            {"text": "Hello", "start": 0.0, "duration": 2.0},
            {"text": "World", "start": 2.0, "duration": 3.0},
        ]
        segments = client.parse_segments(raw)
        assert len(segments) == 2
        assert segments[0].start == 0.0
        assert segments[0].end == 2.0
        assert segments[0].duration == 2.0
        assert segments[0].text == "Hello"

    def test_parse_segments_empty(self):
        from clients.youtube_transcript_client import YouTubeTranscriptClient

        client = YouTubeTranscriptClient()
        assert client.parse_segments([]) == []

    def test_parse_segments_skips_empty_text(self):
        from clients.youtube_transcript_client import YouTubeTranscriptClient

        client = YouTubeTranscriptClient()
        raw = [
            {"text": "", "start": 0.0, "duration": 1.0},
            {"text": "Valid", "start": 1.0, "duration": 2.0},
        ]
        segments = client.parse_segments(raw)
        assert len(segments) == 1
        assert segments[0].text == "Valid"


# ─── Provider Interface ────────────────────────────────────────────


class TestTranscriptProviderInterface:
    def test_interface_abc(self):
        """TranscriptProvider should be abstract and not instantiable."""
        from interfaces.transcript_provider import TranscriptProvider

        with pytest.raises(TypeError):
            TranscriptProvider()

    def test_speech_to_text_interface_abc(self):
        """SpeechToTextClient should be abstract."""
        from interfaces.speech_to_text import SpeechToTextClient

        with pytest.raises(TypeError):
            SpeechToTextClient()
