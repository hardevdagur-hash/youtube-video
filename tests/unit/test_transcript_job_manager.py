"""Unit tests for TranscriptJobManager and 15-column CSV export."""

import csv
import io
import pytest
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import TranscriptJobManager


def test_transcript_job_manager_lifecycle():
    """Verify job creation, progress tracking, and 15-column CSV generation."""
    manager = TranscriptJobManager()

    job = TranscriptJobProgress(
        job_id="012301230123",
        channel_handle="physicsgalaxyworld",
        channel_id="UC_test_chan",
        channel_title="Physics Galaxy",
        status=JobStatus.RUNNING,
        total_discovered=5,
        eligible_videos=2,
        skipped_videos=3,
        processed=1,
        successful=1,
        caption_count=1,
        whisper_count=0,
        no_captions=0,
        failed=0,
        remaining=1,
        progress_percent=50,
        videos=[
            TranscriptVideoItem(
                video_id="KxzI2CqkD6g",
                video_url="https://www.youtube.com/watch?v=KxzI2CqkD6g",
                channel_id="UC_test_chan",
                channel_title="Physics Galaxy",
                title="1 Que = IIT Selection | Episode 4",
                published_at="2023-01-01T00:00:00Z",
                duration_seconds=708,
                duration="11:48",
                language="hi",
                status="success",
                transcript="Hello students this is episode 4 mechanics",
                source="whisper",
                method="speech_to_text",
            ),
            TranscriptVideoItem(
                video_id="wDhrCBV8iBA",
                video_url="https://www.youtube.com/watch?v=wDhrCBV8iBA",
                channel_id="UC_test_chan",
                channel_title="Physics Galaxy",
                title="1 Que = IIT Selection | Episode 3",
                published_at="2023-01-02T00:00:00Z",
                duration_seconds=575,
                duration="9:35",
                language="hi",
                status="failed",
                transcript="",
                source=None,
                method=None,
                error_code="NO_CAPTIONS",
                error_message="Subtitles/transcripts are disabled or not available.",
            ),
        ],
    )

    manager._jobs[job.job_id] = job

    # Verify retrieval
    retrieved = manager.get_job("012301230123")
    assert retrieved is not None
    assert retrieved.job_id == "012301230123"
    assert retrieved.eligible_videos == 2

    # Verify CSV generation has exact 15 columns
    csv_str = manager.generate_csv("012301230123")
    reader = csv.reader(io.StringIO(csv_str))
    rows = list(reader)

    expected_headers = [
        "video_id",
        "video_url",
        "channel_id",
        "channel_title",
        "title",
        "published_at",
        "duration_seconds",
        "duration",
        "language",
        "status",
        "transcript",
        "source",
        "method",
        "error_code",
        "error_message",
    ]

    assert rows[0] == expected_headers
    assert len(rows[0]) == 15

    # Check row 1 (success, whisper)
    assert rows[1][0] == "KxzI2CqkD6g"
    assert rows[1][1] == "https://www.youtube.com/watch?v=KxzI2CqkD6g"
    assert rows[1][4] == "1 Que = IIT Selection | Episode 4"
    assert rows[1][6] == "708"
    assert rows[1][7] == "11:48"
    assert rows[1][8] == "hi"
    assert rows[1][9] == "success"
    assert rows[1][10] == "Hello students this is episode 4 mechanics"
    assert rows[1][11] == "whisper"
    assert rows[1][12] == "speech_to_text"
    assert rows[1][13] == ""

    # Check row 2 (failed, no captions)
    assert rows[2][0] == "wDhrCBV8iBA"
    assert rows[2][9] == "failed"
    assert rows[2][10] == ""
    assert rows[2][13] == "NO_CAPTIONS"
    assert "disabled or not available" in rows[2][14]
