"""Tests for Phase 5: models, exceptions, validators, processors, pipeline, service, utils."""


import pytest

from exceptions.processing_errors import (
    EmptyTranscriptError,
    ProcessingError,
    ProcessingLimitError,
)
from exceptions.processing_errors import (
    ValidationError as ProcessingValidationError,
)
from models.processing_result import (
    LanguageDistribution,
    ProcessedTimestamp,
    ProcessingFlags,
    ProcessingResult,
    ProcessingStatistics,
    ProcessingStatus,
    ProcessingStep,
    ProcessingStepName,
)
from pipeline.capitalization_processor import CapitalizationProcessor
from pipeline.caption_merger import CaptionMerger
from pipeline.filler_processor import FillerProcessor
from pipeline.language_processor import LanguageProcessor
from pipeline.paragraph_processor import ParagraphProcessor
from pipeline.processing_pipeline import ProcessingPipeline
from pipeline.punctuation_processor import PunctuationProcessor
from pipeline.quality_checker import QualityChecker
from pipeline.timestamp_processor import TimestampProcessor
from services.transcript_processor import TranscriptProcessor
from utils.metrics import compute_statistics
from utils.text_utils import (
    count_sentences,
    count_words,
    is_blank,
    is_mixed_script,
    normalize_whitespace,
    remove_empty_lines,
    remove_repeated_lines,
    remove_repeated_words,
    split_sentences,
)
from utils.unicode_utils import (
    is_valid_unicode,
    normalize_unicode,
    safe_decode,
)
from validators.transcript_validator import (
    validate_segments,
    validate_text,
    validate_video_id,
)

# =========================================================================
# Models
# =========================================================================

class TestProcessingStep:
    def test_create_step(self):
        step = ProcessingStep(name=ProcessingStepName.MERGE_CAPTIONS, status=ProcessingStatus.RUNNING)
        assert step.name == ProcessingStepName.MERGE_CAPTIONS
        assert step.status == ProcessingStatus.RUNNING
        assert step.detail == ""

    def test_step_with_duration(self):
        step = ProcessingStep(name=ProcessingStepName.FIX_PUNCTUATION, status=ProcessingStatus.OK, duration_ms=42.5)
        assert step.duration_ms == 42.5

    def test_step_serialization(self):
        step = ProcessingStep(name=ProcessingStepName.VALIDATE_INPUT, status=ProcessingStatus.OK, detail="10 words")
        d = step.model_dump()
        assert d["name"] == "validate_input"
        assert d["status"] == "ok"
        assert d["detail"] == "10 words"


class TestProcessingStatistics:
    def test_defaults(self):
        s = ProcessingStatistics()
        assert s.word_count == 0
        assert s.estimated_read_time == ""

    def test_all_fields(self):
        s = ProcessingStatistics(
            word_count=1000, character_count=5000, paragraph_count=20,
            sentence_count=50, estimated_read_time="5 min",
            avg_sentence_length_words=20.0, avg_paragraph_length_words=50.0,
            longest_sentence_words=35, filler_word_count=5,
        )
        assert s.word_count == 1000
        assert s.longest_sentence_words == 35


class TestLanguageDistribution:
    def test_english(self):
        lang = LanguageDistribution(primary="en", primary_confidence=0.95)
        assert lang.primary == "en"
        assert lang.secondary is None

    def test_mixed(self):
        lang = LanguageDistribution(
            primary="hi", secondary="en",
            primary_confidence=0.8, secondary_confidence=0.15,
            mixed_ratio=0.1875,
        )
        assert lang.primary == "hi"
        assert lang.secondary == "en"
        assert lang.mixed_ratio == 0.1875


class TestProcessingFlags:
    def test_defaults(self):
        f = ProcessingFlags()
        assert f.timestamps_removed is False
        assert f.quality_passed is False

    def test_all_true(self):
        f = ProcessingFlags(
            timestamps_removed=True, captions_merged=True,
            punctuation_restored=True, capitalization_fixed=True,
            duplicates_removed=True, language_detected=True,
            fillers_removed=True, quality_passed=True,
        )
        assert all([f.timestamps_removed, f.captions_merged, f.quality_passed])


class TestProcessedTimestamp:
    def test_create(self):
        ts = ProcessedTimestamp(
            original_start=0.0, original_end=5.0,
            original_text="Hello world", cleaned_text="Hello world",
        )
        assert ts.original_start == 0.0
        assert ts.original_end == 5.0


