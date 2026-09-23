"""Abstract base class and models for speech-to-text providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class TranscriptionSegment:
    start: float
    end: float
    text: str
    duration: float = 0.0

    def __post_init__(self):
        if self.duration == 0.0 and self.end > self.start:
            self.duration = round(self.end - self.start, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.duration, 2),
            "text": self.text.strip(),
        }


@dataclass
class TranscriptionResult:
    text: str
    segments: List[TranscriptionSegment] = field(default_factory=list)
    language: str = "en"
    duration: float = 0.0
    confidence: float = 1.0
    provider: str = "groq_whisper_large_v3"


class TranscriptionProvider(ABC):
    """Abstract interface for speech-to-text providers."""

    @abstractmethod
    def transcribe(
        self, audio_path: Path, language: Optional[str] = None
    ) -> TranscriptionResult:
        """Transcribe an audio file into text and timestamps.

        Args:
            audio_path: Path to the audio file on disk.
            language: Optional language hint (e.g. 'hi', 'en').

        Returns:
            TranscriptionResult with text, segments, language, duration.
        """
        pass
