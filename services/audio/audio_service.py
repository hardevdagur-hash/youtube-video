"""Audio download for the channel/export speech-to-text pipeline (``WhisperProvider``).

Delegates to ``YouTubeAudioExtractor`` so both pipelines share one download policy:
unique temp file names (no clashes between concurrent jobs), the duration cap checked
before download, and the persistent ``DATA_DIR/tmp/audio`` directory.
"""

import logging
from pathlib import Path

from exceptions.transcript_errors import AudioDownloadError
from services.youtube.audio import AudioExtractionError, YouTubeAudioExtractor

logger = logging.getLogger(__name__)


class AudioService:
    """Service to download YouTube audio streams and manage temporary audio files."""

    def __init__(self, temp_dir: Path | str | None = None) -> None:
        self._extractor = YouTubeAudioExtractor(temp_dir=temp_dir)
        self.temp_dir = self._extractor.temp_dir

    def download_audio(self, video_id: str) -> Path:
        """Download the audio stream of ``video_id`` to a unique temporary file.

        Raises:
            AudioDownloadError: If the download fails or the video is over the length cap
                (``error_code`` carries the extractor's code, e.g. AUDIO_TOO_LONG).
        """
        try:
            return self._extractor.extract_audio(video_id)
        except AudioExtractionError as exc:
            error = AudioDownloadError(exc.message)
            error.error_code = exc.error_code
            raise error from exc

    def cleanup(self, audio_path: Path | str | None) -> None:
        """Safely delete a temporary audio file."""
        self._extractor.cleanup(audio_path)
