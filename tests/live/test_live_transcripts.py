# ruff: noqa: ARG001  (fixture-activation args)
"""Live end-to-end transcript tests against real YouTube and Groq (opt-in, costs quota).

    RUN_LIVE_TESTS=1 pytest -m live tests/live -o log_cli=false

Keys are read from the project .env (never from the test environment, which is
blanked by tests/conftest.py). Each run uses a fresh temporary DATA_DIR, so nothing
is served from a previous cache. Approximate cost: a few YouTube API units and
three short Groq translations.
"""

from __future__ import annotations

import csv
import io
import os
import re
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

pytestmark = pytest.mark.live

ENGLISH_VIDEO = "dQw4w9WgXcQ"  # English captions
HINDI_VIDEO = "la-6erBNvE8"  # Hindi (Devanagari) captions, ~4 minutes
HINDI_CHANNEL = "matrixacademysikar"

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN_WORD = re.compile(r"\b[A-Za-z]{3,}\b")

_ENV = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
_YOUTUBE_KEY = (_ENV.get("YOUTUBE_API_KEY") or "").strip()
_GROQ_KEY = (_ENV.get("GROQ_API_KEY") or "").strip()

if os.environ.get("RUN_LIVE_TESTS") != "1" or not _YOUTUBE_KEY:
    pytest.skip("live tests are opt-in: RUN_LIVE_TESTS=1 and YOUTUBE_API_KEY in .env", allow_module_level=True)


def _devanagari_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if _DEVANAGARI.match(c)) / max(1, len(letters))


@pytest.fixture
def live(auth_config, monkeypatch):
    import api.youtube_client as youtube_client
    import webapp.main as web
    from config.settings import settings
    from tests.conftest import TEST_USER_KEY

    monkeypatch.setenv("YOUTUBE_API_KEY", _YOUTUBE_KEY)
    monkeypatch.setattr(settings, "youtube_api_key", _YOUTUBE_KEY)
    monkeypatch.setattr(settings, "groq_api_key", _GROQ_KEY)
    if hasattr(youtube_client, "settings"):
        monkeypatch.setattr(youtube_client.settings, "youtube_api_key", _YOUTUBE_KEY)
    for name in ("_shared_transcript_service", "_transcript_service", "_channel_service", "_video_service"):
        monkeypatch.setattr(web, name, None)
    with TestClient(web.app, headers={"X-API-Key": TEST_USER_KEY}) as client:
        yield client


def _transcript(client, video: str, mode: str) -> dict:
    resp = client.post("/api/transcript", json={"video_url": f"https://www.youtube.com/watch?v={video}", "output_language": mode})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    return body


def test_original_spoken_english_video(live):
    body = _transcript(live, ENGLISH_VIDEO, "original")
    assert body["output_language"] == "original"
    assert body["fallback_to_original"] is False
    assert body["transcript"] == body["raw_transcript"]
    assert len(_LATIN_WORD.findall(body["transcript"])) > 50


def test_original_spoken_hindi_video_is_not_translated(live):
    body = _transcript(live, HINDI_VIDEO, "original")
    assert body["output_language"] == "original"
    assert body["transcript"] == body["raw_transcript"]
    assert _devanagari_ratio(body["transcript"]) > 0.5, "Original Spoken must stay in the spoken script"


@pytest.mark.skipif(not _GROQ_KEY, reason="GROQ_API_KEY not configured")
def test_simple_english_from_hindi(live):
    original = _transcript(live, HINDI_VIDEO, "original")["transcript"]
    body = _transcript(live, HINDI_VIDEO, "en")
    assert body["output_language"] == "en" and body["fallback_to_original"] is False
    assert body["transcript"] != original
    assert _devanagari_ratio(body["transcript"]) < 0.05
    assert body["raw_transcript"] == original  # the original stays available and untouched


@pytest.mark.skipif(not _GROQ_KEY, reason="GROQ_API_KEY not configured")
def test_simple_hindi_from_hindi_and_english(live):
    for video in (HINDI_VIDEO, ENGLISH_VIDEO):
        body = _transcript(live, video, "hi")
        assert body["output_language"] == "hi" and body["fallback_to_original"] is False
        assert _devanagari_ratio(body["transcript"]) > 0.5


