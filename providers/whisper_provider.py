"""Stage 3: Whisper speech-to-text fallback provider."""

import logging
import time
from typing import Any

from clients.whisper_client import FasterWhisperClient
from config.settings import get_settings
from exceptions.transcript_errors import (
    AudioDownloadError,
    TranscriptionError,
)
from interfaces.speech_to_text import SpeechToTextClient
from interfaces.transcript_provider import TranscriptProvider
from models.transcript import (
    TranscriptProviderName,
    TranscriptResult,
    TranscriptSegment,
    TranscriptSource,
    WhisperProcessingInfo,
)
from services.audio.audio_service import AudioService
from services.english_converter import english_converter
from services.stt.hardware import detect_hardware
from utils.language_detector import LanguageDetector
from utils.read_time import estimate_read_time
from utils.text_cleaner import TextCleaner

logger = logging.getLogger(__name__)

# Verbatim transcription in the spoken language (never "translate", which outputs English).
WHISPER_TASK = "transcribe"


class WhisperProvider(TranscriptProvider):
    """Stage 3 provider: downloads audio and transcribes with Whisper.

    Used when no YouTube transcript (manual or auto) is available.
    The speech-to-text backend is abstracted via ``SpeechToTextClient``,
    allowing future provider swaps (OpenAI Whisper API, Deepgram, etc.)
    without changing business logic.
    """

    def __init__(
        self,
        stt_client: SpeechToTextClient | None = None,
        text_cleaner: TextCleaner | None = None,
        language_detector: LanguageDetector | None = None,
        temp_dir: str | None = None,
        keep_audio: bool = False,
    ) -> None:
        if stt_client is not None:
            self._stt = stt_client
        else:
            settings = get_settings()
            hw = detect_hardware()
            device = settings.whisper_device if settings.whisper_device != "auto" else hw.device
            compute_type = (
                settings.whisper_compute_type
                if settings.whisper_compute_type != "auto"
                else hw.compute_type
            )
            model_size = settings.whisper_model or "base"
            logger.info(
                "Initializing FasterWhisperClient: model=%s, device=%s, compute=%s",
                model_size,
                device,
                compute_type,
            )
            self._stt = FasterWhisperClient(
                model_size=model_size,
                device=device,
                compute_type=compute_type,
            )

        self._text_cleaner = text_cleaner or TextCleaner()
        self._language_detector = language_detector or LanguageDetector()
        self._temp_dir = temp_dir
        self._keep_audio = keep_audio
        self._audio_service = AudioService(temp_dir=temp_dir)

    def name(self) -> str:
        return f"Whisper ({self._stt.model_name()})"

    def get_transcript(
        self,
        video_id: str,
        language: str | None = None,
        title: str | None = None,
        channel_title: str | None = None,
        **kwargs: Any,  # noqa: ARG002 - TranscriptProvider interface
    ) -> TranscriptResult:
        """Download audio and transcribe with Whisper.

        Pipeline:
            1. Download direct audio stream (.m4a) via AudioService (yt-dlp)
            2. Transcribe with configured STT backend (faster-whisper) with dynamic context prompt
            3. Parse segments and normalize
            4. Safely clean up temporary audio files

        Args:
            video_id: 11-character YouTube video ID.
            language: Optional language hint for transcription.
            title: Optional video title for domain entity biasing.
            channel_title: Optional channel name for series/channel biasing.

        Returns:
            ``TranscriptResult`` with whisper-generated transcript.

        Raises:
            AudioDownloadError: If audio cannot be downloaded.
            TranscriptionError: If transcription fails.
        """
        # If title not provided, attempt fast oEmbed lookup
        if not title:
            try:
                import json
                import urllib.request
                oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
                req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310 (fixed https URL)
                with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 (fixed https URL)
                    oembed_data = json.loads(resp.read().decode("utf-8"))
                    title = oembed_data.get("title", "")
                    channel_title = channel_title or oembed_data.get("author_name", "")
            except Exception as exc:  # the title only biases transcription; continue without it
                logger.debug("oEmbed title lookup failed for %s: %s", video_id, exc)

        audio_path = None
        download_start = time.time()

        try:
            audio_path = self._audio_service.download_audio(video_id)
            download_time = time.time() - download_start
            logger.info(
                "Audio downloaded for %s in %.1fs: %s",
                video_id,
                download_time,
                audio_path,
            )
        except Exception as exc:
            raise AudioDownloadError(f"Failed to download audio for {video_id}: {exc}") from exc

        # Dynamic context prompt for Whisper beam search
        prompt_parts: list[str] = []
        if title:
            prompt_parts.append(title)
        if channel_title:
            prompt_parts.append(channel_title)
        prompt_parts.append(
            "In 2026, DSA vs AI, LeetCode, FAANG, Y Combinator, boilerplate code, vibe coding, "
            "Jira ticket, whiteboard, AI orchestrator, junior dev, 1 Que = IIT Selection, JEE Advanced, "
            "JEE Main, IIT Delhi, NEET, NCERT, Physics Galaxy, Ashish Arora, Advanced Illustrations, "
            "percentile, marks, syllabus, revision, strategy."
        )
        initial_prompt = ". ".join(prompt_parts)

        # Always transcribe verbatim in the spoken language. Whisper's task="translate"
        # outputs English, which would corrupt the canonical "Original Spoken" text;
        # English / Hindi output is produced later by the explicit translation step.
        try:
            stt_result = self._stt.transcribe(
                str(audio_path),
                language=language,
                task=WHISPER_TASK,
                initial_prompt=initial_prompt,
            )
        except TypeError:
            # Fallback for clients without task or initial_prompt parameter
            try:
                stt_result = self._stt.transcribe(str(audio_path), language=language, task=WHISPER_TASK)
            except TypeError:
                stt_result = self._stt.transcribe(str(audio_path), language=language)
        except Exception as exc:
            raise TranscriptionError(f"Whisper transcription failed for {video_id}: {exc}") from exc
        finally:
            if audio_path and not self._keep_audio:
                self._audio_service.cleanup(audio_path)

        segments = [
            TranscriptSegment(
                start=seg.start,
                end=seg.end,
                duration=round(seg.end - seg.start, 2),
                text=seg.text.strip(),
            )
            for seg in stt_result.segments
            if seg.text.strip()
        ]

        segments = self._text_cleaner.clean_segments(segments)

        raw_plain_text = " ".join(seg.text for seg in segments)
        duration = segments[-1].end if segments else 0

        # raw_transcript stays verbatim in the spoken language (the "Original Spoken" source).
        # plain_text is the romanised/cleaned rendering of the same words (transliteration,
        # not translation); real English comes only from the explicit translation step.
        detected = (getattr(stt_result, "language", None) or language or "").lower()
        english_converter.convert_segments(segments, title=title, channel=channel_title)
        plain_text = english_converter.convert(raw_plain_text, title=title, channel=channel_title)
        if detected in ("hi", "ur", "hindi", "urdu") or english_converter.contains_non_roman_script(raw_plain_text):
            final_language = "Hinglish"
        else:
            final_language = detected or "unknown"
        final_confidence = getattr(stt_result, "language_confidence", None) or 0.99

        paragraph_text = self._text_cleaner.build_paragraphs(segments)
        word_count = len(plain_text.split())
        char_count = len(plain_text)

        return TranscriptResult(
            success=True,
            video_id=video_id,
            source=TranscriptSource.WHISPER,
            provider=TranscriptProviderName.FASTER_WHISPER,
            method="speech_to_text",
            language=final_language,
            language_confidence=final_confidence,
            segments=segments,
            plain_text=plain_text,
            raw_transcript=raw_plain_text,
            paragraph_text=paragraph_text,
            word_count=word_count,
            character_count=char_count,
            estimated_read_time=estimate_read_time(word_count),
            duration_seconds=duration,
            whisper_info=WhisperProcessingInfo(
                model_name=self._stt.model_name(),
                transcription_duration_seconds=stt_result.processing_time_seconds,
                audio_duration_seconds=stt_result.duration_seconds,
                processing_time_seconds=(
                    stt_result.processing_time_seconds or 0
                ) + (time.time() - download_start),
                language_detected=stt_result.language,
                language_confidence=stt_result.language_confidence,
                word_timestamps=True,
                audio_download_time_seconds=time.time() - download_start,
                task=WHISPER_TASK,
            ),
            error=None,
        )
