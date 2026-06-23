"""
Split a long story into balanced parts at sentence boundaries.

Used when a story is longer than max_word_count: instead of rejecting
it, we cut it into "Part 1", "Part 2", ... — each its own video,
published in order.
"""

import math
import re


def split_into_sentences(text: str) -> list[str]:
    """Naive sentence splitter (good enough for narration chunks)."""
    pieces = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in pieces if p.strip()]


def num_parts(word_count: int, max_words: int, max_parts: int) -> int | None:
    """How many parts this story needs. None = too long even to split."""
    n = max(1, math.ceil(word_count / max_words))
    if n > max_parts:
        return None
    return n


def split_text(text: str, n: int) -> list[str]:
    """Split text into n parts of roughly equal word count, on sentences."""
    if n <= 1:
        return [text.strip()]

    sentences = split_into_sentences(text)
    total_words = sum(len(s.split()) for s in sentences)
    target = total_words / n  # aim for this many words per part

    parts: list[str] = []
    current: list[str] = []
    current_words = 0

    for s in sentences:
        current.append(s)
        current_words += len(s.split())
        # Close this part once it reaches the target, unless it's the
        # last part (which gets whatever remains).
        if len(parts) < n - 1 and current_words >= target:
            parts.append(" ".join(current))
            current, current_words = [], 0

    if current:
        parts.append(" ".join(current))

    return parts
