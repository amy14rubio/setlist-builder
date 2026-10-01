"""
Database service for Setlist Builder.

Backs the monthly-import + scheduling feature (and, going forward, the
original single-screenshot flow too) with a single local SQLite file:

    ~/Library/Application Support/SetlistBuilder/setlist_builder.db

WHY SQLITE, AND WHY NOT JUST EXTEND settings.json
---------------------------------------------------
settings.json (see app/models/settings.py) is fine for small, singular
config values, but this feature needs to answer questions like "which
tasks are due right now?" and needs two different processes (the Qt app
and, later, a separate background scheduler script) to read/write
without corrupting each other's changes. SQLite is a single local file
(no server, ships with Python) that handles both of those correctly,
where a flat JSON file would not.

WHAT'S STORED, AND HOW SongEntry FITS IN
-------------------------------------------
`SongEntry` (app/models/song_entry.py) is untouched -- it's still the
one object every part of the app already knows how to build a UI
around. This module just knows how to save a list of them under a
Setlist row, and load them back out again. The three match dataclasses
(LibraryMatch, YouTubeMatch, ChartMatch) are stored as JSON text in
their own column each, rather than as separate tables -- they're small,
always read/written as a whole with their parent SongEntry, and never
queried independently, so normalizing them into more tables would only
add join complexity with no real benefit.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, Optional

from app.models.scheduled_task import ActionType, MonthlyImport, ScheduledTask, Setlist, TaskStatus
from app.models.song_entry import ChartMatch, LibraryMatch, SongEntry, YouTubeMatch

logger = logging.getLogger(__name__)


def _database_dir() -> Path:
    """
    Same Application Support folder Settings already uses (see
    app/models/settings.py) -- keeping every piece of app data in one
    known place, separate from the project folder itself.
    """
    directory = Path.home() / "Library" / "Application Support" / "SetlistBuilder"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _database_file() -> Path:
    return _database_dir() / "setlist_builder.db"


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS monthly_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_path TEXT NOT NULL,
        month INTEGER NOT NULL,
        year INTEGER NOT NULL,
        imported_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS setlists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        monthly_import_id INTEGER REFERENCES monthly_imports(id),
        source_type TEXT NOT NULL CHECK (source_type IN ('monthly_pdf', 'single_screenshot')),
        service_date TEXT NOT NULL,
        day_name TEXT,
        note TEXT,
        created_at TEXT NOT NULL,
        drive_target_name TEXT,
        youtube_target_name TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS song_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        setlist_id INTEGER NOT NULL REFERENCES setlists(id),
        order_index INTEGER NOT NULL,
        ocr_title TEXT NOT NULL,
        audio_match_json TEXT,
        logic_match_json TEXT,
        logic_tempo INTEGER,
        manually_verified INTEGER NOT NULL DEFAULT 0,
        youtube_match_json TEXT,
        output_folder TEXT,
        chart_match_json TEXT,
        chart_pdf_path TEXT,
        notes TEXT NOT NULL DEFAULT '',
        excluded_from_drive INTEGER NOT NULL DEFAULT 0,
        excluded_from_youtube INTEGER NOT NULL DEFAULT 0,
        excluded_from_logic INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        setlist_id INTEGER NOT NULL REFERENCES setlists(id),
        action_type TEXT NOT NULL CHECK (
            action_type IN ('prepare_logic_project', 'logic_automation', 'drive', 'youtube')
        ),
        approval_group_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (
            status IN ('pending', 'awaiting_approval', 'approved', 'declined', 'running', 'done', 'failed')
        ),
        scheduled_for TEXT NOT NULL,
        created_at TEXT NOT NULL,
        approved_at TEXT,
        executed_at TEXT,
        manifest_path TEXT,
        error_message TEXT,
        target_name TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_song_entries_setlist_id ON song_entries(setlist_id)",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_setlist_id ON scheduled_tasks(setlist_id)",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due ON scheduled_tasks(status, scheduled_for)",
)


def initialize_database() -> None:
    """
    Create every table (and index) if it doesn't already exist yet.

    Safe to call every time the app starts -- CREATE TABLE IF NOT
    EXISTS is a no-op once the schema is already in place, the same way
    Settings.load() is safe to call whether or not settings.json exists.
    """
    with _connect() as connection:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _migrate_scheduled_tasks_if_needed(connection)
        _migrate_target_name_column_if_needed(connection)
        _migrate_action_type_values_if_needed(connection)
        _migrate_song_entry_exclusion_columns_if_needed(connection)
        _migrate_setlist_target_columns_if_needed(connection)
    logger.info("Database ready at %s", _database_file())


def _migrate_scheduled_tasks_if_needed(connection: sqlite3.Connection) -> None:
    """
    One-off fixups for the scheduled_tasks table, whose shape changed
    twice during early development (a missing manifest_path column,
    then the Logic Pro pipeline splitting into copy_audio/build_manifest/
    logic_automation) after a real database file already existed on
    disk. `CREATE TABLE IF NOT EXISTS` alone can't apply either change
    to an existing table, so this checks the table's actual stored SQL
    and, if it's out of date AND still empty, drops and recreates it --
    safe only because nothing scheduled yet would be lost. If it's out
    of date but already has rows, this refuses to guess and raises
    instead, rather than silently discarding scheduled work.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'"
    ).fetchone()
    current_sql = row[0] if row else ""
    # Only checking for manifest_path (not also requiring "copy_audio"
    # in the CHECK constraint text, as this used to) -- action_type's
    # allowed values moved on again since (see
    # _migrate_action_type_values_if_needed), so "copy_audio" no longer
    # appears in an up-to-date table's stored SQL at all. This function's
    # only real concern was ever the manifest_path column's existence;
    # tying its "already current" check to unrelated action_type wording
    # made it wrongly think an already-migrated table was still stale.
    if "manifest_path" in current_sql:
        return  # already current

    row_count = connection.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0]
    if row_count > 0:
        raise RuntimeError(
            "The scheduled_tasks table is out of date and already has rows in it -- "
            "a manual migration is needed before the app can start safely."
        )

    logger.warning("scheduled_tasks table is out of date (and empty) -- recreating it with the current schema.")
    connection.execute("DROP TABLE scheduled_tasks")
    for statement in _SCHEMA_STATEMENTS:
        if "scheduled_tasks" in statement:
            connection.execute(statement)


