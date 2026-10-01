"""
Scheduling / execution service for Setlist Builder.

This is the backend half of "Schedule" (see the architecture
discussion): turning an already-reviewed week's songs into a deferred,
approval-gated action, and later actually carrying that action out once
you approve it. No UI wires into this yet -- the per-tab Schedule
buttons and approval queue are a separate, later pass; this module is
what that UI will eventually call.

THE THREE STAGES, AND WHERE MATCHING FITS IN
-----------------------------------------------
1. PREPARE (prepare_youtube_matches / prepare_drive_matches): run
   fuzzy-matching for a setlist's songs against the relevant remote
   library and persist the results. Matching happens HERE, at "Schedule"
   time -- not later at execution time -- so you review/fix matches
   before anything is locked in, per the design decision to never guess
   unattended.
2. SCHEDULE (schedule_task): create a ScheduledTask row for a future
   date. Drive and YouTube tasks for the SAME setlist share one
   `approval_group_id` (so they notify/approve together) if the other
   one is already scheduled and still pending/awaiting. The three Logic
   Pro pipeline steps (see below) are always solo -- three separate
   notifications, per the design decision, never bundled with anything.
3. EXECUTE (execute_task, called by approve_task/approve_group): once
   approved, actually perform the real action. Every execute_* function
   re-checks its own preconditions (every song still has a match, a
   playlist/folder is configured, credentials are valid, the previous
   pipeline step actually finished) and fails the task clearly with
   error_message set rather than guessing or skipping -- per the
   decision that a missing precondition at execution time should never
   be silently papered over.

THE LOGIC PRO PIPELINE: TWO STEPS, IN ORDER
-----------------------------------------------
Two distinct ActionTypes, each its own scheduled/notified/approved task,
mirroring the interactive Build page's own two buttons:

  1. PREPARE_LOGIC_PROJECT -- copies each song's matched audio folder
     into a destination folder (see _logic_pro_destination_folder: the
     same `desktop_output_folder` Settings field the interactive Build
     page's own Prepare button already uses), then generates the Build
     Manifest JSON from those copied files and writes it into that same
     destination folder, recording its path on this task's own
     `manifest_path` column. These were originally two separate
     ActionTypes (COPY_AUDIO, BUILD_MANIFEST), merged into one per
     direct feedback -- a manifest can never meaningfully be built
     without the audio having just been copied first, so scheduling
     them as two separately-approved steps was pure friction.
  2. LOGIC_AUTOMATION -- runs the actual Logic Pro automation
     (logic_automation.build_full_rehearsal_project) against the
     manifest PREPARE_LOGIC_PROJECT just wrote. Kept as its own solo
     step, unlike the merge above, since it takes over your keyboard
     and screen -- a materially more disruptive action that deserves
     its own separate approval, not bundled with anything.

LOGIC_AUTOMATION refuses to run until PREPARE_LOGIC_PROJECT for the SAME
Setlist has actually completed (status DONE) -- see
_require_prior_step_done. Since there's no one to answer the
interactive "folder already exists, overwrite?" prompt during
unattended execution, the Copy Audio half of PREPARE_LOGIC_PROJECT
always overwrites (a deliberate simplification: this folder is reused,
not per-date, since only one Logic Pro build is ever in flight at a
time in practice).
"""

from __future__ import annotations

import datetime
import logging
import uuid
from pathlib import Path
from typing import Optional

from app.models.scheduled_task import ActionType, ScheduledTask, Setlist, TaskStatus
from app.models.settings import Settings
from app.models.song_entry import ChartMatch, SongEntry, YouTubeMatch
from app.services import copy_service, database_service, drive_service, logic_automation, manifest_service
from app.services import pdf_merge_service, video_lookup_service, youtube_service

_LOGIC_PRO_MANIFEST_FILENAME = "build_manifest.json"

logger = logging.getLogger(__name__)


