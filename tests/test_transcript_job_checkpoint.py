import json
import os
import shutil
import tempfile
from unittest.mock import patch

import pytest

from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import TranscriptJobManager


@pytest.fixture
def temp_jobs_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_job_checkpoint_save_and_load(temp_jobs_dir):
    manager = TranscriptJobManager(jobs_dir=temp_jobs_dir)

    # Create a job manually
    job = TranscriptJobProgress(
        job_id="test_job_save_load",
        channel_handle="@techchannel",
        channel_id="UC123",
        channel_title="Tech Channel",
        status=JobStatus.COOLDOWN,
        total_discovered=3,
        eligible_videos=3,
        processed=2,
        successful=1,
        rate_limited=1,
        remaining=1,
        cooldown_seconds_remaining=25.0,
        videos=[
            TranscriptVideoItem(video_id="vid_1", video_url="https://youtube.com/watch?v=vid_1", title="Video 1", status="success", transcript="Text 1"),
            TranscriptVideoItem(video_id="vid_2", video_url="https://youtube.com/watch?v=vid_2", title="Video 2", status="rate_limited", error_code="RATE_LIMITED"),
            TranscriptVideoItem(video_id="vid_3", video_url="https://youtube.com/watch?v=vid_3", title="Video 3", status="pending"),
        ],
    )

    manager._jobs["test_job_save_load"] = job
    manager._save_checkpoint(job)

    # Check that file exists on disk
    expected_path = os.path.join(temp_jobs_dir, "test_job_save_load.json")
    assert os.path.exists(expected_path)

    with open(expected_path, encoding="utf-8") as f:
        data = json.load(f)
        assert data["job_id"] == "test_job_save_load"
        assert data["rate_limited"] == 1
        assert data["status"] == "cooldown"
        assert len(data["videos"]) == 3

    # Create a fresh manager and verify it loads the checkpoint from disk
    new_manager = TranscriptJobManager(jobs_dir=temp_jobs_dir)
    loaded_job = new_manager.get_job("test_job_save_load")
    assert loaded_job is not None
    assert loaded_job.job_id == "test_job_save_load"
    assert loaded_job.status == JobStatus.COOLDOWN
    assert loaded_job.successful == 1
    assert loaded_job.rate_limited == 1
    assert loaded_job.remaining == 1
    assert len(loaded_job.videos) == 3


@pytest.mark.asyncio
async def test_job_resume_logic(temp_jobs_dir):
    manager = TranscriptJobManager(jobs_dir=temp_jobs_dir)

    # Create a paused job with 1 success, 1 rate_limited, 1 pending
    job = TranscriptJobProgress(
        job_id="test_resume",
        channel_handle="@techchannel",
        channel_id="UC123",
        channel_title="Tech Channel",
        status=JobStatus.PAUSED,
        eligible_videos=3,
        processed=2,
        successful=1,
        rate_limited=1,
        remaining=1,
        videos=[
            TranscriptVideoItem(video_id="vid_1", video_url="https://youtube.com/watch?v=vid_1", title="Video 1", status="success", transcript="Text 1"),
            TranscriptVideoItem(video_id="vid_2", video_url="https://youtube.com/watch?v=vid_2", title="Video 2", status="rate_limited", error_code="RATE_LIMITED"),
            TranscriptVideoItem(video_id="vid_3", video_url="https://youtube.com/watch?v=vid_3", title="Video 3", status="pending"),
        ],
    )
    manager._jobs["test_resume"] = job
    manager._save_checkpoint(job)

    # Mock _run_job so it doesn't make live network requests
    with patch.object(manager, "_run_job") as mock_run:
        resumed_job = await manager.resume_job("test_resume")
        assert resumed_job is not None
        assert resumed_job.status == JobStatus.RUNNING

        # vid_2 (rate_limited) should have been reset to pending for retry
        item_2 = next(i for i in resumed_job.videos if i.video_id == "vid_2")
        assert item_2.status == "pending"

        # vid_1 should remain success
        item_1 = next(i for i in resumed_job.videos if i.video_id == "vid_1")
        assert item_1.status == "success"


@pytest.mark.asyncio
async def test_resume_completed_job_with_no_rate_limited(temp_jobs_dir):
    manager = TranscriptJobManager(jobs_dir=temp_jobs_dir)
    job = TranscriptJobProgress(
        job_id="completed_job",
        channel_handle="@techchannel",
        channel_id="UC123",
        channel_title="Tech Channel",
        status=JobStatus.COMPLETED,
        eligible_videos=1,
        processed=1,
        successful=1,
        rate_limited=0,
        remaining=0,
        videos=[
            TranscriptVideoItem(video_id="vid_1", video_url="https://youtube.com/watch?v=vid_1", title="Video 1", status="success", transcript="Text 1"),
        ],
    )
    manager._jobs["completed_job"] = job

    res = await manager.resume_job("completed_job")
    # No pending items -> returns job as completed, doesn't launch task
    assert res is not None
    assert res.status == JobStatus.COMPLETED
