from __future__ import annotations

import pytest


class TestTranscriptValidator:
    def test_valid_transcript(self):
        from validators import transcript_validator as tv
        data = {"plain_text": "Python is a programming language. It is used widely.", "segments": [{"text": "Python is a programming language.", "start": 0.0, "duration": 5.0}]}
        tv.validate_segments(data["segments"])
        tv.validate_text(data["plain_text"])

    def test_empty_transcript(self):
        from exceptions.processing_errors import EmptyTranscriptError
        from validators import transcript_validator as tv
        data = {"plain_text": "", "segments": []}
        with pytest.raises(EmptyTranscriptError):
            tv.validate_segments(data["segments"])

    def test_transcript_too_short(self):
        from validators import transcript_validator as tv
        data = {"plain_text": "Hi", "segments": [{"text": "Hi", "start": 0.0}]}
        tv.validate_text(data["plain_text"])
        tv.validate_segments(data["segments"])

    def test_missing_segments(self):
        from exceptions.processing_errors import EmptyTranscriptError
        from validators import transcript_validator as tv
        data = {"plain_text": "Some text"}
        with pytest.raises(EmptyTranscriptError):
            tv.validate_segments(data.get("segments", []))

    def test_quality_score(self):
        from validators import transcript_validator as tv
        data = {"plain_text": "Python is a programming language used for data science and machine learning applications." * 3, "segments": [{"text": "Test", "start": 0.0} for _ in range(10)]}
        tv.validate_segments(data["segments"])
        tv.validate_text(data["plain_text"])

    def test_quality_score_empty(self):
        from exceptions.processing_errors import EmptyTranscriptError
        from validators import transcript_validator as tv
        data = {"plain_text": "", "segments": []}
        with pytest.raises(EmptyTranscriptError):
            tv.validate_segments(data["segments"])
