"""Hinglish / Roman Hindi Normalization Engine.

Converts Devanagari Hindi, Urdu/Perso-Arabic script, and mixed tokens into
natural Roman script (Hinglish) while preserving:
- English words, numbers, and technical terms in their original form
- Exact spoken meaning (transliteration / romanization, NOT translation)
- Natural sentence flow and capitalization
"""

from __future__ import annotations

import re
from typing import Any

import anyascii

# ---------------------------------------------------------------------------
# High-Frequency Spoken Hindi / Devanagari Dictionary
# ---------------------------------------------------------------------------

HINDI_DICT: dict[str, str] = {
    # Pronouns
    "आपको": "aapko",
    "आप": "aap",
    "आपका": "aapka",
    "आपके": "aapke",
    "आपकी": "aapki",
    "हम": "hum",
    "हमें": "humein",
    "हमारा": "hamara",
    "हमारे": "hamare",
    "हमारी": "hamari",
    "तुम": "tum",
    "तुम्हें": "tumhein",
    "तुम्हारा": "tumhara",
    "तुम्हारे": "tumhare",
    "तुम्हारी": "tumhari",
    "मैं": "main",
    "मुझे": "mujhe",
    "मेरा": "mera",
    "मेरे": "mere",
    "मेरी": "meri",
    "मुझमें": "mujhmein",
    "यह": "yeh",
    "ये": "ye",
    "वह": "woh",
    "वो": "woh",
    "वे": "ve",
    "इस": "is",
    "इसे": "ise",
    "इसका": "iska",
    "इसके": "iske",
    "इसकी": "iski",
    "इसमें": "ismein",
    "इससे": "isse",
    "इसको": "isko",
    "उस": "us",
    "उसे": "use",
    "उसका": "uska",
    "उसके": "uske",
    "उसकी": "uski",
    "उसमें": "usmein",
    "उससे": "usse",
    "उसको": "usko",
    "इन": "in",
    "इन्हें": "inhein",
    "इनका": "inka",
    "इनके": "inke",
    "इनकी": "inki",
    "उन": "un",
    "उन्हें": "unhein",
    "उनका": "unka",
    "उनके": "unke",
    "उनकी": "unki",
    "खुद": "khud",
    "अपने": "apne",
    "अपना": "apna",
    "अपनी": "apni",
    # Postpositions / particles
    "का": "ka",
    "के": "ke",
    "की": "ki",
    "को": "ko",
    "से": "se",
    "में": "mein",
    "पर": "par",
    "पे": "pe",
    "ने": "ne",
    "तक": "tak",
    "भी": "bhi",
    "ही": "hi",
    "तो": "to",
    "सा": "sa",
    "सी": "si",
    # Conjunctions
    "और": "aur",
    "या": "ya",
    "अथवा": "athwa",
    "लेकिन": "lekin",
    "मगर": "magar",
    "किंतु": "kintu",
    "परंतु": "parantu",
    "क्योंकि": "kyunki",
    "ताकि": "taaki",
    "अगर": "agar",
    "यदि": "yadi",
    "फिर": "phir",
    "इसलिए": "isliye",
    "कि": "ki",
    "जैसे": "jaise",
    "वैसे": "waise",
    # Adverbs / Questions / Locations
    "यहाँ": "yahan",
    "यहां": "yahan",
    "वहाँ": "wahan",
    "वहां": "wahan",
    "कहाँ": "kahan",
    "कहां": "kahan",
    "जहाँ": "jahan",
    "जहां": "jahan",
    "आज": "aaj",
    "कल": "kal",
    "परसों": "parson",
    "अब": "ab",
    "जब": "jab",
    "तब": "tab",
    "कब": "kab",
    "सब": "sab",
    "क्या": "kya",
    "क्यों": "kyun",
    "कैसे": "kaise",
    "कैसा": "kaisa",
    "कैसी": "kaisi",
    "कौन": "kaun",
    "किस": "kis",
    "किसे": "kise",
    "किसका": "kiska",
    "किसके": "kiske",
    "किसकी": "kiski",
    "कितना": "kitna",
    "कितने": "kitne",
    "कितनी": "kitni",
    "बहुत": "bahut",
    "ज़्यादा": "zyada",
    "ज्यादा": "zyada",
    "कम": "kam",
    "थोड़ा": "thoda",
    "थोड़े": "thode",
    "थोड़ी": "thodi",
    "काफ़ी": "kaafi",
    "काफी": "kaafi",
    "सिर्फ": "sirf",
    "सिर्फ़": "sirf",
    "केवल": "kewal",
    "बिल्कुल": "bilkul",
    "हमेशा": "hamesha",
    "कभी": "kabhi",
    "बार": "baar",
    "दोबारा": "dobaara",
    # Verbs & Auxiliaries
    "है": "hai",
    "हैं": "hain",
    "हो": "ho",
    "हूँ": "hoon",
    "हूं": "hoon",
    "था": "tha",
    "थी": "thi",
    "थे": "the",
    "होगा": "hoga",
    "होगी": "hogi",
    "होंगे": "honge",
    "होना": "hona",
    "होता": "hota",
    "होती": "hoti",
    "होते": "hote",
    "होकर": "hokar",
    "करना": "karna",
    "कर": "kar",
    "करता": "karta",
    "करती": "karti",
    "करते": "karte",
    "किया": "kiya",
    "किए": "kiye",
    "किये": "kiye",
    "करेंगे": "karenge",
    "करूंगा": "karunga",
    "करूँगा": "karunga",
    "करूंगी": "karungi",
    "करेंगी": "karengi",
    "करें": "karein",
    "करो": "karo",
    "कीजिए": "kijiye",
    "कीजिये": "kijiye",
    "निकालना": "nikalna",
    "निकाल": "nikal",
    "निकालता": "nikalta",
    "निकालती": "nikalti",
    "निकालते": "nikalte",
    "निकालेंगे": "nikalenge",
    "निकाला": "nikala",
    "निकाले": "nikale",
    "निकाली": "nikali",
    "पढ़ेंगे": "padhenge",
    "पढ़ना": "padhna",
    "पढ़": "padh",
    "पढ़ते": "padhte",
    "पढ़ा": "padha",
    "बारे": "baare",
    "सकते": "sakte",
    "सकता": "sakta",
    "सकती": "sakti",
    "सकेंगे": "sakenge",
    "सके": "sake",
    "चाहिए": "chahiye",
    "चाहते": "chahte",
    "चाहता": "chahta",
    "चाहती": "chahti",
    "जाना": "jaana",
    "जा": "jaa",
    "जाता": "jaata",
    "जाती": "jaati",
    "जाते": "jaate",
    "जाएंगे": "jaenge",
    "गया": "gaya",
    "गए": "gaye",
    "गई": "gayi",
    "गयी": "gayi",
    "आना": "aana",
    "आ": "aa",
    "आता": "aata",
    "आती": "aati",
    "आते": "aate",
    "आएंगे": "aaenge",
    "आया": "aaya",
    "आए": "aaye",
    "आई": "aayi",
    "देना": "dena",
    "दे": "de",
    "देता": "deta",
    "देती": "deti",
    "देते": "dete",
    "देंगे": "denge",
    "दिया": "diya",
    "दिए": "diye",
    "लेना": "lena",
    "ले": "le",
    "लेता": "leta",
    "लेती": "leti",
    "लेते": "lete",
    "लेंगे": "lenge",
    "लिया": "liya",
    "लिए": "liye",
    "कहना": "kehna",
    "कह": "kah",
    "कहते": "kehte",
    "कहा": "kaha",
    "देखना": "dekhna",
    "देख": "dekh",
    "देखते": "dekhte",
    "देखेंगे": "dekhenge",
    "देखा": "dekha",
    "समझना": "samajhna",
    "समझ": "samajh",
    "समझते": "samajhte",
    "समझेंगे": "samajhenge",
    "समझा": "samjha",
    "जानना": "jaanna",
    "जानते": "jaante",
    "पता": "pata",
    "लिखना": "likhna",
    "लिख": "likh",
    "लिखते": "likhte",
    "लिखेंगे": "likhenge",
    "लिखा": "likha",
    "बोलना": "bolna",
    "बोल": "bol",
    "बोलते": "bolte",
    "बोला": "bola",
    "मिलना": "milna",
    "मिलता": "milta",
    "मिलते": "milte",
    "मिलेगा": "milega",
    "मिला": "mila",
    "मिले": "mile",
    "रखना": "rakhna",
    "रख": "rakh",
    "रखते": "rakhte",
    "रखेंगे": "rakhenge",
    "रखा": "rakha",
    "बताना": "batana",
    "बता": "bata",
    "बताते": "batate",
    "बताएंगे": "bataenge",
    "बताया": "bataya",
    "सीखना": "seekhna",
    "सीख": "seekh",
    "सीखते": "seekhte",
    "सीखेंगे": "seekhenge",
    "चलना": "chalna",
    "चल": "chal",
    "चलते": "chalate",
    "चलता": "chalta",
    "रहा": "raha",
    "रही": "rahi",
    "रहे": "rahe",
    "नहीं": "nahin",
    "ना": "na",
    "मत": "mat",
    # Nouns & Common Words
    "बात": "baat",
    "चीज": "cheez",
    "काम": "kaam",
    "नाम": "naam",
    "समय": "samay",
    "वक्त": "waqt",
    "साल": "saal",
    "दिन": "din",
    "रात": "raat",
    "तरह": "tarah",
    "साथ": "saath",
    "पास": "paas",
    "बाद": "baad",
    "पहले": "pehle",
    "अंदर": "andar",
    "बाहर": "bahar",
    "ऊपर": "upar",
    "नीचे": "neeche",
    "आगे": "aage",
    "पीछे": "peeche",
    "बीच": "beech",
    "अच्छा": "achha",
    "अच्छे": "achhe",
    "अच्छी": "achhi",
    "बड़ा": "bada",
    "बड़े": "bade",
    "बड़ी": "badi",
    "छोटा": "chhota",
    "छोटे": "chhote",
    "छोटी": "chhoti",
    "पहला": "pehla",
    "दूसरा": "doosra",
    "तीसरा": "teesra",
    "एक": "ek",
    "दो": "do",
    "तीन": "teen",
    "चार": "chaar",
    "पांच": "paanch",
    "पाँच": "paanch",
    "छह": "chhah",
    "सात": "saat",
    "आठ": "aath",
    "नौ": "nau",
    "दस": "das",
    "सही": "sahi",
    "गलत": "galat",
    "ज़रूरी": "zaroori",
    "जरूरी": "zaroori",
    "क्वेश्चन": "question",
    "सॉल्व": "solve",
    "आंसर": "answer",
    "न्यूटन": "Newton",
}