class TestProcessingResult:
    def test_defaults(self):
        r = ProcessingResult(video_id="dQw4w9WgXcQ")
        assert r.success is True
        assert r.clean_transcript == ""
        assert r.statistics.word_count == 0
        assert r.processing_time_ms == 0.0

    def test_full_result(self):
        r = ProcessingResult(
            success=True,
            video_id="test123",
            language=LanguageDistribution(primary="en", primary_confidence=0.95),
            statistics=ProcessingStatistics(word_count=100),
            clean_transcript="Hello world.",
            paragraphs=["Hello world."],
            sentences=["Hello world."],
            processing_time_ms=500.0,
        )
        assert r.language is not None
        assert r.language.primary == "en"
        assert len(r.paragraphs) == 1
        assert r.processing_time_ms == 500.0

    def test_error_result(self):
        r = ProcessingResult(success=False, video_id="bad", error="Failed to process")
        assert r.success is False
        assert r.error == "Failed to process"


# =========================================================================
# Exceptions
# =========================================================================

class TestProcessingExceptions:
    def test_base_exception(self):
        assert issubclass(ProcessingError, Exception)

    def test_empty_transcript(self):
        exc = EmptyTranscriptError("No segments")
        assert str(exc) == "No segments"

    def test_limit_error(self):
        exc = ProcessingLimitError("Too many words")
        assert "Too many words" in str(exc)


# =========================================================================
# Validators
# =========================================================================

class TestTranscriptValidator:
    def test_validate_segments_valid(self):
        segments = [{"text": "Hello", "start": 0.0, "duration": 1.0}]
        validate_segments(segments)  # should not raise

    def test_validate_segments_empty(self):
        with pytest.raises(EmptyTranscriptError):
            validate_segments([])

    def test_validate_text_valid(self):
        validate_text("Hello world")  # should not raise

    def test_validate_text_empty(self):
        with pytest.raises(EmptyTranscriptError):
            validate_text("")

    def test_validate_video_id_valid(self):
        validate_video_id("dQw4w9WgXcQ")

    def test_validate_video_id_short(self):
        with pytest.raises(ProcessingValidationError):
            validate_video_id("abc")

    def test_validate_video_id_empty(self):
        with pytest.raises(ProcessingValidationError):
            validate_video_id("")


# =========================================================================
# Processors
# =========================================================================

class TestTimestampProcessor:
    def test_removes_timestamps(self):
        proc = TimestampProcessor()
        ctx = {"text": "00:00:01 Hello world\n00:00:05 How are you?"}
        result = proc.process(ctx)
        assert "00:00:01" not in result["text"]
        assert "Hello world" in result["text"]

    def test_removes_chapter_markers(self):
        proc = TimestampProcessor()
        ctx = {"text": "0:00 - Introduction\nHello world"}
        result = proc.process(ctx)
        assert "Introduction" not in result["text"]
        assert "Hello world" in result["text"]

    def test_no_timestamps(self):
        proc = TimestampProcessor()
        ctx = {"text": "Hello world. How are you?"}
        result = proc.process(ctx)
        assert result["text"] == "Hello world. How are you?"

    def test_empty_text(self):
        proc = TimestampProcessor()
        ctx = {"text": ""}
        result = proc.process(ctx)
        assert result["text"] == ""


class TestCaptionMerger:
    def test_merges_fragments(self):
        proc = CaptionMerger()
        segments = [
            {"text": "Today we will", "start": 0.0, "duration": 1.0},
            {"text": "talk about machine", "start": 1.0, "duration": 1.0},
            {"text": "learning.", "start": 2.0, "duration": 1.0},
        ]
        ctx = {"segments": segments}
        result = proc.process(ctx)
        assert "Today we will talk about machine learning." in result["text"]

    def test_preserves_sentence_starts(self):
        proc = CaptionMerger()
        segments = [
            {"text": "First sentence.", "start": 0.0, "duration": 1.0},
            {"text": "Second sentence.", "start": 1.0, "duration": 1.0},
        ]
        ctx = {"segments": segments}
        result = proc.process(ctx)
        assert "First sentence. Second sentence." in result["text"]

    def test_empty_segments(self):
        proc = CaptionMerger()
        ctx = {"segments": []}
        result = proc.process(ctx)
        assert result["text"] == ""


class TestPunctuationProcessor:
    def test_fixes_spacing_before_punctuation(self):
        proc = PunctuationProcessor()
        ctx = {"text": "Hello , world . How are you ?"}
        result = proc.process(ctx)
        assert "Hello, world. How are you?" in result["text"]

    def test_fixes_missing_space(self):
        proc = PunctuationProcessor()
        ctx = {"text": "Hello world.Next sentence."}
        result = proc.process(ctx)
        assert "Hello world. Next sentence." in result["text"]

    def test_normalizes_ellipsis(self):
        proc = PunctuationProcessor()
        ctx = {"text": "Hello..... world"}
        result = proc.process(ctx)
        assert "..." in result["text"]
        assert "....." not in result["text"]

    def test_empty_text(self):
        proc = PunctuationProcessor()
        ctx = {"text": ""}
        result = proc.process(ctx)
        assert result["text"] == ""


