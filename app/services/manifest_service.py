"""
Manifest service for Setlist Builder.

Generates the Build Manifest JSON -- the handoff file logic_automation.py
reads to actually build the Logic Pro rehearsal project. This is the
final output of the Python "brain"; the automation itself never
computes song ordering, file categorization, or the like -- it just
follows what's already decided here.

SCHEMA VERSIONING
------------------
The manifest includes a "schemaVersion" field. Costs nothing today, but
means that if this schema needs to change in six months (a new field, a
renamed field), logic_automation.py can check the version and know
whether it's looking at a manifest shape it understands, rather than
guessing based on which fields happen to be present.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from app.models.settings import Settings
from app.models.song_entry import SongEntry
from app.services.song_file_categorizer import categorize_song_files

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 2


def build_manifest(entries: list[SongEntry], settings: Settings) -> dict:
    """
    Build the manifest dictionary from the (reviewed, possibly
    hand-edited) list of SongEntry objects. Does NOT write anything to
    disk -- see write_manifest() for that. Kept separate so the manifest
    contents can be inspected/tested without touching the filesystem.
    """
    songs = [_song_to_manifest_entry(entry, settings) for entry in entries]
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "songs": songs,
    }


def _song_to_manifest_entry(entry: SongEntry, settings: Settings) -> dict:
    # Prefer the matched library title over the raw OCR title, since the
    # library title is the "official" spelling/capitalization -- the OCR
    # title was only ever a means to find that match, not the source of
    # truth for how the song's name should appear downstream.
    title = entry.audio_match.title if entry.audio_match else entry.ocr_title

    # Only songs that were actually copied (output_folder set) have real
    # files on disk to categorize. Songs that weren't copied yet (or
    # couldn't be) get empty placeholders rather than crashing here --
    # the manifest should always be generatable, even from a partial run.
    if entry.output_folder:
        file_breakdown = categorize_song_files(entry.output_folder)
    else:
        file_breakdown = {"clickFile": None, "guideFile": None, "stackFiles": []}

    logic_project_path = entry.logic_match.folder_path if entry.logic_match else None

    # Logic's window title follows the pattern "{ProjectFilename} - Tracks"
    # (confirmed by testing, e.g. "Agradecido.logicx" -> "Agradecido - Tracks").
    # Computed here rather than in logic_automation.py, so the automation
    # can just wait for this exact string rather than trying to derive it
    # itself.
    logic_project_window_title = None
    if logic_project_path:
        project_name = Path(logic_project_path).stem  # strips the .logicx extension
        logic_project_window_title = f"{project_name} - Tracks"

    return {
        "order": entry.order,
        "title": title,
        "tempo": entry.audio_bpm,
        "logicTempo": entry.logic_tempo,
        "needsTempoCopy": entry.needs_tempo_copy,
        "audioFolder": entry.audio_match.folder_path if entry.audio_match else None,
        "logicProject": logic_project_path,
        "logicProjectWindowTitle": logic_project_window_title,
        "outputFolder": entry.output_folder,
        "trackSpacingBars": settings.track_spacing_bars,
        "clickFile": file_breakdown["clickFile"],
        "guideFile": file_breakdown["guideFile"],
        "stackFiles": file_breakdown["stackFiles"],
        "chartPdf": entry.chart_pdf_path,
    }


def write_manifest(manifest: dict, output_path: str) -> None:
    """Write the manifest dict to disk as formatted, UTF-8 JSON."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        # ensure_ascii=False keeps accented characters (á, ñ, etc.)
        # readable in the file instead of escaped as \u00e1.
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    logger.info("Build manifest written to %s", path)