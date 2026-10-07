# ruff: noqa: ARG001  (fixture-activation args)
"""Security hardening: path containment, CSV formula injection, error hygiene, resource limits."""

from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

import webapp.main as web
from config.settings import settings
from models.transcript import TranscriptResult, TranscriptSource
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from repositories.transcript_repository import TranscriptRepository
from services.csv_safety import safe_csv_cell
from services.jobs.transcript_job_manager import JobLimitError, TranscriptJobManager
from services.public_errors import public_error
from tests.conftest import TEST_OTHER_USER_KEY, TEST_USER_KEY

pytestmark = pytest.mark.security

TRAVERSAL_IDS = [
    "../../etc/passwd",
    "..\\..\\windows\\win.ini",
    "%2e%2e%2f%2e%2e%2f",
    "/etc/passwd",
    "C:\\Windows\\win.ini",
    "0123456789ab/../x",
    "0123456789AB",
    "0123456789abc",
    "",
]


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", TRAVERSAL_IDS)
def test_job_manager_rejects_unsafe_job_ids(tmp_path, job_id):
    manager = TranscriptJobManager(jobs_dir=tmp_path / "jobs")
    assert manager._job_path(job_id) is None
    assert manager.get_job(job_id) is None


def test_job_manager_never_writes_outside_jobs_dir(tmp_path):
    jobs_dir = tmp_path / "jobs"
    manager = TranscriptJobManager(jobs_dir=jobs_dir)
    job = TranscriptJobProgress(
        job_id="../../escaped", channel_handle="c", channel_id="", channel_title="c",
        status=JobStatus.QUEUED, total_discovered=0, eligible_videos=0, skipped_videos=0, remaining=0, videos=[],
    )
    manager._save_checkpoint(job)
    assert not (tmp_path / "escaped.json").exists()
    assert list(jobs_dir.iterdir()) == []


def test_checkpoint_file_does_not_expose_server_paths(tmp_path):
    manager = TranscriptJobManager(jobs_dir=tmp_path / "jobs")
    job = TranscriptJobProgress(
        job_id="0123456789ab", channel_handle="c", channel_id="", channel_title="c",
        status=JobStatus.QUEUED, total_discovered=0, eligible_videos=0, skipped_videos=0, remaining=0, videos=[],
    )
    manager._save_checkpoint(job)
    assert job.checkpoint_file == "0123456789ab.json"
    assert str(tmp_path) not in json.dumps(job.model_dump())


@pytest.mark.parametrize("video_id", ["../../../x", "..\\..\\a.b", "a/b/c/d/e/f", "short", "x" * 12])
def test_repository_rejects_unsafe_video_ids(tmp_path, video_id):
    cache_dir = tmp_path / "cache"
    repo = TranscriptRepository(persist_dir=str(cache_dir))
    assert repo._file_for(video_id) is None
    repo.save(TranscriptResult(success=True, video_id=video_id, source=TranscriptSource.MANUAL, plain_text="x"))
    assert list(cache_dir.iterdir()) == []
    assert not any(p.suffix == ".json" for p in tmp_path.rglob("*") if p.parent != cache_dir)


def test_repository_rejects_unsafe_language_keys(tmp_path):
    repo = TranscriptRepository(persist_dir=str(tmp_path))
    assert repo._file_for("dQw4w9WgXcQ", "../../x") is None
    assert repo._file_for("dQw4w9WgXcQ", "en:simple").name == "dQw4w9WgXcQ_en_simple.json"


