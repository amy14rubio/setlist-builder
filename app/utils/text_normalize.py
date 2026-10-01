"""
Text normalization for song title matching.

RapidFuzz scores similarity based on the literal characters it's given.
Without normalization, "Dios subió a su trono" (from OCR) and
"dios subio a su trono" (a folder name) would be penalized for every
accented character and case difference, even though they're the same
title to a human reader. This module makes both sides directly comparable
before they're ever handed to RapidFuzz.
"""

from __future__ import annotations

import re
import unicodedata

# Matches any character that ISN'T a letter, digit, or whitespace -- i.e.
# punctuation like "¡", "¿", "!", "," etc. These add noise to matching
# without carrying meaningful information about which song it is.
_PUNCTUATION_PATTERN = re.compile(r"[^\w\s]", re.UNICODE)

# Matches runs of 2+ whitespace characters, so we can collapse them to a
# single space after removing punctuation (which can leave gaps behind).
_MULTIPLE_SPACES_PATTERN = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    """
    Remove accents/diacritics from text, leaving the base letters.

    Example: "subió" -> "subio", "Dios" -> "Dios" (unchanged, no accent).

    HOW THIS WORKS:
    unicodedata.normalize("NFKD", text) decomposes each accented character
    into two separate Unicode characters: the plain base letter, followed
    by a separate "combining mark" character representing just the accent.
    unicodedata.category(char) == "Mn" identifies those combining-mark
    characters ("Mn" = "Mark, nonspacing"), so we can filter them out and
    keep only the base letters.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def collapse_whitespace(text: str) -> str:
    """
    Collapse runs of 2+ whitespace characters into a single space, and
    trim leading/trailing whitespace. Exposed as its own public function
    (rather than just inlined inside normalize_for_matching) because other
    callers -- like the OCR service, cleaning up spacing artifacts in raw
    Tesseract output -- need this exact behavior without wanting the
    accent-stripping/case-folding/punctuation-removal that
    normalize_for_matching also does.
    """
    return _MULTIPLE_SPACES_PATTERN.sub(" ", text).strip()


def normalize_for_matching(text: str) -> str:
    """
    Fully normalize a song title for fuzzy matching purposes:
      1. Strip accents (á -> a, ñ -> n, etc.)
      2. Case-fold (more robust than .lower() for non-English text --
         handles edge cases like German ß correctly, where .lower() alone
         does not)
      3. Remove punctuation (¡, ¿, commas, etc.)
      4. Collapse repeated whitespace and trim the ends

    Two titles that are "the same song" to a human should produce
    identical strings after this function, regardless of how OCR or a
    folder name happened to capitalize, accent, or punctuate them.
    """
    text = strip_accents(text)
    text = text.casefold()
    text = _PUNCTUATION_PATTERN.sub("", text)
    return collapse_whitespace(text)
