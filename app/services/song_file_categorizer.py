"""
Song file categorizer for Setlist Builder.

After Copy Audio places a song's files into its destination folder, this
splits them into: the Click file, the Guia (guide) file, and everything
else (the song's individual instrument/vocal tracks, which get grouped
into a Track Stack in Logic).

Files are expected to follow the user's existing library numbering
convention, e.g. "1 Click - Song.wav", "2 Guia - Song.wav",
"3 Bass - Song.wav", ... The leading number is ALSO used to determine
import order for the "stack" files, since the folders are already
numbered in the order the user wants them imported -- no need to invent
a separate ordering scheme.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_LEADING_NUMBER_PATTERN = re.compile(r"^(\d+)\s")

_AUDIO_EXTENSIONS = {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".caf"}

_CLICK_NUMBER = 1
_GUIDE_NUMBER = 2


def categorize_song_files(folder_path: str) -> dict:
    """
    Scan a song's copied folder and split its audio files into the click
    file, the guide file, and "everything else" (stack files), in import
    order.

    Returns a dict with keys "clickFile", "guideFile", "stackFiles" --
    FULL ABSOLUTE PATHS, not just filenames. This matters for how the
    files actually get imported: Logic's file picker (opened via
    Cmd+Shift+G) accepts a complete file path pasted directly into its
    "Go to Folder" field, which both navigates there AND pre-selects that
    exact file -- one Return imports it. Handing logic_automation.py a
    ready-to-paste absolute path means it never needs to separately
    combine a folder with a filename itself.
    """
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        logger.warning("Cannot categorize files -- folder does not exist: %s", folder)
        return {"clickFile": None, "guideFile": None, "stackFiles": []}

    numbered_files: list[tuple[float, Path]] = []
    for item in sorted(folder.iterdir()):
        if not item.is_file():
            continue
        if item.name.startswith("."):
            # Defensive: skip hidden files (macOS AppleDouble junk like
            # "._Song.wav", .DS_Store, etc.) even though copy_service
            # should already have removed these -- belt-and-suspenders,
            # since these files share real audio extensions and would
            # otherwise be silently miscategorized as legitimate stack files.
            continue
        if item.suffix.lower() not in _AUDIO_EXTENSIONS:
            continue

        match = _LEADING_NUMBER_PATTERN.match(item.name)
        if not match:
            logger.warning(
                "File '%s' in %s has no leading number -- treating as an "
                "unordered stack file (will sort last).",
                item.name,
                folder,
            )
            numbered_files.append((float("inf"), item))
            continue

        numbered_files.append((int(match.group(1)), item))

    numbered_files.sort(key=lambda pair: pair[0])

    click_file: str | None = None
    guide_file: str | None = None
    stack_files: list[str] = []

    for number, path_obj in numbered_files:
        full_path = str(path_obj)
        if number == _CLICK_NUMBER and click_file is None:
            click_file = full_path
        elif number == _GUIDE_NUMBER and guide_file is None:
            guide_file = full_path
        else:
            if number == _CLICK_NUMBER:
                logger.warning("Duplicate click file '%s' in %s -- adding to stack instead.", path_obj.name, folder)
            elif number == _GUIDE_NUMBER:
                logger.warning("Duplicate guide file '%s' in %s -- adding to stack instead.", path_obj.name, folder)
            stack_files.append(full_path)

    if click_file is None:
        logger.warning("No click file (number 1) found in %s", folder)
    if guide_file is None:
        logger.warning("No guide file (number 2) found in %s", folder)

    return {"clickFile": click_file, "guideFile": guide_file, "stackFiles": stack_files}