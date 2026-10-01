"""
OCR service for Setlist Builder.

Extracts song titles from a playlist screenshot using Tesseract. The
real-world images look like:

    LISTA PARA EL
    DOMINGO 13 DE JULIO

    1- Dios subió a su trono
    2- Mi Cristo Vive
    3- Con júbilo y con gozo
    ...

EXTRACTION STRATEGY
--------------------
Rather than trying to specifically detect "this line is a header" or
"this line is a date," we only keep lines that start with a number
(the numbered list marker), and discard everything else. This is more
robust than pattern-matching against dates/headers directly: a header
like "DOMINGO 13 DE JULIO" contains a number too, but it doesn't start
with one followed by a separator, so it's naturally excluded without
needing separate date-detection logic. Real playlists have been observed
to number songs inconsistently ("1-", "5 -", "6-  "), so the pattern
below tolerates a dash or not, and any amount of whitespace around it.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import pytesseract
from PIL import Image

from app.utils.text_normalize import collapse_whitespace

logger = logging.getLogger(__name__)

# pytesseract shells out to the bare command "tesseract" by default,
# relying on PATH to find it -- which works fine from a Terminal (whose
# PATH includes Homebrew's /opt/homebrew/bin), but silently fails when
# launched from a double-clicked .app bundle, since Finder-launched
# processes get a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that
# doesn't include it. Same root cause already hit (and fixed the same
# way) for terminal-notifier in notification_action_listener.py and for
# launchd's own scheduler_runner.py -- resolve an absolute path once at
# import time instead of trusting PATH.
_TESSERACT_CANDIDATES = (
    "/opt/homebrew/bin/tesseract",  # Apple Silicon Homebrew
    "/usr/local/bin/tesseract",  # Intel Homebrew
)


def _resolve_tesseract_cmd() -> str:
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return "tesseract"  # let pytesseract raise its own clear TesseractNotFoundError


pytesseract.pytesseract.tesseract_cmd = _resolve_tesseract_cmd()

# Matches: optional leading whitespace, one or more digits, optional
# whitespace, an optional dash (-, en dash, or em dash), optional
# whitespace, then captures the rest of the line as the title.
#
# Examples this matches:
#   "1- Dios subió a su trono"   -> "Dios subió a su trono"
#   "5 - Nuestro Dios"           -> "Nuestro Dios"
#   "6-  Dios incomparable"      -> "Dios incomparable"
#   "2 Mi Cristo Vive"           -> "Mi Cristo Vive"   (no dash at all)
_NUMBERED_LINE_PATTERN = re.compile(r"^\s*\d+\s*[-–—]?\s*(.+?)\s*$")


def extract_song_titles(image_path: str, language: str = "spa+eng") -> list[str]:
    """
    Run OCR on a playlist screenshot and return the extracted song titles,
    in the order they appear in the image.

    Parameters
    ----------
    image_path:
        Path to the playlist screenshot (png/jpg/etc).
    language:
        Tesseract language pack(s) to use. Defaults to "spa+eng" (Spanish
        + English combined) since this church's playlists are in Spanish,
        but combining both makes the app resilient if an English song
        title or an English word ever shows up in a list without
        requiring the user to change a setting.

    Returns
    -------
    A list of song title strings. Lines that don't start with a number
    (headers, blank lines, stray OCR noise) are silently dropped -- see
    module docstring for why.
    """
    path = Path(image_path)
    if not path.exists():
        logger.error("Image file does not exist: %s", path)
        return []

    try:
        image = Image.open(path)
    except Exception as error:
        logger.error("Failed to open image %s: %s", path, error)
        return []

    try:
        raw_text = pytesseract.image_to_string(image, lang=language)
    except pytesseract.TesseractNotFoundError:
        logger.error(
            "Tesseract is not installed or not on PATH. Install it with "
            "'brew install tesseract tesseract-lang'."
        )
        return []

    logger.debug("Raw OCR output for %s:\n%s", path, raw_text)

    titles: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _NUMBERED_LINE_PATTERN.match(line)
        if not match:
            logger.debug("Ignoring non-numbered OCR line: %r", line)
            continue

        # Collapse any internal double-spaces OCR sometimes introduces
        # between words (reusing the same helper text_normalize uses).
        title = collapse_whitespace(match.group(1))

        if title:
            titles.append(title)

    logger.info("Extracted %d song title(s) from %s", len(titles), path)
    return titles
