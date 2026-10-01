"""
Monthly PDF import service for Setlist Builder.

Splits ONE monthly PDF -- like:

    MES DE OCTUBRE

    DOMINGO 4:
    1- Popurri Proezas (MSM)
    2- Libre soy (MDA)
    ...
    MIÉRCOLES 7:
    1- No callaré
    ...
    DOMINGO 25 (Servicio Evangelistico):
    1- Vamos a cantar
    ...

-- into one WeekSection per day header, each holding its own ordered
list of song titles. This is the multi-week counterpart to
ocr_service.extract_song_titles(), which only ever handles a single
numbered list from one screenshot.

WHY pypdf INSTEAD OF OCR HERE
-------------------------------
Unlike the single-screenshot flow (a photo of a printed list, which
genuinely needs Tesseract to read pixels), these monthly PDFs are
typed documents with a real text layer already in them -- pypdf (an
existing dependency, used elsewhere for merging chart PDFs) can read
that text directly. This is both simpler and far more accurate than
converting pages to images and running OCR on text that was never a
picture in the first place.

YEAR INFERENCE
---------------
The source PDF only ever says "MES DE OCTUBRE" -- no year. Since these
imports are always for upcoming services, we assume the *next*
occurrence of that month: the current year if the named month hasn't
finished yet, otherwise next year. This lives in `_infer_year()` and
takes `today` as a parameter (rather than calling datetime.date.today()
directly) so it can be tested against a fixed date.
"""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pypdf import PdfReader

from app.models.scheduled_task import Setlist
from app.models.song_entry import SongEntry
from app.services.database_service import add_song_entries, create_monthly_import, create_setlist
from app.utils.text_normalize import collapse_whitespace

logger = logging.getLogger(__name__)


_SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

# Recognized as a week's day-header, e.g. "DOMINGO 4:" -- see
# _DAY_HEADER_PATTERN below. Accent-stripped, lowercased.
_SPANISH_DAYS = {"domingo", "lunes", "martes", "miercoles", "jueves", "viernes", "sabado"}

# Matches: "MES DE OCTUBRE" (the one line naming the whole document's month).
_MONTH_HEADER_PATTERN = re.compile(r"^\s*MES\s+DE\s+([A-Za-zÁÉÍÓÚáéíóúÑñ]+)\s*$", re.IGNORECASE)

# Matches: "DOMINGO 4:", "MIÉRCOLES 7:", "DOMINGO 25 (Servicio Evangelistico):"
# Group 1: day name. Group 2: day-of-month number. Group 3: optional
# parenthetical note.
_DAY_HEADER_PATTERN = re.compile(
    r"^\s*([A-Za-zÁÉÍÓÚáéíóúÑñ]+)\s+(\d{1,2})\s*(?:\(([^)]*)\))?\s*:?\s*$"
)

# Same numbered-list pattern ocr_service.py uses -- the "1- Song Title"
# lines are identical in shape whether they came from a screenshot or a
# typed PDF, so there's no reason to duplicate a different regex here.
_NUMBERED_LINE_PATTERN = re.compile(r"^\s*\d+\s*[-–—]?\s*(.+?)\s*$")


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _join_split_digits(line: str) -> str:
    """
    Undo a PDF-export quirk seen in real files: bold/underlined header
    text (e.g. "DOMINGO 11:") sometimes gets exported with stray spaces
    injected between individual digits -- "DOMINGO 1 1 :" -- because of
    how the word processor kerns underlined/bold runs. This only ever
    splits digits that belong together (never touches letters), so it's
    safe to run on every line before header-matching: "DOMINGO 1 1 :"
    becomes "DOMINGO 11 :", while genuinely separate numbers elsewhere
    in a line stay separate since real songs don't have adjacent
    digit-space-digit runs in this position.
    """
    return re.sub(r"(?<=\d)\s+(?=\d)", "", line)


@dataclass
class WeekSection:
    """One day header's worth of songs, before being saved to the database."""

    day_name: str  # e.g. "Domingo" (title-cased, accents preserved)
    day_number: int  # e.g. 4
    note: Optional[str]  # e.g. "Servicio Evangelistico", or None
    service_date: datetime.date
    titles: list[str] = field(default_factory=list)