def _migrate_action_type_values_if_needed(connection: sqlite3.Connection) -> None:
    """
    Collapses the old 'copy_audio' / 'build_manifest' action_type values
    into the new merged 'prepare_logic_project' one (see ActionType's
    docstring for why they were merged), on a database that predates the
    merge and already has real rows using the old values.

    SQLite can't ALTER a CHECK constraint in place, so this uses SQLite's
    own recommended pattern for that: rename the table aside, create a
    fresh one from the current schema (which already has the new CHECK
    constraint AND the target_name column), copy every row across with
    action_type remapped, then drop the renamed-aside copy. Both old
    values collapsing into the same new one is fine even for two
    genuinely distinct historical rows -- they're already terminal
    (done/failed) log entries by the time this ever runs, not something
    still being actively tracked by day.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'"
    ).fetchone()
    current_sql = row[0] if row else ""
    if "prepare_logic_project" in current_sql:
        return  # already current

    logger.warning("scheduled_tasks.action_type is out of date -- migrating 'copy_audio'/'build_manifest' rows to 'prepare_logic_project'.")
    connection.execute("ALTER TABLE scheduled_tasks RENAME TO scheduled_tasks_old")
    for statement in _SCHEMA_STATEMENTS:
        if "CREATE TABLE IF NOT EXISTS scheduled_tasks " in statement:
            connection.execute(statement)

    connection.execute(
        """
        INSERT INTO scheduled_tasks
            (id, setlist_id, action_type, approval_group_id, status, scheduled_for,
             created_at, approved_at, executed_at, manifest_path, error_message, target_name)
        SELECT
            id, setlist_id,
            CASE action_type
                WHEN 'copy_audio' THEN 'prepare_logic_project'
                WHEN 'build_manifest' THEN 'prepare_logic_project'
                ELSE action_type
            END,
            approval_group_id, status, scheduled_for,
            created_at, approved_at, executed_at, manifest_path, error_message, target_name
        FROM scheduled_tasks_old
        """
    )
    connection.execute("DROP TABLE scheduled_tasks_old")

    # The RENAME above carried scheduled_tasks' two indexes over to
    # scheduled_tasks_old under their original names (SQLite indexes
    # aren't renamed by ALTER TABLE RENAME, but stay attached to
    # whatever the table is now called) -- DROP TABLE just destroyed
    # them along with it. The initial _SCHEMA_STATEMENTS loop at the top
    # of initialize_database() already ran once before any table existed
    # to recreate them into, so they need to be created again here, now
    # that the new scheduled_tasks table (and those index names) exists.
    for statement in _SCHEMA_STATEMENTS:
        if "idx_scheduled_tasks" in statement:
            connection.execute(statement)


def _migrate_target_name_column_if_needed(connection: sqlite3.Connection) -> None:
    """
    Adds the `target_name` column to an existing scheduled_tasks table
    that predates it -- unlike _migrate_scheduled_tasks_if_needed above,
    this uses ALTER TABLE ADD COLUMN rather than drop-and-recreate, since
    by the time this column was added the table already had real
    scheduled tasks in it on at least one real install. ADD COLUMN is
    always safe here: it can only add a nullable column, never lose data.

    `target_name` records exactly which named Drive folder / YouTube
    playlist you had selected in that tab's dropdown at the moment you
    clicked Schedule -- execute_task reads it back at execution time
    instead of re-deriving a folder/playlist from the Setlist's
    day_name, which doesn't necessarily match what you actually picked
    (see scheduling_service._execute_drive/_execute_youtube).
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_tasks)").fetchall()}
    if "target_name" not in columns:
        connection.execute("ALTER TABLE scheduled_tasks ADD COLUMN target_name TEXT")


