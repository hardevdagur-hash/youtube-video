"""YouTube audio extraction service using yt-dlp."""

import contextlib
import logging
import tempfile
import uuid
from collections.abc import Generator
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)


class AudioExtractionError(Exception):
    """Raised when audio download/extraction fails."""

    def __init__(self, message: str, error_code: str = "AUDIO_EXTRACTION_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class YouTubeAudioExtractor:
    """Extracts lightweight audio stream from YouTube videos for STT processing."""

    def __init__(self, temp_dir: Path | str | None = None) -> None:
        if temp_dir:
            self.temp_dir = Path(temp_dir)
        else:
            self.temp_dir = Path(tempfile.gettempdir()) / "youtube_audio_stt"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def extract_audio(self, video_id: str) -> Path:
        """Download YouTube audio stream directly as an m4a/opus/aac file.

        Args:
            video_id: 11-character YouTube video ID.

        Returns:
            Path to downloaded audio file.

        Raises:
            AudioExtractionError: If extraction fails or file is not found.
        """
        unique_token = uuid.uuid4().hex[:8]
        file_prefix = f"{video_id}_{unique_token}"
        output_template = str(self.temp_dir / f"{file_prefix}.%(ext)s")

        # Select lightweight audio formats (m4a preferred for Groq Whisper compatibility)
        ydl_opts = {
            # Speech needs little bandwidth: ~50-70 kbps keeps a 30-minute video near 16 MB,
            # well under Groq's 25 MB upload limit (webm/opus and m4a are both accepted).
            "format": "ba[abr<=72]/wa/ba",
            "max_filesize": 25 * 1024 * 1024,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
        }

        url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info("Extracting audio stream for %s (token=%s) via yt-dlp", video_id, unique_token)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                ext = info.get("ext", "m4a")

            target_path = self.temp_dir / f"{file_prefix}.{ext}"
            if target_path.is_file() and target_path.stat().st_size > 0:
                logger.info(
                    "Audio extracted successfully: %s (%.2f MB)",
                    target_path.name,
                    target_path.stat().st_size / (1024 * 1024),
                )
                return target_path

            # Fallback search for any matching file with that prefix
            for candidate in self.temp_dir.glob(f"{file_prefix}.*"):
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate

            raise AudioExtractionError(
                f"Audio file for {video_id} was not produced.",
                error_code="AUDIO_EXTRACTION_FAILED",
            )
        except AudioExtractionError:
            raise
        except Exception as exc:
            logger.error("Audio extraction failed for %s: %s", video_id, exc)
            raise AudioExtractionError(
                f"Audio extraction failed for {video_id}: {exc}",
                error_code="AUDIO_EXTRACTION_FAILED",
            ) from exc

    def cleanup(self, audio_path: Path | str | None) -> None:
        """Safely delete temporary audio file."""
        if not audio_path:
            return
        try:
            p = Path(audio_path)
            if p.is_file():
                p.unlink(missing_ok=True)
                logger.debug("Cleaned up temporary audio file: %s", p)
        except Exception as exc:
            logger.warning("Failed to remove audio file %s: %s", audio_path, exc)

    @contextlib.contextmanager
    def audio_context(self, video_id: str) -> Generator[Path, None, None]:
        """Context manager that downloads audio and guarantees cleanup on exit."""
        audio_path: Path | None = None
        try:
            audio_path = self.extract_audio(video_id)
            yield audio_path
        finally:
            if audio_path:
                self.cleanup(audio_path)