# ---------------------------------------------------------------------------
# High-Frequency Urdu / Perso-Arabic Dictionary (frequently decoded by Whisper)
# ---------------------------------------------------------------------------

URDU_DICT: dict[str, str] = {
    # Pronouns
    "آپ": "aap",
    "آپکو": "aapko",
    "آپ کو": "aap ko",
    "اپنی": "apni",
    "اپنا": "apna",
    "اپنے": "apne",
    "ہم": "hum",
    "ہمیں": "humein",
    "ہمارا": "hamara",
    "ہمارے": "hamare",
    "ہماری": "hamari",
    "تم": "tum",
    "تمہیں": "tumhein",
    "تمہारा": "tumhara",
    "میں": "main",
    "مجھے": "mujhe",
    "میرا": "mera",
    "میرے": "mere",
    "میری": "meri",
    "یہ": "ye",
    "وہ": "woh",
    "اس": "is",
    "اسکا": "iska",
    "اسکے": "iske",
    "اسکی": "iski",
    "اسکو": "isko",
    "اسمیں": "ismein",
    "ان": "in",
    "انہیں": "inhein",
    "انکا": "inka",
    "انکے": "inke",
    "انکی": "inki",
    "انھوں": "unhon",
    "انہوں": "unhon",
    # Postpositions / particles
    "کا": "ka",
    "کے": "ke",
    "کی": "ki",
    "کو": "ko",
    "سے": "se",
    "میں": "mein",
    "پر": "par",
    "پہ": "pe",
    "نے": "ne",
    "تک": "tak",
    "بھی": "bhi",
    "ہی": "hi",
    "تو": "to",
    # Conjunctions
    "اور": "aur",
    "یا": "ya",
    "لیکن": "lekin",
    "مگر": "magar",
    "کیونکہ": "kyunki",
    "تاکہ": "taaki",
    "اگر": "agar",
    "پھر": "phir",
    "اس لیے": "isliye",
    "کہ": "ke",
    # Verbs
    "ہے": "hai",
    "ہیں": "hain",
    "ہو": "ho",
    "ہوں": "hoon",
    "تھا": "tha",
    "تھی": "thi",
    "تھے": "the",
    "ہونا": "hona",
    "ہوتا": "hota",
    "ہوتی": "hoti",
    "ہوتے": "hote",
    "ہوگا": "hoga",
    "ہوگی": "hogi",
    "ہوں گے": "honge",
    "کر": "kar",
    "करना": "karna",
    "کرنا": "karna",
    "کرتا": "karta",
    "کرتی": "karti",
    "کرتے": "karte",
    "کریں": "karein",
    "کریں گے": "karenge",
    "کرینگے": "karenge",
    "کروں گا": "karunga",
    "کیا": "kiya",
    "کئے": "kiye",
    "کیے": "kiye",
    "لیتے": "lete",
    "لینا": "lena",
    "لیتا": "leta",
    "لیتی": "leti",
    "لیں": "lein",
    "لیں گے": "lenge",
    "دینا": "dena",
    "دیتے": "dete",
    "دیں گے": "denge",
    "دیا": "diya",
    "بنا": "bana",
    "بنانا": "banana",
    "بناتے": "banate",
    "بنانے": "banane",
    "بنائیں": "banayein",
    "بنا لیتے": "bana lete",
    "دیکھ": "dekh",
    "دیکھنا": "dekhna",
    "دیکھتے": "dekhte",
    "دیکھیں": "dekhein",
    "دیکھا": "dekha",
    "دیکھیں گے": "dekhenge",
    "سمجھ": "samajh",
    "سمجھنا": "samajhna",
    "سمجھتے": "samajhte",
    "سمجھیں گے": "samajhenge",
    "پڑھ": "padh",
    "پڑھنا": "padhna",
    "پڑھیں": "padhein",
    "پڑھیں گے": "padhenge",
    "پڑھینگے": "padhenge",
    "نکال": "nikal",
    "نکالنا": "nikalna",
    "نکالتے": "nikalte",
    "نکالیں": "nikalein",
    "نکالیں گے": "nikalenge",
    "نکالینگے": "nikalenge",
    "رہا": "raha",
    "رہی": "rahi",
    "رہے": "rahe",
    "نہیں": "nahin",
    "نہ": "na",
    "مت": "mat",
    # Common Adverbs & Words
    "یہاں": "yahan",
    "وہاں": "wahan",
    "کہاں": "kahan",
    "جہاں": "jahan",
    "آج": "aaj",
    "کل": "kal",
    "اب": "ab",
    "جب": "jab",
    "تب": "tab",
    "سب": "sab",
    "کب": "kab",
    "کیا": "kya",
    "کیوں": "kyun",
    "کیسے": "kaise",
    "کون": "kaun",
    "کس": "kis",
    "کتنا": "kitna",
    "کتنے": "kitne",
    "کتنی": "kitni",
    "بہت": "bahut",
    "زیادہ": "zyada",
    "کم": "kam",
    "تھوڑا": "thoda",
    "کافی": "kaafi",
    "صرف": "sirf",
    "ایک": "ek",
    "دو": "do",
    "تین": "teen",
    "چار": "chaar",
    "پانچ": "paanch",
    "بات": "baat",
    "چیز": "cheez",
    "کام": "kaam",
    "نام": "naam",
    "وقت": "waqt",
    "طرح": "tarah",
    "ساتھ": "saath",
    "پاس": "paas",
    "بعد": "baad",
    "پہلا": "pehla",
    "پہلے": "pehle",
    "پہلی": "pehli",
    "دوسرا": "doosra",
    "دوسرے": "doosre",
    "اچھا": "achha",
    "اچھے": "achhe",
    "اچھی": "achhi",
    "بڑا": "bada",
    "بڑے": "bade",
    "بڑی": "badi",
    "چھوٹا": "chhota",
    "صحیح": "sahi",
    "غلط": "galat",
    "ضروری": "zaroori",
    "ہمیشہ": "hamesha",
    "کبھی": "kabhi",
    # Technical / Loanwords frequently decoded in Urdu
    "کمانڈ": "command",
    "کمانٹ": "command",
    "پیپر": "paper",
    "بچوں": "bachon",
    "ہزاروں": "hazaron",
    "ہزارو": "hazaron",
    "سکھائیں": "sikhaayein",
    "سکھاں": "sikhaayein",
    "سکھا": "sikha",
    "سکھایا": "sikhaya",
    "پوچھا": "poochha",
    "پوچھے": "poochhe",
    "بتانا": "batana",
    "بتائیں": "bataayein",
    "لائے": "laaye",
    "لائیں": "laayein",
    "سلکشن": "selection",
    "سریس": "series",
    "پرپس": "purpose",
    "فزکس": "physics",
    "میکینکس": "mechanics",
    "کانسیپٹ": "concept",
    "کانسیپٹس": "concepts",
    "فارمولا": "formula",
    "ویلیو": "value",
    "کیلکولیشن": "calculation",
    "اسپیڈ": "speed",
    "ویسٹی": "velocity",
    "ایکسلریشن": "acceleration",
    "فورس": "force",
    "ماس": "mass",
    "فرکشن": "friction",
    "گریوٹی": "gravity",
    "انرجی": "energy",
    "پاور": "power",
    "موشن": "motion",
    "کوئسچن": "question",
    "کوئسچنز": "questions",
    "سوال": "question",
    "سالو": "solve",
    "انسر": "answer",
    "جواب": "answer",
}

