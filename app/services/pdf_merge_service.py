"""
PDF merge service for Setlist Builder.

Combines every song's downloaded chart PDF (see drive_service.py /
ChartsPage) into a single PDF for the whole setlist, in setlist order --
the printable rehearsal packet.

Reads directly from a Build Manifest dict (see manifest_service.py)
rather than taking a raw file list, so "what order do charts go in" has
exactly one source of truth: the manifest's "order" field, the same
field logic_automation.py uses to sequence Logic Pro tracks. A song
with no chart downloaded yet (chartPdf is None, or its file went
missing) is skipped with a warning rather than failing the whole merge
-- one missing chart shouldn't block printing the rest.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pypdf import PdfWriter
from pypdf.errors import PdfReadError

logger = logging.getLogger(__name__)


def merge_charts_from_manifest(manifest: dict, output_path: str) -> int:
    """
    Merge each song's chart PDF into one combined PDF at output_path, in
    manifest "order".

    Returns the number of charts actually merged. Does not write
    output_path at all if zero charts were available to merge, rather
    than producing an empty/meaningless PDF.
    """
    songs = sorted(manifest.get("songs", []), key=lambda song: song["order"])

    writer = PdfWriter()
    merged_count = 0

    for song in songs:
        title = song.get("title", "(untitled)")
        chart_pdf = song.get("chartPdf")

        if not chart_pdf:
            logger.warning("No chart PDF for '%s' -- skipping in merge.", title)
            continue

        if not Path(chart_pdf).exists():
            logger.warning(
                "Chart PDF for '%s' no longer exists on disk (%s) -- skipping.", title, chart_pdf
            )
            continue

        try:
            writer.append(chart_pdf)
            merged_count += 1
        except PdfReadError as error:
            logger.error("Failed to read chart PDF for '%s' (%s): %s", title, chart_pdf, error)

    if merged_count == 0:
        logger.warning("No chart PDFs available to merge -- not writing %s", output_path)
        writer.close()
        return 0

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        writer.write(f)
    writer.close()

    logger.info("Merged %d chart PDF(s) into %s", merged_count, path)
    return merged_count