class SchedulingError(Exception):
    """
    Raised for a problem that should stop scheduling/execution before
    (or instead of) doing anything irreversible -- caught by
    execute_task and turned into a FAILED status with this message, or
    raised straight out of the prepare_*/schedule_task calls for the
    caller (the future UI) to show directly.
    """


def compute_default_schedule_datetime(service_date: datetime.date, settings: Settings) -> datetime.datetime:
    """
    The suggested (still overridable) schedule date/time for a setlist:
    the Monday that starts its service week, at Settings.default_schedule_time.

    Applies to any service day, not just Sunday/Wednesday -- a Sunday
    service (weekday()==6) lands 6 days back on that week's Monday; a
    Wednesday service (weekday()==2) lands 2 days back on the SAME
    Monday, per direct feedback that this should be one universal rule
    rather than special-cased per day name. date.weekday() already
    returns "days since this week's Monday" directly (Monday=0 ...
    Sunday=6), so subtracting it is the whole computation -- no
    day-name string matching involved at all.
    """
    monday = service_date - datetime.timedelta(days=service_date.weekday())
    time_of_day = datetime.time.fromisoformat(settings.default_schedule_time)
    return datetime.datetime.combine(monday, time_of_day)


# ----------------------------------------------------------------------
# Stage 1: matching, at schedule time
# ----------------------------------------------------------------------


def prepare_youtube_matches(setlist_id: int, settings: Settings) -> list[SongEntry]:
    """
    Fuzzy-match every song in this setlist against the configured video
    lookup site (Settings.video_lookup_url), for any song that doesn't
    already have a youtube_match -- an existing match (e.g. one already
    hand-corrected) is left untouched, the same policy the interactive
    YouTube page already follows. Persists the results and returns the
    updated entries for review before you actually call schedule_task.
    """
    entries = database_service.get_song_entries(setlist_id)
    library = video_lookup_service.fetch_video_lookup_library(settings.video_lookup_url)

    for entry in entries:
        if entry.youtube_match is not None:
            continue
        result = video_lookup_service.find_video_for_song(entry.ocr_title, library)
        if result is not None:
            matched_entry, score = result
            entry.youtube_match = YouTubeMatch(title=matched_entry.title, video_id=matched_entry.video_id, confidence=score)

    database_service.replace_song_entries(setlist_id, entries)
    return entries


def prepare_drive_matches(setlist_id: int, settings: Settings) -> list[SongEntry]:
    """
    Fuzzy-match every song in this setlist against the configured Drive
    charts folder, for any song that doesn't already have a chart_match.
    Persists the results and returns the updated entries for review.

    Raises SchedulingError if Drive authentication fails -- unlike
    execution (which must never pop an interactive login), this runs
    while you're sitting at the app clicking "Schedule," so a normal
    (possibly interactive) login attempt is appropriate here.
    """
    entries = database_service.get_song_entries(setlist_id)

    creds = drive_service.authenticate(settings.google_drive_client_secret_path)
    if creds is None:
        raise SchedulingError("Google Drive authentication failed -- check Settings and the log file.")

    drive = drive_service.get_drive_client(creds)
    library = drive_service.list_docs_in_folder(drive, settings.google_drive_charts_folder_id)

    for entry in entries:
        if entry.chart_match is not None:
            continue
        result = drive_service.find_chart_for_song(entry.ocr_title, library)
        if result is not None:
            doc, score = result
            entry.chart_match = ChartMatch(title=doc.name, doc_id=doc.id, confidence=score)

    database_service.replace_song_entries(setlist_id, entries)
    return entries


# ----------------------------------------------------------------------
# Stage 2: scheduling
# ----------------------------------------------------------------------


