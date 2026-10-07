"""Unit and integration tests for channel transcript date range filtering."""

from unittest.mock import MagicMock, patch

import pytest

from models.transcript_job import JobStatus, TranscriptJobProgress
from services.jobs.transcript_job_manager import (
    TranscriptJobManager,
    parse_date_boundary,
    parse_published_at,
)


@pytest.fixture
def client(authed_client):
    return authed_client


def test_parse_date_boundary():
    """Verify YYYY-MM-DD and ISO parsing with start and end of day."""
    # Start of day
    dt_start = parse_date_boundary("2024-05-10", is_end_of_day=False)
    assert dt_start is not None
    assert dt_start.year == 2024 and dt_start.month == 5 and dt_start.day == 10
    assert dt_start.hour == 0 and dt_start.minute == 0

    # End of day
    dt_end = parse_date_boundary("2024-05-10", is_end_of_day=True)
    assert dt_end is not None
    assert dt_end.year == 2024 and dt_end.month == 5 and dt_end.day == 10
    assert dt_end.hour == 23 and dt_end.minute == 59 and dt_end.second == 59

    # ISO format
    dt_iso = parse_date_boundary("2024-05-10T14:30:00Z")
    assert dt_iso is not None
    assert dt_iso.hour == 14 and dt_iso.minute == 30

    # None and empty
    assert parse_date_boundary(None) is None
    assert parse_date_boundary("") is None


def test_parse_published_at():
    """Verify YouTube snippet publishedAt ISO parsing."""
    dt = parse_published_at("2024-06-01T12:00:00Z")
    assert dt is not None
    assert dt.year == 2024 and dt.month == 6 and dt.day == 1
    assert dt.tzinfo is not None

    assert parse_published_at(None) is None
    assert parse_published_at("") is None


@pytest.mark.asyncio
async def test_channel_discovery_with_date_filters(tmp_path):
    """Test that _discover_and_run filters videos outside the published window."""
    manager = TranscriptJobManager(jobs_dir=tmp_path)

    progress = TranscriptJobProgress(
        job_id="0dd0dd0dd0dd",
        channel_handle="test_channel",
        channel_id="UC_test",
        channel_title="Test Channel",
        status=JobStatus.QUEUED,
        published_after="2024-05-01",
        published_before="2024-05-31",
        output_language="en",
    )

    # 3 mock videos:
    # 1. 2024-06-15 (too new - should be excluded)
    # 2. 2024-05-15 (within window - should be eligible)
    # 3. 2024-04-10 (too old - should trigger early exit / be excluded)
    mock_channel_svc = MagicMock()
    mock_channel_svc.resolve_handle.return_value = {
        "id": "UC_test",
        "snippet": {"title": "Test Channel"},
    }

    mock_video_svc = MagicMock()
    mock_video_svc.get_uploads_playlist_id.return_value = "UU_test"
    mock_video_svc.get_playlist_items.return_value = {
        "video_ids": ["vid_new", "vid_mid", "vid_old"],
        "next_page_token": None,
    }
    mock_video_svc.get_videos_batch.return_value = [
        {
            "id": "vid_new",
            "snippet": {"title": "New Video", "publishedAt": "2024-06-15T10:00:00Z", "liveBroadcastContent": "none"},
            "contentDetails": {"duration": "PT10M"},
        },
        {
            "id": "vid_mid",
            "snippet": {"title": "Mid Video", "publishedAt": "2024-05-15T10:00:00Z", "liveBroadcastContent": "none"},
            "contentDetails": {"duration": "PT10M"},
        },
        {
            "id": "vid_old",
            "snippet": {"title": "Old Video", "publishedAt": "2024-04-10T10:00:00Z", "liveBroadcastContent": "none"},
            "contentDetails": {"duration": "PT10M"},
        },
    ]

    with patch("api.channel_service.ChannelService", return_value=mock_channel_svc), \
         patch("api.video_service.VideoService", return_value=mock_video_svc), \
         patch.object(manager, "_run_job") as mock_run:

        await manager._discover_and_run(
            progress=progress,
            clean_handle="test_channel",
            max_videos=100,
            min_duration=180,
            max_duration=1800,
            force_refresh=False,
            caption_concurrency=1,
            whisper_concurrency=1,
            published_after="2024-05-01",
            published_before="2024-05-31",
            output_language="en",
        )

        # Only vid_mid should be eligible
        assert len(progress.videos) == 1
        assert progress.videos[0].video_id == "vid_mid"
        assert progress.eligible_videos == 1
        assert progress.skipped_videos == 2
        mock_run.assert_called_once()


def test_api_channel_transcript_job_accepts_date_filters(client):
    """POST /api/channel/{handle}/transcript-job should accept published_after and published_before."""
    with patch("services.jobs.transcript_job_manager.transcript_job_manager.start_channel_job") as mock_start:
        mock_progress = TranscriptJobProgress(
            job_id="0ee0ee0ee0ee",
            channel_handle="test_handle",
            channel_id="UC_test",
            channel_title="Test Handle",
            published_after="2024-01-01",
            published_before="2024-06-01",
            output_language="en",
        )
        mock_start.return_value = mock_progress

        resp = client.post(
            "/api/channel/test_handle/transcript-job",
            json={
                "max_videos": 50,
                "published_after": "2024-01-01",
                "published_before": "2024-06-01",
                "output_language": "en",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["job_id"] == "0ee0ee0ee0ee"
        assert data["data"]["published_after"] == "2024-01-01"
        assert data["data"]["published_before"] == "2024-06-01"
        assert data["data"]["output_language"] == "en"
