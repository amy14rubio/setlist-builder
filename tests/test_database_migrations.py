"""
Regression tests for app/services/database_service.py's schema
migrations -- specifically the two real bugs hit (and fixed) this
session, both against what was, at the time, the developer's actual
production database:

1. _migrate_scheduled_tasks_if_needed's "already current" check used to
   require the literal substring "copy_audio" in the table's stored SQL
   to consider it up to date. Once _migrate_action_type_values_if_needed
   (added later) rewrote the table to no longer mention "copy_audio" at
   all, that check started returning False on an ALREADY-migrated,
   ALREADY-non-empty table, hitting the "has rows, can't safely guess"
   branch and raising RuntimeError -- a real crash-loop in the deployed
   scheduler_runner.py launchd agent, happening every single run.

2. _migrate_action_type_values_if_needed renames the table aside,
   rebuilds it, and drops the old one -- but SQLite indexes stay
   attached to whatever the table is currently called, so the DROP TABLE
   silently destroyed scheduled_tasks' two indexes along with the
   renamed-aside old table. Fixed by re-running the CREATE INDEX
   statements after the rebuild.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from app.services import database_service


def _real_db_path(isolated_home) -> str:
    return str(isolated_home / "Library" / "Application Support" / "SetlistBuilder" / "setlist_builder.db")


def _create_pre_migration_schema(db_path: str) -> None:
    """
    Builds a scheduled_tasks table shaped like it was BEFORE either
    migration in this file existed: the old action_type CHECK
    constraint (copy_audio/build_manifest, no prepare_logic_project),
    and no target_name column -- with one real row in it, since both
    bugs above only manifest once the table already has data.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE setlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monthly_import_id INTEGER,
            source_type TEXT NOT NULL,
            service_date TEXT NOT NULL,
            day_name TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setlist_id INTEGER NOT NULL REFERENCES setlists(id),
            action_type TEXT NOT NULL CHECK (
                action_type IN ('copy_audio', 'build_manifest', 'logic_automation', 'drive', 'youtube')
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
            error_message TEXT
        )
        """
    )
    connection.execute("CREATE INDEX idx_scheduled_tasks_setlist_id ON scheduled_tasks(setlist_id)")
    connection.execute("CREATE INDEX idx_scheduled_tasks_due ON scheduled_tasks(status, scheduled_for)")

    now = datetime.datetime.now().isoformat()
    connection.execute(
        "INSERT INTO setlists (source_type, service_date, created_at) VALUES ('single_screenshot', ?, ?)",
        (datetime.date.today().isoformat(), now),
    )
    connection.execute(
        """
        INSERT INTO scheduled_tasks (setlist_id, action_type, status, scheduled_for, created_at)
        VALUES (1, 'copy_audio', 'done', ?, ?)
        """,
        (now, now),
    )
    connection.commit()
    connection.close()


def test_migrating_a_real_pre_existing_database_does_not_raise(isolated_home):
    """The exact scenario that crashed scheduler_runner.py: an old-shape table that already has rows."""
    db_path = _real_db_path(isolated_home)
    _create_pre_migration_schema(db_path)

    database_service.initialize_database()  # must not raise


def test_migration_is_idempotent_on_a_second_run(isolated_home):
    """
    The actual bug: the FIRST initialize_database() call migrated the
    table successfully, but a SECOND call (e.g. the next launchd tick)
    crashed with RuntimeError, because the "already current" check for
    the OLDER migration was still looking for a substring ("copy_audio")
    that the newer migration had since removed from the schema.
    """
    db_path = _real_db_path(isolated_home)
    _create_pre_migration_schema(db_path)

    database_service.initialize_database()
    database_service.initialize_database()  # this second call is what actually crashed in production
    database_service.initialize_database()  # and a third, for good measure


def test_migration_preserves_existing_rows_with_remapped_action_type(isolated_home):
    db_path = _real_db_path(isolated_home)
    _create_pre_migration_schema(db_path)

    database_service.initialize_database()

    # The row is DONE, so it won't show up in list_active_scheduled_tasks
    # (which excludes DONE) -- fetch it directly instead.
    task = database_service.get_task(1)
    assert task is not None
    assert task.action_type.value == "prepare_logic_project"  # remapped from 'copy_audio'


def test_migration_preserves_the_two_indexes(isolated_home):
    """
    The rename-and-rebuild the migration does drops the old table's
    indexes along with it (SQLite indexes stay attached to whatever the
    table is currently called) -- this asserts they get explicitly
    recreated on the new table afterward.
    """
    db_path = _real_db_path(isolated_home)
    _create_pre_migration_schema(db_path)

    database_service.initialize_database()

    connection = sqlite3.connect(db_path)
    index_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='scheduled_tasks'"
        ).fetchall()
    }
    connection.close()

    assert "idx_scheduled_tasks_setlist_id" in index_names
    assert "idx_scheduled_tasks_due" in index_names


def test_migration_adds_target_name_column(isolated_home):
    """A pre-existing database also predates the target_name column entirely -- confirms it gets added, not just left missing."""
    db_path = _real_db_path(isolated_home)
    _create_pre_migration_schema(db_path)

    database_service.initialize_database()

    connection = sqlite3.connect(db_path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_tasks)").fetchall()}
    connection.close()

    assert "target_name" in columns


def test_fresh_database_needs_no_migration(isolated_home):
    """A brand-new install (no pre-existing file at all) should just work, with no migration path involved."""
    database_service.initialize_database()
    database_service.initialize_database()  # idempotent here too


def _create_pre_exclusion_song_entries_schema(db_path: str) -> None:
    """
    A song_entries table shaped like it was before per-destination
    inclusion existed -- no excluded_from_drive/_youtube/_logic columns
    at all -- with one real row in it, since the migration only matters
    once there's existing data to preserve.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE setlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monthly_import_id INTEGER,
            source_type TEXT NOT NULL,
            service_date TEXT NOT NULL,
            day_name TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE song_entries (
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
            notes TEXT NOT NULL DEFAULT ''
        )
        """
    )
    now = datetime.datetime.now().isoformat()
    connection.execute(
        "INSERT INTO setlists (source_type, service_date, created_at) VALUES ('single_screenshot', ?, ?)",
        (datetime.date.today().isoformat(), now),
    )
    connection.execute(
        "INSERT INTO song_entries (setlist_id, order_index, ocr_title) VALUES (1, 1, 'Pre-Existing Song')"
    )
    connection.commit()
    connection.close()


def test_migration_adds_song_entry_exclusion_columns(isolated_home):
    """A pre-existing database also predates per-destination inclusion entirely -- confirms the columns get added, defaulting to 0 (included everywhere)."""
    db_path = _real_db_path(isolated_home)
    _create_pre_exclusion_song_entries_schema(db_path)

    database_service.initialize_database()  # must not raise, and must not lose the existing row

    connection = sqlite3.connect(db_path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(song_entries)").fetchall()}
    row = connection.execute(
        "SELECT ocr_title, excluded_from_drive, excluded_from_youtube, excluded_from_logic FROM song_entries WHERE id = 1"
    ).fetchone()
    connection.close()

    assert {"excluded_from_drive", "excluded_from_youtube", "excluded_from_logic"} <= columns
    assert row == ("Pre-Existing Song", 0, 0, 0)  # existing row defaults to "included everywhere"


def test_exclusion_flags_round_trip_through_save_and_load(isolated_home):
    """The actual bug this schema change exists to fix: per-tab exclusion must survive a save/load cycle, not just live in memory."""
    from app.models.song_entry import SongEntry

    database_service.initialize_database()
    setlist_id = database_service.create_setlist(
        source_type="single_screenshot", service_date=datetime.date(2026, 9, 27)
    )
    entries = [
        SongEntry(order=1, ocr_title="Included Everywhere"),
        SongEntry(order=2, ocr_title="Drive Only", excluded_from_youtube=True, excluded_from_logic=True),
        SongEntry(order=3, ocr_title="YouTube Only", excluded_from_drive=True, excluded_from_logic=True),
    ]
    database_service.add_song_entries(setlist_id, entries)

    loaded = database_service.get_song_entries(setlist_id)

    by_title = {entry.ocr_title: entry for entry in loaded}
    assert by_title["Included Everywhere"].excluded_from_drive is False
    assert by_title["Included Everywhere"].excluded_from_youtube is False
    assert by_title["Included Everywhere"].excluded_from_logic is False
    assert by_title["Drive Only"].excluded_from_drive is False
    assert by_title["Drive Only"].excluded_from_youtube is True
    assert by_title["Drive Only"].excluded_from_logic is True
    assert by_title["YouTube Only"].excluded_from_drive is True
    assert by_title["YouTube Only"].excluded_from_youtube is False
    assert by_title["YouTube Only"].excluded_from_logic is True