def schedule_task(
    setlist_id: int,
    action_type: ActionType,
    scheduled_for: datetime.datetime,
    target_name: Optional[str] = None,
) -> int:
    """
    Create a ScheduledTask for `setlist_id`. Returns the new task's id.

    `target_name` is the Drive folder / YouTube playlist name that was
    selected in that tab's dropdown at the moment Schedule was clicked
    (pass None for the two Logic Pro steps, which have no such
    selection) -- execute_task reads this back directly rather than
    re-deriving a target from the Setlist's day_name, which doesn't
    necessarily match whatever you actually had picked (day_name is
    even None outright for single-screenshot setlists).

    Drive and YouTube tasks for the SAME setlist are linked (share one
    approval_group_id) so they notify/approve together: if the other
    action already has a pending/awaiting task for this setlist, this
    new task joins its group (creating one now if the sibling didn't
    have one yet); otherwise this task starts a fresh group of its own,
    ready for a sibling to join later. The two Logic Pro pipeline steps
    (PREPARE_LOGIC_PROJECT/LOGIC_AUTOMATION) are always solo
    (approval_group_id stays None) -- each is its own notification.

    Nothing here checks whether a Logic Pro step's PREDECESSOR has
    finished -- that's deliberately left to execute_task
    (_require_prior_step_done), so scheduling both in advance (for a
    later date each) works fine; only actually RUNNING one out of order
    is refused.
    """
    approval_group_id: Optional[str] = None
    if action_type in (ActionType.DRIVE, ActionType.YOUTUBE):
        sibling_action = ActionType.YOUTUBE if action_type is ActionType.DRIVE else ActionType.DRIVE
        sibling = database_service.find_active_task(setlist_id, sibling_action)
        if sibling is not None:
            if sibling.approval_group_id is not None:
                approval_group_id = sibling.approval_group_id
            else:
                approval_group_id = str(uuid.uuid4())
                database_service.set_task_approval_group(sibling.id, approval_group_id)
        else:
            approval_group_id = str(uuid.uuid4())

    task_id = database_service.create_scheduled_task(
        setlist_id, action_type, scheduled_for, approval_group_id=approval_group_id, target_name=target_name
    )
    logger.info(
        "Scheduled %s task %d for setlist %d at %s (group=%s, target=%r)",
        action_type.value, task_id, setlist_id, scheduled_for, approval_group_id, target_name,
    )
    return task_id


def check_auth_health(settings: Settings) -> dict[str, bool]:
    """
    Proactively check whether each configured Google login is currently
    usable, WITHOUT ever popping an interactive browser window -- a
    silent refresh attempt only. Returns e.g. {"youtube": True, "drive":
    False}; a service with no client secret configured at all is left
    out entirely (nothing to check).

    This is what lets the "please re-authenticate" prompt fire AHEAD of
    a scheduled task actually failing (see the architecture discussion
    on Google's 7-day Testing-mode refresh token limit) --
    scheduler_runner.py calls this once a task that needs one of these
    logins is close to its run time, so you're asked to log back in
    while there's still time, rather than on every background check.
    """
    health: dict[str, bool] = {}

    if settings.youtube_client_secret_path:
        health["youtube"] = youtube_service.authenticate(
            settings.youtube_client_secret_path, allow_interactive=False
        ) is not None

    if settings.google_drive_client_secret_path:
        health["drive"] = drive_service.authenticate(
            settings.google_drive_client_secret_path, allow_interactive=False
        ) is not None

    return health


# ----------------------------------------------------------------------
# Stage 3: the background check, approval, and execution
# ----------------------------------------------------------------------


def flag_due_tasks(now: Optional[datetime.datetime] = None) -> list[list[ScheduledTask]]:
    """
    Flip every due PENDING task to AWAITING_APPROVAL, grouped for
    notification purposes: tasks sharing an approval_group_id come back
    together in one inner list (one notification for the pair), tasks
    with no group each come back in their own single-item list.

    This is what the background scheduler check (a separate process,
    triggered by launchd -- see scheduler_runner.py and the architecture
    discussion) calls before sending notifications. It's deliberately
    NOT something the Qt app calls on a timer -- the whole point of the
    launchd approach is that this runs even when the app isn't open.
    """
    due = database_service.list_due_tasks(now)

    groups: dict[str, list[ScheduledTask]] = {}
    solo: list[list[ScheduledTask]] = []

    for task in due:
        database_service.update_task_status(task.id, TaskStatus.AWAITING_APPROVAL)
        if task.approval_group_id:
            groups.setdefault(task.approval_group_id, []).append(task)
        else:
            solo.append([task])

    return list(groups.values()) + solo


