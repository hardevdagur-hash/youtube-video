"""Split audio that exceeds the speech-to-text upload limit into time-ordered chunks.

Groq's transcription endpoint accepts at most 25 MB per request, which a typical
YouTube audio stream reaches after roughly 45 minutes. Longer audio is re-encoded by
ffmpeg into mono 16 kHz Opus segments (about 2.4 MB per 10 minutes, the sample rate
Whisper uses internally) and each segment is transcribed separately. Segment start
times come from ffmpeg's own segment list, so merged timestamps stay accurate.
"""

from __future__ import annotations

import csv
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Opus at 32 kbps mono is transparent for speech and keeps chunks far below the limit.
_CHUNK_CODEC_ARGS = ("-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k")
_FFMPEG_TIMEOUT_SECONDS = 900
_STDERR_LOG_CHARS = 2000


class AudioChunkingError(Exception):
    """Raised when audio cannot be split (ffmpeg missing, failed or produced nothing)."""


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start: float  # seconds from the beginning of the original audio
    end: float


def ffmpeg_path() -> str | None:
    """Absolute path of the ffmpeg binary, or None when it is not installed."""
    return shutil.which("ffmpeg")


def split_audio(audio_path: Path, out_dir: Path, chunk_seconds: int) -> list[AudioChunk]:
    """Split ``audio_path`` into ``chunk_seconds`` Opus segments inside ``out_dir``.

    Returns the chunks in playback order. Raises ``AudioChunkingError`` on any failure;
    the caller owns ``out_dir`` and its cleanup.
    """
    if chunk_seconds < 1:
        raise ValueError("chunk_seconds must be positive")
    binary = ffmpeg_path()
    if binary is None:
        raise AudioChunkingError("ffmpeg is required to transcribe audio longer than the upload limit")

    out_dir.mkdir(parents=True, exist_ok=True)
    segment_list = out_dir / "segments.csv"
    command = [
        binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(audio_path),
        *_CHUNK_CODEC_ARGS,
        "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
        "-segment_list", str(segment_list), "-segment_list_type", "csv",
        str(out_dir / "chunk_%05d.ogg"),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 (fixed argv, no shell; paths are our own temp files)
            command, capture_output=True, timeout=_FFMPEG_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioChunkingError(f"ffmpeg did not finish within {_FFMPEG_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise AudioChunkingError(f"ffmpeg could not be started: {exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace")[-_STDERR_LOG_CHARS:]
        logger.error("ffmpeg failed to split %s (exit %d): %s", audio_path.name, completed.returncode, stderr)
        raise AudioChunkingError(f"ffmpeg exited with status {completed.returncode}")

    chunks = _read_segment_list(segment_list, out_dir)
    if not chunks:
        raise AudioChunkingError("ffmpeg produced no audio chunks")
    logger.info(
        "Split %s into %d chunk(s) of up to %ds (%.0fs of audio)",
        audio_path.name, len(chunks), chunk_seconds, chunks[-1].end,
    )
    return chunks


def _read_segment_list(segment_list: Path, out_dir: Path) -> list[AudioChunk]:
    """Parse ffmpeg's ``filename,start,end`` segment list, keeping only non-empty files."""
    if not segment_list.is_file():
        return []
    chunks: list[AudioChunk] = []
    with segment_list.open(newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            if len(row) < 3:
                continue
            name = Path(row[0]).name  # ffmpeg may write a path; only names inside out_dir are trusted
            path = out_dir / name
            try:
                start, end = float(row[1]), float(row[2])
            except ValueError:
                continue
            if path.is_file() and path.stat().st_size > 0 and end > start:
                chunks.append(AudioChunk(path=path, start=start, end=end))
    chunks.sort(key=lambda c: c.start)
    return chunks
