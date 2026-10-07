"""Capitalization normalization processor."""
import re
from typing import Any

from models.processing_result import ProcessingStepName
from pipeline.base_processor import BaseProcessor

_COMMON_ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "ave.", "blvd.", "rd.", "ct.", "ln.",
    "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "oct.", "nov.", "dec.",
    "vs.", "etc.", "e.g.", "i.e.", "a.m.", "p.m.",
    "inc.", "ltd.", "corp.", "co.", "dept.",
    "gen.", "col.", "maj.", "cap.", "lt.", "sgt.",
    "gov.", "sen.", "rep.", "pres.", "vp.",
    "al.", "ch.", "ed.", "no.", "vol.",
}

_KNOWN_PROPER_NOUNS: set[str] = {
    "youtube", "google", "facebook", "twitter", "instagram", "linkedin", "tiktok",
    "ios", "android", "windows", "macos", "linux", "iphone", "ipad", "macbook",
    "aws", "gcp", "azure", "api", "sql", "nosql", "json", "xml", "html", "css",
    "javascript", "typescript", "python", "java", "c++", "ruby", "php", "swift",
    "kotlin", "rust", "go", "react", "angular", "vue", "node.js", "docker",
    "kubernetes", "ai", "ml", "nlp", "llm", "rag", "gpt", "bert", "transformer",
    "openai", "anthropic", "meta", "apple", "microsoft", "amazon", "netflix",
    "spotify", "uber", "airbnb", "slack", "github", "gitlab", "bitbucket",
}


class CapitalizationProcessor(BaseProcessor):
    step_name = ProcessingStepName.CORRECT_CAPITALIZATION

    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        text = context.get("text", "")
        if not text:
            return context
        text = self._fix_sentence_start(text)
        text = self._fix_proper_nouns(text)
        context["text"] = text
        return context

    @staticmethod
    def _fix_sentence_start(text: str) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        fixed: list[str] = []
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            for abbr in _COMMON_ABBREVIATIONS:
                if s.lower().startswith(abbr):
                    fixed.append(s)
                    break
            else:
                fixed.append(s[0].upper() + s[1:] if s else s)
        return " ".join(fixed)

    @staticmethod
    def _fix_proper_nouns(text: str) -> str:
        def _capitalize(m: re.Match) -> str:
            word = m.group(0)
            lower = word.lower()
            if lower in _KNOWN_PROPER_NOUNS:
                return _KNOWN_PROPER_NOUNS_PREFERRED.get(lower, word.title())
            return word

        pattern = re.compile(r"\b[A-Za-z]+(?:[''][A-Za-z]+)?\b")
        result = pattern.sub(_capitalize, text)
        return result


_KNOWN_PROPER_NOUNS_PREFERRED: dict[str, str] = {
    "ios": "iOS",
    "macos": "macOS",
    "macbook": "MacBook",
    "iphone": "iPhone",
    "ipad": "iPad",
    "apple": "Apple",
    "google": "Google",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "twitter": "Twitter",
    "instagram": "Instagram",
    "linkedin": "LinkedIn",
    "tiktok": "TikTok",
    "github": "GitHub",
    "gitlab": "GitLab",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "microsoft": "Microsoft",
    "netflix": "Netflix",
    "spotify": "Spotify",
    "amazon": "Amazon",
    "slack": "Slack",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "python": "Python",
    "kotlin": "Kotlin",
    "swift": "Swift",
    "react": "React",
    "angular": "Angular",
    "vue": "Vue",
    "node.js": "Node.js",
    "airbnb": "Airbnb",
}