def _migrate_song_entry_exclusion_columns_if_needed(connection: sqlite3.Connection) -> None:
    """
    Adds the three excluded_from_drive/_youtube/_logic columns to an
    existing song_entries table that predates them -- ALTER TABLE ADD
    COLUMN, same reasoning as _migrate_target_name_column_if_needed
    above (always safe: only ever adds a column, defaulting every
    existing row to 0/"included everywhere", which matches exactly how
    those rows behaved before per-destination exclusion existed at all).
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(song_entries)").fetchall()}
    for column in ("excluded_from_drive", "excluded_from_youtube", "excluded_from_logic"):
        if column not in columns:
            connection.execute(f"ALTER TABLE song_entries ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")


def _migrate_setlist_target_columns_if_needed(connection: sqlite3.Connection) -> None:
    """
    Adds the two per-week target columns to an existing setlists table
    that predates them -- ALTER TABLE ADD COLUMN, same always-safe
    reasoning as the two migrations above (only ever adds a nullable
    column; every existing row defaults to NULL, i.e. "no target picked
    yet", which is exactly how those weeks already behaved).
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(setlists)").fetchall()}
    for column in ("drive_target_name", "youtube_target_name"):
        if column not in columns:
            connection.execute(f"ALTER TABLE setlists ADD COLUMN {column} TEXT")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """
    One connection per call, committed on success and closed either way.

    A fresh connection per operation (rather than one long-lived global
    connection) is what lets a separate background-scheduler process
    (see the architecture discussion -- a launchd-triggered script) read
    and write this same file safely alongside the running Qt app.
    """
    connection = sqlite3.connect(_database_file())
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


# ----------------------------------------------------------------------
# Match (de)serialization -- LibraryMatch/YouTubeMatch/ChartMatch <-> JSON
# ----------------------------------------------------------------------


def _match_to_json(match: Optional[object]) -> Optional[str]:
    if match is None:
        return None
    return json.dumps(asdict(match))


def _library_match_from_json(raw: Optional[str]) -> Optional[LibraryMatch]:
    if raw is None:
        return None
    return LibraryMatch(**json.loads(raw))


def _youtube_match_from_json(raw: Optional[str]) -> Optional[YouTubeMatch]:
    if raw is None:
        return None
    return YouTubeMatch(**json.loads(raw))


def _chart_match_from_json(raw: Optional[str]) -> Optional[ChartMatch]:
    if raw is None:
        return None
    return ChartMatch(**json.loads(raw))


# ----------------------------------------------------------------------
# MonthlyImport
# ----------------------------------------------------------------------


def create_monthly_import(source_path: str, month: int, year: int) -> int:
    """Record one imported monthly PDF. Returns the new row's id."""
    imported_at = datetime.datetime.now().isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO monthly_imports (source_path, month, year, imported_at) VALUES (?, ?, ?, ?)",
            (source_path, month, year, imported_at),
        )
        return cursor.lastrowid


