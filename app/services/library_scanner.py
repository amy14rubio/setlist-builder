"""
Library scanner for Setlist Builder.

Reads the immediate subfolders of a library root (Audio Library or Logic
Library) and turns each one into a LibraryEntry, with its title and BPM
already parsed out of the folder name via bpm_parser.

This is intentionally the ONLY file in the matching pipeline that touches
the real filesystem. Everything downstream (matching_service.py) works
with plain LibraryEntry objects in memory, which means matching logic can
be tested with a hand-built list of fake entries -- no need to create
real folders on disk just to test a fuzzy-matching algorithm.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.models.library_item import LibraryEntry
from app.services.bpm_parser import parse_folder_name

logger = logging.getLogger(__name__)


def scan_library(root_path: str, expect_bpm_suffix: bool = True) -> list[LibraryEntry]:
    """
    Scan a library root folder and return a LibraryEntry for each
    immediate subfolder found.

    Only ONE level deep is scanned (not recursive) -- per the library
    layout described in the spec, song folders live directly inside the
    Audio/Logic Library root, not nested further. Hidden folders (macOS
    things like ".DS_Store" being a file, not a folder, but also things
    like ".Trash" if ever present) are skipped.

    Parameters
    ----------
    root_path:
        Path to the Audio Library or Logic Library root, as a string
        (this is how it's stored in Settings).
    expect_bpm_suffix:
        Whether folder names in this library are expected to follow the
        "Title - 125bpm" convention. Pass True for the Audio Library.
        Pass False for the Logic Library, whose project folders only
        contain a plain title with no tempo suffix at all -- passing
        False skips bpm_parser entirely and uses the folder name as-is,
        avoiding a flood of misleading "no separator found" warnings for
        folders that were never expected to have one.

    Returns
    -------
    A list of LibraryEntry objects. Returns an empty list (rather than
    raising) if the root path doesn't exist or isn't a directory --
    logged as an error, since this typically means a misconfigured path
    in Settings, which the UI should surface to the user rather than
    crashing the scan.
    """
    root = Path(root_path)

    if not root.exists():
        logger.error("Library path does not exist: %s", root)
        return []

    if not root.is_dir():
        logger.error("Library path is not a directory: %s", root)
        return []

    entries: list[LibraryEntry] = []

    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue  # skip stray files sitting in the library root
        if item.name.startswith("."):
            continue  # skip hidden folders (e.g. macOS system folders)

        if expect_bpm_suffix:
            parsed = parse_folder_name(item.name)
            entries.append(
                LibraryEntry(title=parsed.title, folder_path=str(item), bpm=parsed.bpm)
            )
        else:
            entries.append(
                LibraryEntry(title=item.name.strip(), folder_path=str(item), bpm=None)
            )

    logger.info("Scanned %s: found %d song folder(s).", root, len(entries))
    return entries
