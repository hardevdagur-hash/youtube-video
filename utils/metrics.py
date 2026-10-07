"""Readability and transcript statistics computation."""


from utils.read_time import estimate_read_time as _estimate_read_time
from utils.text_utils import count_sentences, count_words, split_sentences


def compute_statistics(
    text: str,
    paragraphs: list[str],
    filler_word_count: int = 0,
    wpm: int = 200,
) -> dict:
    """Compute comprehensive readability and transcript statistics.

    Args:
        text: Full clean transcript text.
        paragraphs: List of paragraph strings.
        filler_word_count: Number of filler words removed.
        wpm: Words per minute for read time estimation.

    Returns:
        Dict with word_count, character_count, paragraph_count,
        sentence_count, estimated_read_time, avg_sentence_length_words,
        avg_paragraph_length_words, longest_sentence_words,
        filler_word_count.
    """
    word_count = count_words(text)
    character_count = len(text)
    paragraph_count = max(len(paragraphs), 1)
    sentence_count = count_sentences(text)
    sentences = split_sentences(text)

    avg_sentence_length = round(word_count / max(sentence_count, 1), 1)
    avg_paragraph_length = round(word_count / max(paragraph_count, 1), 1)
    longest_sentence = max(
        (count_words(s) for s in sentences),
        default=0,
    )

    return {
        "word_count": word_count,
        "character_count": character_count,
        "paragraph_count": paragraph_count,
        "sentence_count": sentence_count,
        "estimated_read_time": _estimate_read_time(word_count, wpm),
        "avg_sentence_length_words": avg_sentence_length,
        "avg_paragraph_length_words": avg_paragraph_length,
        "longest_sentence_words": longest_sentence,
        "filler_word_count": filler_word_count,
    }
