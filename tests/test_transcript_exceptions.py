"""Unit tests for transcript exception hierarchy."""

from exceptions.transcript_errors import (
    AudioDownloadError,
    AudioExtractionError,
    InvalidVideoIdError,
    TranscriptCacheError,
    TranscriptCleanupError,
    TranscriptDisabledError,
    TranscriptError,
    TranscriptFetchError,
    TranscriptionError,
    TranscriptLanguageNotSupportedError,
    TranscriptQuotaExceededError,
    TranscriptTimeoutError,
    TranscriptUnavailableError,
    WhisperModelError,
)


class TestTranscriptExceptions:
    def test_base_exception(self):
        assert issubclass(TranscriptUnavailableError, TranscriptError)
        assert issubclass(TranscriptDisabledError, TranscriptError)
        assert issubclass(TranscriptFetchError, TranscriptError)
        assert issubclass(AudioDownloadError, TranscriptError)
        assert issubclass(AudioExtractionError, TranscriptError)
        assert issubclass(TranscriptionError, TranscriptError)
        assert issubclass(WhisperModelError, TranscriptError)
        assert issubclass(TranscriptQuotaExceededError, TranscriptError)
        assert issubclass(TranscriptTimeoutError, TranscriptError)
        assert issubclass(TranscriptCleanupError, TranscriptError)
        assert issubclass(InvalidVideoIdError, TranscriptError)
        assert issubclass(TranscriptCacheError, TranscriptError)
        assert issubclass(TranscriptLanguageNotSupportedError, TranscriptError)

    def test_base_is_exception(self):
        assert issubclass(TranscriptError, Exception)

    def test_exception_message(self):
        exc = TranscriptUnavailableError("No transcript available")
        assert str(exc) == "No transcript available"

    def test_transcript_disabled(self):
        exc = TranscriptDisabledError("Transcripts disabled for this video")
        assert "disabled" in str(exc).lower()
