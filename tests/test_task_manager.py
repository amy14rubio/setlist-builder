"""
Regression tests for app/ui/widgets/task_manager_dialog.py and the
database_service functions it relies on -- covering:

1. The "Run Now must not re-run an already-succeeded sibling" bug: a
   Drive+YouTube pair scheduled together shares an approval_group_id,
   and the OLD "Run Now" implementation called approve_group(), which
   re-executes EVERY task in that group regardless of status. If Drive
   succeeded and YouTube failed, retrying the row would silently
   re-upload the YouTube playlist a second time even though it had
   nothing to do with the failure. Fixed by only re-running the tasks
   actually shown in that row (i.e. still "active").

2. list_active_scheduled_tasks: FAILED tasks must show up (so they can
   be retried), DONE tasks must not (once succeeded, that's not
   "active" anymore).

3. Declining a task must never be equivalent to deleting it -- the
   entire reason the Task Manager exists. reschedule_task/delete_task
   must act correctly on individual tasks.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from app.models.scheduled_task import ActionType, TaskStatus
from app.models.settings import Settings
from app.models.song_entry import SongEntry, YouTubeMatch
from app.services import database_service, scheduling_service
from app.ui.widgets.task_manager_dialog import TaskManagerDialog


@pytest.fixture
def setlist_id(isolated_home):
    database_service.initialize_database()
    sid = database_service.create_setlist(source_type="single_screenshot", service_date=datetime.date(2026, 9, 27))
    database_service.add_song_entries(sid, [SongEntry(order=1, ocr_title="Song A")])
    return sid


def test_declining_a_task_does_not_delete_it(setlist_id):
    task_id = scheduling_service.schedule_task(setlist_id, ActionType.DRIVE, datetime.datetime.now())
    scheduling_service.decline_task(task_id)

    task = database_service.get_task(task_id)
    assert task is not None
    assert task.status == TaskStatus.DECLINED
    assert task_id in [t.id for t in database_service.list_active_scheduled_tasks()]


def test_active_tasks_include_failed_but_exclude_done(setlist_id):
    done_id = scheduling_service.schedule_task(setlist_id, ActionType.DRIVE, datetime.datetime.now())
    failed_id = scheduling_service.schedule_task(setlist_id, ActionType.YOUTUBE, datetime.datetime.now())
    database_service.update_task_status(done_id, TaskStatus.DONE, executed_at=datetime.datetime.now())
    database_service.update_task_status(failed_id, TaskStatus.FAILED, error_message="boom")

    active_ids = {t.id for t in database_service.list_active_scheduled_tasks()}
    assert failed_id in active_ids
    assert done_id not in active_ids


def test_reschedule_resets_status_and_moves_the_date(setlist_id):
    task_id = scheduling_service.schedule_task(setlist_id, ActionType.DRIVE, datetime.datetime.now())
    scheduling_service.decline_task(task_id)

    new_time = datetime.datetime.now() + datetime.timedelta(days=3)
    scheduling_service.reschedule_task(task_id, new_time)

    task = database_service.get_task(task_id)
    assert task.status == TaskStatus.PENDING
    assert task.scheduled_for == new_time


def test_delete_actually_removes_the_row(setlist_id):
    task_id = scheduling_service.schedule_task(setlist_id, ActionType.DRIVE, datetime.datetime.now())
    scheduling_service.delete_task(task_id)

    assert database_service.get_task(task_id) is None


def test_run_now_does_not_rerun_an_already_done_sibling(setlist_id, qapp):
    """
    The core Task Manager bug: Drive succeeds, YouTube fails, both share
    an approval_group_id. Clicking Run Now on the (now failed-only) row
    must re-run ONLY YouTube, never touch Drive's already-successful run
    again.
    """
    drive_id = scheduling_service.schedule_task(setlist_id, ActionType.DRIVE, datetime.datetime.now())
    youtube_id = scheduling_service.schedule_task(setlist_id, ActionType.YOUTUBE, datetime.datetime.now())
    group_id = database_service.get_task(drive_id).approval_group_id
    assert group_id is not None and database_service.get_task(youtube_id).approval_group_id == group_id

    # Simulate the first run: Drive succeeded, YouTube failed.
    database_service.update_task_status(drive_id, TaskStatus.DONE, executed_at=datetime.datetime.now())
    database_service.update_task_status(youtube_id, TaskStatus.FAILED, error_message="expired login")

    settings = Settings()
    dialog = TaskManagerDialog(settings)

    # The row a real click would act on: only the tasks list_active_scheduled_tasks
    # actually returns for this group (i.e. NOT the already-DONE Drive task).
    active_in_group = [t for t in database_service.list_active_scheduled_tasks() if t.approval_group_id == group_id]
    assert [t.id for t in active_in_group] == [youtube_id]

    # QMessageBox.warning would otherwise block forever in a headless test
    # waiting for a click -- approve_task is fully mocked below (it does
    # nothing to the database), so the task's status stays FAILED and
    # _on_run_now's own "surface a failure" dialog would normally fire.
    with patch("app.ui.widgets.task_manager_dialog.approve_task") as mock_approve, \
         patch("app.ui.widgets.task_manager_dialog.QMessageBox"):
        dialog._on_run_now(group_id, active_in_group)

    # approve_task must be called for YouTube, and Drive's id must never appear.
    called_ids = [call.args[0] for call in mock_approve.call_args_list]
    assert called_ids == [youtube_id]
    assert drive_id not in called_ids
