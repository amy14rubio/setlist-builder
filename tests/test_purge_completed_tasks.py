"""
Tests for database_service.purge_completed_tasks -- the cleanup that
stops successfully-executed task rows accumulating forever.

The safety property under test is the Logic Pro one. The two Logic Pro
steps are linked: LOGIC_AUTOMATION reads its build manifest back off
the DONE PREPARE_LOGIC_PROJECT row for the same setlist
(_require_prior_step_done in scheduling_service). Purging that row too
eagerly is not a cosmetic loss -- it makes the automation half
permanently un-runnable, including retrying one that FAILED. Everything
else about a DONE row really is residue: the Task Manager already hides
DONE tasks, and the log file keeps the outcome.
"""

from __future__ import annotations

import datetime

import pytest

from app.models.scheduled_task import ActionType, TaskStatus
from app.models.song_entry import SongEntry
from app.services import database_service


@pytest.fixture
def setlist_id(isolated_home):
    database_service.initialize_database()
    sid = database_service.create_setlist(source_type="single_screenshot", service_date=datetime.date(2026, 10, 4))
    database_service.add_song_entries(sid, [SongEntry(order=1, ocr_title="Song A")])
    return sid


def _task(setlist_id, action_type, status):
    task_id = database_service.create_scheduled_task(setlist_id, action_type, datetime.datetime.now())
    if status is not TaskStatus.PENDING:
        database_service.update_task_status(task_id, status, executed_at=datetime.datetime.now())
    return task_id


def test_a_done_drive_task_is_purged(setlist_id):
    task_id = _task(setlist_id, ActionType.DRIVE, TaskStatus.DONE)

    assert database_service.purge_completed_tasks() == 1
    assert database_service.get_task(task_id) is None


@pytest.mark.parametrize(
    "status",
    [TaskStatus.PENDING, TaskStatus.AWAITING_APPROVAL, TaskStatus.DECLINED, TaskStatus.FAILED],
)
def test_an_unfinished_task_is_never_purged(setlist_id, status):
    """A FAILED task especially -- it stays in the Task Manager precisely so it can be retried."""
    task_id = _task(setlist_id, ActionType.YOUTUBE, status)

    database_service.purge_completed_tasks()
    assert database_service.get_task(task_id) is not None


def test_purging_leaves_the_setlist_and_its_songs_alone(setlist_id):
    _task(setlist_id, ActionType.DRIVE, TaskStatus.DONE)

    database_service.purge_completed_tasks()

    assert database_service.get_setlist(setlist_id) is not None
    assert len(database_service.get_song_entries(setlist_id)) == 1


def test_a_done_prepare_step_survives_while_its_automation_is_unfinished(setlist_id):
    """The real hazard: that row holds the manifest_path the automation half still has to read."""
    prepare_id = _task(setlist_id, ActionType.PREPARE_LOGIC_PROJECT, TaskStatus.DONE)
    _task(setlist_id, ActionType.LOGIC_AUTOMATION, TaskStatus.FAILED)

    database_service.purge_completed_tasks()
    assert database_service.get_task(prepare_id) is not None


def test_a_done_prepare_step_survives_when_no_automation_is_scheduled_yet(setlist_id):
    """Preparing today and scheduling the automation next week is normal -- don't strand it."""
    prepare_id = _task(setlist_id, ActionType.PREPARE_LOGIC_PROJECT, TaskStatus.DONE)

    database_service.purge_completed_tasks()
    assert database_service.get_task(prepare_id) is not None


def test_both_logic_steps_are_purged_once_the_automation_has_also_finished(setlist_id):
    prepare_id = _task(setlist_id, ActionType.PREPARE_LOGIC_PROJECT, TaskStatus.DONE)
    automation_id = _task(setlist_id, ActionType.LOGIC_AUTOMATION, TaskStatus.DONE)

    database_service.purge_completed_tasks()

    assert database_service.get_task(prepare_id) is None
    assert database_service.get_task(automation_id) is None


def test_one_setlists_finished_logic_chain_does_not_free_anothers_prepare_row(isolated_home):
    """The EXISTS check has to be per-setlist, or a prepare row loses its manifest to an unrelated week."""
    database_service.initialize_database()
    finished = database_service.create_setlist("single_screenshot", datetime.date(2026, 10, 4))
    in_progress = database_service.create_setlist("single_screenshot", datetime.date(2026, 10, 11))

    finished_prepare = _task(finished, ActionType.PREPARE_LOGIC_PROJECT, TaskStatus.DONE)
    finished_automation = _task(finished, ActionType.LOGIC_AUTOMATION, TaskStatus.DONE)
    still_needed = _task(in_progress, ActionType.PREPARE_LOGIC_PROJECT, TaskStatus.DONE)

    database_service.purge_completed_tasks()

    assert database_service.get_task(still_needed) is not None
    assert database_service.get_task(finished_prepare) is None
    assert database_service.get_task(finished_automation) is None


def test_purging_an_already_clean_database_is_a_no_op(setlist_id):
    _task(setlist_id, ActionType.DRIVE, TaskStatus.DONE)
    database_service.purge_completed_tasks()

    assert database_service.purge_completed_tasks() == 0
