"""Unit and integration tests for Simple English default transcript output."""

from unittest.mock import MagicMock, patch

import pytest

from webapp.main import UnifiedTranscriptRequest


@pytest.fixture
def client(authed_client):
    return authed_client


def test_unified_transcript_request_default():
    """Verify that UnifiedTranscriptRequest defaults output_language to 'en'."""
    req = UnifiedTranscriptRequest(video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert req.output_language == "en"


def test_post_transcript_defaults_to_simple_english(client):
    """POST /api/transcript should default to 'en' (Simple English) and return both transcript & raw_transcript."""
    canonical_mock = {
        "video_id": "test_sample_1",
        "source_language": "hi",
        "transcript": "Namaste dosto, aaj hum seekhenge web development.",
        "segments": [{"start": 0.0, "end": 3.0, "duration": 3.0, "text": "Namaste dosto"}],
        "provider": "youtube_captions",
        "duration_seconds": 180,
        "confidence": 0.98,
        "from_cache": True,
    }

    translation_mock = {
        "video_id": "test_sample_1",
        "source_language": "hi",
        "output_language": "en",
        "provider": "groq_openai/gpt-oss-120b",
        "transcript": "Hello friends, today we will learn web development.",
        "segments": [],
        "word_count": 8,
        "from_cache": False,
    }

    mock_ts_svc = MagicMock()
    mock_ts_svc.get_canonical_transcript.return_value = canonical_mock

    mock_trans_svc = MagicMock()
    mock_trans_svc.translate.return_value = translation_mock

    with patch("webapp.main._get_unified_services", return_value=(mock_ts_svc, mock_trans_svc)), \
         patch("webapp.main._get_video_service") as mock_vsvc:
        mock_vsvc.return_value.get_videos_batch.return_value = [
            {"snippet": {"title": "Web Dev Hindi Tutorial"}}
        ]

        # Call with NO output_language specified in request body
        resp = client.post("/api/transcript", json={"video_url": "https://www.youtube.com/watch?v=test_sample_1"})
        assert resp.status_code == 200
        data = resp.json()

        assert data["success"] is True
        assert data["output_language"] == "en"
        assert data["source_language"] == "hi"
        # transcript must be Simple English
        assert data["transcript"] == "Hello friends, today we will learn web development."
        # raw_transcript must preserve canonical original
        assert data["raw_transcript"] == "Namaste dosto, aaj hum seekhenge web development."
        assert data["raw_segments"] == canonical_mock["segments"]
        assert data["fallback_to_original"] is False
        mock_trans_svc.translate.assert_called_once_with(
            video_id="test_sample_1",
            original_text="Namaste dosto, aaj hum seekhenge web development.",
            target_language="en",
            source_language="hi",
        )


def test_post_transcript_graceful_fallback_when_groq_fails(client):
    """If Groq translation encounters an error, the endpoint should gracefully fall back to canonical text."""
    canonical_mock = {
        "video_id": "test_sample_2",
        "source_language": "hi",
        "transcript": "Original raw transcript that cannot be translated right now.",
        "segments": [],
        "provider": "youtube_captions",
        "duration_seconds": 120,
        "confidence": 0.95,
        "from_cache": True,
    }

    mock_ts_svc = MagicMock()
    mock_ts_svc.get_canonical_transcript.return_value = canonical_mock

    mock_trans_svc = MagicMock()
    mock_trans_svc.translate.side_effect = Exception("Groq connection timeout")

    with patch("webapp.main._get_unified_services", return_value=(mock_ts_svc, mock_trans_svc)), \
         patch("webapp.main._get_video_service") as mock_vsvc:
        mock_vsvc.return_value.get_videos_batch.return_value = []

        resp = client.post("/api/transcript", json={"video_url": "https://www.youtube.com/watch?v=test_sample_2"})
        assert resp.status_code == 200
        data = resp.json()

        assert data["success"] is True
        assert data["fallback_to_original"] is True
        # Falls back to canonical text instead of failing with 500 error
        assert data["transcript"] == "Original raw transcript that cannot be translated right now."
        assert data["raw_transcript"] == "Original raw transcript that cannot be translated right now."
