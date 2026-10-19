"""Simple English / Simple Hindi must never silently show the untranslated original.

Regression for: Groq retired ``llama-3.3-70b-versatile`` (404 model_not_found); every
translation failed, the job fell back to the raw Devanagari captions and the UI still
labelled them "Simple English".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.translation.service as translation_module
import webapp.main as web
from models.transcript_job import TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import TranscriptJobManager
from services.public_errors import public_error, public_message
from services.translation.service import TranslationError, TranslationService

RAW = "आई एम पूजा दुबे आई एम हियर विद माय डॉटर"


class _FakeCompletions:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def create(self, **kwargs):
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.behaviour), finish_reason="stop",
        )])


def _service(tmp_path, behaviour, model="openai/gpt-oss-120b") -> TranslationService:
    from repositories.transcript_repository import TranscriptRepository

    svc = TranslationService(repository=TranscriptRepository(persist_dir=str(tmp_path)), api_key="gsk_test", model=model)
    svc._client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(behaviour)))
    return svc


def test_default_translation_model_is_not_the_retired_llama(monkeypatch):
    from config.settings import Settings

    monkeypatch.delenv("GROQ_TRANSLATION_MODEL", raising=False)
    assert Settings().groq_translation_model == "openai/gpt-oss-120b"


def test_missing_model_is_a_non_retryable_configuration_error(tmp_path):
    retired = Exception(
        "Error code: 404 - {'error': {'message': 'The model `llama-3.3-70b-versatile` does not exist or you "
        "do not have access to it.', 'type': 'invalid_request_error', 'code': 'model_not_found'}}"
    )
    svc = _service(tmp_path, retired, model="llama-3.3-70b-versatile")
    with pytest.raises(TranslationError) as exc:
        svc.translate(video_id="sXLWQoDeRoE", original_text=RAW, target_language="en")
    assert exc.value.error_code == "TRANSLATION_UNAVAILABLE"
    assert exc.value.retryable is False
    code, err = public_error("TRANSLATION_UNAVAILABLE")
    assert code == "TRANSLATION_UNAVAILABLE" and err.status_code == 503 and "llama" not in err.message


def test_reasoning_model_gets_low_reasoning_effort(tmp_path):
    seen = {}

    class Recorder(_FakeCompletions):
        def create(self, **kwargs):
            seen.update(kwargs)
            return super().create(**kwargs)

    svc = _service(tmp_path, "I am Pooja Dubey.")
    svc._client = SimpleNamespace(chat=SimpleNamespace(completions=Recorder("I am Pooja Dubey.")))
    result = svc.translate(video_id="sXLWQoDeRoE", original_text=RAW, target_language="en")
    assert result["transcript"] == "I am Pooja Dubey."
    assert seen["model"] == "openai/gpt-oss-120b" and seen["reasoning_effort"] == "low"


def _job_with_item(output_language="en"):
    item = TranscriptVideoItem(
        video_id="sXLWQoDeRoE", video_url="https://www.youtube.com/watch?v=sXLWQoDeRoE",
        status="success", raw_transcript=RAW, transcript=RAW, source_language="Hindi", language="Hindi",
    )
    job = TranscriptJobProgress(
        job_id="0123456789ab", channel_handle="@c", channel_id="UC1", channel_title="C",
        output_language=output_language, videos=[item],
    )
    return job, item


@pytest.mark.parametrize(("failure", "code"), [
    (TranslationError("boom", error_code="TRANSLATION_UNAVAILABLE", retryable=False), "TRANSLATION_UNAVAILABLE"),
    (TranslationError("Groq 503 upstream"), "TRANSLATION_FAILED"),
    (RuntimeError("network"), "TRANSLATION_FAILED"),
])
async def test_job_marks_failed_translation_instead_of_hiding_it(tmp_path, monkeypatch, failure, code):
    class Down:
        def translate(self, **kwargs):
            raise failure

    monkeypatch.setattr(translation_module, "get_translation_service", lambda: Down())
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    job, item = _job_with_item()
    await manager._apply_output_language(job, item)

    assert item.transcript == RAW  # original kept, never empty
    assert item.simple_english_transcript == ""
    assert item.language == "Hindi"  # never labelled "Simple English"
    assert item.translation_error == public_message(code)


async def test_job_success_clears_a_previous_translation_error(tmp_path, monkeypatch):
    class Up:
        def translate(self, **kwargs):
            return {"transcript": "I am Pooja Dubey."}

    monkeypatch.setattr(translation_module, "get_translation_service", lambda: Up())
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    job, item = _job_with_item()
    item.translation_error = "old failure"
    await manager._apply_output_language(job, item)
    assert item.transcript == item.simple_english_transcript == "I am Pooja Dubey."
    assert item.language == "Simple English"
    assert item.translation_error is None


async def test_original_mode_never_reports_a_translation_error(tmp_path):
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    job, item = _job_with_item(output_language="original")
    await manager._apply_output_language(job, item)
    assert item.transcript == RAW and item.translation_error is None


async def test_sync_routes_report_translation_error(monkeypatch):
    class Down:
        def translate(self, **kwargs):
            raise TranslationError("model gone", error_code="TRANSLATION_UNAVAILABLE", retryable=False)

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(web, "_get_unified_services", lambda: (None, Down()))
    monkeypatch.setattr(web, "_transcript_work", run_inline)
    text, label, error = await web._localized_transcript("sXLWQoDeRoE", RAW, "Hindi", "en", "rid")
    assert (text, label) == (RAW, "Hindi")
    assert error == public_message("TRANSLATION_UNAVAILABLE")

    text, label, error = await web._localized_transcript("sXLWQoDeRoE", RAW, "Hindi", "original", "rid")
    assert (text, label, error) == (RAW, "Hindi", None)
