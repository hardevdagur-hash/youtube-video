"""Long-video speech-to-text: duration cap, ffmpeg chunking, merge and all-or-nothing failure."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.transcription.groq as groq_module
from config.settings import settings
from services.transcription.audio_chunking import (
    AudioChunk,
    AudioChunkingError,
    ffmpeg_path,
    split_audio,
)
from services.transcription.groq import GroqTranscriptionError, GroqWhisperProvider
from services.transcription.provider import TranscriptionResult, TranscriptionSegment

needs_ffmpeg = pytest.mark.skipif(ffmpeg_path() is None, reason="ffmpeg is not installed")


def _make_audio(path: Path, seconds: int) -> Path:
    subprocess.run(  # noqa: S603 (fixed argv)
        [ffmpeg_path(), "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "libopus", "-b:a", "24k", str(path)],
        check=True, capture_output=True, timeout=120,
    )
    return path


class _FakeTranscriptions:
    """Stands in for ``groq_client.audio.transcriptions``; one response per uploaded chunk."""

    def __init__(self, languages: list[str], fail_on_call: int | None = None) -> None:
        self.languages = languages
        self.fail_on_call = fail_on_call
        self.uploads: list[str] = []

    def create(self, **kwargs):
        name, handle = kwargs["file"]
        assert handle.read(1), "chunk upload must not be empty"
        self.uploads.append(name)
        call = len(self.uploads)
        if call == self.fail_on_call:
            raise RuntimeError("upstream exploded")
        return {
            "text": f"part{call}",
            "segments": [{"start": 1.0, "end": 2.5, "text": f"part{call}"}],
            "language": self.languages[call - 1],
            "duration": 60.0,
        }


def _provider(fake: _FakeTranscriptions) -> GroqWhisperProvider:
    provider = GroqWhisperProvider(api_key="gsk_test_not_real", max_retries=1)
    provider._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=fake))
    return provider


# ---------------------------------------------------------------------------
# ffmpeg splitting
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_split_audio_produces_ordered_chunks_with_real_offsets(tmp_path):
    source = _make_audio(tmp_path / "long.ogg", 150)
    chunks = split_audio(source, tmp_path / "chunks", chunk_seconds=60)
    assert [round(c.start) for c in chunks] == [0, 60, 120]
    assert round(chunks[-1].end) == 150
    assert all(c.path.parent == tmp_path / "chunks" and c.path.stat().st_size > 0 for c in chunks)


def test_split_audio_without_ffmpeg_fails_clearly(tmp_path, monkeypatch):
    import services.transcription.audio_chunking as chunking

    monkeypatch.setattr(chunking, "ffmpeg_path", lambda: None)
    with pytest.raises(AudioChunkingError, match="ffmpeg is required"):
        split_audio(tmp_path / "x.ogg", tmp_path / "out", 60)


@needs_ffmpeg
def test_split_audio_rejects_non_audio(tmp_path):
    bogus = tmp_path / "bogus.m4a"
    bogus.write_bytes(b"not audio at all" * 100)
    with pytest.raises(AudioChunkingError):
        split_audio(bogus, tmp_path / "out", 60)


# ---------------------------------------------------------------------------
# Provider: chunked transcription
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_audio_over_upload_limit_is_chunked_and_merged(tmp_path, monkeypatch):
    source = _make_audio(tmp_path / "long.ogg", 150)
    monkeypatch.setattr(groq_module, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(settings, "stt_chunk_seconds", 60)
    fake = _FakeTranscriptions(["hindi", "hindi", "english"])

    result = _provider(fake).transcribe(source)

    assert len(fake.uploads) == 3
    assert result.text == "part1 part2 part3"
    # Chunk-relative timestamps are shifted onto the original timeline.
    assert [(round(s.start), s.text) for s in result.segments] == [(1, "part1"), (61, "part2"), (121, "part3")]
    assert result.language == "hi"  # spoken for 120 of 150 seconds
    assert round(result.duration) == 150
    assert list(tmp_path.glob("*_chunks_*")) == []  # chunk files are cleaned up


@needs_ffmpeg
def test_one_failed_chunk_fails_the_whole_transcript(tmp_path, monkeypatch):
    source = _make_audio(tmp_path / "long.ogg", 150)
    monkeypatch.setattr(groq_module, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(settings, "stt_chunk_seconds", 60)
    fake = _FakeTranscriptions(["en", "en", "en"], fail_on_call=2)

    with pytest.raises(GroqTranscriptionError):
        _provider(fake).transcribe(source)
    assert len(fake.uploads) == 2  # stopped at the failure; no partial result returned
    assert list(tmp_path.glob("*_chunks_*")) == []


@needs_ffmpeg
def test_audio_longer_than_cap_rejected_before_any_upload(tmp_path, monkeypatch):
    source = _make_audio(tmp_path / "long.ogg", 150)
    monkeypatch.setattr(groq_module, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(settings, "stt_chunk_seconds", 60)
    monkeypatch.setattr(settings, "stt_max_audio_seconds", 120)
    fake = _FakeTranscriptions(["en", "en", "en"])

    with pytest.raises(GroqTranscriptionError) as info:
        _provider(fake).transcribe(source)
    assert info.value.error_code == "AUDIO_TOO_LONG"
    assert fake.uploads == []


def test_small_audio_is_uploaded_unchanged(tmp_path):
    audio = tmp_path / "short.m4a"
    audio.write_bytes(b"x" * 2048)
    fake = _FakeTranscriptions(["english"])
    result = _provider(fake).transcribe(audio)
    assert fake.uploads == ["short.m4a"]
    assert result.text == "part1" and result.language == "en"


def test_merge_ignores_silent_chunks_for_language():
    def result(text, lang):
        segs = [TranscriptionSegment(start=0.5, end=1.0, text=text)] if text else []
        return TranscriptionResult(text=text, segments=segs, language=lang, duration=10.0)

    merged = GroqWhisperProvider._merge_chunks([
        (AudioChunk(Path("a"), 0.0, 10.0), result("", "en")),
        (AudioChunk(Path("b"), 10.0, 20.0), result("नमस्ते", "hi")),
    ])
    assert merged.text == "नमस्ते"
    assert merged.language == "hi"
    assert merged.segments[0].start == 10.5


# ---------------------------------------------------------------------------
# Download: duration cap before any audio is fetched
# ---------------------------------------------------------------------------


class _FakeYDL:
    downloads: list[str] = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        assert download is False
        return dict(self.info)

    def process_ie_result(self, info, download):
        _FakeYDL.downloads.append(info["id"])
        Path(self.opts["outtmpl"].replace("%(ext)s", "m4a")).write_bytes(b"audio")
        return {**info, "ext": "m4a"}


@pytest.fixture
def fake_ydl(monkeypatch):
    import services.youtube.audio as audio_module

    _FakeYDL.downloads = []
    monkeypatch.setattr(audio_module.yt_dlp, "YoutubeDL", _FakeYDL)
    return _FakeYDL


@pytest.mark.parametrize(("info", "code"), [
    ({"id": "abcdefghijk", "duration": 7201}, "AUDIO_TOO_LONG"),
    ({"id": "abcdefghijk", "duration": 100, "live_status": "is_live"}, "AUDIO_EXTRACTION_FAILED"),
])
def test_extractor_refuses_before_download(fake_ydl, tmp_path, info, code):
    from services.youtube.audio import AudioExtractionError, YouTubeAudioExtractor

    fake_ydl.info = info
    with pytest.raises(AudioExtractionError) as err:
        YouTubeAudioExtractor(temp_dir=tmp_path, max_duration_seconds=7200).extract_audio("abcdefghijk")
    assert err.value.error_code == code
    assert fake_ydl.downloads == []


def test_extractor_downloads_within_cap_to_unique_files(fake_ydl, tmp_path):
    from services.youtube.audio import YouTubeAudioExtractor

    fake_ydl.info = {"id": "abcdefghijk", "duration": 3600}
    extractor = YouTubeAudioExtractor(temp_dir=tmp_path, max_duration_seconds=7200)
    first, second = extractor.extract_audio("abcdefghijk"), extractor.extract_audio("abcdefghijk")
    assert first != second and first.is_file() and second.is_file()


def test_channel_audio_service_keeps_error_code(fake_ydl, tmp_path, monkeypatch):
    from exceptions.transcript_errors import AudioDownloadError
    from services.audio.audio_service import AudioService

    monkeypatch.setattr(settings, "stt_max_audio_seconds", 60)
    fake_ydl.info = {"id": "abcdefghijk", "duration": 61}
    with pytest.raises(AudioDownloadError) as err:
        AudioService(temp_dir=tmp_path).download_audio("abcdefghijk")
    assert err.value.error_code == "AUDIO_TOO_LONG"


def test_both_pipelines_default_to_data_dir_audio(tmp_path):
    from services.audio.audio_service import AudioService
    from services.youtube.audio import YouTubeAudioExtractor

    assert YouTubeAudioExtractor().temp_dir == settings.audio_temp_dir
    assert AudioService().temp_dir == settings.audio_temp_dir


# ---------------------------------------------------------------------------
# API and startup
# ---------------------------------------------------------------------------


def test_api_reports_audio_too_long(authed_client, monkeypatch):
    import webapp.main as web
    from services.youtube.audio import AudioExtractionError

    class TooLong:
        def get_canonical_transcript(self, _url):
            raise AudioExtractionError("3h video", error_code="AUDIO_TOO_LONG")

    monkeypatch.setattr(web, "_shared_transcript_service", TooLong())
    resp = authed_client.post("/api/transcript", json={"video_url": "dQw4w9WgXcQ", "output_language": "original"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "AUDIO_TOO_LONG" and "3h video" not in resp.text


def test_stale_audio_removed_at_startup(monkeypatch, tmp_path):
    import webapp.main as web

    audio_dir = tmp_path / "audio"
    (audio_dir / "x_chunks_1").mkdir(parents=True)
    (audio_dir / "x_chunks_1" / "chunk_00000.ogg").write_bytes(b"a")
    (audio_dir / "abcdefghijk_1234.m4a").write_bytes(b"a")
    monkeypatch.setattr(settings, "audio_temp_dir", audio_dir)
    web._clear_stale_audio()
    assert list(audio_dir.iterdir()) == []
