"""YouTube audio extraction service using yt-dlp."""

import contextlib
import logging
import uuid
from collections.abc import Generator
from pathlib import Path

import yt_dlp

from config.settings import get_settings
from services.transcript_failures import looks_bot_blocked

logger = logging.getLogger(__name__)

SOCKET_TIMEOUT_SECONDS = 30


class AudioExtractionError(Exception):
    """Raised when audio download/extraction fails."""

    def __init__(self, message: str, error_code: str = "AUDIO_EXTRACTION_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


# Disk guard for one download. Audio over Groq's upload limit is chunked later, so the
# real bound on length is the duration check (STT_MAX_AUDIO_SECONDS) before download.
MAX_AUDIO_FILE_BYTES = 256 * 1024 * 1024


class YouTubeAudioExtractor:
    """Extracts lightweight audio stream from YouTube videos for STT processing."""

    def __init__(self, temp_dir: Path | str | None = None, max_duration_seconds: int | None = None) -> None:
        settings = get_settings()
        self.temp_dir = Path(temp_dir) if temp_dir else settings.audio_temp_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.max_duration_seconds = (
            max_duration_seconds if max_duration_seconds is not None else settings.stt_max_audio_seconds
        )
        self._proxy_url = settings.youtube_proxy_url
        self._cookies_file = settings.ytdlp_cookies_file

    def extract_audio(self, video_id: str) -> Path:
        """Download YouTube audio stream directly as an m4a/opus/aac file.

        Args:
            video_id: 11-character YouTube video ID.

        Returns:
            Path to downloaded audio file.

        Raises:
            AudioExtractionError: If extraction fails or file is not found; error_code
                AUDIO_TOO_LONG when the video exceeds ``max_duration_seconds``.
        """
        unique_token = uuid.uuid4().hex[:8]
        file_prefix = f"{video_id}_{unique_token}"
        output_template = str(self.temp_dir / f"{file_prefix}.%(ext)s")

        ydl_opts = {
            # Speech needs little bandwidth: ~50-70 kbps keeps a 45-minute video under Groq's
            # 25 MB upload limit; longer audio is split into chunks before upload.
            "format": "ba[abr<=72]/wa/ba",
            "max_filesize": MAX_AUDIO_FILE_BYTES,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
            # A stalled connection must not hold a worker (and a speech-to-text slot) forever.
            "socket_timeout": SOCKET_TIMEOUT_SECONDS,
            "retries": 2,
            "fragment_retries": 2,
        }
        ydl_opts.update(self._network_options())

        url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info("Extracting audio stream for %s (token=%s) via yt-dlp", video_id, unique_token)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Metadata first: refuse over-long or live videos before downloading anything.
                info = ydl.extract_info(url, download=False)
                self._check_downloadable(video_id, info)
                info = ydl.process_ie_result(info, download=True)
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
            if looks_bot_blocked(str(exc)):
                # Typical on datacenter IPs ("Sign in to confirm you're not a bot"): a property
                # of this server's IP, not of the video. Never reported as "no captions".
                logger.error("YouTube blocked the audio download for %s (bot check): %s", video_id, exc)
                raise AudioExtractionError(
                    f"YouTube blocked the audio download for {video_id} (bot check): {exc}",
                    error_code="BOT_BLOCKED",
                ) from exc
            logger.error("Audio extraction failed for %s: %s", video_id, exc)
            raise AudioExtractionError(
                f"Audio extraction failed for {video_id}: {exc}",
                error_code="AUDIO_EXTRACTION_FAILED",
            ) from exc

    def _network_options(self) -> dict:
        """Optional proxy / cookies (YOUTUBE_PROXY_URL, YTDLP_COOKIES_FILE); values never logged."""
        options: dict = {}
        if self._proxy_url:
            options["proxy"] = self._proxy_url
        if self._cookies_file is not None:
            options["cookiefile"] = str(self._cookies_file)
        return options

    def _check_downloadable(self, video_id: str, info: dict) -> None:
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise AudioExtractionError(
                f"{video_id} is a live or upcoming stream; its audio cannot be transcribed yet.",
                error_code="AUDIO_EXTRACTION_FAILED",
            )
        duration = info.get("duration")
        if isinstance(duration, int | float) and duration > self.max_duration_seconds:
            raise AudioExtractionError(
                f"{video_id} is {duration:.0f}s long; the speech-to-text limit is {self.max_duration_seconds}s.",
                error_code="AUDIO_TOO_LONG",
            )

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