def approve_task(task_id: int, settings: Settings) -> None:
    """Approve one task and immediately execute it."""
    database_service.update_task_status(task_id, TaskStatus.APPROVED, approved_at=datetime.datetime.now())
    execute_task(task_id, settings)


def approve_group(approval_group_id: str, settings: Settings) -> None:
    """Approve and execute every task sharing one approval_group_id."""
    for task in database_service.list_tasks_in_group(approval_group_id):
        approve_task(task.id, settings)


def decline_task(task_id: int) -> None:
    database_service.update_task_status(task_id, TaskStatus.DECLINED)


def decline_group(approval_group_id: str) -> None:
    for task in database_service.list_tasks_in_group(approval_group_id):
        decline_task(task.id)


def reschedule_task(task_id: int, scheduled_for: datetime.datetime) -> None:
    database_service.reschedule_task(task_id, scheduled_for)


def reschedule_group(approval_group_id: str, scheduled_for: datetime.datetime) -> None:
    for task in database_service.list_tasks_in_group(approval_group_id):
        reschedule_task(task.id, scheduled_for)


def delete_task(task_id: int) -> None:
    database_service.delete_scheduled_task(task_id)


def delete_group(approval_group_id: str) -> None:
    for task in database_service.list_tasks_in_group(approval_group_id):
        delete_task(task.id)


def execute_task(task_id: int, settings: Settings) -> None:
    """
    Actually perform a task's real-world action. Always leaves the task
    in DONE or FAILED (with error_message set) -- never leaves it
    hanging in RUNNING, even if something raises unexpectedly.
    """
    task = database_service.get_task(task_id)
    if task is None:
        logger.error("execute_task called with unknown task id %d", task_id)
        return

    database_service.update_task_status(task_id, TaskStatus.RUNNING)

    try:
        if task.action_type is ActionType.YOUTUBE:
            _execute_youtube(task, settings)
        elif task.action_type is ActionType.DRIVE:
            _execute_drive(task, settings)
        elif task.action_type is ActionType.PREPARE_LOGIC_PROJECT:
            _execute_prepare_logic_project(task, settings)
        elif task.action_type is ActionType.LOGIC_AUTOMATION:
            _execute_logic_automation(task, settings)
    except SchedulingError as error:
        logger.error("Task %d (%s) failed: %s", task_id, task.action_type.value, error)
        database_service.update_task_status(task_id, TaskStatus.FAILED, error_message=str(error))
        return
    except Exception as error:  # noqa: BLE001 -- a real failure here must never crash the caller
        logger.exception("Task %d (%s) failed unexpectedly", task_id, task.action_type.value)
        database_service.update_task_status(task_id, TaskStatus.FAILED, error_message=str(error))
        return

    database_service.update_task_status(task_id, TaskStatus.DONE, executed_at=datetime.datetime.now())
    logger.info("Task %d (%s) completed.", task_id, task.action_type.value)


def _require_setlist(setlist_id: int) -> Setlist:
    setlist = database_service.get_setlist(setlist_id)
    if setlist is None:
        raise SchedulingError(f"Setlist {setlist_id} no longer exists.")
    return setlist


def _require_prior_step_done(setlist_id: int, action_type: ActionType, label: str) -> ScheduledTask:
    """
    The Logic Pro pipeline's ordering guard: refuses to let a step run
    until the previous step for this SAME setlist has actually finished.
    Returns the finished task (so the caller can read fields off it,
    e.g. LOGIC_AUTOMATION reading PREPARE_LOGIC_PROJECT's manifest_path).
    """
    prior = database_service.find_latest_task(setlist_id, action_type)
    if prior is None or prior.status != TaskStatus.DONE:
        raise SchedulingError(f"{label} hasn't completed for this week yet -- schedule/approve that first.")
    return prior


