from abc import ABC, abstractmethod

from models.transcript import TranscriptResult


class TranscriptProvider(ABC):
    """Abstract interface for transcript providers.

    All transcript sources (manual, auto, whisper, etc.) must implement
    this interface. This enables the Strategy pattern so new providers
    can be added without modifying existing pipeline orchestration.
    """

    @abstractmethod
    def get_transcript(self, video_id: str, language: str | None = None) -> TranscriptResult:
        """Retrieve or generate a transcript for the given video.

        Args:
            video_id: 11-character YouTube video ID.
            language: Optional ISO language code (e.g. "en", "es").
                      If None, the provider should auto-detect.

        Returns:
            ``TranscriptResult`` with segments and metadata.

        Raises:
            TranscriptUnavailableError: No transcript available.
            TranscriptFetchError: Network or API failure.
            TranscriptionError: Speech-to-text failure.
        """
        pass

    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name for logging and UI."""
        pass