# ----------------------------------------------------------------------
# Setlist
# ----------------------------------------------------------------------


def create_setlist(
    source_type: str,
    service_date: datetime.date,
    monthly_import_id: Optional[int] = None,
    day_name: Optional[str] = None,
    note: Optional[str] = None,
) -> int:
    """
    Create one Setlist row (one Sunday or Wednesday's worth of songs).
    Returns the new row's id.
    """
    created_at = datetime.datetime.now().isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO setlists
                (monthly_import_id, source_type, service_date, day_name, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (monthly_import_id, source_type, service_date.isoformat(), day_name, note, created_at),
        )
        return cursor.lastrowid


_SETLIST_COLUMNS = (
    "id, monthly_import_id, source_type, service_date, day_name, note, created_at, "
    "drive_target_name, youtube_target_name"
)


def list_setlists_for_import(monthly_import_id: int) -> list[Setlist]:
    """All Setlist rows created from one MonthlyImport, in service-date order."""
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT {_SETLIST_COLUMNS}
            FROM setlists
            WHERE monthly_import_id = ?
            ORDER BY service_date ASC
            """,
            (monthly_import_id,),
        ).fetchall()

    return [_setlist_from_row(row) for row in rows]


def get_setlist(setlist_id: int) -> Optional[Setlist]:
    """One Setlist by id, or None if it doesn't exist."""
    with _connect() as connection:
        row = connection.execute(
            f"""
            SELECT {_SETLIST_COLUMNS}
            FROM setlists
            WHERE id = ?
            """,
            (setlist_id,),
        ).fetchone()

    return _setlist_from_row(row) if row is not None else None


def set_setlist_target(setlist_id: int, action_type: ActionType, target_name: str) -> None:
    """
    Remember which Drive folder / YouTube playlist this ONE week is for,
    so the choice survives an app restart (and so monthly bulk
    scheduling can still find it for a week you aren't currently
    looking at). Only Drive and YouTube have a target at all -- the two
    Logic Pro steps have no folder/playlist to pick, so they're ignored
    here rather than raising, keeping call sites free of per-action
    branching.
    """
    column = {ActionType.DRIVE: "drive_target_name", ActionType.YOUTUBE: "youtube_target_name"}.get(action_type)
    if column is None:
        return

    with _connect() as connection:
        connection.execute(f"UPDATE setlists SET {column} = ? WHERE id = ?", (target_name or None, setlist_id))


def _setlist_from_row(row: tuple) -> Setlist:
    return Setlist(
        id=row[0],
        monthly_import_id=row[1],
        source_type=row[2],
        service_date=datetime.date.fromisoformat(row[3]),
        day_name=row[4],
        note=row[5],
        created_at=datetime.datetime.fromisoformat(row[6]),
        drive_target_name=row[7],
        youtube_target_name=row[8],
    )