def _logic_pro_destination_folder(settings: Settings) -> Path:
    """
    Where PREPARE_LOGIC_PROJECT reads/writes for a given Setlist: the
    same single `desktop_output_folder` Settings field the interactive
    Build page's Prepare button already uses (see
    build_page.py's own _destination_folder) -- one shared folder, not
    split by day, so a scheduled run lands in exactly the same place a
    manual click would have.

    Falls back to ~/Desktop/Secuencias if unconfigured, matching
    build_page.py's own fallback, rather than raising -- there's always
    a usable destination even before you've touched Settings.
    """
    if settings.desktop_output_folder:
        return Path(settings.desktop_output_folder)
    return Path.home() / "Desktop" / "Secuencias"


def _execute_youtube(task: ScheduledTask, settings: Settings) -> None:
    setlist = _require_setlist(task.setlist_id)
    entries = database_service.get_song_entries(task.setlist_id)
    # Only YouTube's OWN included songs -- entries excluded from YouTube
    # (but maybe included in Drive/Logic Pro) must never end up in this
    # playlist. See SongEntry.excluded_from_youtube.
    included = [entry for entry in entries if not entry.excluded_from_youtube]

    missing = [entry.ocr_title for entry in included if entry.youtube_match is None]
    if missing:
        raise SchedulingError(f"{len(missing)} song(s) have no YouTube match yet: {', '.join(missing)}")

    # The task's own captured target first, then this week's saved
    # target, then its day name -- the last two resolved case/accent
    # insensitively, since "Domingo" (from the PDF header) never
    # literally equals the "domingo" key you typed in Settings.
    target_name = (
        task.target_name
        or setlist.youtube_target_name
        or settings.playlist_name_for_day(setlist.day_name)
        or ""
    )
    playlist_id = settings.youtube_playlists.get(target_name)
    if not playlist_id:
        raise SchedulingError(f"No YouTube playlist configured for '{target_name}' in Settings.")

    creds = youtube_service.authenticate(settings.youtube_client_secret_path, allow_interactive=False)
    if creds is None:
        raise SchedulingError(
            "YouTube login has expired or was never completed -- open the app and re-authenticate, "
            "then approve this task again."
        )

    youtube = youtube_service.get_youtube_client(creds)
    youtube_service.clear_playlist(youtube, playlist_id)
    video_ids = [entry.youtube_match.video_id for entry in included]
    youtube_service.add_videos_to_playlist(youtube, playlist_id, video_ids)


def _execute_drive(task: ScheduledTask, settings: Settings) -> None:
    setlist = _require_setlist(task.setlist_id)
    entries = database_service.get_song_entries(task.setlist_id)
    # Only Drive's OWN included songs -- see _execute_youtube above.
    included = [entry for entry in entries if not entry.excluded_from_drive]

    missing = [entry.ocr_title for entry in included if entry.chart_match is None]
    if missing:
        raise SchedulingError(f"{len(missing)} song(s) have no chart match yet: {', '.join(missing)}")

    # Same resolution order as _execute_youtube above.
    target_name = (
        task.target_name
        or setlist.drive_target_name
        or settings.chart_folder_name_for_day(setlist.day_name)
        or ""
    )
    download_folder = settings.chart_folders.get(target_name)
    if not download_folder:
        raise SchedulingError(f"No chart download folder configured for '{target_name}' in Settings.")
    if not settings.charts_merged_output_folder:
        raise SchedulingError("The Charts Merged PDF Folder isn't configured in Settings.")

    creds = drive_service.authenticate(settings.google_drive_client_secret_path, allow_interactive=False)
    if creds is None:
        raise SchedulingError(
            "Google Drive login has expired or was never completed -- open the app and re-authenticate, "
            "then approve this task again."
        )

    drive = drive_service.get_drive_client(creds)

    download_dir = Path(download_folder)
    download_dir.mkdir(parents=True, exist_ok=True)
    for existing_pdf in download_dir.glob("*.pdf"):
        existing_pdf.unlink()

    for entry in included:
        output_path = download_dir / f"{entry.order:02d} - {entry.ocr_title}.pdf"
        result = drive_service.export_doc_as_pdf(drive, entry.chart_match.doc_id, str(output_path))
        if result is None:
            raise SchedulingError(f"Failed to download the chart for '{entry.ocr_title}'.")
        entry.chart_pdf_path = result

    # The FULL entries list, not just `included` -- see the identical
    # note on ChartsPage/YouTubePage/ReviewPage's own Schedule handlers:
    # replace_song_entries deletes every row for this setlist first, so
    # saving only Drive's subset would erase the other destinations'
    # own excluded-from-drive songs. The mutations just made above
    # (chart_pdf_path) already landed on these same SongEntry objects,
    # since `included` only ever holds references into `entries`.
    database_service.replace_song_entries(task.setlist_id, entries)

    manifest = manifest_service.build_manifest(included, settings)
    # Matches ChartsPage._merged_pdf_filename()'s own convention exactly
    # (named directly after the Target folder, nothing else appended) --
    # a scheduled run should produce the identical filename a manual
    # click on Merge would have, not a different day_name/date-based one.
    output_path = Path(settings.charts_merged_output_folder) / f"{Path(target_name).name}.pdf"
    pdf_merge_service.merge_charts_from_manifest(manifest, str(output_path))