# ---------------------------------------------------------------------------
# Multi-Word Compound Replacements (Urdu & Hindi Verb Conjugations)
# ---------------------------------------------------------------------------

COMPOUNDS: list[tuple[str, str]] = [
    ("نکالیں گے", "nikalenge"),
    ("نکالیں گی", "nikalengi"),
    ("کریں گے", "karenge"),
    ("کریں گی", "karengi"),
    ("کروں گا", "karunga"),
    ("کروں گی", "karungi"),
    ("ہوں گے", "honge"),
    ("ہوں گی", "hongi"),
    ("لیں گے", "lenge"),
    ("دیں گے", "denge"),
    ("پڑھیں گے", "padhenge"),
    ("دیکھیں گے", "dekhenge"),
    ("سمجھیں گے", "samajhenge"),
    ("جائیں گے", "jaenge"),
    ("آئیں گے", "aaenge"),
    ("بتائیں گے", "bataenge"),
    ("آپ کو", "Aap ko"),
    ("اس لیے", "isliye"),
]

# Regex patterns for scripts
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_URDU_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


class HinglishNormalizer:
    """Normalizes Hindi / Urdu / mixed text into clean, readable Hinglish (Roman Hindi)."""

    def __init__(self) -> None:
        self._hindi_dict = HINDI_DICT
        self._urdu_dict = URDU_DICT
        self._compounds = COMPOUNDS

    @staticmethod
    def contains_non_roman_script(text: str) -> bool:
        """Check if the text contains non-Roman characters (Devanagari or Arabic/Urdu)."""
        if not text:
            return False
        return bool(_DEVANAGARI_RE.search(text) or _URDU_ARABIC_RE.search(text))

    @staticmethod
    def is_hinglish_or_hindi(text: str) -> bool:
        """Check if text contains Hindi, Urdu, or Hinglish linguistic patterns."""
        if not text:
            return False
        if _DEVANAGARI_RE.search(text) or _URDU_ARABIC_RE.search(text):
            return True
        # Common Hinglish marker words
        markers = {"hai", "hain", "karna", "karenge", "karke", "hoga", "apne", "aapko", "yahan", "padhenge"}
        lower_tokens = set(re.findall(r"\b[a-zA-Z]+\b", text.lower()))
        return len(lower_tokens.intersection(markers)) >= 2

    def _transliterate_token(self, token: str) -> str:
        """Transliterate an individual token while preserving surrounding punctuation."""
        m = re.match(r"^([^\w\u0600-\u06FF\u0900-\u097F]*)(.*?)([^\w\u0600-\u06FF\u0900-\u097F]*)$", token)
        if not m:
            return token
        prefix, core, suffix = m.groups()
        if not core:
            return token

        # Check dictionaries
        if core in self._hindi_dict:
            core_trans = self._hindi_dict[core]
        elif core in self._urdu_dict:
            core_trans = self._urdu_dict[core]
        else:
            # Fallback for unknown Devanagari or Urdu words
            has_deva = bool(_DEVANAGARI_RE.search(core))
            has_urdu = bool(_URDU_ARABIC_RE.search(core))
            if has_deva or has_urdu:
                core_trans = anyascii.anyascii(core)
            else:
                core_trans = core

        return prefix + core_trans + suffix

    def normalize(self, text: str) -> str:
        """Convert Hindi/Urdu/mixed text to clean Roman Hinglish.

        If text is already pure English/Latin, returns it untouched with standard formatting.
        """
        if not text or not text.strip():
            return ""

        # Step 1: Normalize punctuation symbols from Indian/Arabic typographies
        normalized = (
            text.replace("।", ".")
            .replace("॥", ".")
            .replace("۔", ".")
            .replace("؟", "?")
            .replace("،", ",")
        )

        # Step 2: Multi-word phrase replacements for Urdu/Hindi compound verbs
        for k, v in self._compounds:
            normalized = normalized.replace(k, v)

        # Step 3: Tokenize by space, preserving spacing
        tokens = normalized.split(" ")
        out_tokens = []
        for t in tokens:
            if t:
                out_tokens.append(self._transliterate_token(t))
            else:
                out_tokens.append("")

        res = " ".join(out_tokens)

        # Step 4: Universal safety fallback to guarantee ZERO non-Roman characters remain
        res = anyascii.anyascii(res)

        # Step 5: Clean spacing around punctuation
        res = re.sub(r"\s+([.,!?:;])", r"\1", res)
        res = re.sub(r"\s+", " ", res).strip()

        # Step 6: Sentence capitalization
        sentences = re.split(r"([.!?]\s*)", res)
        cap_sentences = []
        for s in sentences:
            if s and s[0].islower():
                s = s[0].upper() + s[1:]
            cap_sentences.append(s)
        res = "".join(cap_sentences)

        return res

    def normalize_segments(self, segments: list[Any]) -> list[Any]:
        """Normalize a list of transcript segments in place or as copies."""
        for seg in segments:
            raw_text = getattr(seg, "text", "")
            if raw_text:
                normalized_text = self.normalize(raw_text)
                setattr(seg, "text", normalized_text)
        return segments


# Global Singleton instance
hinglish_normalizer = HinglishNormalizer()
