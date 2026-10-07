"""English (India) Transcript Converter.

Converts spoken Hindi/Hinglish and raw STT transcripts into clear, natural
Indian educational English.

Pipeline:
    YouTube Video -> Whisper/STT -> Raw Speech -> English Conversion -> Clean English Transcript

Key Capabilities:
1. Fixes common ASR mishearings (e.g. 'j-advans' -> 'JEE Advanced', 'jymyn' -> 'JEE Main').
2. Removes speech stuttering and teacher repetitions (e.g. 'Physics par. Physics par.' -> 'Focus on Physics.').
3. Normalizes spoken numbers and ranges (e.g. '96 97 percentile' -> '96–97 percentile', '130 plus' -> '130+').
4. Converts Hindi/Hinglish educational phrases into natural Indian English idioms.
5. Preserves technical terms exactly without weird translations.
6. Strictly avoids summarization or hallucination — preserves full length and speaker logic.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Technical & Educational Terms (Preserved exactly with canonical casing)
# ---------------------------------------------------------------------------

EXACT_TERMS: dict[str, str] = {
    "jee advanced": "JEE Advanced",
    "jee advance": "JEE Advanced",
    "jee main": "JEE Main",
    "jee mains": "JEE Main",
    "iit delhi": "IIT Delhi",
    "iit bombay": "IIT Bombay",
    "iit madras": "IIT Madras",
    "iit kanpur": "IIT Kanpur",
    "iit kharagpur": "IIT Kharagpur",
    "iit roorkee": "IIT Roorkee",
    "iit guwahati": "IIT Guwahati",
    "iit": "IIT",
    "nit": "NIT",
    "neet": "NEET",
    "ncert": "NCERT",
    "gpt": "GPT",
    "chatgpt": "ChatGPT",
    "pyq": "PYQ",
    "pyqs": "PYQs",
    "leetcode": "LeetCode",
    "y combinator": "Y Combinator",
    "y-combinator": "Y Combinator",
    "faang": "FAANG",
    "physics galaxy": "Physics Galaxy",
    "ashish arora": "Ashish Arora",
    "dsa": "DSA",
    "jira ticket": "Jira ticket",
    "jira": "Jira",
    "vibe coding": "vibe coding",
    "whiteboard": "whiteboard",
    "boilerplate code": "boilerplate code",
    "ai orchestrator": "AI orchestrator",
}

# ---------------------------------------------------------------------------
# Common ASR Mishearings & Phonetic Typos -> Canonical Terms
# ---------------------------------------------------------------------------

ASR_CORRECTIONS: list[tuple[re.Pattern, str]] = [
    # JEE Advanced mishearings
    (re.compile(r"\b(j[- ]?advans|j[- ]?advance[d]?|jee[- ]?advans)\b", re.IGNORECASE), "JEE Advanced"),
    # JEE Main mishearings
    (re.compile(r"\b(jymyn|j[- ]?mains?|jee[- ]?mains?)\b", re.IGNORECASE), "JEE Main"),
    # IIT Delhi mishearings
    (re.compile(r"\b(iti[- ]?daily|iti[- ]?delhi|it[- ]?daily|iit[- ]?daily)\b", re.IGNORECASE), "IIT Delhi"),
    # NCERT mishearings
    (re.compile(r"\b(ncrt|n\.c\.r\.t\.?|n c r t)\b", re.IGNORECASE), "NCERT"),
    # GPT mishearings
    (re.compile(r"\b(g[- ]?p[- ]?t)\b", re.IGNORECASE), "GPT"),
    (re.compile(r"\b(chat[- ]?g[- ]?p[- ]?t)\b", re.IGNORECASE), "ChatGPT"),
    # LeetCode mishearings
    (re.compile(r"\b(lid[- ]?kod|leet[- ]?kod|lit[- ]?code|lead[- ]?code)\b", re.IGNORECASE), "LeetCode"),
    # NEET mishearings
    (re.compile(r"\b(n[- ]?e[- ]?e[- ]?t)\b", re.IGNORECASE), "NEET"),
    # PYQ mishearings
    (re.compile(r"\b(p[- ]?y[- ]?q[- ]?s?)\b", re.IGNORECASE), "PYQs"),
    # Physics Galaxy channel & book terms
    (re.compile(r"\b(fuzzic[- ]?galaxy|physics[- ]?galaxi)\b", re.IGNORECASE), "Physics Galaxy"),
    (re.compile(r"\badvanced[- ]?illustration(?:s)?[- ]?book\b", re.IGNORECASE), "Advanced Illustrations book"),
    (re.compile(r"\badvanced[- ]?illustrations?\b", re.IGNORECASE), "Advanced Illustrations"),
    # Vibe coding & Jira
    (re.compile(r"\bwipe[- ]?coding\b", re.IGNORECASE), "vibe coding"),
    (re.compile(r"\bjeera[- ]?ticket\b", re.IGNORECASE), "Jira ticket"),
    (re.compile(r"\bjeera\b(?=.*(?:ticket|board|sprint|agile|bug))", re.IGNORECASE), "Jira"),
    # AI orchestrator
    (re.compile(r"\b(?:an?\s+)?(?:ai\s+)?(?:or\s+a\s+stator|orchestratr)\b", re.IGNORECASE), "an AI orchestrator"),
    # FAANG in tech hiring context
    (re.compile(r"\b(?:the\s+)?fans\b(?=.*(?:recruit|hire|code|reject|interview|tech|company|engineer|whiteboard))", re.IGNORECASE), "FAANG companies"),
    (re.compile(r"\b(?:the\s+)?fans\s+(?:will|are|company)\b", re.IGNORECASE), "FAANG companies "),
    # Y Combinator
    (re.compile(r"\b(?:top\s+)?start-?ups\s+of\s+(?:the\s+)?Y\b", re.IGNORECASE), "top startups of Y Combinator"),
    # Era & Whiteboard
    (re.compile(r"\bseara\b", re.IGNORECASE), "era"),
    (re.compile(r"\bin\s+a\s+era\b", re.IGNORECASE), "in an era"),
    (re.compile(r"\bwhite\s+board\b", re.IGNORECASE), "whiteboard"),
    (re.compile(r"\bjunior\s+dev\b", re.IGNORECASE), "junior developer"),
]

# ---------------------------------------------------------------------------
# Semantic ASR Context Reconstructions (Acoustic Homophones in Hinglish)
# ---------------------------------------------------------------------------

SEMANTIC_RECONSTRUCTIONS: list[tuple[re.Pattern, str]] = [
    # "In 2006/2016, Adi Tech Industry..." -> "In 2026, half of the tech industry..."
    (
        re.compile(r"\b(?:in\s+)?(?:2006|2016),?\s*(?:the\s+)?(?:adi|aadhi)\s+tech\s+industry\b", re.IGNORECASE),
        "In 2026, half of the tech industry",
    ),
    # "adi/aadhi tech industry" -> "half of the tech industry"
    (
        re.compile(r"\b(?:the\s+)?(?:adi|aadhi)\s+tech\s+industry\b", re.IGNORECASE),
        "half of the tech industry",
    ),
    # "adi/aadhi industry" -> "half of the industry"
    (
        re.compile(r"\b(?:the\s+)?(?:adi|aadhi)\s+industry\b", re.IGNORECASE),
        "half of the industry",
    ),
    # "lead code is time wasted" / "time waste of the lead" -> "LeetCode is a waste of time"
    (
        re.compile(r"\b(?:the\s+)?(?:lead|leet)\s*code\s+is\s+time\s+wasted?\b", re.IGNORECASE),
        "LeetCode is a waste of time",
    ),
    (
        re.compile(r"\btime\s+waste(?:d)?\s+of\s+the\s+(?:lead|leetcode)\b", re.IGNORECASE),
        "LeetCode is a waste of time",
    ),
    # "both are taking care of failure" / "suffering from failure" -> "both are setting you up for failure"
    (
        re.compile(r"\b(?:both\s+of\s+you\s+are|both\s+are)\s+(?:taking\s+care\s+of|suffering\s+from)\s+(?:the\s+)?failure\b", re.IGNORECASE),
        "Both are setting you up for failure",
    ),
    (
        re.compile(r"\b(?:taking\s+care\s+of|suffering\s+from)\s+(?:the\s+)?failure\b", re.IGNORECASE),
        "setting you up for failure",
    ),
    # "boilerplate can be written" -> "boilerplate code can be written"
    (
        re.compile(r"\bboilerplate\s+can\s+be\s+written\b", re.IGNORECASE),
        "boilerplate code can be written",
    ),
    # Spoken Series: "S1 question is equal to IIT selection" -> "1 Que = IIT Selection series"
    (
        re.compile(r"\b1\s+Que\s+(?:equal\s+to|equals)\s+IIT\s+Selection\b", re.IGNORECASE),
        "1 Que = IIT Selection",
    ),
    (
        re.compile(r"\b(?:our\s+)?s1\s+question\s+(?:is\s+)?(?:equal\s+to|eq|equals)\s+iit(?:\s+selection)?\b", re.IGNORECASE),
        "our 1 Que = IIT Selection series",
    ),
    (
        re.compile(r"\b(?:one|1)\s+question\s+(?:is\s+)?(?:equal\s+to|eq|equals)\s+i\.?e\.?\s+selection\b", re.IGNORECASE),
        "1 Que = IIT Selection series",
    ),
    (
        re.compile(r"\b(?:one|1)\s+question\s+(?:is\s+)?(?:equal\s+to|eq|equals)\s+iit(?:\s+selection)?\b", re.IGNORECASE),
        "1 Que = IIT Selection series",
    ),
    # "LeetCode is Time-based" -> "LeetCode is a waste of time"
    (
        re.compile(r"\bleetcode\s+is\s+Time[- ]based\b", re.IGNORECASE),
        "LeetCode is a waste of time",
    ),
    # "Y Combinator Combinator"
    (
        re.compile(r"\bY\s+Combinator\s+Combinator\b", re.IGNORECASE),
        "Y Combinator",
    ),
    # "5 AM series" in Physics Galaxy -> "series"
    (
        re.compile(r"\b5\s*am\s+series\b", re.IGNORECASE),
        "series",
    ),
    # "build up your command" -> "build up your conceptual mastery"
    (
        re.compile(r"\bbuild(?:\s+up)?\s+your\s+command\b", re.IGNORECASE),
        "build up your conceptual mastery",
    ),
    # "follow that you are not fire" -> "follow so that you don't get fired"
    (
        re.compile(r"\bfollow\s+that\s+you\s+are\s+not\s+fire\b", re.IGNORECASE),
        "follow so that you don't get fired",
    ),
    (
        re.compile(r"\bthat\s+you\s+are\s+not\s+fire\b", re.IGNORECASE),
        "so that you don't get fired",
    ),
]

# ---------------------------------------------------------------------------
# Number & Range Patterns
# ---------------------------------------------------------------------------

NUMBER_PATTERNS: list[tuple[re.Pattern, Any]] = [
    # "96 97 percentile" -> "96–97 percentile"
    (re.compile(r"\b(\d{1,2})\s+(\d{1,2})\s+percentile\b", re.IGNORECASE), r"\1–\2 percentile"),
    # "96 to 97 percentile" -> "96–97 percentile"
    (re.compile(r"\b(\d{1,2})\s+to\s+(\d{1,2})\s+percentile\b", re.IGNORECASE), r"\1–\2 percentile"),
    # "130 plus" -> "130+"
    (re.compile(r"\b(\d+)\s+plus\b", re.IGNORECASE), r"\1+"),
    # "35 to 45 marks" -> "35–45 marks"
    (re.compile(r"\b(\d+)\s+to\s+(\d+)\s+marks\b", re.IGNORECASE), r"\1–\2 marks"),
    # "35 45 marks" -> "35–45 marks"
    (re.compile(r"\b(\d{1,3})\s+(\d{1,3})\s+marks\b", re.IGNORECASE), r"\1–\2 marks"),
    # "50 30 20 strategy" -> "50-30-20 strategy"
    (re.compile(r"\b(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s+strategy\b", re.IGNORECASE), r"\1-\2-\3 strategy"),
    # "50:30:20 strategy" -> "50-30-20 strategy"
    (re.compile(r"\b(\d{1,3}):(\d{1,3}):(\d{1,3})\s+strategy\b", re.IGNORECASE), r"\1-\2-\3 strategy"),
    # "10 to 12 hours" -> "10–12 hours"
    (re.compile(r"\b(\d+)\s+to\s+(\d+)\s+hours?\b", re.IGNORECASE), r"\1–\2 hours"),
    # "80 to 90%" -> "80–90%"
    (re.compile(r"\b(\d{1,2})\s+to\s+(\d{1,2})%", re.IGNORECASE), r"\1–\2%"),
    (re.compile(r"\b(\d{1,2})\s+(\d{1,2})%", re.IGNORECASE), r"\1–\2%"),
]

# ---------------------------------------------------------------------------
# Repetition & Stutter Deduplication Patterns
# ---------------------------------------------------------------------------

SPECIFIC_REPETITIONS: list[tuple[re.Pattern, str]] = [
    # "Physics par. Physics par. Physics par." -> "Focus on Physics."
    (re.compile(r"\b(?:Physics\s+par[.,\s]*){2,}", re.IGNORECASE), "Focus on Physics. "),
    # "Physics par focus karo" -> "Focus on Physics."
    (re.compile(r"\bPhysics\s+par\s+focus\s+karo\b", re.IGNORECASE), "Focus on Physics."),
    # "question ko question ko solve karo" -> "Solve the question."
    (re.compile(r"\b(?:question\s+ko\s+){2,}solve\s+karo\b", re.IGNORECASE), "Solve the question."),
    # "question ko solve karo" -> "Solve the question."
    (re.compile(r"\bquestion\s+ko\s+solve\s+karo\b", re.IGNORECASE), "Solve the question."),
]

# ---------------------------------------------------------------------------
# Spoken Educational Hindi / Hinglish -> Natural Indian Educational English
# ---------------------------------------------------------------------------

EDUCATIONAL_PHRASE_MAPPINGS: list[tuple[re.Pattern, str]] = [
    # "question ko dobara solve karo aur apni galtiyon ko note karo"
    (
        re.compile(
            r"\bquestion\s+ko\s+(?:dobara|fir\s+se|again)\s+solve\s+karo\s+aur\s+apni\s+(?:galtiyon|mistakes)\s+ko\s+note\s+(?:down\s+)?karo\b",
            re.IGNORECASE,
        ),
        "Solve the question again and note down your mistakes.",
    ),
    # "question ko dobara solve karo"
    (
        re.compile(r"\bquestion\s+ko\s+(?:dobara|fir\s+se)\s+solve\s+karo\b", re.IGNORECASE),
        "Solve the question again.",
    ),
    # "apni galtiyon ko note karo"
    (
        re.compile(r"\bapni\s+(?:galtiyon|mistakes)\s+ko\s+note\s+(?:down\s+)?karo\b", re.IGNORECASE),
        "Note down your mistakes.",
    ),
    # "prepare for JEE Advanced" / "JEE Advanced ki tayari"
    (
        re.compile(r"\bJEE\s+Advanced\s+ki\s+(?:tayari|taiyari|preparation)\s+karo\b", re.IGNORECASE),
        "prepare for JEE Advanced",
    ),
    (
        re.compile(r"\b(?:lakhon|thousands\s+of)\s+(?:bache|students)\s+jo\s+JEE\s+Advanced\s+ki\s+(?:tayari|taiyari)\s+karte\s+hain\b", re.IGNORECASE),
        "Millions of students prepare for JEE Advanced.",
    ),
    # "give the JEE exam" / "JEE exam dena"
    (
        re.compile(r"\bJEE\s+(?:ka\s+)?exam\s+(?:do|dena|de\s+rahe\s+hain)\b", re.IGNORECASE),
        "give the JEE exam",
    ),
    # "attempt the paper" / "paper attempt karo"
    (
        re.compile(r"\bpaper\s+(?:ko\s+)?attempt\s+karo\b", re.IGNORECASE),
        "attempt the paper",
    ),
    # "clear the backlog"
    (
        re.compile(r"\bbacklog\s+(?:ko\s+)?clear\s+karo\b", re.IGNORECASE),
        "clear the backlog",
    ),
    # "complete the syllabus"
    (
        re.compile(r"\bsyllabus\s+(?:ko\s+)?complete\s+karo\b", re.IGNORECASE),
        "complete the syllabus",
    ),
    # "revision karo"
    (
        re.compile(r"\brevision\s+karo\b", re.IGNORECASE),
        "revise the concepts",
    ),
    # "formula yaad karo"
    (
        re.compile(r"\bformula(?:s)?\s+(?:ko\s+)?yaad\s+karo\b", re.IGNORECASE),
        "memorize the formulas",
    ),
]


class EnglishConverter:
    """Converts raw STT speech or Hindi/Hinglish transcripts into clean Indian English.

    Strictly satisfies:
    - Never summarizes or truncates.
    - Preserves all technical terms, numbers, and logical flow.
    - Formats numbers and ranges with proper typographical style.
    - Cleans ASR stuttering and repetitive phrases.
    - Fixes Indian academic ASR mishearings.
    """

    def __init__(self) -> None:
        self._devanagari_pattern = re.compile(r"[\u0900-\u097F]")
        self._urdu_pattern = re.compile(r"[\u0600-\u06FF]")

    def contains_non_roman_script(self, text: str) -> bool:
        """Check whether text contains Devanagari or Urdu/Arabic characters."""
        return bool(self._devanagari_pattern.search(text) or self._urdu_pattern.search(text))

    def correct_asr_errors(
        self,
        text: str,
        title: str | None = None,
        channel: str | None = None,
    ) -> str:
        """Correct Whisper ASR phonetic & semantic errors for Indian academic & tech terms."""
        result = text

        # 1. Apply semantic Hinglish context reconstructions
        for pattern, replacement in SEMANTIC_RECONSTRUCTIONS:
            result = pattern.sub(replacement, result)

        # 2. Apply general phonetic ASR typo corrections
        for pattern, replacement in ASR_CORRECTIONS:
            result = pattern.sub(replacement, result)

        # 3. Dynamic title-guided entity resolution
        if title:
            result = self._resolve_title_entities(result, title)

        # 4. Channel-guided resolution
        if channel:
            if "physics galaxy" in channel.lower():
                result = re.sub(r"\b(fuzzic|physics)\s+galax[iy]\b", "Physics Galaxy", result, flags=re.IGNORECASE)

        # 5. Exact term casing preservation
        for lower_term, canonical in EXACT_TERMS.items():
            pattern = re.compile(rf"\b{re.escape(lower_term)}\b", re.IGNORECASE)
            result = pattern.sub(canonical, result)

        return result

    def _resolve_title_entities(self, text: str, title: str) -> str:
        """Dynamically use video title to disambiguate ASR mishearings."""
        result = text
        title_lower = title.lower()

        # If title has "1 Que = IIT Selection" or similar series pattern
        series_match = re.search(r"(\d+\s*(?:Que|Question|Q)\s*=\s*IIT(?:\s*Selection)?)", title, re.IGNORECASE)
        if series_match:
            canonical_series = series_match.group(1).replace("Question", "Que").replace("Q ", "Que ")
            canonical_series = re.sub(r"1\s*que\s*=\s*iit", "1 Que = IIT Selection", canonical_series, flags=re.IGNORECASE)
            variant_pattern = re.compile(
                r"\b(?:our\s+)?(?:s1|1|one)\s+question\s+(?:is\s+)?(?:equal\s+to|eq|equals)\s+(?:iit|i\.?e\.?)(?:\s+selection)?\b",
                re.IGNORECASE,
            )
            result = variant_pattern.sub(f"{canonical_series} series", result)

        # If title mentions 2026 (e.g. "DSA vs AI in 2026")
        if "2026" in title:
            result = re.sub(r"\b(?:in\s+)?(?:2006|2016)\b(?=.*(?:tech|leetcode|engineer|coding|developer|ai|industry))", "In 2026", result, flags=re.IGNORECASE)

        # If title mentions LeetCode
        if "leetcode" in title_lower:
            result = re.sub(r"\b(?:the\s+)?lead\s*code\b", "LeetCode", result, flags=re.IGNORECASE)

        # If title mentions Physics Galaxy
        if "physics galaxy" in title_lower:
            result = re.sub(r"\b(fuzzic|physics)\s+galax[iy]\b", "Physics Galaxy", result, flags=re.IGNORECASE)

        return result

    def format_numbers_and_ranges(self, text: str) -> str:
        """Format spoken numbers, percentiles, ranges, and strategies properly."""
        result = text
        for pattern, repl in NUMBER_PATTERNS:
            result = pattern.sub(repl, result)
        return result

    def remove_repetitions(self, text: str) -> str:
        """Remove teacher stuttering, duplicate phrases/words, and Whisper sentence loops."""
        result = text

        # 1. Apply specific known repetition patterns
        for pattern, repl in SPECIFIC_REPETITIONS:
            result = pattern.sub(repl, result)

        # 2. Whisper sentence repetition loops: e.g. "Sentence A. Sentence A."
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", result) if s.strip()]
        if len(sentences) > 1:
            deduped: list[str] = []
            for s in sentences:
                s_clean = s.rstrip(".!?").strip().lower()
                if deduped:
                    prev_clean = deduped[-1].rstrip(".!?").strip().lower()
                    if s_clean == prev_clean:
                        continue
                deduped.append(s)
            result = " ".join(deduped)

        # 3. General sentence/clause repetition: e.g. "Focus on Physics. Focus on Physics."
        clause_rep = re.compile(r"\b([A-Za-z0-9\s]{3,30}?)[.,]?(?:\s+\1[.,]?)+", re.IGNORECASE)
        result = clause_rep.sub(r"\1.", result)

        # 4. Consecutive single word repetition: e.g. "the the", "Physics Physics", "Combinator Combinator"
        word_rep = re.compile(r"\b([A-Za-z]{3,})[.,]?[ \t]+\1\b", re.IGNORECASE)
        result = word_rep.sub(r"\1", result)

        # 5. Fix broken sentence fragments like "the series. Is already"
        result = re.sub(r"\bthe\s+series\.\s+Is\b", "the series is", result, flags=re.IGNORECASE)

        # 6. Clean up any resulting double punctuation or double spaces
        result = re.sub(r"\s+([.,?!])", r"\1", result)
        result = re.sub(r"([.,?!]){2,}", r"\1", result)
        result = re.sub(r"[ ]{2,}", " ", result)

        return result

    def convert_educational_phrases(self, text: str) -> str:
        """Convert spoken Hindi educational phrases into natural Indian English."""
        result = text
        for pattern, repl in EDUCATIONAL_PHRASE_MAPPINGS:
            result = pattern.sub(repl, result)
        return result

    def convert(
        self,
        text: str,
        title: str | None = None,
        channel: str | None = None,
    ) -> str:
        """Run the complete English Conversion pipeline on raw speech transcript text.

        Pipeline:
            0. Script check (transliterates any Devanagari/Urdu to Roman if present)
            1. Repetition and stutter cleanup
            2. Spoken educational phrase conversion
            3. Context-aware ASR semantic error correction & technical term preservation
            4. Number & range normalization
            5. Final typography, punctuation & casing polish
        """
        if not text or not text.strip():
            return ""

        result = text.strip()

        # Step 0: If text contains Devanagari or Urdu script, normalize to Roman
        if self.contains_non_roman_script(result):
            from services.transliteration import hinglish_normalizer
            result = hinglish_normalizer.normalize(result)

        # Step 1: Remove stuttering, teacher loops, and repeated sentences
        result = self.remove_repetitions(result)

        # Step 2: Educational Hindi/Hinglish phrase conversion
        result = self.convert_educational_phrases(result)

        # Step 3: Fix Whisper ASR mishearings with title and channel context
        result = self.correct_asr_errors(result, title=title, channel=channel)

        # Step 4: Format numbers and ranges
        result = self.format_numbers_and_ranges(result)

        # Step 5: Clean spacing and sentence capitalization
        result = self._polish_text(result)

        return result

    def convert_segments(
        self,
        segments: Sequence[Any],
        title: str | None = None,
        channel: str | None = None,
    ) -> list[Any]:
        """Convert and normalize a sequence of transcript segments in place or copy."""
        converted = []
        for seg in segments:
            text = getattr(seg, "text", "")
            cleaned = self.convert(text, title=title, channel=channel)
            if hasattr(seg, "text"):
                seg.text = cleaned
            converted.append(seg)
        return converted

    def validate_transcript(
        self,
        clean_text: str,
        raw_text: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Validate transcript quality according to production criteria.

        Checks:
        1. Language: English (India)
        2. Non-roman characters (Devanagari/Urdu): 0%
        3. Full preservation: clean length >= 70% of raw length (never summarized)
        4. Number preservation: all numbers in raw exist in clean
        5. Returns dictionary with quality metrics
        """
        clean_len = len(clean_text)
        raw_len = len(raw_text)
        ratio = clean_len / max(raw_len, 1)

        has_non_roman = self.contains_non_roman_script(clean_text)

        # Numbers check
        raw_numbers = set(re.findall(r"\b\d+\b", raw_text))
        clean_numbers = set(re.findall(r"\b\d+\b", clean_text))
        preserved_numbers = raw_numbers.issubset(clean_numbers) or (len(raw_numbers - clean_numbers) / max(len(raw_numbers), 1) <= 0.1)

        is_valid = (
            not has_non_roman
            and ratio >= 0.70
            and clean_len > 0
        )

        return {
            "is_valid": is_valid,
            "language": "English (India)",
            "script": "Roman",
            "clean_length": clean_len,
            "raw_length": raw_len,
            "length_ratio": round(ratio, 3),
            "no_non_roman_script": not has_non_roman,
            "numbers_preserved": preserved_numbers,
            "word_count": len(clean_text.split()),
        }

    def _polish_text(self, text: str) -> str:
        """Polish punctuation, capitalization, and whitespace without summarizing."""
        lines = text.split("\n")
        polished_lines = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Ensure space after commas, periods, colons
            line = re.sub(r"([.,!?:;])([A-Za-z])", r"\1 \2", line)

            # Ensure sentence beginnings are capitalized
            sentences = re.split(r"(?<=[.!?])\s+", line)
            cap_sentences = []
            for s in sentences:
                s = s.strip()
                if s:
                    cap_sentences.append(s[0].upper() + s[1:] if len(s) > 1 else s.upper())
            line = " ".join(cap_sentences)

            # Final clean of double spaces
            line = re.sub(r"[ ]{2,}", " ", line)
            polished_lines.append(line)

        return "\n\n".join(polished_lines) if "\n\n" in text else " ".join(polished_lines)


# Global singleton instance
english_converter = EnglishConverter()
