"""Shared text utilities for the processing pipeline."""

import re

_REPEATED_WORDS = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)
_REPEATED_LINES = re.compile(r"^(.+)\n\1$", re.MULTILINE)
_EMPTY_LINES = re.compile(r"^\s*$", re.MULTILINE)
_LEADING_TRAILING_WS = re.compile(r"^[ \t]+|[ \t]+$", re.MULTILINE)


def is_blank(text: str) -> bool:
    return not text or not text.strip()


def count_words(text: str) -> int:
    if not text:
        return 0
    return len(text.split())


def count_sentences(text: str) -> int:
    if not text:
        return 0
    count = len(re.findall(r"[.!?]+", text))
    return max(count, 1)


def split_sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in raw if s.strip()]


def remove_repeated_words(text: str) -> str:
    return _REPEATED_WORDS.sub(r"\1", text)


def remove_repeated_lines(text: str) -> str:
    prev: str | None = None
    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and stripped == prev:
            continue
        result.append(line)
        prev = stripped
    return "\n".join(result)


def remove_empty_lines(text: str) -> str:
    return _EMPTY_LINES.sub("", text).strip()


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", " ")
    text = re.sub(r" {2,}", " ", text)
    text = _LEADING_TRAILING_WS.sub("", text)
    return text.strip()


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def is_mixed_script(text: str, primary_script: str, secondary_script: str) -> bool:
    """Check if text contains significant characters from two scripts."""
    scripts = {
        "latin": (0x0041, 0x007A),  # basic Latin
        "devanagari": (0x0900, 0x097F),
        "arabic": (0x0600, 0x06FF),
        "cyrillic": (0x0400, 0x04FF),
        "cjk": (0x4E00, 0x9FFF),
    }
    total = max(len(text), 1)
    primary_count = sum(
        1 for c in text if _in_range(ord(c), scripts.get(primary_script, (0, 0)))
    )
    secondary_count = sum(
        1 for c in text if _in_range(ord(c), scripts.get(secondary_script, (0, 0)))
    )
    if total == 0:
        return False
    primary_ratio = primary_count / total
    secondary_ratio = secondary_count / total
    return secondary_ratio > 0.05 and primary_ratio > 0.3


def _in_range(codepoint: int, bounds: tuple[int, int]) -> bool:
    return bounds[0] <= codepoint <= bounds[1]