# ----------------------------------------------------------------------
# SongEntry
# ----------------------------------------------------------------------


def _insert_song_entries(connection: sqlite3.Connection, setlist_id: int, entries: list[SongEntry]) -> None:
    connection.executemany(
        """
        INSERT INTO song_entries (
            setlist_id, order_index, ocr_title, audio_match_json, logic_match_json,
            logic_tempo, manually_verified, youtube_match_json, output_folder,
            chart_match_json, chart_pdf_path, notes,
            excluded_from_drive, excluded_from_youtube, excluded_from_logic
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                setlist_id,
                entry.order,
                entry.ocr_title,
                _match_to_json(entry.audio_match),
                _match_to_json(entry.logic_match),
                entry.logic_tempo,
                int(entry.manually_verified),
                _match_to_json(entry.youtube_match),
                entry.output_folder,
                _match_to_json(entry.chart_match),
                entry.chart_pdf_path,
                entry.notes,
                int(entry.excluded_from_drive),
                int(entry.excluded_from_youtube),
                int(entry.excluded_from_logic),
            )
            for entry in entries
        ],
    )


def add_song_entries(setlist_id: int, entries: list[SongEntry]) -> None:
    """Persist every SongEntry in `entries` under the given Setlist."""
    with _connect() as connection:
        _insert_song_entries(connection, setlist_id, entries)


def replace_song_entries(setlist_id: int, entries: list[SongEntry]) -> None:
    """
    Delete this Setlist's existing SongEntry rows and insert `entries` in
    their place -- used after re-matching (see scheduling_service.py's
    prepare_*_matches) to persist hand-corrected or freshly-matched
    entries. SongEntry has no row id of its own (see its dataclass docs)
    -- the whole list is always the unit of truth, the same way the rest
    of the app already treats it, so a full replace is simpler and no
    less correct than trying to update individual rows.
    """
    with _connect() as connection:
        connection.execute("DELETE FROM song_entries WHERE setlist_id = ?", (setlist_id,))
        _insert_song_entries(connection, setlist_id, entries)


def get_song_entries(setlist_id: int) -> list[SongEntry]:
    """Load every SongEntry for a Setlist, in their original `order`."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT order_index, ocr_title, audio_match_json, logic_match_json, logic_tempo,
                   manually_verified, youtube_match_json, output_folder, chart_match_json, chart_pdf_path, notes,
                   excluded_from_drive, excluded_from_youtube, excluded_from_logic
            FROM song_entries
            WHERE setlist_id = ?
            ORDER BY order_index ASC
            """,
            (setlist_id,),
        ).fetchall()

    return [
        SongEntry(
            order=row[0],
            ocr_title=row[1],
            audio_match=_library_match_from_json(row[2]),
            logic_match=_library_match_from_json(row[3]),
            logic_tempo=row[4],
            manually_verified=bool(row[5]),
            youtube_match=_youtube_match_from_json(row[6]),
            output_folder=row[7],
            chart_match=_chart_match_from_json(row[8]),
            chart_pdf_path=row[9],
            notes=row[10],
            excluded_from_drive=bool(row[11]),
            excluded_from_youtube=bool(row[12]),
            excluded_from_logic=bool(row[13]),
        )
        for row in rows
    ]


# ----------------------------------------------------------------------
# ScheduledTask
# ----------------------------------------------------------------------


def create_scheduled_task(
    setlist_id: int,
    action_type: ActionType,
    scheduled_for: datetime.datetime,
    approval_group_id: Optional[str] = None,
    manifest_path: Optional[str] = None,
    target_name: Optional[str] = None,
) -> int:
    """Create one deferred, approval-gated task. Returns the new row's id."""
    created_at = datetime.datetime.now().isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO scheduled_tasks
                (setlist_id, action_type, approval_group_id, status, scheduled_for, created_at, manifest_path, target_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                setlist_id,
                action_type.value,
                approval_group_id,
                TaskStatus.PENDING.value,
                scheduled_for.isoformat(),
                created_at,
                manifest_path,
                target_name,
            ),
        )
        return cursor.lastrowid


