"""Unicode normalization and sanitization utilities."""

import re
import unicodedata

_INVISIBLE_CHARS = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f"
    "\u0080-\u009f"
    "\u00ad"
    "\u034f"
    "\u061c"
    "\u115f\u1160"
    "\u17b4\u17b5"
    "\u180b-\u180e"
    "\u200b-\u200f"
    "\u2028-\u202f"
    "\u205f-\u2064"
    "\u2066-\u206f"
    "\u2800"
    "\u3164"
    "\ufeff"
    "\uffa0"
    "\U0001d159-\U0001d15e"
    "]"
)

_BIDI_OVERRIDES = re.compile("[\u202a\u202b\u202c\u202d\u202e]")
_ZWJ_SEQUENCES = re.compile("[\u200d\U0001f3fb-\U0001f3ff]")


def normalize_unicode(text: str, form: str = "NFC") -> str:
    """Normalize to NFC/NFD and remove non-characters."""
    text = unicodedata.normalize(form, text)
    text = _INVISIBLE_CHARS.sub("", text)
    text = _BIDI_OVERRIDES.sub("", text)
    return text


def is_valid_unicode(text: str) -> bool:
    """Check for valid Unicode without surrogates or non-characters."""
    try:
        text.encode("utf-8")
        for c in text:
            cp = ord(c)
            if 0xD800 <= cp <= 0xDFFF:
                return False
            if cp in (0xFFFE, 0xFFFF):
                return False
        return True
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def safe_decode(data: bytes, encoding: str = "utf-8") -> str:
    """Safely decode bytes, replacing errors."""
    return data.decode(encoding, errors="replace")


def estimate_unicode_width(text: str) -> int:
    """Estimate display width (CJK = 2, else 1)."""
    width = 0
    for c in text:
        cp = ord(c)
        if 0x1100 <= cp <= 0x11FF or 0x2E80 <= cp <= 0x9FFF or 0xA000 <= cp <= 0xA4CF or 0xAC00 <= cp <= 0xD7AF:
            width += 2
        elif 0xFE00 <= cp <= 0xFE0F:
            width += 0
        elif 0x1F000 <= cp <= 0x1FFFF:
            width += 2
        else:
            width += 1
    return width
