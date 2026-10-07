"""Whisper speech-to-text client using faster-whisper.

Abstracted behind SpeechToTextClient interface for future provider swaps.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any

from exceptions.transcript_errors import TranscriptionError, WhisperModelError
from interfaces.speech_to_text import (
    SpeechToTextClient,
    TranscriptionResult,
    TranscriptionSegment,
)

logger = logging.getLogger(__name__)

try:
    from services.stt.hardware import setup_cuda_dll_paths
    setup_cuda_dll_paths()
except Exception as _cuda_exc:  # optional local-STT acceleration only
    logger.debug("CUDA DLL path setup skipped: %s", _cuda_exc)

_HAS_FASTER_WHISPER = False
FasterWhisperModel = None

try:
    from faster_whisper import WhisperModel as _FasterWhisperModel
    FasterWhisperModel = _FasterWhisperModel
    _HAS_FASTER_WHISPER = True
except ImportError:
    pass


class FasterWhisperClient(SpeechToTextClient):
    """Speech-to-text using faster-whisper (CTranslate2-optimized).

    Supports all Whisper model sizes: tiny, base, small, medium, large-v3.
    Falls back to CPU if no GPU is available.
    """

    _MODEL_SIZES = ("tiny", "base", "small", "medium", "large", "large-v3")

    def __init__(
        self,
        model_size: str = "base",
        device: str = "auto",
        compute_type: str = "auto",
        cpu_threads: int = 4,
        num_workers: int = 2,
    ) -> None:
        if not _HAS_FASTER_WHISPER:
            raise ImportError(
                "faster-whisper is required for local transcription. "
                "Install with: pip install faster-whisper"
            )

        if model_size not in self._MODEL_SIZES:
            raise WhisperModelError(
                f"Invalid model size '{model_size}'. Choose from: {', '.join(self._MODEL_SIZES)}"
            )

        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._num_workers = num_workers
        self._model = None

    def _load_model(self):
        """Lazy-load the Whisper model."""
        if self._model is not None:
            return self._model

        logger.info(
            "Loading faster-whisper model '%s' (device=%s, compute=%s)",
            self._model_size,
            self._device,
            self._compute_type,
        )
        try:
            self._model = FasterWhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
                cpu_threads=self._cpu_threads,
                num_workers=self._num_workers,
            )
            logger.info("Whisper model '%s' loaded successfully", self._model_size)
            return self._model
        except Exception as exc:
            if self._device == "cuda":
                logger.warning(
                    "Failed to load Whisper on CUDA (%s). Falling back to CPU int8.", exc
                )
                try:
                    self._device = "cpu"
                    self._compute_type = "int8"
                    self._model = FasterWhisperModel(
                        self._model_size,
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=self._cpu_threads,
                        num_workers=self._num_workers,
                    )
                    logger.info("Whisper model '%s' loaded successfully on CPU fallback", self._model_size)
                    return self._model
                except Exception as cpu_exc:
                    raise WhisperModelError(
                        f"Failed to load Whisper model '{self._model_size}' on both CUDA and CPU: {cpu_exc}"
                    ) from cpu_exc
            raise WhisperModelError(
                f"Failed to load Whisper model '{self._model_size}': {exc}"
            ) from exc

    def _do_transcribe(
        self,
        model: Any,
        audio_path: str,
        language: str | None,
        start_time: float,
        initial_prompt: str | None = None,
        task: str = "translate",
    ) -> TranscriptionResult:
        logger.info(
            "Starting transcription: file=%s, language=%s, task=%s",
            Path(audio_path).name,
            language or "auto",
            task,
        )

        prompt = (
            initial_prompt
            if initial_prompt is not None
            else (
                "This video discusses JEE Advanced, JEE Main, IIT Delhi, NEET, NCERT, GPT, "
                "Physics, Chemistry, Mathematics, questions, percentile, marks, syllabus, revision, strategy."
            )
        )

        segments_gen, info = model.transcribe(
            audio_path,
            language=language,
            task=task,
            initial_prompt=prompt,
            beam_size=5,
            best_of=5,
            vad_filter=True,
            vad_parameters={
                "threshold": 0.5,
                "min_speech_duration_ms": 250,
                "max_speech_duration_s": 30,
                "min_silence_duration_ms": 100,
            },
        )

        segments_list = list(segments_gen)
        elapsed = time.time() - start_time
        audio_duration = getattr(info, "duration", None)
        detected_lang = getattr(info, "language", None)
        detection_confidence = getattr(info, "language_probability", None)

        parsed_segments = [
            TranscriptionSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text.strip(),
                confidence=seg.avg_logprob if hasattr(seg, "avg_logprob") else None,
            )
            for seg in segments_list
            if seg.text.strip()
        ]

        logger.info(
            "Transcription complete: %d segments, %.1fs audio, %.1fs processing",
            len(parsed_segments),
            audio_duration or 0,
            elapsed,
        )

        return TranscriptionResult(
            segments=parsed_segments,
            language="en" if task == "translate" else (detected_lang or "en"),
            language_confidence=detection_confidence,
            duration_seconds=float(audio_duration) if audio_duration else None,
            processing_time_seconds=elapsed,
        )

    def transcribe(
        self,
        audio_path: str,
        language: str | None = None,
        initial_prompt: str | None = None,
        task: str = "translate",
    ) -> TranscriptionResult:
        """Transcribe audio file using faster-whisper.

        Args:
            audio_path: Path to audio file (WAV, MP3, M4A, etc.).
            language: Optional language code hint (e.g. "en", "es").
            initial_prompt: Optional initial prompt for context/vocabulary priming.
            task: 'translate' (convert speech directly to English) or 'transcribe'.

        Returns:
            ``TranscriptionResult`` with segments and metadata.

        Raises:
            TranscriptionError: If transcription fails.
            WhisperModelError: If model cannot be loaded.
        """
        if not os.path.isfile(audio_path):
            raise TranscriptionError(f"Audio file not found: {audio_path}")

        model = self._load_model()
        start_time = time.time()

        try:
            return self._do_transcribe(
                model,
                audio_path,
                language,
                start_time,
                initial_prompt=initial_prompt,
                task=task,
            )
        except Exception as exc:
            if self._device == "cuda":
                logger.warning(
                    "Transcription failed on CUDA (%s). Retrying on CPU int8...", exc
                )
                try:
                    self._device = "cpu"
                    self._compute_type = "int8"
                    self._model = FasterWhisperModel(
                        self._model_size,
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=self._cpu_threads,
                        num_workers=self._num_workers,
                    )
                    return self._do_transcribe(
                        self._model,
                        audio_path,
                        language,
                        start_time,
                        initial_prompt=initial_prompt,
                        task=task,
                    )
                except Exception as cpu_exc:
                    raise TranscriptionError(f"Whisper transcription failed on CPU fallback: {cpu_exc}") from cpu_exc
            raise TranscriptionError(f"Whisper transcription failed: {exc}") from exc

    def model_name(self) -> str:
        return f"faster-whisper/{self._model_size}"


class DummyWhisperClient(SpeechToTextClient):
    """Dummy implementation for testing — returns canned segments.

    This allows the transcript pipeline to be tested without
    loading the actual Whisper model.
    """

    def __init__(self, canned_segments: list[TranscriptionSegment] | None = None) -> None:
        self._canned = canned_segments or [
            TranscriptionSegment(start=0.0, end=2.0, text="Hello world."),
            TranscriptionSegment(start=2.0, end=4.0, text="This is a test transcript."),
        ]

    def transcribe(  # test double: signature matches SpeechToTextClient
        self,
        audio_path: str,  # noqa: ARG002
        language: str | None = None,
        *args: Any,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> TranscriptionResult:
        total_duration = max((s.end for s in self._canned), default=0)
        return TranscriptionResult(
            segments=self._canned,
            language=language or "en",
            language_confidence=0.95,
            duration_seconds=total_duration,
            processing_time_seconds=0.1,
        )

    def model_name(self) -> str:
        return "dummy"
