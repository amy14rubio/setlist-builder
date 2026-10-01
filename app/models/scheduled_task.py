"""
Scheduling data models for Setlist Builder.

These describe the pieces needed to import a whole month's worth of
setlists at once and let each week's YouTube/Drive/Logic Pro update be
scheduled for a later date instead of applied immediately (see
database_service.py for how these get persisted to SQLite).

THREE NEW CONCEPTS, AND HOW THEY RELATE TO THE EXISTING SongEntry MODEL
------------------------------------------------------------------------
- MonthlyImport: one row per monthly PDF you import (e.g. "October
  2026"). Purely a record of where a batch of Setlists came from.
- Setlist: one row per service (one Sunday or one Wednesday). This is
  the persisted version of what used to only exist as an in-memory
  `list[SongEntry]` on the Playlist page -- a Setlist owns a list of
  SongEntry rows (see database_service.add_song_entries), the same
  SongEntry dataclass the rest of the app already uses. A Setlist
  either came from a MonthlyImport (source_type="monthly_pdf") or from
  the original single-screenshot flow (source_type="single_screenshot").
- ScheduledTask: one row per action (a Logic Pro pipeline step, a
  Drive chart download, a YouTube playlist sync) that's been deferred
  to a future date instead of run immediately. `approval_group_id`
  links Drive and YouTube tasks for the same Setlist so they
  notify/approve together, while still tracking success/failure
  independently -- the three Logic Pro pipeline steps are never
  grouped with each other or with Drive/YouTube, since each is its own
  separate notification/approval (see ActionType).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ActionType(Enum):
    """
    Which real-world action a ScheduledTask represents.

    The Logic Pro pipeline is TWO separate, sequential action types:
    PREPARE_LOGIC_PROJECT copies each song's matched audio into place
    AND generates the Build Manifest JSON from those files, both in one
    step -- they were originally two separate action types (COPY_AUDIO,
    BUILD_MANIFEST), but were merged since a manifest can never
    meaningfully be built without the audio having just been copied
    first, per direct feedback. LOGIC_AUTOMATION -- the actual Logic Pro
    automation run against that manifest, which takes over your
    keyboard/screen -- stays its own separate, solo-notified step, since
    that's a materially different (and more disruptive) kind of action
    than the first. scheduling_service.py enforces that LOGIC_AUTOMATION
    can only run once PREPARE_LOGIC_PROJECT for the same Setlist has
    finished (status DONE) -- see its _require_prior_step_done.
    """

    PREPARE_LOGIC_PROJECT = "prepare_logic_project"
    LOGIC_AUTOMATION = "logic_automation"
    DRIVE = "drive"
    YOUTUBE = "youtube"


class TaskStatus(Enum):
    """
    Lifecycle of a ScheduledTask, from creation to completion.

    PENDING -> AWAITING_APPROVAL happens automatically once
    `scheduled_for` arrives (flipped by the background scheduler check).
    AWAITING_APPROVAL -> APPROVED/DECLINED only happens by your explicit
    action -- nothing here ever runs without that step.
    """

    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DECLINED = "declined"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class MonthlyImport:
    """One imported monthly PDF (e.g. 'MES DE OCTUBRE')."""

    id: Optional[int]
    source_path: str
    month: int  # 1-12
    year: int
    imported_at: datetime.datetime


@dataclass
class Setlist:
    """
    One service's worth of songs (one Sunday or one Wednesday) --
    the persisted counterpart to an in-memory `list[SongEntry]`.

    `monthly_import_id` is None for setlists created via the original
    single-screenshot flow. `day_name` and `note` are None for
    single-screenshot setlists too, since that flow has no headers to
    parse them from -- only `service_date` is asked for in that case.

    `drive_target_name`/`youtube_target_name` remember which Drive
    folder / YouTube playlist THIS week is for (e.g. the Sunday week's
    "domingo" playlist vs the Wednesday week's "miércoles" one). They
    live here, on the setlist, rather than only in the UI's memory, so
    monthly bulk scheduling still knows each week's own target after an
    app restart -- see PlaylistPage._remember_target.
    """

    id: Optional[int]
    monthly_import_id: Optional[int]
    source_type: str  # "monthly_pdf" or "single_screenshot"
    service_date: datetime.date
    day_name: Optional[str]  # e.g. "Domingo", "Miércoles"
    note: Optional[str]  # e.g. "Servicio Evangelistico"
    created_at: datetime.datetime
    drive_target_name: Optional[str] = None
    youtube_target_name: Optional[str] = None


@dataclass
class ScheduledTask:
    """
    One deferred, approval-gated action against a single Setlist.

    `manifest_path` is only ever set on PREPARE_LOGIC_PROJECT tasks, and
    only once that task actually executes -- it's the path to the Build
    Manifest JSON (see manifest_service.py) that its execution just
    wrote, which the subsequent LOGIC_AUTOMATION task looks up (from the
    completed PREPARE_LOGIC_PROJECT task for the same Setlist, not from
    its own row) when it runs. See scheduling_service.py for the full
    pipeline.
    """

    id: Optional[int]
    setlist_id: int
    action_type: ActionType
    scheduled_for: datetime.datetime
    status: TaskStatus
    created_at: datetime.datetime
    approval_group_id: Optional[str] = None
    approved_at: Optional[datetime.datetime] = None
    executed_at: Optional[datetime.datetime] = None
    manifest_path: Optional[str] = None
    error_message: Optional[str] = None
    # The Drive folder / YouTube playlist name selected in that tab's
    # dropdown at the moment Schedule was clicked -- execution reads
    # this back instead of re-deriving a target from the Setlist's
    # day_name (see scheduling_service._execute_drive/_execute_youtube).
    # None for Logic Pro's three pipeline steps, which have no per-task
    # target selection.
    target_name: Optional[str] = None
