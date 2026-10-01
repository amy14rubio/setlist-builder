"""
Settings model for Setlist Builder.

This module defines the persistent configuration the app needs to remember
between launches: library paths, output folders, and a few tunable defaults.

WHERE SETTINGS LIVE ON DISK
----------------------------
We store settings.json under:

    ~/Library/Application Support/SetlistBuilder/settings.json

This is the standard macOS convention for "app data that isn't the app
itself." Keeping it outside the project folder means:
  - You can move/rename the setlist_builder/ project folder without losing
    your configured library paths.
  - If you ever package this as a real .app bundle, settings still work
    the same way.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from app.utils.text_normalize import normalize_for_matching

logger = logging.getLogger(__name__)


def _match_day_key(configured: dict, day_name: Optional[str]) -> Optional[str]:
    """
    Find the configured Drive-folder / YouTube-playlist name that means
    the same day as `day_name`, ignoring case and accents.

    The two sides are written by different hands and never matched
    literally: a monthly PDF's header gives "Domingo"/"Miércoles" (title
    case, accented -- see pdf_import_service), while these dictionaries
    are keyed by whatever you typed in Settings ("domingo", "miércoles").
    A plain dict lookup misses every one of those pairs, which is why
    the old day_name fallback could never actually resolve a target.

    Returns the configured name EXACTLY as configured (not the
    normalized form), since that's the key the rest of the app looks up.
    """
    if not day_name:
        return None

    wanted = normalize_for_matching(day_name)
    for configured_name in configured:
        if normalize_for_matching(configured_name) == wanted:
            return configured_name
    return None


def _settings_dir() -> Path:
    """
    Return the folder where settings.json should live, creating it if needed.

    Path.home() gives us the current user's home folder (e.g. /Users/yourname)
    in a way that works regardless of who is logged in — never hardcode a
    username into a path.
    """
    directory = Path.home() / "Library" / "Application Support" / "SetlistBuilder"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _settings_file() -> Path:
    """Full path to the settings.json file itself."""
    return _settings_dir() / "settings.json"


@dataclass
class Settings:
    """
    Holds every path and tunable value the app needs to remember.

    WHY A DATACLASS?
    A @dataclass is a plain Python class whose entire job is to hold data.
    Writing `@dataclass` above a class automatically generates the boring
    boilerplate for us: __init__, __repr__ (so it prints nicely), and
    __eq__ (so two Settings objects with the same values compare equal).
    Without @dataclass we'd have to hand-write all of that ourselves.

    Every field below has a default, so `Settings()` with no arguments
    gives you a valid (if empty) settings object — useful on first launch,
    before the user has configured anything yet.
    """

    audio_library_path: str = ""
    logic_library_path: str = ""
    desktop_output_folder: str = ""
    weekly_project_folder: str = ""
    logic_template_project: str = ""

    # Default spacing (in bars) between songs when Logic automation is
    # eventually built. Exposed here now so it's editable/persisted from
    # day one, even though nothing consumes it yet.
    track_spacing_bars: int = 30

    # Minimum RapidFuzz confidence (0-100) below which a match should be
    # flagged for manual review rather than trusted automatically.
    match_confidence_threshold: int = 75

    # Named YouTube playlists (e.g. "Lista de Domingo" -> its playlist
    # ID), so each one only ever needs to be entered once. A dict rather
    # than a single ID, since you maintain several distinct playlists
    # (Sunday, Wednesday, Youth Group) and switch between them.
    youtube_playlists: dict = field(default_factory=dict)

    # The external site scraped to look up each song's YouTube video
    # (see app/services/video_lookup_service.py) -- blank by default,
    # since this only works once you point it at a site that lists
    # songs with linked YouTube videos AND happens to share the page
    # structure app/services/video_lookup_service.py's parser expects.
    # A real limitation of this feature, not something the URL alone
    # fixes for an arbitrary site.
    video_lookup_url: str = ""

    # Path to the OAuth "client secret" JSON file downloaded from Google
    # Cloud Console. Needed once to authenticate as you; after the first
    # successful login, a separate token file (stored in Application
    # Support, not configured here) is reused automatically.
    youtube_client_secret_path: str = ""

    # Path to a SEPARATE OAuth "client secret" JSON for Google Drive
    # access (chart downloads). Kept separate from the YouTube one since
    # it's a distinct Google Cloud OAuth client with a narrower,
    # read-only Drive scope -- blank until configured in Settings.
    google_drive_client_secret_path: str = ""

    # Drive folder ID containing one Google Doc chart per song (e.g. a
    # chord chart). Copy this from the folder's URL, the part after
    # 'folders/'. Used by the Charts page to list and fuzzy-match every
    # song against this one folder, the same way the external site is
    # scraped and matched for the YouTube workflow.
    google_drive_charts_folder_id: str = ""

    # Named chart download folders (e.g. "Miércoles" -> its folder path),
    # so each one only ever needs to be picked/browsed-to once. A dict
    # rather than a single path, mirroring youtube_playlists above --
    # you maintain several distinct weekly services (Sunday, Wednesday,
    # Youth Group) and switch between them on the Charts page. Each
    # song's individually downloaded chart PDF is written into whichever
    # of these folders is currently selected. Deliberately separate from
    # desktop_output_folder/weekly_project_folder -- the Charts workflow
    # is independent of the Logic Pro pipeline (no Copy Audio step, no
    # per-song output_folder), so it needs destinations that don't
    # depend on that pipeline having run at all.
    chart_folders: dict = field(default_factory=dict)

    # Where the final merged setlist PDF gets moved/written -- kept
    # separate from chart_folders (deliberately a single fixed folder,
    # not a named list) since you may want the raw per-song downloads to
    # stay wherever that week's chart_folders entry points, while the
    # finished packet always lands in the same one place (e.g. a shared
    # folder the whole team can access) regardless of which folder was
    # selected for downloads.
    charts_merged_output_folder: str = ""

    # The last date/time picked in ANY Schedule dialog (Drive, YouTube,
    # or Logic Pro), any week -- stored as an ISO datetime string (or ""
    # before you've ever scheduled anything). ScheduleDialog defaults to
    # this instead of "now + 1 hour" so picking, say, Sep 28 9am for
    # Drive makes YouTube's and Logic Pro's dialogs default to that same
    # date/time next, per direct feedback -- global, not per-week/setlist.
    last_scheduled_at: str = ""

    # The time of day (24h "HH:MM") used when auto-computing a
    # schedule's default date -- see
    # scheduling_service.compute_default_schedule_datetime: a setlist's
    # notification is suggested for the Monday that starts its service
    # week, at this time. One shared default rather than per-week, per
    # direct feedback.
    default_schedule_time: str = "09:00"

    @classmethod
    def load(cls) -> "Settings":
        """
        Load settings from disk, or return sensible defaults if no
        settings file exists yet (e.g. first time the app is ever run).
        """
        settings_path = _settings_file()

        if not settings_path.exists():
            logger.info("No settings file found at %s — using defaults.", settings_path)
            return cls()

        try:
            with settings_path.open("r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except (json.JSONDecodeError, OSError) as error:
            # If the file exists but is corrupted or unreadable, we do NOT
            # want to crash the whole app on launch. Log it and fall back
            # to defaults instead — the user can reconfigure paths.
            logger.error("Failed to read settings file (%s): %s", settings_path, error)
            return cls()

        # Only pass along keys that Settings actually knows about. This
        # protects us if an older settings.json has fields we've since
        # removed, or is missing fields we've since added — either way,
        # cls(**known_fields) fills in defaults for anything missing.
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in raw_data.items() if k in known_fields}

        return cls(**filtered_data)

    def save(self) -> None:
        """Write the current settings to disk as formatted JSON."""
        settings_path = _settings_file()

        try:
            with settings_path.open("w", encoding="utf-8") as f:
                # asdict() converts our dataclass into a plain dict so
                # json.dump can serialize it. indent=2 keeps the file
                # human-readable if you ever want to inspect/edit it by hand.
                json.dump(asdict(self), f, indent=2, ensure_ascii=False)
            logger.info("Settings saved to %s", settings_path)
        except OSError as error:
            logger.error("Failed to save settings to %s: %s", settings_path, error)
            raise

    def is_configured(self) -> bool:
        """
        Quick check for whether the required library paths have been set.

        Used later by the UI to decide whether to show a "please configure
        your libraries first" prompt instead of the main workflow.
        """
        required = (self.audio_library_path, self.logic_library_path)
        return all(bool(path) for path in required)

    def chart_folder_name_for_day(self, day_name: Optional[str]) -> Optional[str]:
        """The configured Drive chart folder whose name means this day, if any -- see _match_day_key."""
        return _match_day_key(self.chart_folders, day_name)

    def playlist_name_for_day(self, day_name: Optional[str]) -> Optional[str]:
        """The configured YouTube playlist whose name means this day, if any -- see _match_day_key."""
        return _match_day_key(self.youtube_playlists, day_name)
