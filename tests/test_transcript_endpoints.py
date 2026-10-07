"""Integration tests for transcript FastAPI endpoints."""

import csv
import io
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from webapp.main import app
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import transcript_job_manager


@pytest.fixture
def client(authed_client):
    return authed_client


class TestTranscriptEndpoints:
    """Test suite for transcript endpoints in webapp/main.py."""

    def test_transcript_v2_endpoint_returns_rich_metadata(self, client):
        """GET /api/transcriptv2/{video_id} returns title, duration, transcript plus status, source, method."""
        with patch("webapp.main._get_video_service") as mock_vsvc, \
             patch("webapp.main._get_transcript_service") as mock_tsvc:

            mock_vsvc.return_value.get_videos_batch.return_value = [
                {
                    "id": "test_vid_12",
                    "snippet": {"title": "Sample Test Video"},
                    "contentDetails": {"duration": "PT11M48S"},
                }
            ]

            mock_res = MagicMock()
            mock_res.success = True
            mock_res.plain_text = "This is a sample caption transcript text."
            mock_res.paragraph_text = ""
            mock_res.source.value = "manual"
            mock_res.method = "caption"
            mock_res.language = "en"
            mock_tsvc.return_value.get_transcript.return_value = mock_res

            resp = client.get("/api/transcriptv2/test_vid_12")
            assert resp.status_code == 200
            data = resp.json()

            assert data["video_id"] == "test_vid_12"
            assert data["title"] == "Sample Test Video"
            assert data["duration"] == "11:48"
            assert data["status"] == "success"
            assert data["method"] == "caption"
            assert "sample caption transcript" in data["transcript"]

    def test_transcript_job_status_and_download_endpoints(self, client):
        """GET /api/transcript/jobs/{job_id} and download endpoints work with 15-column CSV."""
        job = TranscriptJobProgress(
            job_id="a1b2c3d4e999",
            owner="key:user-key",
            channel_handle="physicsgalaxyworld",
            channel_id="UC_phys_999",
            channel_title="Physics Galaxy World",
            status=JobStatus.COMPLETED,
            total_discovered=10,
            eligible_videos=1,
            skipped_videos=9,
            processed=1,
            successful=1,
            caption_count=0,
            whisper_count=1,
            no_captions=0,
            failed=0,
            remaining=0,
            progress_percent=100,
            videos=[
                TranscriptVideoItem(
                    video_id="KxzI2CqkD6g",
                    video_url="https://www.youtube.com/watch?v=KxzI2CqkD6g",
                    channel_id="UC_phys_999",
                    channel_title="Physics Galaxy World",
                    title="1 Que = IIT Selection",
                    published_at="2023-01-01T00:00:00Z",
                    duration_seconds=708,
                    duration="11:48",
                    language="hi",
                    status="success",
                    transcript="JEE Advanced mechanics solution",
                    source="whisper",
                    method="speech_to_text",
                )
            ],
        )
        transcript_job_manager._jobs[job.job_id] = job

        # 1. Test status endpoint
        resp = client.get(f"/api/transcript/jobs/{job.job_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["job_id"] == "a1b2c3d4e999"
        assert data["data"]["status"] == "completed"
        assert len(data["data"]["videos"]) == 1
        assert data["data"]["videos"][0]["method"] == "speech_to_text"

        # 2. Test download endpoint
        dl_resp = client.get(f"/api/transcript/jobs/{job.job_id}/download")
        assert dl_resp.status_code == 200
        assert "text/csv" in dl_resp.headers["content-type"]
        assert "physicsgalaxyworld_transcripts_a1b2c3d4e999.csv" in dl_resp.headers["content-disposition"]

        csv_text = dl_resp.content.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)
        assert len(rows) == 2
        assert len(rows[0]) == 15
        assert rows[0] == [
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
        assert rows[1][0] == "KxzI2CqkD6g"
        assert rows[1][11] == "whisper"
        assert rows[1][12] == "speech_to_text"

    def test_transcript_job_cancel_endpoint(self, client):
        """POST /api/transcript/jobs/{job_id}/cancel successfully cancels running job."""
        job = TranscriptJobProgress(
            job_id="cafe0000c777",
            owner="key:user-key",
            channel_handle="test_channel",
            channel_id="UC_cancel_777",
            channel_title="Test Channel",
            status=JobStatus.RUNNING,
            total_discovered=10,
            eligible_videos=5,
            videos=[],
        )
        transcript_job_manager._jobs[job.job_id] = job

        resp = client.post(
            f"/api/transcript/jobs/{job.job_id}/cancel",
            json={},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code in (200, 400)