_SCHEDULED_TASK_COLUMNS = (
    "id, setlist_id, action_type, approval_group_id, status, scheduled_for, "
    "created_at, approved_at, executed_at, manifest_path, error_message, target_name"
)


def list_due_tasks(now: Optional[datetime.datetime] = None) -> list[ScheduledTask]:
    """
    Every PENDING task whose `scheduled_for` has arrived -- what the
    background scheduler check (see the architecture discussion) polls
    for before flipping them to AWAITING_APPROVAL and notifying you.
    """
    now = now or datetime.datetime.now()
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT {_SCHEDULED_TASK_COLUMNS}
            FROM scheduled_tasks
            WHERE status = ? AND scheduled_for <= ?
            ORDER BY scheduled_for ASC
            """,
            (TaskStatus.PENDING.value, now.isoformat()),
        ).fetchall()

    return [_scheduled_task_from_row(row) for row in rows]


def list_tasks_due_within(cutoff: datetime.datetime) -> list[ScheduledTask]:
    """
    Every task that will want to RUN at or before `cutoff` -- i.e.
    anything still PENDING that's about to come due, plus anything
    already AWAITING_APPROVAL (which runs the instant you approve it).

    Unlike list_due_tasks, this is a pure read: it looks into the near
    future and flips nothing. It's what lets the re-authentication
    reminder fire only when a task is actually imminent, instead of
    nagging on every background check (see scheduler_runner).
    """
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT {_SCHEDULED_TASK_COLUMNS}
            FROM scheduled_tasks
            WHERE status IN (?, ?) AND scheduled_for <= ?
            ORDER BY scheduled_for ASC
            """,
            (TaskStatus.PENDING.value, TaskStatus.AWAITING_APPROVAL.value, cutoff.isoformat()),
        ).fetchall()

    return [_scheduled_task_from_row(row) for row in rows]