@dataclass
class MonthlySegmentationResult:
    """
    The full result of segmenting one monthly PDF.

    `warnings` collects every non-fatal issue found along the way (an
    empty week, a skipped unnumbered line, stray text before the first
    header) as plain, user-facing sentences -- the same information
    that's also logged, but structured so the UI can show them in an
    after-import summary dialog instead of only living in the log file.
    """

    month: int
    year: int
    weeks: list[WeekSection]
    warnings: list[str] = field(default_factory=list)


class PdfSegmentationError(ValueError):
    """
    Raised when the PDF doesn't look like the expected monthly-list
    format (e.g. no "MES DE ___" line found) -- distinct from ValueError
    so callers can catch it specifically and show a clear message
    instead of quietly failing.
    """


def _infer_year(month: int, today: datetime.date) -> int:
    """
    Assume the *next* occurrence of `month`: this year if it hasn't
    finished yet, otherwise next year. See module docstring.
    """
    return today.year if month >= today.month else today.year + 1


def _extract_lines(pdf_path: str) -> list[str]:
    """
    Read every page's text out of the PDF, in order, one line per entry.

    extraction_mode="layout" (rather than pypdf's default "plain") is
    what makes this reliable: "plain" extracts text in the PDF's
    internal drawing order and reconstructs line breaks heuristically
    from glyph positions, which -- depending on what produced the PDF --
    can merge an entire page into one giant line. "layout" instead
    preserves the page's visual line structure directly, which is what
    this parser's line-by-line header detection depends on.
    """
    reader = PdfReader(pdf_path)
    lines: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text(extraction_mode="layout") or ""
        page_lines = [line.strip() for line in text.splitlines() if line.strip()]
        logger.debug("Page %d: %d non-blank line(s)", page_number, len(page_lines))
        lines.extend(page_lines)
    return lines


def segment_monthly_pdf(pdf_path: str, today: Optional[datetime.date] = None) -> MonthlySegmentationResult:
    """
    Parse a monthly PDF into one WeekSection per day header.

    Raises PdfSegmentationError if no "MES DE ___" line is found -- that
    line is required to know which month/year every day header's dates
    belong to.
    """
    today = today or datetime.date.today()
    lines = _extract_lines(pdf_path)

    month: Optional[int] = None
    for line in lines:
        match = _MONTH_HEADER_PATTERN.match(line)
        if match:
            month_name = _strip_accents(match.group(1)).lower()
            month = _SPANISH_MONTHS.get(month_name)
            if month is not None:
                break

    if month is None:
        raise PdfSegmentationError(
            f"Couldn't find a 'MES DE ___' line naming a recognized Spanish month in {pdf_path}."
        )

    year = _infer_year(month, today)

    weeks: list[WeekSection] = []
    current: Optional[WeekSection] = None
    unrecognized_before_first_header: list[str] = []
    skipped_unnumbered_lines: list[str] = []

    for line in lines:
        # Only used to detect the header -- the original `line` (not this
        # joined version) is what actually gets stored/displayed, so a
        # legitimate note's wording is never altered by this step.
        day_match = _DAY_HEADER_PATTERN.match(_join_split_digits(line))
        if day_match:
            day_name_raw, day_number_raw, note = day_match.groups()
            day_name_key = _strip_accents(day_name_raw).lower()
            if day_name_key in _SPANISH_DAYS:
                day_number = int(day_number_raw)
                service_date = datetime.date(year, month, day_number)
                weekday_name = _weekday_name_for(service_date)
                if weekday_name != day_name_key:
                    logger.warning(
                        "%s %d is actually a %s in %d-%02d -- keeping the header's stated day name, "
                        "but double check this date is right.",
                        day_name_raw,
                        day_number,
                        weekday_name,
                        year,
                        month,
                    )
                current = WeekSection(
                    day_name=day_name_raw.strip().title(),
                    day_number=day_number,
                    note=collapse_whitespace(note) if note else None,
                    service_date=service_date,
                )
                weeks.append(current)
                continue

        if current is None:
            # Lines before the first day header (the "MES DE ___" line
            # itself, blank lines, etc.) -- nothing to attach them to.
            # The "MES DE ___" line itself is expected here; anything
            # else is worth surfacing, in case a header failed to match
            # for a reason this parser doesn't already know about.
            if not _MONTH_HEADER_PATTERN.match(line):
                unrecognized_before_first_header.append(line)
            continue

        song_match = _NUMBERED_LINE_PATTERN.match(line)
        if song_match:
            title = collapse_whitespace(song_match.group(1))
            if title:
                current.titles.append(title)
        else:
            # A non-blank line under a recognized header that ISN'T a
            # numbered "1- Song Title" line. Deliberately NOT treated as
            # a song title (same policy as ocr_service.extract_song_titles
            # for the single-screenshot flow) -- an un-numbered line is
            # more often a stray artifact (a sub-note, a page footer)
            # than an actual song, and silently guessing wrong here would
            # be worse than surfacing it so you can add it by hand.
            skipped_unnumbered_lines.append(f"{current.day_name} {current.day_number}: {line!r}")

    warnings: list[str] = []

    if unrecognized_before_first_header:
        message = (
            f"{len(unrecognized_before_first_header)} line(s) before the first day header were "
            f"ignored (no week to attach them to): {'; '.join(unrecognized_before_first_header)}"
        )
        logger.warning(message)
        warnings.append(message)

    for skipped in skipped_unnumbered_lines:
        message = f"Skipped a non-numbered line, not added as a song -- {skipped}"
        logger.warning(message)
        warnings.append(message)

    for week in weeks:
        if not week.titles:
            message = f"{week.day_name} {week.day_number} has no songs -- this week's card will start out empty."
            logger.warning(message)
            warnings.append(message)

    logger.info("Segmented %s into %d week(s) for %d-%02d.", pdf_path, len(weeks), year, month)
    return MonthlySegmentationResult(month=month, year=year, weeks=weeks, warnings=warnings)


