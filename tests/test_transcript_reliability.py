"""Transcript reliability under YouTube / speech-to-text failures (no network).

Covers the production fixes for: transient failures never cached as "no captions",
caption request timeouts, no extra YouTube traffic or Groq spend after a YouTube 429 or
bot check, the process-wide speech-to-text limit, the dedicated transcript worker pool,
thread-safe YouTube API clients and retryable channel-job items.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests

from clients.youtube_transcript_client import (
    DEFAULT_TIMEOUT,
    CaptionRequestFailedError,
    TimeoutSession,
    TooManyRequestsError,
    TranscriptSslError,
    YouTubeTranscriptClientError,
)
from exceptions.transcript_errors import AudioDownloadError, TranscriptionError
from models.transcript import PipelineStep, TranscriptResult, TranscriptSource
from services.transcript_failures import classify_stt_error, looks_bot_blocked
from services.transcript_limiter import transcript_limiter
from services.transcript_service import TranscriptService

# isort: split
# Must follow transcript_service (import order as in the app; avoids a circular import).
import providers.whisper_provider as whisper_module
from providers.whisper_provider import WhisperProvider
from services.transcription.stt_gate import STTBusyError, STTGate

VIDEO_ID = "dQw4w9WgXcQ"
BOT_CHECK = "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot. Use --cookies"
IP_BLOCK = "YouTube is blocking requests from your IP. This usually is due to cloud provider IPs"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Captions:
    """Caption provider: returns no captions, or raises the given exception."""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls = 0

    def name(self):
        return "captions"

    def get_transcript(self, video_id, *args, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return TranscriptResult(success=False, video_id=video_id, source=TranscriptSource.MANUAL,
                                error="No captions", error_code="NO_CAPTIONS")


class STT:
    """Speech-to-text provider: raises ``exc``, or returns an empty (definitive) result."""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls = 0

    def name(self):
        return "stt"

    def get_transcript(self, video_id, *args, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return TranscriptResult(success=False, video_id=video_id, source=TranscriptSource.WHISPER,
                                error="no speech detected")


class Repo:
    def __init__(self, cached: TranscriptResult | None = None):
        self.cached = cached
        self.saved: list[TranscriptResult] = []

    def get(self, video_id):
        return self.cached

    def save(self, result):
        self.saved.append(result)


def _coded(exc: Exception, code: str) -> Exception:
    exc.error_code = code
    return exc


def _service(manual=None, auto=None, stt=None, repo=None) -> TranscriptService:
    return TranscriptService(
        manual_provider=manual or Captions(), auto_provider=auto or Captions(),
        whisper_provider=stt or STT(), repository=repo or Repo(),
    )


@pytest.fixture(autouse=True)
def rate_limit_recordings(monkeypatch):
    """Each test starts with YouTube not rate limited; rate-limit recordings are counted."""
    transcript_limiter.record_success()
    recorded: list[str] = []
    real_record = transcript_limiter.record_rate_limit

    def record(video_id=None):
        recorded.append(video_id)
        return real_record(video_id)

    monkeypatch.setattr(transcript_limiter, "record_rate_limit", record)
    yield recorded
    transcript_limiter.record_success()


# ---------------------------------------------------------------------------
# Transient speech-to-text failures are retryable and never cached as NO_CAPTIONS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "code"), [
    pytest.param(_coded(AudioDownloadError(BOT_CHECK), "BOT_BLOCKED"), "BOT_BLOCKED", id="bot-blocked-download"),
    pytest.param(AudioDownloadError(f"Failed to download audio: {BOT_CHECK}"), "BOT_BLOCKED", id="bot-text-only"),
    pytest.param(_coded(TranscriptionError("Groq rate limit exceeded: 429"), "GROQ_RATE_LIMIT"),
                 "STT_RATE_LIMITED", id="groq-429"),
    pytest.param(_coded(TranscriptionError("Request timed out"), "GROQ_TIMEOUT"), "STT_TIMEOUT", id="groq-timeout"),
    pytest.param(TranscriptionError("upstream 502 bad gateway"), "STT_FAILED", id="groq-5xx"),
    pytest.param(_coded(TranscriptionError("busy"), "STT_BUSY"), "STT_BUSY", id="stt-busy"),
    pytest.param(RuntimeError("raw provider error"), "STT_FAILED", id="unwrapped-error"),
])
def test_transient_stt_failure_is_retryable_and_not_cached(rate_limit_recordings, exc, code):
    repo = Repo()
    result = _service(stt=STT(exc), repo=repo).get_transcript(VIDEO_ID, allow_whisper=True)
    assert result.success is False
    assert result.error_code == code
    assert result.error_code != "NO_CAPTIONS"
    assert repo.saved == []  # nothing poisons the cache
    assert rate_limit_recordings == []  # an STT failure never trips the YouTube circuit breaker


def test_genuine_no_captions_after_stt_ran_is_cached():
    repo = Repo()
    result = _service(repo=repo).get_transcript(VIDEO_ID, allow_whisper=True)
    assert result.error_code == "NO_CAPTIONS"
    assert [r.error_code for r in repo.saved] == ["NO_CAPTIONS"]


def test_caption_only_no_captions_is_cached():
    repo = Repo()
    stt = STT()
    result = _service(stt=stt, repo=repo).get_transcript(VIDEO_ID, allow_whisper=False)
    assert result.error_code == "NO_CAPTIONS" and stt.calls == 0
    assert len(repo.saved) == 1


@pytest.mark.parametrize(("message", "code"), [
    ("Manual transcript unavailable: HTTPSConnectionPool: Read timed out.", "TIMEOUT"),
    ("Manual transcript unavailable: Connection reset by peer", "NETWORK_ERROR"),
])
def test_caption_request_failures_are_transient_not_no_captions(message, code):
    repo = Repo()
    failing = Captions(CaptionRequestFailedError(message))
    result = _service(manual=failing, auto=Captions(CaptionRequestFailedError(message)), repo=repo).get_transcript(
        VIDEO_ID, allow_whisper=False,
    )
    assert result.error_code == code
    assert repo.saved == []


@pytest.mark.parametrize(("library_error", "transient"), [
    pytest.param(requests.exceptions.ConnectionError("connection reset"), True, id="connection-error"),
    pytest.param(requests.exceptions.ReadTimeout("read timed out"), True, id="timeout"),
    pytest.param(type("YouTubeRequestFailed", (Exception,), {})("HTTP 503"), True, id="youtube-request-failed"),
    pytest.param(type("AgeRestricted", (Exception,), {})("age restricted"), False, id="age-restricted"),
    pytest.param(type("VideoUnplayable", (Exception,), {})("members only"), False, id="unplayable"),
])
def test_providers_separate_request_failures_from_facts_about_the_video(library_error, transient):
    from clients.youtube_transcript_client import (
        NoTranscriptFoundError,
        YouTubeTranscriptClientError,
    )
    from providers.manual_transcript_provider import ManualTranscriptProvider

    class Client:
        def find_best_transcript(self, *args, **kwargs):
            try:
                raise library_error
            except Exception as exc:  # what list_transcripts does with unhandled library errors
                raise YouTubeTranscriptClientError(f"Failed to list transcripts: {exc}") from exc

    with pytest.raises(NoTranscriptFoundError) as err:
        ManualTranscriptProvider(client=Client()).get_transcript(VIDEO_ID)
    assert isinstance(err.value, CaptionRequestFailedError) is transient


def test_caption_ip_block_is_bot_blocked_and_stops_all_youtube_traffic(rate_limit_recordings):
    auto, stt, repo = Captions(), STT(), Repo()
    manual = Captions(CaptionRequestFailedError(f"Manual transcript unavailable: {IP_BLOCK}"))
    result = _service(manual=manual, auto=auto, stt=stt, repo=repo).get_transcript(VIDEO_ID, allow_whisper=True)
    assert result.error_code == "BOT_BLOCKED"
    assert auto.calls == 0 and stt.calls == 0  # no further YouTube requests, no Groq spend
    assert rate_limit_recordings == [VIDEO_ID]  # the cooldown starts
    assert repo.saved == []


def test_youtube_429_does_not_fall_back_to_speech_to_text(rate_limit_recordings):
    auto, stt, repo = Captions(), STT(), Repo()
    manual = Captions(TooManyRequestsError("Rate limited by YouTube."))
    result = _service(manual=manual, auto=auto, stt=stt, repo=repo).get_transcript(VIDEO_ID, allow_whisper=True)
    assert result.error_code == "RATE_LIMITED"
    assert auto.calls == 0 and stt.calls == 0
    assert rate_limit_recordings == [VIDEO_ID]
    assert repo.saved == []


def test_groq_rate_limit_text_does_not_open_the_youtube_circuit(rate_limit_recordings):
    stt = STT(TranscriptionError("Whisper transcription failed: Groq rate limit exceeded (429 Too Many Requests)"))
    result = _service(stt=stt).get_transcript(VIDEO_ID, allow_whisper=True)
    assert result.error_code == "STT_RATE_LIMITED"
    assert rate_limit_recordings == []
    assert not transcript_limiter.is_in_cooldown()


# ---------------------------------------------------------------------------
# Cache state transitions
# ---------------------------------------------------------------------------


def _cached_failure(code: str, whisper: dict | None = None) -> TranscriptResult:
    steps = [PipelineStep(name="Manual Transcript", status="skipped", error_type="NO_CAPTIONS")]
    if whisper is not None:
        steps.append(PipelineStep(name="Whisper STT", **whisper))
    return TranscriptResult(success=False, video_id=VIDEO_ID, source=TranscriptSource.MANUAL,
                            error="x", error_code=code, pipeline_steps=steps)


@pytest.mark.parametrize(("cached", "allow_whisper", "reused"), [
    pytest.param(_cached_failure("NO_CAPTIONS", {"status": "error"}), True, True, id="no-captions-after-stt"),
    pytest.param(_cached_failure("NO_CAPTIONS"), False, True, id="no-captions-caption-only"),
    pytest.param(_cached_failure("NO_CAPTIONS"), True, False, id="stt-never-tried"),
    pytest.param(_cached_failure("NO_CAPTIONS", {"status": "skipped"}), True, False, id="stt-was-unavailable"),
    pytest.param(_cached_failure("NO_CAPTIONS", {"status": "error", "error_type": "BOT_BLOCKED"}), True, False,
                 id="legacy-poisoned-bot-blocked"),
    pytest.param(_cached_failure("NO_CAPTIONS", {"status": "error", "error_type": "STT_FAILED"}), True, False,
                 id="legacy-poisoned-stt-failed"),
    pytest.param(_cached_failure("NO_CAPTIONS", {"status": "error", "detail": f"AudioDownloadError: {BOT_CHECK}"}),
                 True, False, id="legacy-unclassified-stt-exception"),
    pytest.param(_cached_failure("NO_CAPTIONS", {"status": "error", "detail": "no speech detected"}),
                 True, True, id="legacy-definitive-stt-result"),
    pytest.param(_cached_failure("STT_FAILED"), False, False, id="transient-code"),
    pytest.param(_cached_failure("NETWORK_ERROR"), False, False, id="network-error"),
    pytest.param(_cached_failure("CAPTIONS_DISABLED"), False, True, id="captions-disabled"),
    pytest.param(_cached_failure("VIDEO_UNAVAILABLE"), True, True, id="video-unavailable"),
])
def test_cached_failures_are_reused_only_when_they_are_facts(cached, allow_whisper, reused):
    manual = Captions()
    result = _service(manual=manual, repo=Repo(cached=cached)).get_transcript(VIDEO_ID, allow_whisper=allow_whisper)
    if reused:
        assert result is cached and manual.calls == 0
    else:
        assert manual.calls == 1  # fetched again


def test_classification_helpers():
    assert looks_bot_blocked(BOT_CHECK) and looks_bot_blocked(IP_BLOCK)
    assert not looks_bot_blocked("No transcript found for video")
    # Age-restricted videos are a property of the video, not a block of this server.
    age = "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users."
    assert not looks_bot_blocked(age)
    assert classify_stt_error(AudioDownloadError(age)) == "STT_FAILED"
    cause = _coded(RuntimeError("x"), "GROQ_RATE_LIMIT")
    wrapped = TranscriptionError("Whisper transcription failed")
    wrapped.__cause__ = cause
    assert classify_stt_error(wrapped) == "STT_RATE_LIMITED"  # code read from the cause too
    assert classify_stt_error(_coded(AudioDownloadError("too long"), "AUDIO_TOO_LONG")) == "AUDIO_TOO_LONG"


# ---------------------------------------------------------------------------
# Single-video (canonical) pipeline: no STT after a YouTube 429 / bot check
# ---------------------------------------------------------------------------


class _Reached(Exception):
    """Raised by fakes to prove a stage was reached (and stop the pipeline there)."""


def _canonical(caption_error: Exception | None):
    from services.transcription.service import TranscriptService as CanonicalService

    calls = SimpleNamespace(captions=0, audio=0, stt=0)

    class CaptionsService:
        def fetch_captions(self, video_id, preferred_languages=None):
            calls.captions += 1
            raise caption_error

    class Audio:
        def audio_context(self, video_id):
            calls.audio += 1
            raise _Reached("audio download started")

    class Provider:
        def transcribe(self, path):
            calls.stt += 1
            raise _Reached("stt")

    service = CanonicalService(
        resolver=SimpleNamespace(resolve_video_id=lambda value: VIDEO_ID),
        captions_service=CaptionsService(), audio_extractor=Audio(), transcription_provider=Provider(),
        repository=Repo(),
    )
    return service, calls


@pytest.mark.parametrize("code", ["CAPTIONS_RATE_LIMITED", "BOT_BLOCKED"])
def test_canonical_youtube_block_raises_without_audio_or_groq(rate_limit_recordings, code):
    from services.youtube.captions import CaptionsUnavailableError

    service, calls = _canonical(CaptionsUnavailableError("blocked", error_code=code))
    with pytest.raises(CaptionsUnavailableError) as err:
        service.get_canonical_transcript(VIDEO_ID)
    assert err.value.error_code == code
    assert (calls.captions, calls.audio, calls.stt) == (1, 0, 0)
    assert rate_limit_recordings == [VIDEO_ID]


def test_canonical_unavailable_video_does_not_fall_back(rate_limit_recordings):
    from services.youtube.captions import CaptionsUnavailableError

    service, calls = _canonical(CaptionsUnavailableError("gone", error_code="VIDEO_UNAVAILABLE"))
    with pytest.raises(CaptionsUnavailableError):
        service.get_canonical_transcript(VIDEO_ID)
    assert calls.audio == 0 and rate_limit_recordings == []


def test_canonical_cooldown_makes_no_youtube_request_at_all():
    from services.youtube.captions import CaptionsUnavailableError

    service, calls = _canonical(AssertionError("captions must not be requested"))
    transcript_limiter.record_rate_limit(VIDEO_ID)
    with pytest.raises(CaptionsUnavailableError) as err:
        service.get_canonical_transcript(VIDEO_ID)
    assert err.value.error_code == "CAPTIONS_RATE_LIMITED"
    assert calls.captions == 0 and calls.audio == 0


def test_canonical_disabled_captions_still_fall_back_to_stt():
    from services.youtube.captions import CaptionsUnavailableError

    service, calls = _canonical(CaptionsUnavailableError("disabled", error_code="CAPTIONS_DISABLED"))
    with pytest.raises(_Reached):
        service.get_canonical_transcript(VIDEO_ID)
    assert calls.audio == 1  # normal fallback is unchanged


@pytest.mark.parametrize(("exc", "code"), [
    (TooManyRequestsError("Rate limited by YouTube."), "CAPTIONS_RATE_LIMITED"),
    (YouTubeTranscriptClientError(f"Failed to list transcripts: {IP_BLOCK}"), "BOT_BLOCKED"),
    (RuntimeError(f"RequestBlocked: {IP_BLOCK}"), "BOT_BLOCKED"),
    (YouTubeTranscriptClientError("Failed to list transcripts: boom"), "CAPTIONS_UNAVAILABLE"),
])
def test_caption_service_classifies_youtube_blocks(exc, code):
    from services.youtube.captions import CaptionsUnavailableError, YouTubeCaptionsService

    class Client:
        def find_best_transcript(self, **kwargs):
            raise exc

    with pytest.raises(CaptionsUnavailableError) as err:
        YouTubeCaptionsService(client=Client()).fetch_captions(VIDEO_ID)
    assert err.value.error_code == code


def test_unified_endpoint_returns_retryable_bot_blocked(authed_client, monkeypatch):
    import webapp.main as web
    from services.youtube.audio import AudioExtractionError

    class Blocked:
        def get_canonical_transcript(self, url):
            raise AudioExtractionError(BOT_CHECK, error_code="BOT_BLOCKED")

    monkeypatch.setattr(web, "_get_unified_services", lambda: (Blocked(), None))
    resp = authed_client.post("/api/transcript", json={"video_url": VIDEO_ID})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "BOT_BLOCKED" and body["retryable"] is True
    assert "cookies" not in resp.text  # upstream detail stays server-side


# ---------------------------------------------------------------------------
# yt-dlp: bot checks are classified; timeouts, proxy and cookies are applied
# ---------------------------------------------------------------------------


class _YDL:
    error: Exception | None = None
    opts: dict = {}

    def __init__(self, opts):
        _YDL.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        raise _YDL.error


def test_ytdlp_bot_check_is_bot_blocked(monkeypatch, tmp_path):
    import services.youtube.audio as audio_module

    _YDL.error = audio_module.yt_dlp.utils.DownloadError(BOT_CHECK)
    monkeypatch.setattr(audio_module.yt_dlp, "YoutubeDL", _YDL)
    with pytest.raises(audio_module.AudioExtractionError) as err:
        audio_module.YouTubeAudioExtractor(temp_dir=tmp_path).extract_audio(VIDEO_ID)
    assert err.value.error_code == "BOT_BLOCKED"
    assert _YDL.opts["socket_timeout"] == audio_module.SOCKET_TIMEOUT_SECONDS
    assert "proxy" not in _YDL.opts and "cookiefile" not in _YDL.opts  # optional, off by default


def test_ytdlp_proxy_and_cookies_come_from_settings(monkeypatch, tmp_path):
    import services.youtube.audio as audio_module
    from config.settings import settings

    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(settings, "youtube_proxy_url", "http://user:pw@proxy.internal:3128")
    monkeypatch.setattr(settings, "ytdlp_cookies_file", cookies)
    _YDL.error = audio_module.yt_dlp.utils.DownloadError("HTTP Error 403")
    monkeypatch.setattr(audio_module.yt_dlp, "YoutubeDL", _YDL)
    with pytest.raises(audio_module.AudioExtractionError) as err:
        audio_module.YouTubeAudioExtractor(temp_dir=tmp_path).extract_audio(VIDEO_ID)
    assert err.value.error_code == "AUDIO_EXTRACTION_FAILED"
    assert _YDL.opts["proxy"] == "http://user:pw@proxy.internal:3128"
    assert _YDL.opts["cookiefile"] == str(cookies)


def test_whisper_provider_keeps_the_download_error_code():
    provider = WhisperProvider(stt_client=SimpleNamespace(model_name=lambda: "x"), temp_dir=tempfile.mkdtemp())

    def blocked(video_id):
        raise _coded(AudioDownloadError(BOT_CHECK), "BOT_BLOCKED")

    provider._audio_service = SimpleNamespace(download_audio=blocked, cleanup=lambda p: None)
    with pytest.raises(AudioDownloadError) as err:
        provider.get_transcript(VIDEO_ID, title="t", channel_title="c")
    assert err.value.error_code == "BOT_BLOCKED"


# ---------------------------------------------------------------------------
# Caption HTTP session: finite timeouts
# ---------------------------------------------------------------------------


def test_caption_session_applies_a_default_timeout(monkeypatch):
    seen: list = []

    def fake_request(self, method, url, *args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    session = TimeoutSession()
    session.get("https://www.youtube.com/watch?v=x")  # youtube-transcript-api passes no timeout
    session.get("https://www.youtube.com/watch?v=x", timeout=5)
    assert seen == [DEFAULT_TIMEOUT, 5]
    assert all(t is not None for t in seen)


def test_caption_session_times_out_instead_of_hanging():
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)  # accepts the TCP connection but never answers
    port = server.getsockname()[1]
    try:
        session = TimeoutSession(timeout=(1.0, 0.5))
        start = time.monotonic()
        with pytest.raises(requests.exceptions.ReadTimeout):
            session.get(f"http://127.0.0.1:{port}/")
        assert time.monotonic() - start < 10
    finally:
        server.close()


def test_caption_session_maps_tls_errors(monkeypatch):
    def tls_fail(self, *args, **kwargs):
        raise requests.exceptions.SSLError("handshake failure")

    monkeypatch.setattr(requests.Session, "request", tls_fail)
    with pytest.raises(TranscriptSslError):
        TimeoutSession().get("https://www.youtube.com/")


def test_transcript_client_uses_the_timeout_session_and_proxy(monkeypatch):
    from clients.youtube_transcript_client import YouTubeTranscriptClient
    from config.settings import settings

    monkeypatch.setattr(settings, "youtube_proxy_url", "http://proxy.internal:3128")
    client = YouTubeTranscriptClient()
    assert isinstance(client._session, TimeoutSession)
    assert client._session.proxies == {"http": "http://proxy.internal:3128", "https": "http://proxy.internal:3128"}


# ---------------------------------------------------------------------------
# Process-wide speech-to-text limit and the dedicated worker pool
# ---------------------------------------------------------------------------


def test_stt_gate_never_exceeds_its_limit():
    gate = STTGate(max_concurrency=2, queue_timeout_seconds=10)
    active = peak = 0
    lock = threading.Lock()

    def work(i):
        nonlocal active, peak
        with gate.slot(f"v{i}"):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(12)))
    assert peak == 2
    assert gate.status() == {"limit": 2, "active": 0, "waiting": 0}


def test_stt_gate_times_out_with_a_retryable_error(monkeypatch):
    gate = STTGate(max_concurrency=1, queue_timeout_seconds=0.05)
    monkeypatch.setattr(whisper_module, "stt_gate", gate)
    provider = WhisperProvider(stt_client=SimpleNamespace(model_name=lambda: "x"), temp_dir=tempfile.mkdtemp())
    with gate.slot("holder"), pytest.raises(TranscriptionError) as err:
        provider.get_transcript(VIDEO_ID, title="t", channel_title="c")
    assert err.value.error_code == "STT_BUSY"
    with pytest.raises(STTBusyError), gate.slot("a"), gate.slot("b"):
        pass


def test_stt_slot_is_released_after_a_failure(monkeypatch):
    gate = STTGate(max_concurrency=1, queue_timeout_seconds=0.05)
    monkeypatch.setattr(whisper_module, "stt_gate", gate)
    provider = WhisperProvider(stt_client=SimpleNamespace(model_name=lambda: "x"), temp_dir=tempfile.mkdtemp())

    def boom(video_id):
        raise RuntimeError("download failed")

    provider._audio_service = SimpleNamespace(download_audio=boom, cleanup=lambda p: None)
    for _ in range(3):  # would raise STT_BUSY if the first failure leaked the slot
        with pytest.raises(AudioDownloadError):
            provider.get_transcript(VIDEO_ID, title="t", channel_title="c")
    assert gate.status()["active"] == 0


async def test_long_transcript_work_does_not_starve_short_blocking_calls(monkeypatch):
    import infrastructure.work_pool as work_pool

    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transcript-work")
    monkeypatch.setattr(work_pool, "_executor", pool)
    try:
        # Saturate every transcript worker with "minutes-long" speech-to-text.
        busy = [asyncio.ensure_future(work_pool.run_transcript_work(release.wait, 30)) for _ in range(4)]
        await asyncio.sleep(0.05)
        # Sign-in style work on the default executor still completes promptly.
        start = time.monotonic()
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "signed in"), timeout=2) == "signed in"
        assert time.monotonic() - start < 2
        assert not any(task.done() for task in busy)  # the long work really was still running
    finally:
        release.set()
        await asyncio.gather(*busy)
        pool.shutdown(wait=True)


async def test_transcript_work_runs_on_its_own_threads_with_the_request_context(monkeypatch):
    import infrastructure.work_pool as work_pool
    from infrastructure.request_context import request_id_var

    monkeypatch.setattr(work_pool, "_executor", None)
    token = request_id_var.set("rid-123")
    try:
        name, rid = await work_pool.run_transcript_work(
            lambda: (threading.current_thread().name, request_id_var.get()),
        )
    finally:
        request_id_var.reset(token)
        work_pool.shutdown()
    assert name.startswith("transcript-work") and rid == "rid-123"


# ---------------------------------------------------------------------------
# YouTube Data API client: no httplib2.Http shared between threads
# ---------------------------------------------------------------------------


def test_youtube_client_uses_one_http_per_thread(monkeypatch):
    import api.youtube_client as yt

    monkeypatch.setattr(yt, "build", lambda *args, **kwargs: SimpleNamespace(http=kwargs["http"]))
    client = yt.YouTubeClient(api_key="AIzaFakeKeyForTests")
    seen: dict[str, object] = {}

    def use(name):
        service = client.get_service()
        assert client.get_service() is service  # cached within the thread
        seen[name] = service.http

    threads = [threading.Thread(target=use, args=(f"t{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(h) for h in seen.values()}) == 3


# ---------------------------------------------------------------------------
# Channel jobs: transient speech-to-text failures stay resumable
# ---------------------------------------------------------------------------


@pytest.fixture
def job_env(monkeypatch, tmp_path):
    import api.channel_service as channel_module
    import api.video_service as video_module
    import services.transcript_service as ts_module
    from config.settings import settings
    from services.jobs.transcript_job_manager import TranscriptJobManager
    from tests.test_phase5_reliability import VIDEOS, FakeYouTube

    behaviour: dict[str, str] = {}

    def result(video_id, allow_whisper):
        if behaviour.get(video_id) == "ok":
            return SimpleNamespace(success=True, plain_text="hi", paragraph_text="hi", raw_transcript="hi",
                                   language="en", source_language="English", source_language_code="en",
                                   error_code=None, error=None)
        code = behaviour.get(video_id, "BOT_BLOCKED") if allow_whisper else "NO_CAPTIONS"
        return SimpleNamespace(success=False, plain_text="", paragraph_text="", raw_transcript="", language="en",
                               source_language=None, source_language_code=None, error_code=code, error="x")

    class FakeTranscripts:
        def get_transcript(self, video_id, allow_whisper=True, **kwargs):
            return result(video_id, allow_whisper)

    async def no_wait(video_id):
        return None

    monkeypatch.setattr(channel_module, "ChannelService", FakeYouTube)
    monkeypatch.setattr(video_module, "VideoService", FakeYouTube)
    monkeypatch.setattr(ts_module, "TranscriptService", FakeTranscripts)
    monkeypatch.setattr(transcript_limiter, "acquire", no_wait)
    monkeypatch.setattr(settings, "whisper_enabled", True)
    return SimpleNamespace(manager=TranscriptJobManager(jobs_dir=tmp_path), behaviour=behaviour, videos=VIDEOS)


async def test_job_item_with_transient_stt_failure_is_resumable(job_env):
    from models.transcript_job import JobStatus
    from tests.test_phase5_reliability import _finish

    job_env.behaviour.update({job_env.videos[0]: "BOT_BLOCKED", job_env.videos[1]: "STT_RATE_LIMITED",
                              job_env.videos[2]: "NO_CAPTIONS"})
    job = await job_env.manager.start_channel_job("chan", owner="u", output_language="original")
    job = await _finish(job_env.manager, job.job_id)
    assert job.status == JobStatus.COMPLETED
    items = {v.video_id: v for v in job.videos}
    for vid, code in ((job_env.videos[0], "BOT_BLOCKED"), (job_env.videos[1], "STT_RATE_LIMITED")):
        assert items[vid].status == "temporary_error" and items[vid].retryable is True
        assert items[vid].error_code == code
    assert items[job_env.videos[2]].status == "no_captions"  # a real "no speech/captions" stays final
    assert job.rate_limited == 2  # retryable items enable "Resume" in the UI

    job_env.behaviour.update({job_env.videos[0]: "ok", job_env.videos[1]: "ok"})
    await job_env.manager.resume_job(job.job_id)
    job = await _finish(job_env.manager, job.job_id)
    statuses = {v.video_id: v.status for v in job.videos}
    assert statuses == {job_env.videos[0]: "success", job_env.videos[1]: "success", job_env.videos[2]: "no_captions"}


async def test_job_does_not_double_record_a_rate_limit_already_recorded(job_env, rate_limit_recordings, monkeypatch):
    import services.transcript_service as ts_module
    from models.transcript_job import JobStatus
    from tests.test_phase5_reliability import _finish

    calls = {"n": 0}

    def rate_limited_once(video_id, allow_whisper):
        calls["n"] += 1
        if calls["n"] == 1:
            # What the service does in its worker thread on a YouTube 429.
            transcript_limiter.record_rate_limit(video_id)
            return SimpleNamespace(success=False, plain_text="", paragraph_text="", raw_transcript="",
                                   language="en", source_language=None, source_language_code=None,
                                   error_code="RATE_LIMITED", error="x")
        return SimpleNamespace(success=True, plain_text="hi", paragraph_text="hi", raw_transcript="hi",
                               language="en", source_language="English", source_language_code="en",
                               error_code=None, error=None)

    class OneVideo:
        def get_transcript(self, video_id, allow_whisper=True, **kwargs):
            return rate_limited_once(video_id, allow_whisper)

    monkeypatch.setattr(ts_module, "TranscriptService", OneVideo)
    monkeypatch.setattr(transcript_limiter, "cooldown_base", 0.01)
    monkeypatch.setattr(transcript_limiter, "cooldown_max", 0.05)
    job = await job_env.manager.start_channel_job("chan", owner="u", output_language="original")
    job = await _finish(job_env.manager, job.job_id, timeout=20)
    assert job.status == JobStatus.COMPLETED
    assert rate_limit_recordings == [job_env.videos[0]]  # recorded once, by the service only


def test_public_error_codes_for_new_failures():
    from services.public_errors import public_error

    for code in ("BOT_BLOCKED", "STT_BUSY"):
        normalized, err = public_error(code)
        assert normalized == code and err.retryable is True and err.status_code == 503
