"""Unit and integration tests for channel job Simple English default transformation."""

import csv
import io
from unittest.mock import MagicMock, patch

import pytest

from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import TranscriptJobManager


@pytest.mark.asyncio
async def test_channel_job_transforms_to_simple_english(tmp_path):
    """Channel job converts successfully scraped videos to Simple English by default."""
    manager = TranscriptJobManager(jobs_dir=tmp_path)

    item = TranscriptVideoItem(
        video_id="vid_hindi_1",
        video_url="https://www.youtube.com/watch?v=vid_hindi_1",
        title="Hindi Physics Lecture",
        duration="10:00",
        duration_seconds=600,
        status="pending",
    )

    job = TranscriptJobProgress(
        job_id="0bb0bb0bb0bb",
        channel_handle="physics_guru",
        channel_id="UC_phys",
        channel_title="Physics Guru",
        status=JobStatus.RUNNING,
        output_language="en",
        videos=[item],
        eligible_videos=1,
    )

    # Mock raw caption result in Hindi
    mock_transcript_res = MagicMock()
    mock_transcript_res.success = True
    mock_transcript_res.plain_text = "Namaste dosto, aaj hum seekhenge Newton ka pehla niyam."
    mock_transcript_res.paragraph_text = ""
    mock_transcript_res.raw_transcript = "Namaste dosto, aaj hum seekhenge Newton ka pehla niyam."
    mock_transcript_res.language = "hi"
    mock_transcript_res.source.value = "manual"
    mock_transcript_res.method = "caption"

    # Mock TranslationService output
    mock_trans_svc = MagicMock()
    mock_trans_svc.translate.return_value = {
        "transcript": "Hello friends, today we will learn Newton's first law of motion.",
        "output_language": "en",
    }

    with patch("services.transcript_service.TranscriptService") as mock_tsvc_cls, \
         patch("services.translation.service.TranslationService", return_value=mock_trans_svc):

        mock_tsvc = mock_tsvc_cls.return_value
        mock_tsvc.get_transcript.return_value = mock_transcript_res

        await manager._run_job(job, force_refresh=False)

        assert item.status == "success"
        # Simple English transcript is in item.transcript
        assert item.transcript == "Hello friends, today we will learn Newton's first law of motion."
        # Raw transcript is preserved
        assert item.raw_transcript == "Namaste dosto, aaj hum seekhenge Newton ka pehla niyam."
        # Language is marked as Simple English
        assert item.language == "Simple English"

        # Verify CSV export
        manager._jobs[job.job_id] = job
        csv_text = manager.generate_csv(job.job_id)
        rows = list(csv.reader(io.StringIO(csv_text)))
        assert len(rows) == 2  # header + 1 data row
        assert len(rows[0]) == 15  # exactly 15 columns
        data_row = rows[1]
        assert data_row[8] == "Simple English"  # language column
        assert data_row[10] == "Hello friends, today we will learn Newton's first law of motion."  # transcript column


@pytest.mark.asyncio
async def test_channel_job_graceful_fallback_on_translation_error(tmp_path):
    """If LLM translation fails, job should fall back to raw transcript without failing video."""
    manager = TranscriptJobManager(jobs_dir=tmp_path)

    item = TranscriptVideoItem(
        video_id="vid_fallback_1",
        video_url="https://www.youtube.com/watch?v=vid_fallback_1",
        title="Fallback Lecture",
        duration="5:00",
        duration_seconds=300,
        status="pending",
    )

    job = TranscriptJobProgress(
        job_id="0cc0cc0cc0cc",
        channel_handle="test_channel",
        channel_id="UC_fallback",
        channel_title="Fallback Channel",
        status=JobStatus.RUNNING,
        output_language="en",
        videos=[item],
        eligible_videos=1,
    )

    mock_transcript_res = MagicMock()
    mock_transcript_res.success = True
    mock_transcript_res.plain_text = "Raw transcript text that fails translation."
    mock_transcript_res.raw_transcript = "Raw transcript text that fails translation."
    mock_transcript_res.language = "en"
    mock_transcript_res.method = "caption"

    mock_trans_svc = MagicMock()
    mock_trans_svc.translate.side_effect = RuntimeError("Groq rate limit exceeded")

    with patch("services.transcript_service.TranscriptService") as mock_tsvc_cls, \
         patch("services.translation.service.TranslationService", return_value=mock_trans_svc):

        mock_tsvc = mock_tsvc_cls.return_value
        mock_tsvc.get_transcript.return_value = mock_transcript_res

        await manager._run_job(job, force_refresh=False)

        # Video must still succeed!
        assert item.status == "success"
        # Falls back to raw text
        assert item.transcript == "Raw transcript text that fails translation."
        assert item.raw_transcript == "Raw transcript text that fails translation."
