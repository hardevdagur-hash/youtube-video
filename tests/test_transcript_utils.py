"""Unit tests for transcript utilities."""

from models.transcript import TranscriptSegment
from utils.language_detector import LanguageDetector
from utils.read_time import estimate_read_time
from utils.text_cleaner import TextCleaner


class TestTextCleaner:
    def setup_method(self):
        self.cleaner = TextCleaner()

    def test_clean_text_removes_extra_spaces(self):
        result = self.cleaner.clean_text("Hello   world    here")
        assert result == "Hello world here"

    def test_clean_text_removes_control_chars(self):
        result = self.cleaner.clean_text("Hello\x00world\x1ftest")
        assert result == "Helloworldtest"

    def test_clean_text_normalizes_unicode(self):
        # Using composed + decomposed forms of é
        result = self.cleaner.clean_text("caf\u00e9")
        assert result == "caf\u00e9"

    def test_clean_text_fixes_spacing_before_period(self):
        result = self.cleaner.clean_text("Hello .World")
        assert result == "Hello. World"

    def test_clean_text_fixes_spacing_before_comma(self):
        result = self.cleaner.clean_text("Hello ,world")
        assert result == "Hello, world"

    def test_clean_text_fixes_missing_space_after_punctuation(self):
        result = self.cleaner.clean_text("Hello.world")
        assert result == "Hello. world"

    def test_clean_text_normalizes_multiple_dots(self):
        result = self.cleaner.clean_text("Hello....world")
        assert result == "Hello...world"

    def test_clean_text_fixes_spacing_after_period_uppercase(self):
        result = self.cleaner.clean_text("Hello.World")
        assert result == "Hello. World"

    def test_clean_text_normalizes_quotes(self):
        result = self.cleaner.clean_text("\u201cHello\u201d")
        assert result == '"Hello"'

    def test_clean_text_strips_whitespace(self):
        result = self.cleaner.clean_text("  hello world  ")
        assert result == "hello world"

    def test_clean_text_empty_string(self):
        assert self.cleaner.clean_text("") == ""
        assert self.cleaner.clean_text(None) == ""

    def test_clean_segments(self):
        segments = [
            TranscriptSegment(start=0.0, end=1.0, duration=1.0, text="Hello   world"),
            TranscriptSegment(start=1.0, end=2.0, duration=1.0, text="Test   here"),
        ]
        result = self.cleaner.clean_segments(segments)
        assert len(result) == 2
        assert result[0].text == "Hello world"
        assert result[0].start == 0.0
        assert result[1].text == "Test here"

    def test_build_paragraphs(self):
        segments = [
            TranscriptSegment(start=0.0, end=1.0, duration=1.0, text="First"),
            TranscriptSegment(start=1.0, end=2.0, duration=1.0, text="Second"),
            TranscriptSegment(start=5.0, end=6.0, duration=1.0, text="Third"),
        ]
        result = self.cleaner.build_paragraphs(segments)
        assert "First Second" in result
        assert "Third" in result

    def test_build_paragraphs_empty(self):
        assert self.cleaner.build_paragraphs([]) == ""


class TestLanguageDetector:
    def setup_method(self):
        self.detector = LanguageDetector()

    def test_detect_english(self):
        info = self.detector.detect("This is a test of the English language")
        assert info is not None
        assert info.language == "en"

    def test_detect_empty_text(self):
        assert self.detector.detect("") is None
        assert self.detector.detect(None) is None

    def test_detect_short_text(self):
        assert self.detector.detect("Hi") is None

    def test_heuristic_cjk(self):
        text = "你好世界这是一个测试" * 5
        info = LanguageDetector._heuristic_detect(text)
        assert info is not None
        assert info.language == "zh"

    def test_heuristic_cyrillic(self):
        text = "Привет мир это тест" * 5
        info = LanguageDetector._heuristic_detect(text)
        assert info is not None
        assert info.language == "ru"

    def test_heuristic_latin(self):
        text = "Hello world this is a test of latin script" * 3
        info = LanguageDetector._heuristic_detect(text)
        assert info is not None
        assert info.language == "en"


class TestReadTime:
    def test_estimate_read_time_normal(self):
        assert estimate_read_time(400) == "2 min"
        assert estimate_read_time(1000) == "5 min"

    def test_estimate_read_time_short(self):
        assert estimate_read_time(10) == "< 1 min"
        assert estimate_read_time(0) == "< 1 min"
        assert estimate_read_time(199) == "< 1 min"

    def test_estimate_read_time_long(self):
        result = estimate_read_time(30000)
        assert "hr" in result

    def test_custom_wpm(self):
        result = estimate_read_time(1000, words_per_minute=250)
        assert result == "4 min"