def test_repository_roundtrip_is_atomic_and_contained(tmp_path):
    repo = TranscriptRepository(persist_dir=str(tmp_path))
    repo.save(TranscriptResult(success=True, video_id="dQw4w9WgXcQ", source=TranscriptSource.MANUAL, plain_text="hi"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dQw4w9WgXcQ.json"]  # no .tmp left behind
    fresh = TranscriptRepository(persist_dir=str(tmp_path))
    assert fresh.get("dQw4w9WgXcQ").plain_text == "hi"


@pytest.mark.parametrize("path", [
    "/api/transcript/jobs/..%2F..%2Fdata",
    "/api/transcript/jobs/..%5C..%5Cdata/download",
    "/api/transcript/jobs/%2Fetc%2Fpasswd",
])
def test_api_rejects_traversal_job_ids(authed_client, path):
    assert authed_client.get(path).status_code in (404, 422)


# ---------------------------------------------------------------------------
# CSV formula injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["=SUM(A1:A9)", "+CMD|' /C calc'!A0", "-2+3", "@SUM(1)", "\t=1", "\r=1"])
def test_formula_cells_neutralised(payload):
    assert safe_csv_cell(payload) == "'" + payload


@pytest.mark.parametrize("value", ["Normal title", "So today we learn = signs", "Price: -5", 42, None, ""])
def test_ordinary_cells_unchanged(value):
    assert safe_csv_cell(value) == value


def _job_with_malicious_metadata() -> TranscriptJobProgress:
    return TranscriptJobProgress(
        job_id="c5c5c5c5c5c5", owner="key:user-key", channel_handle="evil", channel_id="",
        channel_title="evil", status=JobStatus.COMPLETED, total_discovered=1, eligible_videos=1,
        skipped_videos=0, remaining=0, output_language="original",
        videos=[TranscriptVideoItem(
            video_id="dQw4w9WgXcQ", video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            channel_id="=CHANNEL()", channel_title="@channel",
            title='=HYPERLINK("http://evil.example","click")', status="success",
            raw_transcript="+cmd|' /C calc'!A0 then a normal sentence", transcript="x",
            source="youtube", method="caption",
        )],
    )


def test_job_csv_export_neutralises_formulas(auth_config):
    from services.jobs.transcript_job_manager import transcript_job_manager

    job = _job_with_malicious_metadata()
    transcript_job_manager._jobs[job.job_id] = job
    try:
        resp = TestClient(web.app, headers={"X-API-Key": TEST_USER_KEY}).get(
            f"/api/transcript/jobs/{job.job_id}/download")
        assert resp.status_code == 200
        rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
        assert rows[0]["title"] == "'" + '=HYPERLINK("http://evil.example","click")'
        assert rows[0]["channel_id"] == "'=CHANNEL()"
        assert rows[0]["channel_title"] == "'@channel"
        assert rows[0]["transcript"].startswith("'+cmd")
        assert rows[0]["transcript"].endswith("then a normal sentence")  # content otherwise intact
    finally:
        transcript_job_manager._jobs.pop(job.job_id, None)


def test_sync_csv_builder_neutralises_formulas():
    text = web._build_csv_rows([{
        "video_id": "dQw4w9WgXcQ", "title": "=1+1", "channel_title": "-x", "status": "success",
        "raw_transcript": "@here", "language": "en",
    }], output_language="original")
    row = list(csv.DictReader(io.StringIO(text)))[0]
    assert row["title"] == "'=1+1"
    assert row["channel_title"] == "'-x"
    assert row["transcript"] == "'@here"


# ---------------------------------------------------------------------------
# Error hygiene
# ---------------------------------------------------------------------------


def test_public_error_never_echoes_internal_detail():
    code, err = public_error("GROQ_AUTH_ERROR")
    assert code == "STT_UNAVAILABLE"
    assert err.status_code == 503  # never 401: that would sign the browser user out
    assert public_error("SOMETHING_UNKNOWN")[0] == "UNEXPECTED_ERROR"


def test_transcript_route_hides_provider_exception_text(authed_client, monkeypatch):
    from services.transcription.groq import GroqRateLimitError
    from services.youtube.captions import CaptionsUnavailableError

    secret_detail = "upstream said: org_id=org-SECRET123 path=C:\\internal\\x"

    class Boom:
        def get_canonical_transcript(self, url):
            raise CaptionsUnavailableError(f"Could not retrieve YouTube captions: {secret_detail}")

    monkeypatch.setattr(web, "_get_unified_services", lambda: (Boom(), None))
    resp = authed_client.post("/api/transcript", json={"video_url": "dQw4w9WgXcQ", "output_language": "original"})
    assert resp.status_code == 404
    assert secret_detail not in resp.text
    assert resp.json()["error_code"] == "CAPTIONS_UNAVAILABLE"

    class RateLimited:
        def get_canonical_transcript(self, url):
            raise GroqRateLimitError(f"Groq rate limit exceeded: {secret_detail}")

    monkeypatch.setattr(web, "_get_unified_services", lambda: (RateLimited(), None))
    resp = authed_client.post("/api/transcript", json={"video_url": "dQw4w9WgXcQ", "output_language": "original"})
    assert resp.status_code == 429
    assert resp.json()["retryable"] is True
    assert secret_detail not in resp.text


def test_groq_auth_failure_is_503_not_401(authed_client, monkeypatch):
    from services.transcription.groq import GroqAuthError

    class NoKey:
        def get_canonical_transcript(self, url):
            raise GroqAuthError("Groq authentication failed: invalid api key gsk_xxx")

    monkeypatch.setattr(web, "_get_unified_services", lambda: (NoKey(), None))
    resp = authed_client.post("/api/transcript", json={"video_url": "dQw4w9WgXcQ"})
    assert resp.status_code == 503
    assert "gsk_" not in resp.text


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    """Job manager with a temp store; jobs never run (no network)."""
    import services.jobs.transcript_job_manager as jm

    manager = TranscriptJobManager(jobs_dir=tmp_path / "jobs")

    async def never_finishes(*args, **kwargs):
        import asyncio
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_discover_and_run", never_finishes)
    monkeypatch.setattr(jm, "transcript_job_manager", manager)
    return manager


async def test_active_job_limit_per_user(isolated_manager, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs_per_user", 2)
    monkeypatch.setattr(settings, "max_active_jobs", 10)
    await isolated_manager.start_channel_job("a", owner="alice")
    await isolated_manager.start_channel_job("b", owner="alice")
    with pytest.raises(JobLimitError) as exc:
        await isolated_manager.start_channel_job("c", owner="alice")
    assert exc.value.scope == "user"
    await isolated_manager.start_channel_job("d", owner="bob")  # other users unaffected
    await isolated_manager.shutdown()


async def test_active_job_limit_server_wide(isolated_manager, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs_per_user", 10)
    monkeypatch.setattr(settings, "max_active_jobs", 2)
    await isolated_manager.start_channel_job("a", owner="alice")
    await isolated_manager.start_channel_job("b", owner="bob")
    with pytest.raises(JobLimitError) as exc:
        await isolated_manager.start_channel_job("c", owner="carol")
    assert exc.value.scope == "server"
    await isolated_manager.shutdown()


async def test_shutdown_pauses_running_jobs_for_resume(isolated_manager):
    job = await isolated_manager.start_channel_job("a", owner="alice")
    await isolated_manager.shutdown()
    persisted = json.loads((isolated_manager._jobs_dir / f"{job.job_id}.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "paused"


def test_job_limit_returns_429(auth_config, isolated_manager, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs_per_user", 1)
    # One client context = one event loop, so the first job is still running for the second call.
    with TestClient(web.app, headers={"X-API-Key": TEST_OTHER_USER_KEY}) as client:
        first = client.post("/api/channel/somechannel/transcript-job", json={})
        assert first.status_code == 200, first.text
        second = client.post("/api/channel/somechannel/transcript-job", json={})
        assert second.status_code == 429
        assert second.json()["errors"][0]["code"] == "JOB_LIMIT_REACHED"


def test_sync_channel_concurrency_limit(authed_client, monkeypatch):
    monkeypatch.setattr(web, "_sync_channel_runs", settings.max_concurrent_sync_channel_runs)
    resp = authed_client.get("/api/channel/somechannel/transcripts")
    assert resp.status_code == 429
    resp = authed_client.post("/api/transcript/export", json={"channel_handle": "chan"})
    assert resp.status_code == 429


@pytest.mark.parametrize("body", [
    {"output_language": "fr"},
    {"output_language": "<script>"},
    {"published_after": "yesterday"},
    {"published_before": "2024-13-45; DROP"},
    {"max_videos": "abc"},
    {"max_videos": 1.5},
])
def test_job_creation_rejects_invalid_input(authed_client, body):
    assert authed_client.post("/api/channel/somechannel/transcript-job", json=body).status_code == 422


def test_me_reports_server_limits(authed_client):
    limits = authed_client.get("/api/auth/me").json()["limits"]
    assert limits == {
        "max_videos_per_job": settings.max_videos_per_job,
        "max_videos_sync": settings.max_videos_sync_export,
        "max_active_jobs_per_user": settings.max_active_jobs_per_user,
    }
