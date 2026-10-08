"""Channel discovery: ``limit`` counts eligible videos, scanning is bounded, filters apply."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.channel_discovery import scan_channel_uploads


class FakeUploads:
    """Uploads playlist of ``videos`` = [(id, iso_duration, publishedAt, live)], newest first."""

    def __init__(self, videos, page_size=50, fail_metadata_pages=()):
        self.videos = videos
        self.page_size = page_size
        self.fail_metadata_pages = set(fail_metadata_pages)
        self.pages_fetched = 0
        self.metadata_calls = 0

    def get_playlist_items(self, playlist_id, page_token=None):
        start = int(page_token or 0)
        self.pages_fetched += 1
        chunk = self.videos[start:start + self.page_size]
        nxt = start + self.page_size
        return {"video_ids": [v[0] for v in chunk], "next_page_token": str(nxt) if nxt < len(self.videos) else None}

    def get_videos_batch(self, ids):
        self.metadata_calls += 1
        if self.metadata_calls in self.fail_metadata_pages:
            raise RuntimeError("quotaExceeded")
        by_id = {v[0]: v for v in self.videos}
        return [
            {"id": i, "snippet": {"title": f"T {i}", "publishedAt": by_id[i][2],
                                  "liveBroadcastContent": by_id[i][3]},
             "contentDetails": {"duration": by_id[i][1]}}
            for i in ids
        ]


def _video(n, duration="PT10M", published="2026-09-01T00:00:00Z", live="none"):
    return (f"v{n:010d}", duration, published, live)


def _scan(uploads, limit, **kw):
    kw.setdefault("min_seconds", 180)
    kw.setdefault("max_seconds", 1800)
    kw.setdefault("scan_cap", 1000)
    return scan_channel_uploads(uploads, "UU1", limit, **kw)


def test_limit_counts_eligible_videos_not_uploads_scanned():
    # The newest 60 uploads are Shorts; eligible videos only appear on the second page.
    videos = [_video(i, "PT45S") for i in range(60)] + [_video(100 + i) for i in range(10)]
    uploads = FakeUploads(videos)
    scan = _scan(uploads, 2)
    assert [v.video_id for v in scan.eligible] == ["v0000000100", "v0000000101"]
    assert len(scan.scanned) == 62 and uploads.pages_fetched == 2  # stops right after the 2nd eligible
    assert scan.skip_counts()["TOO_SHORT"] == 60


def test_stops_paging_once_enough_eligible_videos_found():
    uploads = FakeUploads([_video(i) for i in range(500)])
    scan = _scan(uploads, 3)
    assert len(scan.eligible) == 3
    assert uploads.pages_fetched == 1  # quota: no further pages once the limit is met


def test_scan_cap_bounds_quota_on_channels_without_eligible_videos():
    uploads = FakeUploads([_video(i, "PT30S") for i in range(5000)])
    scan = _scan(uploads, 5, scan_cap=200)
    assert scan.eligible == [] and scan.hit_scan_cap
    assert len(scan.scanned) == 200 and uploads.pages_fetched == 4


def test_duration_window_and_live_streams():
    videos = [_video(1, "PT2M59S"), _video(2, "PT3M"), _video(3, "PT29M59S"), _video(4, "PT30M"),
              _video(5, "PT1H30M"), _video(6, "PT10M", live="live"), _video(7, "PT10M", live="upcoming")]
    scan = _scan(FakeUploads(videos), 10)
    assert [v.video_id for v in scan.eligible] == ["v0000000002", "v0000000003"]
    assert scan.skip_counts() == {"TOO_SHORT": 1, "TOO_LONG": 2, "LIVE_STREAM": 2}


def test_configurable_window_admits_long_lectures():
    scan = _scan(FakeUploads([_video(1, "PT1H30M"), _video(2, "PT2H1M")]), 10, max_seconds=7200)
    assert [v.video_id for v in scan.eligible] == ["v0000000001"]


def test_published_after_stops_at_the_cutoff():
    videos = [_video(i, published=f"2026-09-{30 - i:02d}T00:00:00Z") for i in range(20)]
    uploads = FakeUploads(videos, page_size=5)
    scan = _scan(uploads, 100, published_after=datetime(2026, 9, 25, tzinfo=UTC))
    assert [v.published_at[:10] for v in scan.eligible] == [f"2026-09-{d}" for d in (30, 29, 28, 27, 26, 25)]
    assert uploads.pages_fetched == 2  # stopped after the page that crossed the cutoff


def test_published_before_skips_newer_uploads():
    videos = [_video(1, published="2026-09-30T00:00:00Z"), _video(2, published="2026-09-01T00:00:00Z")]
    scan = _scan(FakeUploads(videos), 10, published_before=datetime(2026, 9, 15, tzinfo=UTC))
    assert [v.video_id for v in scan.eligible] == ["v0000000002"]
    assert scan.skip_counts() == {"OUT_OF_DATE_RANGE": 1}


def test_failed_metadata_batch_skips_that_page_only():
    videos = [_video(i) for i in range(4)]
    scan = _scan(FakeUploads(videos, page_size=2, fail_metadata_pages={1}), 10)
    assert [v.video_id for v in scan.eligible] == ["v0000000002", "v0000000003"]
    assert scan.skip_counts() == {"NO_METADATA": 2}


def test_should_stop_aborts_discovery():
    scan = _scan(FakeUploads([_video(i, "PT10S") for i in range(500)], page_size=10), 5, should_stop=lambda: True)
    assert scan.stopped and scan.scanned == []


def test_limit_must_be_positive():
    with pytest.raises(ValueError):
        _scan(FakeUploads([]), 0)


def test_settings_reject_inverted_channel_window(monkeypatch):
    from config.settings import ConfigurationError, Settings

    monkeypatch.setenv("CHANNEL_MIN_VIDEO_SECONDS", "1800")
    monkeypatch.setenv("CHANNEL_MAX_VIDEO_SECONDS", "1800")
    with pytest.raises(ConfigurationError):
        Settings()


# ---------------------------------------------------------------------------
# Through the job manager: a channel whose newest uploads are Shorts
# ---------------------------------------------------------------------------


async def test_channel_job_finds_requested_number_of_eligible_videos(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    import api.channel_service as channel_module
    import api.video_service as video_module
    import services.transcript_service as ts_module
    from config.settings import settings
    from models.transcript_job import JobStatus
    from services.jobs.transcript_job_manager import TranscriptJobManager
    from services.transcript_limiter import transcript_limiter

    videos = [_video(i, "PT40S") for i in range(55)] + [_video(200 + i) for i in range(5)]

    class FakeYouTube(FakeUploads):
        def __init__(self):
            super().__init__(videos)

        def resolve_handle(self, handle):
            return {"id": "UC1", "snippet": {"title": "Chan"}}

        def get_uploads_playlist_id(self, channel_id):
            return "UU1"

    class FakeTranscripts:
        def get_transcript(self, video_id, **kwargs):
            return SimpleNamespace(
                success=True, plain_text=f"text {video_id}", paragraph_text="", raw_transcript=f"text {video_id}",
                language="en", source_language="English", source_language_code="en", error_code=None, error=None,
            )

    async def no_wait(video_id):
        return None

    monkeypatch.setattr(channel_module, "ChannelService", FakeYouTube)
    monkeypatch.setattr(video_module, "VideoService", FakeYouTube)
    monkeypatch.setattr(ts_module, "TranscriptService", FakeTranscripts)
    monkeypatch.setattr(transcript_limiter, "acquire", no_wait)
    monkeypatch.setattr(settings, "whisper_enabled", False)

    manager = TranscriptJobManager(jobs_dir=tmp_path)
    job = await manager.start_channel_job("chan", max_videos=2, output_language="original", owner="u")
    await asyncio.wait_for(asyncio.shield(manager._tasks[job.job_id]), 10)
    job = manager.get_job(job.job_id)
    assert job.status == JobStatus.COMPLETED
    assert (job.eligible_videos, job.successful) == (2, 2)
    assert job.total_discovered == 57 and job.skipped_videos == 55