def list_tasks_by_status(status: TaskStatus) -> list[ScheduledTask]:
    """
    Every task currently in one status, regardless of setlist or
    scheduled_for -- unlike list_due_tasks (which only looks at PENDING
    tasks whose time has arrived), this looks at every task in that
    status at once.
    """
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT {_SCHEDULED_TASK_COLUMNS}
            FROM scheduled_tasks
            WHERE status = ?
            ORDER BY scheduled_for ASC
            """,
            (status.value,),
        ).fetchall()
    return [_scheduled_task_from_row(row) for row in rows]


_ACTIVE_TASK_STATUSES = (
    TaskStatus.PENDING.value,
    TaskStatus.AWAITING_APPROVAL.value,
    TaskStatus.DECLINED.value,
    TaskStatus.FAILED.value,
)


def list_active_scheduled_tasks() -> list[ScheduledTask]:
    """
    Every PENDING, AWAITING_APPROVAL, DECLINED, or FAILED task across
    every setlist and action type (Drive, YouTube, Logic Pro alike),
    ordered by scheduled_for -- what the main-menu Task Manager (the
    bell icon) lists. Only DONE tasks are excluded -- once a task
    actually succeeds, it's done being "active" and the log file is
    where its outcome lives; a FAILED task stays listed specifically so
    you can fix whatever caused it (a missing match, an expired login)
    and hit Run Now again, per the decision that failing once shouldn't
    mean starting over from a new Schedule.

    Declining a task (see scheduling_service.decline_task) does NOT
    delete it -- it stays exactly here, still reschedulable and
    deletable, until you explicitly delete it. That's the whole reason
    this manager exists: a decline is a "not now," never a silent
    delete.
    """
    with _connect() as connection:
        placeholders = ", ".join("?" for _ in _ACTIVE_TASK_STATUSES)
        rows = connection.execute(
            f"""
            SELECT {_SCHEDULED_TASK_COLUMNS}
            FROM scheduled_tasks
            WHERE status IN ({placeholders})
            ORDER BY scheduled_for ASC
            """,
            _ACTIVE_TASK_STATUSES,
        ).fetchall()
    return [_scheduled_task_from_row(row) for row in rows]


def delete_scheduled_task(task_id: int) -> None:
    """Permanently removes one task -- the only way a task ever disappears from list_active_scheduled_tasks before finishing."""
    with _connect() as connection:
        connection.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))


def purge_completed_tasks() -> int:
    """
    Deletes successfully-executed (DONE) task rows, returning how many
    went. Called on app startup and on every background scheduler run.

    A DONE task is already invisible -- list_active_scheduled_tasks
    excludes it on purpose, so nothing in the Task Manager changes --
    and it can never run again, so the row is pure residue once the
    Logic Pro chain below no longer needs it. Its outcome is still in
    the log file either way.

    THE ONE EXCEPTION: a DONE prepare_logic_project row holds the
    manifest_path that its setlist's logic_automation step reads back
    via _require_prior_step_done (see scheduling_service). Deleting it
    early wouldn't just lose history -- it would make the automation
    half un-runnable, including a retry of one that FAILED. So a
    prepare row is kept until that same setlist has a DONE
    logic_automation to show for it, which also covers the case where
    the automation half simply hasn't been scheduled yet.

    Setlists and song entries are left completely alone; only task rows
    are purged.
    """
    with _connect() as connection:
        # Resolved to a concrete id list first, rather than deleting
        # straight from a correlated subquery: within a single DELETE,
        # SQLite could remove a setlist's logic_automation row before
        # evaluating the EXISTS check that its prepare row depends on,
        # which would strand that prepare row as undeletable forever.
        rows = connection.execute(
            """
            SELECT id FROM scheduled_tasks AS task
            WHERE task.status = ?
              AND (
                task.action_type != ?
                OR EXISTS (
                    SELECT 1 FROM scheduled_tasks AS automation
                    WHERE automation.setlist_id = task.setlist_id
                      AND automation.action_type = ?
                      AND automation.status = ?
                )
              )
            """,
            (
                TaskStatus.DONE.value,
                ActionType.PREPARE_LOGIC_PROJECT.value,
                ActionType.LOGIC_AUTOMATION.value,
                TaskStatus.DONE.value,
            ),
        ).fetchall()

        task_ids = [row[0] for row in rows]
        if not task_ids:
            return 0

        placeholders = ", ".join("?" for _ in task_ids)
        connection.execute(f"DELETE FROM scheduled_tasks WHERE id IN ({placeholders})", task_ids)

    logger.info("Purged %d completed task row(s).", len(task_ids))
    return len(task_ids)


def reschedule_task(task_id: int, scheduled_for: datetime.datetime) -> None:
    """
    Moves an existing task to a new scheduled_for and resets it to
    PENDING -- used to push a task back or forward in time, or to give a
    DECLINED task a fresh chance, without losing its identity (approval
    group, target_name, etc.) the way delete-and-recreate would.
    """
    with _connect() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET scheduled_for = ?, status = ? WHERE id = ?",
            (scheduled_for.isoformat(), TaskStatus.PENDING.value, task_id),
        )


def get_task(task_id: int) -> Optional[ScheduledTask]:
    """One ScheduledTask by id, or None if it doesn't exist."""
    with _connect() as connection:
        row = connection.execute(
            f"SELECT {_SCHEDULED_TASK_COLUMNS} FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return _scheduled_task_from_row(row) if row is not None else None


def list_tasks_in_group(approval_group_id: str) -> list[ScheduledTask]:
    """Every task sharing one approval_group_id -- what one combined approval acts on."""
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT {_SCHEDULED_TASK_COLUMNS} FROM scheduled_tasks WHERE approval_group_id = ?",
            (approval_group_id,),
        ).fetchall()
    return [_scheduled_task_from_row(row) for row in rows]


