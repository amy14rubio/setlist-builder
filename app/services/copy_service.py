"""
Copy service for Setlist Builder.

Copies each matched song's audio folder from the Audio Library into a
weekly destination folder (e.g. Desktop/Secuencias). Originals are NEVER
modified -- this only ever reads from the library and writes into the
destination.

CONFLICT HANDLING
------------------
If a song's destination folder already exists (e.g. you're rebuilding
the same week's setlist a second time), this module does NOT decide what
to do on its own -- it calls the `on_conflict` function you pass in and
does whatever it says ("overwrite" or "skip").

This is deliberate: whether to ask the user, auto-overwrite, or auto-skip
is a UI/policy decision, not something a file-copying function should
hardcode an opinion about. By taking `on_conflict` as a parameter, this
function has zero dependency on PySide6 or any specific UI -- it can be
tested with a plain Python function standing in for a real dialog, and
reused later even if the UI changes completely.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from app.models.song_entry import SongEntry
from app.services.audio_normalizer import normalize_folder

logger = logging.getLogger(__name__)

# The callback's signature: given a song title, return "overwrite" or "skip".
ConflictHandler = Callable[[str], str]

OVERWRITE = "overwrite"
SKIP = "skip"


def _remove_appledouble_files(folder: Path) -> None:
    """
    Remove macOS AppleDouble metadata files (e.g. "._Some File.wav") from
    a folder.

    These are created automatically by macOS when copying files off
    certain external/non-native-format drives (confirmed: this library's
    audio folders live on an external drive) -- they store resource-fork
    metadata that has nowhere else to go on those filesystems. They're
    pure clutter once copied onto a native Mac drive, and worse: they
    share the exact same file extension as the real audio file they
    shadow (e.g. "._1 Click.wav" ends in .wav too), which silently
    corrupted the manifest by getting scooped up as a bogus extra "stack
    file" for every song copied from that drive.
    """
    for item in folder.iterdir():
        if item.is_file() and item.name.startswith("._"):
            item.unlink()
            logger.info("Removed AppleDouble metadata file: %s", item.name)


def copy_audio_folders(
    entries: list[SongEntry], destination_root: str, on_conflict: ConflictHandler
) -> None:
    """
    Copy each entry's matched audio folder into destination_root.

    Mutates each SongEntry's `output_folder` field in place to point at
    where its files ended up (or leaves it None if there was nothing to
    copy or the song was skipped due to a conflict).

    Parameters
    ----------
    entries:
        The reviewed/edited SongEntry list.
    destination_root:
        Folder to copy into (e.g. ~/Desktop/Secuencias). Created if it
        doesn't exist.
    on_conflict:
        Called with the song's title when its destination folder already
        exists. Must return "overwrite" or "skip".
    """
    destination = Path(destination_root)
    destination.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        if entry.audio_match is None:
            logger.warning(
                "Skipping copy for '%s': no audio match to copy.", entry.ocr_title
            )
            continue

        source = Path(entry.audio_match.folder_path)
        if not source.exists():
            logger.error(
                "Audio folder no longer exists on disk, skipping: %s", source
            )
            continue

        target = destination / source.name

        if target.exists():
            decision = on_conflict(entry.audio_match.title)
            if decision == SKIP:
                logger.info("Skipped '%s': destination already exists.", entry.audio_match.title)
                # The files are already there from a previous run -- point
                # the manifest at them anyway, since they DO exist at that
                # location, just not freshly copied this time. Still worth
                # cleaning/normalizing: if a previous run happened before
                # these fixes existed, these files may not have been
                # cleaned/normalized yet.
                entry.output_folder = str(target)
                _remove_appledouble_files(target)
                normalize_folder(str(target))
                continue
            elif decision == OVERWRITE:
                logger.info("Overwriting existing folder: %s", target)
                shutil.rmtree(target)
            else:
                # Defensive: an on_conflict implementation returned
                # something unexpected. Treat it as "skip" rather than
                # guessing, and log loudly so the bug gets noticed.
                logger.error(
                    "on_conflict returned unrecognized value %r for '%s'; "
                    "treating as skip.",
                    decision,
                    entry.audio_match.title,
                )
                entry.output_folder = str(target)
                _remove_appledouble_files(target)  # clean up junk from before this fix existed
                normalize_folder(str(target))
                continue

        shutil.copytree(source, target)
        entry.output_folder = str(target)
        logger.info("Copied '%s' -> %s", entry.audio_match.title, target)

        # Clean up macOS AppleDouble junk files BEFORE anything downstream
        # (normalization, the file categorizer, the manifest) ever looks
        # at these files -- they'd otherwise be mistaken for real audio.
        _remove_appledouble_files(target)

        # Normalize sample rate/bit depth BEFORE the file categorizer
        # runs -- an mp3->aif conversion changes a filename, so
        # everything after this point needs to see the final, accurate
        # names.
        normalize_folder(str(target))
