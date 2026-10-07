# ruff: noqa: ARG001  (fixture-activation args)
"""Observability: request ids, user/job context in logs, structured output, no secrets."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from infrastructure.logging import ContextTextFormatter, StructuredFormatter
from infrastructure.request_context import ContextFilter, job_id_var, new_request_id, request_id_var, user_var


def _record(msg: str = "hello %s", args=("world",)) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
    ContextFilter().filter(record)
    return record


def test_incoming_request_id_honoured_only_when_well_formed():
    assert new_request_id("3f2a9c1e0b7d4e6fa1b2c3d4e5f60718") == "3f2a9c1e0b7d4e6fa1b2c3d4e5f60718"
    for bad in (None, "", "short", "x" * 65, "bad id\nInjected: header", "<script>"):
        generated = new_request_id(bad)
        assert generated != bad and len(generated) == 8


def test_response_request_id_matches_error_trace_id(authed_client):
    resp = authed_client.post("/api/transcript", json={"video_url": "x", "output_language": "fr"})
    assert resp.status_code == 400
    assert resp.json()["trace_id"] == resp.headers["x-request-id"]


def test_nginx_request_id_propagates(authed_client):
    rid = "0123456789abcdef0123456789abcdef"
    resp = authed_client.get("/api/auth/me", headers={"X-Request-ID": rid})
    assert resp.headers["x-request-id"] == rid


def test_access_log_has_request_id_user_and_duration(authed_client, caplog):
    caplog.set_level(logging.INFO, logger="webapp.access")
    resp = authed_client.get("/api/auth/me")
    records = [r for r in caplog.records if r.name == "webapp.access" and r.path == "/api/auth/me"]
    assert records, "one access line per request"
    record = records[-1]
    assert record.status == 200 and record.duration_ms >= 0
    assert record.user == "key:user-key"
    assert resp.headers["x-request-id"]


def test_route_logs_carry_request_id_and_user(authed_client, caplog):
    caplog.set_level(logging.INFO, logger="webapp")
    resp = authed_client.post("/api/transcript", json={"video_url": "x", "output_language": "fr"})
    rid = resp.headers["x-request-id"]
    lines = [r for r in caplog.records if r.name == "webapp" and "Rejected unsupported output_language" in r.getMessage()]
    assert lines
    assert (lines[-1].request_id, lines[-1].user) == (rid, "key:user-key")


def test_text_and_json_formats_include_context():
    rid_token, job_token, user_token = request_id_var.set("abcd1234"), job_id_var.set("c1c1c1c1c1c1"), user_var.set("alice")
    try:
        record = _record()
        text = ContextTextFormatter().format(record)
        assert "[abcd1234 job=c1c1c1c1c1c1 user=alice] hello world" in text
        data = json.loads(StructuredFormatter().format(record))
        assert (data["request_id"], data["job_id"], data["user"], data["message"]) == (
            "abcd1234", "c1c1c1c1c1c1", "alice", "hello world")
    finally:
        request_id_var.reset(rid_token)
        job_id_var.reset(job_token)
        user_var.reset(user_token)


def test_json_log_line_redacts_secrets():
    from infrastructure.log_redaction import install_secret_redaction

    install_secret_redaction()
    record = logging.getLogRecordFactory()(
        "t", logging.ERROR, __file__, 1, "calling %s", ("https://x/videos?key=AIzaSECRETSECRETSECRETSECRETSECRET123",), None,
    )
    ContextFilter().filter(record)
    assert "AIzaSECRET" not in StructuredFormatter().format(record)


async def test_job_logs_carry_job_id_and_outcome(tmp_path, monkeypatch, caplog):
    from models.transcript_job import JobStatus
    from services.jobs.transcript_job_manager import TranscriptJobManager

    manager = TranscriptJobManager(jobs_dir=tmp_path)
    seen = {}

    async def fake_discover(progress, **kwargs):
        logging.getLogger("services.jobs.transcript_job_manager").info("inside job")
        seen["job_id"] = job_id_var.get()
        progress.status = JobStatus.FAILED
        from services.jobs.transcript_job_manager import _log_job_outcome
        _log_job_outcome(progress)

    monkeypatch.setattr(manager, "_discover_and_run", fake_discover)
    caplog.set_level(logging.INFO, logger="services.jobs.transcript_job_manager")
    job = await manager.start_channel_job("chan", owner="alice")
    await asyncio.wait_for(manager._tasks[job.job_id], 2) if job.job_id in manager._tasks else None
    await asyncio.sleep(0)
    assert seen["job_id"] == job.job_id
    outcome = [r for r in caplog.records if getattr(r, "event", None) == "job_finished"]
    assert outcome and outcome[-1].status == "failed" and outcome[-1].levelno == logging.WARNING
    assert outcome[-1].duration_seconds is not None


@pytest.mark.parametrize("path", ["/api/health"])
def test_health_probes_not_logged_at_info(authed_client, caplog, path):
    caplog.set_level(logging.INFO, logger="webapp.access")
    authed_client.get(path)
    assert not [r for r in caplog.records if r.name == "webapp.access" and r.levelno >= logging.INFO and r.path == path]