def _execute_prepare_logic_project(task: ScheduledTask, settings: Settings) -> None:
    """
    Copies each song's matched audio into place AND builds the Logic
    Pro manifest from those copied files, in one step -- see
    ActionType.PREPARE_LOGIC_PROJECT's docstring for why these two used
    to be separate scheduled steps and no longer are.
    """
    _require_setlist(task.setlist_id)
    entries = database_service.get_song_entries(task.setlist_id)
    # Only Logic Pro's OWN included songs -- see _execute_youtube's note.
    included = [entry for entry in entries if not entry.excluded_from_logic]

    missing_audio = [entry.ocr_title for entry in included if entry.audio_match is None]
    if missing_audio:
        raise SchedulingError(f"{len(missing_audio)} song(s) have no audio match yet: {', '.join(missing_audio)}")
    missing_logic = [entry.ocr_title for entry in included if entry.logic_match is None]
    if missing_logic:
        raise SchedulingError(
            f"{len(missing_logic)} song(s) have no Logic Pro project match yet: {', '.join(missing_logic)}"
        )

    destination = _logic_pro_destination_folder(settings)
    # Always overwrite -- there's no one to answer the interactive
    # "already exists, overwrite?" prompt during unattended execution
    # (see copy_service.copy_audio_folders' on_conflict parameter).
    copy_service.copy_audio_folders(included, str(destination), on_conflict=lambda _title: copy_service.OVERWRITE)
    # The FULL entries list, not just `included` -- see _execute_drive's
    # identical note.
    database_service.replace_song_entries(task.setlist_id, entries)

    copied = sum(1 for entry in included if entry.output_folder is not None)
    if copied == 0:
        raise SchedulingError("No songs were actually copied -- check that the matched audio folders still exist.")

    manifest = manifest_service.build_manifest(included, settings)
    manifest_path = destination / _LOGIC_PRO_MANIFEST_FILENAME
    manifest_service.write_manifest(manifest, str(manifest_path))

    database_service.set_task_manifest_path(task.id, str(manifest_path))


def _execute_logic_automation(task: ScheduledTask, settings: Settings) -> None:
    manifest_task = _require_prior_step_done(task.setlist_id, ActionType.PREPARE_LOGIC_PROJECT, "Prepare Logic Project")

    if not manifest_task.manifest_path:
        raise SchedulingError("Build Manifest task finished but recorded no manifest path -- this shouldn't happen.")
    if not Path(manifest_task.manifest_path).exists():
        raise SchedulingError(f"Build Manifest file not found: {manifest_task.manifest_path}")

    logic_automation.build_full_rehearsal_project(manifest_task.manifest_path)