class TestCapitalizationProcessor:
    def test_fixes_sentence_start(self):
        proc = CapitalizationProcessor()
        ctx = {"text": "hello world. how are you?"}
        result = proc.process(ctx)
        assert result["text"][0].isupper()

    def test_preserves_proper_nouns(self):
        proc = CapitalizationProcessor()
        ctx = {"text": "i love youtube and google."}
        result = proc.process(ctx)
        assert "YouTube" in result["text"]
        assert "Google" in result["text"]

    def test_preserves_abbreviations(self):
        proc = CapitalizationProcessor()
        ctx = {"text": "Dr. Smith said hello."}
        result = proc.process(ctx)
        assert result["text"] == "Dr. Smith said hello."


class TestParagraphProcessor:
    def test_splits_by_topic_shift(self):
        proc = ParagraphProcessor()
        text = "First paragraph about something. More of the first paragraph. Now let's talk about something else. Second paragraph content. More second paragraph."
        ctx = {"text": text}
        result = proc.process(ctx)
        assert len(result["paragraphs"]) >= 2

    def test_single_paragraph(self):
        proc = ParagraphProcessor()
        ctx = {"text": "Just one short paragraph here."}
        result = proc.process(ctx)
        assert len(result["paragraphs"]) == 1

    def test_preserves_content(self):
        proc = ParagraphProcessor()
        ctx = {"text": "Hello world. How are you? I am fine."}
        result = proc.process(ctx)
        assert "Hello world" in result["text"]
        assert "How are you" in result["text"]

    def test_empty_text(self):
        proc = ParagraphProcessor()
        ctx = {"text": ""}
        result = proc.process(ctx)
        assert result["paragraphs"] == []


class TestFillerProcessor:
    def test_counts_fillers(self):
        proc = FillerProcessor()
        ctx = {"text": "Um, like, I think this is basically correct."}
        result = proc.process(ctx)
        assert result["filler_word_count"] >= 3

    def test_no_fillers(self):
        proc = FillerProcessor()
        ctx = {"text": "This is a clean sentence."}
        result = proc.process(ctx)
        assert result["filler_word_count"] == 0

    def test_removes_fillers(self):
        proc = FillerProcessor(remove_fillers=True)
        ctx = {"text": "Um, like, I think this is basically correct."}
        result = proc.process(ctx)
        assert "Um" not in result["text"]
        assert "like" not in result["text"]

    def test_empty_text(self):
        proc = FillerProcessor()
        ctx = {"text": ""}
        result = proc.process(ctx)
        assert result["filler_word_count"] == 0


class TestLanguageProcessor:
    def test_detects_english(self):
        proc = LanguageProcessor()
        ctx = {"text": "This is a test of the language detection system in English."}
        result = proc.process(ctx)
        assert result["language"] is not None
        assert result["language"]["primary"] == "en"

    def test_detects_hindi(self):
        proc = LanguageProcessor()
        ctx = {"text": "यह हिंदी भाषा का परीक्षण है। यह एक वाक्य है।"}
        result = proc.process(ctx)
        assert result["language"] is not None
        assert result["language"]["primary"] == "hi"

    def test_empty_text(self):
        proc = LanguageProcessor()
        ctx = {"text": ""}
        result = proc.process(ctx)
        assert result.get("language") is None


class TestQualityChecker:
    def test_passes_clean_text(self):
        proc = QualityChecker()
        ctx = {"text": "This is a clean transcript. No issues here.", "flags": None}
        result = proc.process(ctx)
        assert result["flags"].quality_passed is True

    def test_repairs_repeated_words(self):
        proc = QualityChecker()
        ctx = {"text": "This this is a test test.", "flags": None}
        result = proc.process(ctx)
        assert "this this" not in result["text"].lower()


# =========================================================================
# Pipeline
# =========================================================================

class TestProcessingPipeline:
    def test_full_pipeline(self):
        pipeline = ProcessingPipeline()
        segments = [
            {"text": "Today we will", "start": 0.0, "duration": 1.0},
            {"text": "talk about machine", "start": 1.0, "duration": 1.0},
            {"text": "learning.", "start": 2.0, "duration": 1.0},
            {"text": "It is a powerful", "start": 3.0, "duration": 1.0},
            {"text": "technology.", "start": 4.0, "duration": 1.0},
            {"text": "Now let's discuss", "start": 5.0, "duration": 1.0},
            {"text": "deep learning.", "start": 6.0, "duration": 1.0},
        ]
        result = pipeline.run(segments, video_id="test123")
        assert "text" in result
        assert len(result["text"]) > 0
        assert "statistics" in result
        assert result["statistics"]["word_count"] > 0
        assert len(result["steps"]) > 0

    def test_pipeline_records_steps(self):
        pipeline = ProcessingPipeline()
        segments = [{"text": "Hello world.", "start": 0.0, "duration": 1.0}]
        result = pipeline.run(segments)
        assert len(result["steps"]) >= 8  # pre + processors + post

    def test_empty_segments(self):
        pipeline = ProcessingPipeline()
        result = pipeline.run([])
        assert result["text"] == ""


