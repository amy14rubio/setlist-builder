"""
Audio normalizer for Setlist Builder.

After Copy Audio places a song's files into its destination folder, this
normalizes every audio file's sample rate and bit depth to a single
consistent target (48kHz / 24-bit), so Logic Pro never shows its "sample
rate mismatch" dialog on import -- a real problem since that dialog
requires a mouse click we can't reliably automate.

This ONLY ever touches the copies in the destination folder, never the
original library files -- consistent with Copy Audio's existing promise
to never modify originals. Every new song added to the library in the
future gets normalized automatically the same way, with no manual step
ever required again.

FORMAT HANDLING
----------------
Confirmed via `afconvert -hf` that MP3 cannot be written back out as a
standalone .mp3 file -- Apple's tools only support MP3 as an INPUT
format, or nested inside other containers (m4a, caf) we don't want here.
So any .mp3 source gets converted to .aif instead, and the original .mp3
is deleted afterward -- otherwise both files would sit in the folder
taking up space for no reason.

.aif/.aiff and .wav files are converted "in place" -- same filename,
same extension, just re-encoded to the target sample rate/bit depth --
via a temporary output file that then replaces the original. Writing a
file to itself mid-conversion risks corrupting it, so the safe order is:
convert to temp -> confirm success -> delete original -> rename temp in.

IDEMPOTENCY
------------
Before converting a PCM file (.aif/.wav), this checks its CURRENT sample
rate via `afinfo`. If it's already 48000 Hz, conversion is skipped
entirely -- since many songs get reused week to week, this avoids
needlessly re-encoding files that are already correct. MP3 files are
always converted regardless, since the format itself needs to change to
AIFF no matter what sample rate the source reports.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TARGET_SAMPLE_RATE = 48000
_TARGET_BIT_DEPTH = 24

_PCM_EXTENSIONS = {".aif", ".aiff", ".wav"}
_MP3_EXTENSIONS = {".mp3"}

_SAMPLE_RATE_PATTERN = re.compile(r"(\d+)\s*Hz")


def _get_current_sample_rate(file_path: Path) -> Optional[int]:
    """
    Return the file's current sample rate in Hz, via `afinfo`, or None
    if it couldn't be determined (afinfo missing, unexpected output,
    etc.). Failing to determine the rate is treated as "needs conversion"
    by the caller, rather than silently skipping a file we're unsure about.
    """
    try:
        result = subprocess.run(
            ["afinfo", str(file_path)], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        logger.error("afinfo not found -- audio normalization only works on macOS.")
        return None

    if result.returncode != 0:
        logger.warning("afinfo failed for %s: %s", file_path, result.stderr.strip())
        return None

    match = _SAMPLE_RATE_PATTERN.search(result.stdout)
    if not match:
        logger.warning("Could not parse sample rate from afinfo output for %s", file_path)
        return None

    return int(match.group(1))


def normalize_audio_file(file_path: str) -> str:
    """
    Normalize a single audio file to 48kHz/24-bit, converting MP3 to
    AIFF if necessary, and deleting the old file once the new one is
    confirmed written so no duplicate/stale copies linger.

    Returns the file's FINAL path -- unchanged if it was already correct
    or if normalization failed (fails safe: leaves the original file
    untouched rather than risk losing it), or the new .aif path if an
    .mp3 was converted.
    """
    path = Path(file_path)
    extension = path.suffix.lower()

    if extension in _PCM_EXTENSIONS:
        current_rate = _get_current_sample_rate(path)
        if current_rate == _TARGET_SAMPLE_RATE:
            logger.debug("Skipping %s -- already at %d Hz.", path.name, _TARGET_SAMPLE_RATE)
            return str(path)

        data_format = "BEI24" if extension in (".aif", ".aiff") else "LEI24"
        file_format = "AIFF" if extension in (".aif", ".aiff") else "WAVE"
        final_path = path

    elif extension in _MP3_EXTENSIONS:
        data_format = "BEI24"
        file_format = "AIFF"
        final_path = path.with_suffix(".aif")

    else:
        logger.warning("Unsupported audio extension '%s' for %s -- leaving as-is.", extension, path)
        return str(path)

    temp_output = path.with_name(path.name + ".converting.tmp")

    try:
        result = subprocess.run(
            [
                "afconvert",
                "-d", f"{data_format}@{_TARGET_SAMPLE_RATE}",
                "-f", file_format,
                str(path),
                str(temp_output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        logger.error("afconvert not found -- audio normalization only works on macOS.")
        return str(path)

    if result.returncode != 0 or not temp_output.exists():
        logger.error("Failed to normalize %s: %s", path, result.stderr.strip())
        temp_output.unlink(missing_ok=True)
        return str(path)  # fail safe -- original file is untouched

    # Success: remove the old file and put the new one in its place.
    # This is the step that actually reclaims the storage space -- without
    # it, both the pre-conversion file and the new one would coexist.
    path.unlink()
    temp_output.rename(final_path)

    logger.info(
        "Normalized %s -> %s (%d Hz, %d-bit)",
        path.name, final_path.name, _TARGET_SAMPLE_RATE, _TARGET_BIT_DEPTH,
    )
    return str(final_path)


def normalize_folder(folder_path: str) -> None:
    """
    Normalize every audio file directly inside folder_path (non-recursive,
    matching how library_scanner/song_file_categorizer also treat these
    folders as flat). Called once per song, right after Copy Audio places
    that song's files into its destination folder -- BEFORE the file
    categorizer runs, since an mp3->aif conversion changes a filename,
    and the categorizer needs to see final, accurate filenames.
    """
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        logger.warning("Cannot normalize -- folder does not exist: %s", folder)
        return

    all_extensions = _PCM_EXTENSIONS | _MP3_EXTENSIONS
    for item in sorted(folder.iterdir()):
        if item.is_file() and item.suffix.lower() in all_extensions:
            normalize_audio_file(str(item))
