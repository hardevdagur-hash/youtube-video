"""Audio extraction service using yt-dlp.

Downloads YouTube audio directly as .m4a without requiring an external ffmpeg binary.
Ensures temporary audio files are safely cleaned up after transcription.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path

import yt_dlp

from exceptions.transcript_errors import AudioDownloadError

logger = logging.getLogger(__name__)


class AudioService:
    """Service to download YouTube audio streams and manage temporary audio files."""

    def __init__(self, temp_dir: Path | str | None = None) -> None:
        if temp_dir:
            self.temp_dir = Path(temp_dir)
        else:
            self.temp_dir = Path(tempfile.gettempdir()) / "youtube_audio"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def download_audio(self, video_id: str) -> Path:
        """Download YouTube audio stream directly as an m4a/opus/aac file.

        Args:
            video_id: 11-character YouTube video ID.

        Returns:
            Path to downloaded audio file.

        Raises:
            AudioDownloadError: If download fails or file is not found.
        """
        output_template = str(self.temp_dir / f"{video_id}.%(ext)s")

        ydl_opts = {
            # Speech needs little bandwidth: ~50-70 kbps keeps a 30-minute video near 16 MB,
            # well under Groq's 25 MB upload limit (webm/opus and m4a are both accepted).
            "format": "ba[abr<=72]/wa/ba",
            "max_filesize": 25 * 1024 * 1024,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }

        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            logger.info("Downloading audio for video %s", video_id)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                ext = info.get("ext", "m4a")

            target_path = self.temp_dir / f"{video_id}.{ext}"
            if target_path.is_file() and target_path.stat().st_size > 0:
                logger.info("Audio downloaded successfully: %s (%d bytes)", target_path.name, target_path.stat().st_size)
                return target_path

            # Fallback search for any matching file
            for candidate in self.temp_dir.glob(f"{video_id}.*"):
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate

            raise AudioDownloadError(f"Downloaded audio file for {video_id} not found on disk.")
        except Exception as exc:
            logger.error("Failed to download audio for %s: %s", video_id, exc)
            raise AudioDownloadError(f"Audio download failed for video {video_id}: {exc}") from exc

    def cleanup(self, audio_path: Path | str | None) -> None:
        """Safely delete temporary audio file."""
        if not audio_path:
            return
        try:
            p = Path(audio_path)
            if p.is_file():
                p.unlink()
                logger.debug("Deleted temporary audio file: %s", p)
        except Exception as exc:
            logger.warning("Failed to delete temporary audio file %s: %s", audio_path, exc)
