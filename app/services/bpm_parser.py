"""
BPM folder-name parser for Setlist Builder.

Audio and Logic library folders encode their title and (for audio) tempo
in the folder name itself, separated by " - ", e.g.:

    "Mi Cristo Vive - 74bpm"        -> title="Mi Cristo Vive", bpm=74
    "Hay Victoria - ???bpm"         -> title="Hay Victoria",   bpm=None
    "De Gloria En Gloria - copy from project" -> title="De Gloria En Gloria", bpm=None

DESIGN NOTE
-----------
Rather than requiring the literal word "bpm" to appear, this parser just
looks for the first run of digits anywhere after the " - " separator. Two
reasons:
  1. The confirmed real-world suffixes vary ("???bpm", "copy from
     project") — searching for digits handles all of them uniformly:
     digits present -> that's the BPM; no digits -> unknown BPM. No need
     to special-case each non-numeric phrase we might encounter.
  2. It's more forgiving of small formatting drift ("125 bpm", "125BPM",
     "125") without needing to update the pattern every time.

No decimal BPMs need to be supported (confirmed), so we match whole
numbers only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# The separator between a folder's title and its trailing info (BPM or
# otherwise). Note the spaces around the dash -- this deliberately does
# NOT match a bare "-" with no surrounding spaces, so titles that happen
# to contain hyphenated words (e.g. "Re-Encuentro") aren't mistakenly
# split apart.
_SEPARATOR = " - "

# Matches the first run of one or more digits, anywhere in a string.
_DIGITS_PATTERN = re.compile(r"\d+")


@dataclass
class ParsedFolderName:
    """The result of parsing a single library folder name."""

    title: str
    bpm: Optional[int]


def parse_folder_name(folder_name: str) -> ParsedFolderName:
    """
    Parse a library folder's name into a title and an optional BPM.

    Parameters
    ----------
    folder_name:
        Just the folder's own name (e.g. "Mi Cristo Vive - 74bpm"), NOT
        a full path. Pass `some_path.name` if you have a Path object.

    Returns
    -------
    ParsedFolderName with `bpm=None` whenever no digits are found in the
    suffix after " - " (covers "???bpm", "copy from project", a blank
    suffix, or any other non-numeric text).
    """
    folder_name = folder_name.strip()

    if _SEPARATOR not in folder_name:
        # Every folder in this library is expected to contain " - ", per
        # the established naming convention. If one doesn't, that's a
        # library organization problem worth knowing about -- log it
        # clearly rather than silently guessing, but don't crash the
        # whole scan over one oddly-named folder.
        logger.warning(
            "Folder name '%s' has no ' - ' separator; treating the whole "
            "name as the title with no BPM.",
            folder_name,
        )
        return ParsedFolderName(title=folder_name, bpm=None)

    # rsplit with maxsplit=1 splits on the LAST occurrence of " - ".
    # This matters if the title itself legitimately contains " - "
    # somewhere in it -- we want everything before the final separator
    # treated as the title, and only the trailing chunk checked for a BPM.
    title_part, suffix = folder_name.rsplit(_SEPARATOR, 1)
    title = title_part.strip()

    digits_match = _DIGITS_PATTERN.search(suffix)
    bpm = int(digits_match.group()) if digits_match else None

    if bpm is None:
        logger.debug("No BPM found in folder '%s' (suffix: '%s').", folder_name, suffix)

    return ParsedFolderName(title=title, bpm=bpm)