# =========================================================================
# Service
# =========================================================================

class TestTranscriptProcessor:
    def test_process_segments(self):
        processor = TranscriptProcessor()
        segments = [
            {"text": "Hello and welcome", "start": 0.0, "duration": 1.0},
            {"text": "to this video.", "start": 1.0, "duration": 1.0},
            {"text": "Today we will", "start": 2.0, "duration": 1.0},
            {"text": "talk about AI.", "start": 3.0, "duration": 1.0},
        ]
        result = processor.process(segments, video_id="dQw4w9WgXcQ")
        assert result.success is True
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.statistics.word_count > 0
        assert len(result.paragraphs) > 0
        assert result.processing_time_ms >= 0

    def test_process_invalid_video_id(self):
        processor = TranscriptProcessor()
        segments = [{"text": "Hello", "start": 0.0, "duration": 1.0}]
        result = processor.process(segments, video_id="bad")
        assert result.success is False
        assert result.error is not None

    def test_process_empty_segments(self):
        processor = TranscriptProcessor()
        result = processor.process([], video_id="dQw4w9WgXcQ")
        assert result.success is False

    def test_process_with_filler_removal(self):
        processor = TranscriptProcessor()
        segments = [{"text": "Um like hello world.", "start": 0.0, "duration": 1.0}]
        result = processor.process(segments, video_id="dQw4w9WgXcQ", remove_fillers=False)
        assert result.success is True
        assert result.statistics.filler_word_count >= 2

    def test_returns_paragraphs(self):
        processor = TranscriptProcessor()
        segments = [{"text": f"Sentence {i}.", "start": float(i), "duration": 1.0} for i in range(5)]
        result = processor.process(segments, video_id="dQw4w9WgXcQ")
        assert len(result.paragraphs) >= 1


# =========================================================================
# Utils — text_utils
# =========================================================================

class TestTextUtils:
    def test_is_blank_empty(self):
        assert is_blank("") is True

    def test_is_blank_whitespace(self):
        assert is_blank("   ") is True

    def test_is_blank_not(self):
        assert is_blank("hello") is False

    def test_count_words_empty(self):
        assert count_words("") == 0

    def test_count_words(self):
        assert count_words("hello world foo") == 3

    def test_count_sentences_empty(self):
        assert count_sentences("") == 0

    def test_count_sentences(self):
        assert count_sentences("Hello. World! How are you?") == 3

    def test_split_sentences(self):
        s = split_sentences("Hello world. How are you?")
        assert len(s) == 2
        assert s[0] == "Hello world."

    def test_remove_repeated_words(self):
        assert remove_repeated_words("the the cat sat on on the mat") == "the cat sat on the mat"

    def test_remove_repeated_lines(self):
        assert remove_repeated_lines("hello\nhello\nworld") == "hello\nworld"

    def test_remove_empty_lines(self):
        assert remove_empty_lines("hello\n\n\nworld") == "hello\n\nworld"

    def test_normalize_whitespace(self):
        assert normalize_whitespace("hello   world") == "hello world"

    def test_is_mixed_script(self):
        assert is_mixed_script("Hello यह है world", "latin", "devanagari") is True
        assert is_mixed_script("Hello world", "latin", "devanagari") is False


# =========================================================================
# Utils — unicode_utils
# =========================================================================

class TestUnicodeUtils:
    def test_normalize_unicode(self):
        text = "héllo"
        result = normalize_unicode(text)
        assert result == "héllo"

    def test_removes_invisible_chars(self):
        text = "hello\u200bworld"
        result = normalize_unicode(text)
        assert "\u200b" not in result

    def test_is_valid_unicode_valid(self):
        assert is_valid_unicode("hello") is True

    def test_is_valid_unicode_surrogate(self):
        assert is_valid_unicode("\uD800") is False

    def test_safe_decode(self):
        data = b"hello world"
        assert safe_decode(data) == "hello world"


# =========================================================================
# Utils — metrics
# =========================================================================

class TestMetrics:
    def test_compute_statistics(self):
        stats = compute_statistics(
            text="Hello world. How are you? I am fine.",
            paragraphs=["Hello world. How are you?", "I am fine."],
        )
        assert stats["word_count"] == 8
        assert stats["sentence_count"] >= 2
        assert stats["paragraph_count"] == 2
        assert stats["estimated_read_time"] != ""
        assert stats["avg_sentence_length_words"] > 0
