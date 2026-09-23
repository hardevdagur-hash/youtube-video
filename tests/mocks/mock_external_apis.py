from __future__ import annotations

import random
import time
from typing import Any


class MockTranslationAPI:
    def __init__(self) -> None:
        self._call_history: list[dict[str, Any]] = []
        self._fail_mode: str | None = None
        self._translations: dict[str, str] = {}

    def _record(self, method: str, **kwargs: Any) -> None:
        self._call_history.append({"method": method, **kwargs})

    def translate(self, text: str, target_language: str, source_language: str | None = None) -> str:
        self._record("translate", text=text, target_language=target_language, source_language=source_language)
        if self._fail_mode == "error":
            raise RuntimeError("Translation API error")
        if self._fail_mode == "timeout":
            time.sleep(5)
            raise TimeoutError("Translation API timeout")
        key = f"{text}:{target_language}"
        if key in self._translations:
            return self._translations[key]
        return f"[{target_language}] {text}"

    def detect_language(self, text: str) -> dict[str, Any]:
        self._record("detect_language", text=text)
        if self._fail_mode == "error":
            raise RuntimeError("Language detection API error")
        return {"language": "en", "confidence": 0.98, "reliable": True}

    def get_supported_languages(self) -> list[dict[str, str]]:
        self._record("get_supported_languages")
        return [
            {"code": "en", "name": "English"},
            {"code": "es", "name": "Spanish"},
            {"code": "fr", "name": "French"},
            {"code": "de", "name": "German"},
            {"code": "zh", "name": "Chinese"},
            {"code": "ja", "name": "Japanese"},
        ]

    def register_translation(self, text: str, target_language: str, translation: str) -> None:
        self._translations[f"{text}:{target_language}"] = translation

    def simulate_error(self) -> None:
        self._fail_mode = "error"

    def simulate_timeout(self) -> None:
        self._fail_mode = "timeout"

    def reset(self) -> None:
        self._call_history.clear()
        self._fail_mode = None
        self._translations.clear()

    def record_calls(self) -> list[dict[str, Any]]:
        return list(self._call_history)


class MockWhisperProvider:
    def __init__(self) -> None:
        self._call_history: list[dict[str, Any]] = []
        self._fail_mode: str | None = None
        self._transcriptions: dict[str, dict[str, Any]] = {}
        self._delay: float = 0.0

    def transcribe(self, audio_path: str, language: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self._record("transcribe", audio_path=audio_path, language=language, **kwargs)
        if self._fail_mode == "error":
            raise RuntimeError("Whisper transcription error")
        if self._fail_mode == "timeout":
            time.sleep(5)
            raise TimeoutError("Whisper API timeout")
        if self._delay > 0:
            time.sleep(self._delay)
        if audio_path in self._transcriptions:
            return dict(self._transcriptions[audio_path])
        return {
            "text": "Mock transcribed text from audio file.",
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "Mock transcribed"},
                {"start": 2.0, "end": 4.0, "text": "text from audio"},
                {"start": 4.0, "end": 5.5, "text": "file."},
            ],
            "language": language or "en",
            "duration_seconds": 5.5,
            "processing_time_seconds": 0.5,
        }

    def register_transcription(self, audio_path: str, result: dict[str, Any]) -> None:
        self._transcriptions[audio_path] = dict(result)

    def simulate_error(self) -> None:
        self._fail_mode = "error"

    def simulate_timeout(self) -> None:
        self._fail_mode = "timeout"

    def set_delay(self, seconds: float) -> None:
        self._delay = seconds

    def reset(self) -> None:
        self._call_history.clear()
        self._fail_mode = None
        self._transcriptions.clear()
        self._delay = 0.0

    def record_calls(self) -> list[dict[str, Any]]:
        return list(self._call_history)

    def _record(self, method: str, **kwargs: Any) -> None:
        self._call_history.append({"method": method, **kwargs})
