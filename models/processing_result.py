"""Pydantic models for transcript processing pipeline output."""

from enum import Enum

from pydantic import BaseModel, Field


class ProcessingStepName(str, Enum):
    VALIDATE_INPUT = "validate_input"
    NORMALIZE_UNICODE = "normalize_unicode"
    REMOVE_INVALID_CHARS = "remove_invalid_chars"
    MERGE_CAPTIONS = "merge_captions"
    DEDUPLICATE = "deduplicate"
    REMOVE_EMPTY = "remove_empty"
    NORMALIZE_WHITESPACE = "normalize_whitespace"
    RESTORE_SENTENCES = "restore_sentences"
    FIX_PUNCTUATION = "fix_punctuation"
    CORRECT_CAPITALIZATION = "correct_capitalization"
    DETECT_LANGUAGE = "detect_language"
    DETECT_PARAGRAPHS = "detect_paragraphs"
    CALCULATE_METRICS = "calculate_metrics"
    GENERATE_OUTPUT = "generate_output"


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


class ProcessingStep(BaseModel):
    name: ProcessingStepName
    status: ProcessingStatus
    detail: str = ""
    duration_ms: float | None = None


class LanguageDistribution(BaseModel):
    primary: str
    secondary: str | None = None
    primary_confidence: float = 0.0
    secondary_confidence: float | None = None
    mixed_ratio: float | None = None


class ProcessingStatistics(BaseModel):
    word_count: int = 0
    character_count: int = 0
    paragraph_count: int = 0
    sentence_count: int = 0
    estimated_read_time: str = ""
    avg_sentence_length_words: float = 0.0
    avg_paragraph_length_words: float = 0.0
    longest_sentence_words: int = 0
    filler_word_count: int = 0


class ProcessingFlags(BaseModel):
    timestamps_removed: bool = False
    captions_merged: bool = False
    punctuation_restored: bool = False
    capitalization_fixed: bool = False
    duplicates_removed: bool = False
    language_detected: bool = False
    fillers_removed: bool = False
    quality_passed: bool = False


class ProcessedTimestamp(BaseModel):
    original_start: float
    original_end: float
    original_text: str
    cleaned_text: str


class ProcessingResult(BaseModel):
    success: bool = True
    video_id: str = ""
    language: LanguageDistribution | None = None
    statistics: ProcessingStatistics = Field(default_factory=ProcessingStatistics)
    clean_transcript: str = ""
    paragraphs: list[str] = Field(default_factory=list)
    sentences: list[str] = Field(default_factory=list)
    processing_steps: list[ProcessingStep] = Field(default_factory=list)
    timestamps: list[ProcessedTimestamp] = Field(default_factory=list)
    flags: ProcessingFlags = Field(default_factory=ProcessingFlags)
    processing_time_ms: float = 0.0
    error: str | None = None
