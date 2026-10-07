"""Translation package."""

from services.translation.english import SIMPLE_ENGLISH_SYSTEM_PROMPT
from services.translation.hindi import SIMPLE_HINDI_SYSTEM_PROMPT
from services.translation.service import TranslationError, TranslationService

__all__ = [
    "TranslationService",
    "TranslationError",
    "SIMPLE_ENGLISH_SYSTEM_PROMPT",
    "SIMPLE_HINDI_SYSTEM_PROMPT",
]