_WEEKDAY_NAMES_BY_INDEX = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


def _weekday_name_for(date: datetime.date) -> str:
    """Accent-stripped, lowercased Spanish weekday name for `date`."""
    return _WEEKDAY_NAMES_BY_INDEX[date.weekday()]


@dataclass
class ImportOutcome:
    """
    What `import_monthly_pdf` hands back to the UI: the newly created
    Setlist rows (what the carousel loads to build its cards) plus any
    warnings collected during segmentation, so the caller can show them
    in an after-import summary dialog instead of them only reaching the
    log file.
    """

    setlists: list[Setlist]
    warnings: list[str]


def import_monthly_pdf(pdf_path: str, today: Optional[datetime.date] = None) -> ImportOutcome:
    """
    Segment `pdf_path` and persist everything to the database in one go:
    one MonthlyImport row, one Setlist row per week, and that week's
    songs as SongEntry rows underneath it.

    Returns an ImportOutcome: the newly created Setlist objects (in
    service-date order -- what the carousel UI will load to build its
    cards) plus any warnings from segmentation.
    """
    result = segment_monthly_pdf(pdf_path, today=today)

    monthly_import_id = create_monthly_import(
        source_path=str(Path(pdf_path)), month=result.month, year=result.year
    )

    setlists: list[Setlist] = []
    for week in result.weeks:
        setlist_id = create_setlist(
            source_type="monthly_pdf",
            service_date=week.service_date,
            monthly_import_id=monthly_import_id,
            day_name=week.day_name,
            note=week.note,
        )
        entries = [SongEntry(order=order, ocr_title=title) for order, title in enumerate(week.titles, start=1)]
        add_song_entries(setlist_id, entries)

        setlists.append(
            Setlist(
                id=setlist_id,
                monthly_import_id=monthly_import_id,
                source_type="monthly_pdf",
                service_date=week.service_date,
                day_name=week.day_name,
                note=week.note,
                created_at=datetime.datetime.now(),
            )
        )

    logger.info("Imported %d week(s) from %s into the database.", len(setlists), pdf_path)
    return ImportOutcome(setlists=setlists, warnings=result.warnings)
