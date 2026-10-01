"""
SongEntry data model for Setlist Builder.

This is the central object of the whole app: one SongEntry represents a
single row in the setlist, from the moment OCR extracts a title through
matching, review, copying, and manifest generation. Every stage of the
pipeline reads from and writes to SongEntry objects.

DESIGN NOTE ON DERIVED DATA
----------------------------
`status`, `needs_tempo_copy`, and the `is_missing_*` flags below are all
@property/method values, NOT fields you set directly -- they're
calculated fresh from the actual data every time they're read, so the
displayed status can never disagree with the real state of the song.

A NOTE ON `logic_tempo` AND WHAT A BLANK VALUE MEANS
------------------------------------------------------
Most Logic projects here have one single, fixed tempo -- you check the
project in Logic and type that number in. A blank `logic_tempo`
specifically means the project's tempo VARIES internally (e.g. ramps
from 140 down to 80) -- there simply isn't one number to report. That's
meaningful information, not "haven't checked yet": a fixed-tempo audio
recording will almost always need adjustment against a variable-tempo
project, so `needs_tempo_copy` treats a blank logic_tempo as "yes,
definitely needs a tempo copy" rather than "unknown."

`manually_verified` is a separate escape hatch: after you've reviewed a
song and you're satisfied it's fine (even if some automatic check still
flags a specific detail), this lets the overall Status say "OK" without
requiring every underlying flag to be individually resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SongStatus(Enum):
    """
    Overall summary status shown in the Review table's Status column.

    Kept deliberately simple (just two values) because a song can have
    several *specific* problems at once (missing BPM AND low confidence,
    for example) — those specifics are exposed separately as boolean
    properties on SongEntry, so the UI can highlight the exact problem
    cells. This enum is just the "does this row need my attention?" summary.
    """

    OK = "OK"
    NEEDS_REVIEW = "Needs Review"


@dataclass
class LibraryMatch:
    """
    A single fuzzy-match result against either the Audio Library or the
    Logic Library. Both libraries produce this same shape, so one class
    covers both cases rather than duplicating near-identical classes.

    `bpm` is only meaningful for Audio Library matches — Logic project
    folders in this church's setup only encode a title, not a tempo, so
    `bpm` stays None for logic_match entries. See `logic_tempo` on
    SongEntry for how the Logic side's tempo is captured (manually --
    it lives inside the .logicx file itself).
    """

    title: str
    folder_path: str
    confidence: float  # RapidFuzz score, 0-100
    bpm: Optional[int] = None


@dataclass
class YouTubeMatch:
    """
    A single fuzzy-match result against the configured video lookup
    site's song list, used by the (entirely separate) YouTube playlist
    workflow.

    Stored directly on SongEntry -- the same way LibraryMatch is -- so
    that a manual correction made via the dropdown survives a table
    rebuild (e.g. after Add/Remove Song), rather than being recomputed
    from scratch every time and silently discarding the user's fix.
    """

    title: str
    video_id: str
    confidence: float


@dataclass
class ChartMatch:
    """
    A single fuzzy-match result against the Google Drive charts folder
    (one Google Doc per song, e.g. a chord chart), used by the (entirely
    separate) Charts workflow.

    Stored directly on SongEntry -- same reasoning as YouTubeMatch -- so
    a manual correction via the dropdown survives a table rebuild.
    """

    title: str
    doc_id: str
    confidence: float


@dataclass
class SongEntry:
    """
    One row of the setlist, tracked from OCR through to the Build Manifest.

    `order` is the song's position in the setlist (1, 2, 3, ...) as it
    appeared in the original playlist image. Kept as an explicit field
    (rather than just relying on list position) so that reordering songs
    in the Review screen later is a simple matter of editing this number,
    without needing to physically move the object around in memory.
    """

    order: int
    ocr_title: str

    audio_match: Optional[LibraryMatch] = None
    logic_match: Optional[LibraryMatch] = None

    # Manually entered after checking the Logic project. None means the
    # project's tempo VARIES internally (no single number applies) --
    # see module docstring.
    logic_tempo: Optional[int] = None

    # Set once you've reviewed a song and are satisfied it's correct,
    # even if an automatic check still has a specific detail flagged.
    # Overrides the overall Status column to OK without hiding or
    # resolving the underlying flags themselves.
    manually_verified: bool = False

    # The matched video (from the configured lookup site), for the (entirely separate)
    # YouTube playlist workflow. None means "not matched yet" -- once
    # set (whether automatically or by hand), it's left alone by future
    # table rebuilds, so a manual correction never silently reverts.
    youtube_match: Optional[YouTubeMatch] = None

    # Filled in later, once Copy Audio actually runs.
    output_folder: Optional[str] = None

    # The matched Google Drive chart doc, for the (entirely separate)
    # Charts workflow. None means "not matched yet" -- same persistence
    # reasoning as youtube_match above.
    chart_match: Optional[ChartMatch] = None

    # Filled in once the matched chart has actually been downloaded and
    # exported as a PDF into output_folder. None until that download
    # step runs (or if it hasn't been matched at all yet).
    chart_pdf_path: Optional[str] = None

    notes: str = ""

    # Per-destination inclusion: each of Drive/YouTube/Logic Pro tracks
    # its OWN songs independently now (removing a song from Drive's list
    # must never remove it from YouTube's or Logic Pro's -- see
    # app/ui/widgets/entry_table_row_ops.py). False (the default) means
    # "included" -- a freshly OCR'd entry starts included everywhere,
    # matching the old shared-list behavior, until a specific tab's
    # Remove button excludes it from just that one destination.
    excluded_from_drive: bool = False
    excluded_from_youtube: bool = False
    excluded_from_logic: bool = False

    # ------------------------------------------------------------------
    # Derived / computed properties.
    # ------------------------------------------------------------------

    @property
    def audio_bpm(self) -> Optional[int]:
        """The BPM detected from the matched audio folder's name, if any."""
        return self.audio_match.bpm if self.audio_match else None

    @property
    def is_missing_audio(self) -> bool:
        """True if no Audio Library match has been found for this song."""
        return self.audio_match is None

    @property
    def is_missing_logic(self) -> bool:
        """True if no Logic Library match has been found for this song."""
        return self.logic_match is None

    @property
    def is_missing_bpm(self) -> bool:
        """True if we have an audio match but couldn't parse a BPM from it."""
        return self.audio_match is not None and self.audio_match.bpm is None

    @property
    def needs_tempo_copy(self) -> Optional[bool]:
        """
        Whether a tempo-adjusted copy of this song's audio will be needed.

        - If logic_tempo is None, the Logic project's tempo varies
          internally -- a fixed-tempo audio recording will almost always
          need adjustment against it, so this returns True.
        - If logic_tempo is a specific number, compare it directly
          against the audio's detected BPM. Returns None (genuinely
          unknown) only if the audio BPM itself hasn't been determined
          yet, since there's nothing to compare against in that case.
        """
        if self.logic_tempo is None:
            return True
        if self.audio_bpm is None:
            return None
        return self.audio_bpm != self.logic_tempo

    def is_low_confidence(self, threshold: int) -> bool:
        """
        True if either match's confidence score falls below the given
        threshold. Takes `threshold` as a parameter (from Settings)
        rather than hardcoding it here, since the acceptable confidence
        level is a user preference, not a fact about the song itself.

        A missing match (None) does NOT count as "low confidence" here —
        that's a separate, more specific problem covered by
        is_missing_audio / is_missing_logic. Conflating "no match found"
        with "found a bad match" would make the two problems harder to
        tell apart in the UI.
        """
        if self.audio_match is not None and self.audio_match.confidence < threshold:
            return True
        if self.logic_match is not None and self.logic_match.confidence < threshold:
            return True
        return False

    def status(self, confidence_threshold: int) -> SongStatus:
        """
        Overall summary status for the Review table's Status column.

        Takes the confidence threshold as a parameter for the same reason
        is_low_confidence() does: it's a user setting, not intrinsic to
        the song, so this method shouldn't reach out and know about
        Settings directly. The caller (the Review page) already has the
        Settings object and passes the number in.

        `manually_verified` short-circuits everything else: once you've
        looked at a song and confirmed it's fine, the app takes your word
        for it rather than continuing to second-guess you.
        """
        if self.manually_verified:
            return SongStatus.OK

        has_any_problem = (
            self.is_missing_audio
            or self.is_missing_logic
            or self.is_missing_bpm
            or self.is_low_confidence(confidence_threshold)
            or self.needs_tempo_copy is True  # explicitly True -- None means "can't tell yet"
        )
        return SongStatus.NEEDS_REVIEW if has_any_problem else SongStatus.OK
