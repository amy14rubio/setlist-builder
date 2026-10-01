"""
Matching service for Setlist Builder.

Given an OCR-extracted song title and a list of already-scanned library
entries, finds the best fuzzy match using RapidFuzz.

SCORER CHOICE: token_sort_ratio
--------------------------------
RapidFuzz offers several scoring functions. We use token_sort_ratio,
which splits each title into words, sorts them alphabetically, and THEN
compares -- so word order differences don't tank the score. This matters
because OCR can occasionally read multi-line or oddly-wrapped playlist
text in a scrambled order (e.g. "Vive Mi Cristo" instead of "Mi Cristo
Vive"); token_sort_ratio still recognizes these as the same title, while
a plain character-by-character comparison (fuzz.ratio) would not.
"""

from __future__ import annotations

import logging
from typing import Optional

from rapidfuzz import fuzz, process

from app.models.library_item import LibraryEntry
from app.models.song_entry import LibraryMatch
from app.utils.text_normalize import normalize_for_matching

logger = logging.getLogger(__name__)


def find_best_match(
    query_title: str, library: list[LibraryEntry]
) -> Optional[LibraryMatch]:
    """
    Find the single best-matching library entry for a given OCR title.

    Parameters
    ----------
    query_title:
        The song title as extracted by OCR.
    library:
        A list of LibraryEntry objects to search (from library_scanner).

    Returns
    -------
    A LibraryMatch with a confidence score (0-100), or None if the
    library is empty. Note this ALWAYS returns the best available
    candidate when the library is non-empty, even if the score is very
    low -- this function's job is just "what's the closest thing we
    have," not "is this good enough." That judgment call belongs to
    SongEntry's confidence threshold (see is_low_confidence /
    status), which is a user-configurable setting, not something this
    matching function should hardcode an opinion about.
    """
    if not library:
        logger.warning("Cannot match '%s': library is empty.", query_title)
        return None

    normalized_query = normalize_for_matching(query_title)

    # Build a normalized version of every library title to compare against.
    # We keep the original LibraryEntry list in the same order so we can
    # map the winning index straight back to its full entry afterward.
    normalized_choices = [normalize_for_matching(entry.title) for entry in library]

    result = process.extractOne(
        normalized_query, normalized_choices, scorer=fuzz.token_sort_ratio
    )

    if result is None:
        # extractOne can return None if, for example, every choice is an
        # empty string after normalization. Defensive, but worth handling
        # rather than letting an unpacking error happen below.
        logger.warning("No match found for '%s' in a non-empty library.", query_title)
        return None

    _matched_text, score, index = result
    matched_entry = library[index]

    logger.debug(
        "Matched '%s' -> '%s' (score=%.1f, folder=%s)",
        query_title,
        matched_entry.title,
        score,
        matched_entry.folder_path,
    )

    return LibraryMatch(
        title=matched_entry.title,
        folder_path=matched_entry.folder_path,
        confidence=score,
        bpm=matched_entry.bpm,
    )
