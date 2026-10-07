from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def mock_youtube():
    with patch("services.youtube_metadata_service.YouTubeMetadataService") as mock:
        instance = mock.return_value
        instance.get_video_metadata.return_value = {
            "id": "test123",
            "snippet": {"title": "Test Video", "channelTitle": "Test Channel"},
            "statistics": {"viewCount": "1000", "likeCount": "100"},
        }
        yield instance


@pytest.fixture
def mock_redis():
    with patch("redis.Redis") as mock:
        instance = mock.return_value
        instance.get.return_value = None
        instance.set.return_value = True
        instance.publish.return_value = 1
        instance.ping.return_value = True
        yield instance


@pytest.fixture
def mock_file_system(tmp_path):
    base = tmp_path / "work"
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture
def sample_video():
    return {
        "id": "vid_test_001",
        "title": "Test Video Title",
        "channel": "Test Channel",
        "duration_seconds": 600,
        "view_count": 10000,
        "like_count": 500,
    }


@pytest.fixture
def sample_transcript():
    return {
        "video_id": "vid_test_001",
        "language": "en",
        "plain_text": "This is a sample transcript for testing. Python is a versatile language. "
                       "It was created by Guido van Rossum. According to recent data, Python usage has grown by 50 percent. "
                       "The most important thing is to practice regularly. Data cleaning is challenging. "
                       "As the speaker noted, Python simplicity is its greatest strength. "
                       "The language has been adopted by millions of developers worldwide.",
        "segments": [
            {"text": "This is a sample transcript for testing.", "start": 0.0, "duration": 5.0},
            {"text": "Python is a versatile language.", "start": 5.0, "duration": 4.0},
            {"text": "It was created by Guido van Rossum.", "start": 9.0, "duration": 3.0},
        ],
    }


@pytest.fixture
def sample_metadata():
    return {
        "title": "Test Video",
        "description": "A test video for unit testing",
        "channel_title": "Test Channel",
        "channel_id": "UCtest123",
        "published_at": "2024-01-01T00:00:00Z",
        "view_count": 10000,
        "like_count": 500,
        "comment_count": 100,
        "duration_seconds": 600,
        "tags": ["python", "tutorial", "testing"],
        "category_id": "27",
        "default_language": "en",
    }

