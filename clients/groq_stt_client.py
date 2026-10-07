"""Groq Whisper as a ``SpeechToTextClient`` backend for ``WhisperProvider``.

Channel jobs and CSV exports use ``WhisperProvider`` (audio download, cleanup and
normalisation); this adapter lets that pipeline use the same hosted Groq Whisper
model as the single-video endpoint, so production needs no local GPU/torch stack.
Groq's transcription endpoint always transcribes verbatim in the spoken language,
which is what the "Original Spoken" mode requires.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from interfaces.speech_to_text import SpeechToTextClient, TranscriptionResult, TranscriptionSegment
from services.transcription.groq import GroqWhisperProvider


class GroqSpeechToTextClient(SpeechToTextClient):
    def __init__(self, provider: GroqWhisperProvider | None = None) -> None:
        self._provider = provider or GroqWhisperProvider()

    def transcribe(self, audio_path: str, language: str | None = None, **_options: Any) -> TranscriptionResult:
        """Transcribe ``audio_path``. Whisper-specific options (task, initial_prompt) are ignored.

        Raises the Groq* exceptions from ``services.transcription.groq`` on failure.
        """
        started = time.monotonic()
        result = self._provider.transcribe(Path(audio_path), language=language)
        segments = [
            TranscriptionSegment(start=s.start, end=s.end, text=s.text)
            for s in result.segments
            if s.text and s.text.strip()
        ]
        if not segments and result.text and result.text.strip():
            # Responses without segment timing still carry the full text.
            segments = [TranscriptionSegment(start=0.0, end=float(result.duration or 0.0), text=result.text.strip())]
        return TranscriptionResult(
            segments=segments,
            language=result.language,
            language_confidence=result.confidence,
            duration_seconds=result.duration or None,
            processing_time_seconds=round(time.monotonic() - started, 3),
        )

    def model_name(self) -> str:
        return f"groq/{self._provider.model}"