def test_invalid_url_rejected(live):
    resp = live.post("/api/transcript", json={"video_url": "https://example.com/not-youtube", "output_language": "original"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "INVALID_YOUTUBE_URL"


def test_single_video_csv_export_original(live):
    resp = live.post("/api/transcript/export", json={"video_url": f"https://www.youtube.com/watch?v={HINDI_VIDEO}", "output_language": "original"})
    assert resp.status_code == 200, resp.text
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["status"] == "success"
    assert _devanagari_ratio(rows[0]["transcript"]) > 0.5


def test_channel_sync_original(live):
    resp = live.get(f"/api/channel/{HINDI_CHANNEL}/transcripts", params={"limit": 3, "concurrency": 1, "allow_whisper": "false", "output_language": "original"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total_discovered"] >= 1
    for video in data["videos"]:
        if video["status"] == "success":
            assert video["transcript"] == video["raw_transcript"]


def test_channel_job_lifecycle(live):
    created = live.post(f"/api/channel/{HINDI_CHANNEL}/transcript-job", json={"max_videos": 2, "output_language": "original"})
    assert created.status_code == 200, created.text
    job_id = created.json()["data"]["job_id"]
    deadline = time.time() + 240
    job = {}
    while time.time() < deadline:
        job = live.get(f"/api/transcript/jobs/{job_id}").json()["data"]
        if job["status"] in ("completed", "failed", "cancelled", "paused"):
            break
        time.sleep(2)
    assert job["status"] == "completed", job.get("error")
    assert job["total_discovered"] >= 1
    for video in job["videos"]:
        if video["status"] == "success":
            assert video["transcript"] == video["raw_transcript"]
    download = live.get(f"/api/transcript/jobs/{job_id}/download")
    assert download.status_code == 200 and download.headers["content-type"].startswith("text/csv")


@pytest.mark.skipif(not _GROQ_KEY, reason="GROQ_API_KEY not configured")
def test_groq_whisper_single_upload_and_chunked_agree(tmp_path, monkeypatch):
    """Real audio download + real Groq Whisper, once whole and once forced into chunks.

    Uses the English video: YouTube intermittently answers yt-dlp with a "confirm you're
    not a bot" challenge for some videos/IPs, which fails this test (by design, loudly).
    """
    import services.transcription.groq as groq_module
    from config.settings import settings
    from services.transcription.audio_chunking import ffmpeg_path
    from services.transcription.groq import GroqWhisperProvider
    from services.youtube.audio import YouTubeAudioExtractor

    if ffmpeg_path() is None:
        pytest.skip("ffmpeg is not installed")
    provider = GroqWhisperProvider(api_key=_GROQ_KEY)
    with YouTubeAudioExtractor(temp_dir=tmp_path).audio_context(ENGLISH_VIDEO) as audio:
        whole = provider.transcribe(audio)
        monkeypatch.setattr(groq_module, "MAX_UPLOAD_BYTES", 256 * 1024)  # force the long-video path
        monkeypatch.setattr(settings, "stt_chunk_seconds", 90)
        assert audio.stat().st_size > groq_module.MAX_UPLOAD_BYTES
        chunked = provider.transcribe(audio)

    for result in (whole, chunked):
        assert result.language == "en"
        assert len(_LATIN_WORD.findall(result.text)) > 50
        starts = [s.start for s in result.segments]
        assert starts == sorted(starts)
    assert chunked.segments[-1].end > 180  # timestamps run across chunk boundaries (213 s video)
    whole_words, chunked_words = len(whole.text.split()), len(chunked.text.split())
    assert abs(whole_words - chunked_words) / whole_words < 0.25
    assert list(tmp_path.iterdir()) == []  # audio and chunk files cleaned up


def test_duration_cap_checked_on_real_metadata_before_download(tmp_path):
    from services.youtube.audio import AudioExtractionError, YouTubeAudioExtractor

    with pytest.raises(AudioExtractionError) as err:
        YouTubeAudioExtractor(temp_dir=tmp_path, max_duration_seconds=60).extract_audio(ENGLISH_VIDEO)
    assert err.value.error_code == "AUDIO_TOO_LONG"
    assert list(tmp_path.iterdir()) == []  # nothing was downloaded


def test_channel_job_cancel(live):
    created = live.post(f"/api/channel/{HINDI_CHANNEL}/transcript-job", json={"max_videos": 5, "output_language": "original"})
    job_id = created.json()["data"]["job_id"]
    assert live.post(f"/api/transcript/jobs/{job_id}/cancel").status_code == 200
    assert live.get(f"/api/transcript/jobs/{job_id}").json()["data"]["status"] == "cancelled"