def find_active_task(setlist_id: int, action_type: ActionType) -> Optional[ScheduledTask]:
    """
    The most recent still-pending-or-awaiting task of `action_type` for
    this Setlist, if any -- used by scheduling_service.schedule_task to
    decide whether a new Drive/YouTube task should join an existing
    sibling's approval group rather than starting its own.
    """
    with _connect() as connection:
        row = connection.execute(
            f"""
            SELECT {_SCHEDULED_TASK_COLUMNS} FROM scheduled_tasks
            WHERE setlist_id = ? AND action_type = ? AND status IN (?, ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (setlist_id, action_type.value, TaskStatus.PENDING.value, TaskStatus.AWAITING_APPROVAL.value),
        ).fetchone()
    return _scheduled_task_from_row(row) if row is not None else None


def set_task_approval_group(task_id: int, approval_group_id: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET approval_group_id = ? WHERE id = ?", (approval_group_id, task_id)
        )


def find_latest_task(setlist_id: int, action_type: ActionType) -> Optional[ScheduledTask]:
    """
    The most recently created task of `action_type` for this Setlist,
    regardless of status -- used by scheduling_service.py's Logic Pro
    pipeline to check whether the previous step actually finished
    (DONE) before letting the next one run, and to look up a completed
    PREPARE_LOGIC_PROJECT task's manifest_path for the LOGIC_AUTOMATION step
    that depends on it.
    """
    with _connect() as connection:
        row = connection.execute(
            f"""
            SELECT {_SCHEDULED_TASK_COLUMNS} FROM scheduled_tasks
            WHERE setlist_id = ? AND action_type = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (setlist_id, action_type.value),
        ).fetchone()
    return _scheduled_task_from_row(row) if row is not None else None


def set_task_manifest_path(task_id: int, manifest_path: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE scheduled_tasks SET manifest_path = ? WHERE id = ?", (manifest_path, task_id)
        )


def update_task_status(
    task_id: int,
    status: TaskStatus,
    approved_at: Optional[datetime.datetime] = None,
    executed_at: Optional[datetime.datetime] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Update a task's status, optionally stamping approved_at/executed_at
    and/or setting error_message.

    Uses COALESCE so a call that only cares about one field (e.g. moving
    to RUNNING) never wipes out a timestamp/message a PREVIOUS call
    already set -- each field only changes when this call is explicitly
    given a new value for it. error_message is the one exception: it's
    always overwritten (including cleared back to NULL) since a
    successful DONE after a prior FAILED attempt should drop the old
    error, not keep showing it.
    """
    with _connect() as connection:
        connection.execute(
            """
            UPDATE scheduled_tasks
            SET status = ?,
                approved_at = COALESCE(?, approved_at),
                executed_at = COALESCE(?, executed_at),
                error_message = ?
            WHERE id = ?
            """,
            (
                status.value,
                approved_at.isoformat() if approved_at else None,
                executed_at.isoformat() if executed_at else None,
                error_message,
                task_id,
            ),
        )


def _scheduled_task_from_row(row: tuple) -> ScheduledTask:
    return ScheduledTask(
        id=row[0],
        setlist_id=row[1],
        action_type=ActionType(row[2]),
        approval_group_id=row[3],
        status=TaskStatus(row[4]),
        scheduled_for=datetime.datetime.fromisoformat(row[5]),
        created_at=datetime.datetime.fromisoformat(row[6]),
        approved_at=datetime.datetime.fromisoformat(row[7]) if row[7] else None,
        executed_at=datetime.datetime.fromisoformat(row[8]) if row[8] else None,
        manifest_path=row[9],
        error_message=row[10],
        target_name=row[11],
    )
