"""
Tests for the re-authentication prompt -- the notification you can log
back in from directly, without opening the app.

Two behaviors matter here, and they're easy to regress independently:

  1. WHEN it fires. The old version probed both Google logins on every
     single background run (every 15 minutes, forever) and nagged about
     any stale one regardless of whether anything needed it -- so a
     token that expired on Monday nagged all week for a task that
     wasn't until Sunday. It must now fire only inside
     _REAUTH_LEAD_TIME of a task's run time, only for the services
     those particular tasks actually use, and only once per stale
     login rather than once per poll.
  2. WHAT the click does. It has to run the real interactive login
     (allow_interactive=True), which is the whole point -- a prompt
     that only reminded you would be no better than the banner it
     replaced.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from app.models.scheduled_task import ActionType
import notification_action_listener as listener
import scheduler_runner


def _task(action_type, task_id=1):
    task = MagicMock()
    task.id = task_id
    task.setlist_id = 1
    task.action_type = action_type
    return task


def _run_auth_check(tmp_path, imminent, health):
    """Runs _notify_auth_health against a fake world, returning the spawn mock."""
    with patch.object(scheduler_runner, "list_tasks_due_within", return_value=imminent), \
         patch.object(scheduler_runner, "check_auth_health", return_value=health) as mock_health, \
         patch.object(scheduler_runner, "_describe_task", return_value="Domingo 2026-10-04"), \
         patch.object(scheduler_runner, "_MARKER_DIR", tmp_path), \
         patch.object(scheduler_runner, "_spawn_listener", return_value=4242) as mock_spawn:
        scheduler_runner._notify_auth_health(MagicMock())
    return mock_spawn, mock_health


def test_nothing_due_soon_does_not_even_check_the_login(tmp_path):
    """The actual regression: probing Google every 15 minutes whether or not anything needed it."""
    mock_spawn, mock_health = _run_auth_check(tmp_path, imminent=[], health={"drive": False})

    assert not mock_health.called
    assert not mock_spawn.called


def test_an_imminent_logic_pro_task_alone_does_not_trigger_a_login_check(tmp_path):
    """Neither Logic Pro step touches Google, so a stale Drive token is irrelevant to it."""
    mock_spawn, mock_health = _run_auth_check(
        tmp_path,
        imminent=[_task(ActionType.PREPARE_LOGIC_PROJECT), _task(ActionType.LOGIC_AUTOMATION)],
        health={"drive": False, "youtube": False},
    )

    assert not mock_health.called
    assert not mock_spawn.called


def test_a_healthy_login_prompts_nothing(tmp_path):
    mock_spawn, _ = _run_auth_check(tmp_path, [_task(ActionType.DRIVE)], {"drive": True, "youtube": True})

    assert not mock_spawn.called


def test_a_stale_login_for_an_imminent_task_spawns_a_clickable_prompt(tmp_path):
    mock_spawn, _ = _run_auth_check(tmp_path, [_task(ActionType.DRIVE)], {"drive": False, "youtube": True})

    arguments = mock_spawn.call_args.args[0]
    assert arguments[:2] == ["--reauth", "drive"]
    assert "Domingo 2026-10-04" in arguments[2]  # names the task it's actually blocking


def test_only_the_services_the_imminent_tasks_need_are_prompted(tmp_path):
    """A stale YouTube login is not your problem on a week where only Drive runs."""
    mock_spawn, _ = _run_auth_check(tmp_path, [_task(ActionType.DRIVE)], {"drive": False, "youtube": False})

    assert mock_spawn.call_count == 1
    assert mock_spawn.call_args.args[0][1] == "drive"


def test_an_unconfigured_service_still_warns_rather_than_failing_silently(tmp_path):
    """check_auth_health omits a service with no client secret -- but a task needing it will still fail."""
    mock_spawn, _ = _run_auth_check(tmp_path, [_task(ActionType.YOUTUBE)], {})

    assert mock_spawn.call_args.args[0][1] == "youtube"


def test_a_prompt_already_waiting_is_not_duplicated_on_the_next_poll(tmp_path):
    """Without this, a stale login stacks up a fresh identical banner every 15 minutes."""
    (tmp_path / "reauth_drive.pid").write_text("4242")

    with patch.object(scheduler_runner.os, "kill"):  # pid 4242 "is" alive
        mock_spawn, _ = _run_auth_check(tmp_path, [_task(ActionType.DRIVE)], {"drive": False})

    assert not mock_spawn.called


def test_a_marker_left_by_a_dead_listener_does_not_block_a_new_prompt(tmp_path):
    """A listener killed by a restart (or one you already dismissed) must not silence the prompt forever."""
    (tmp_path / "reauth_drive.pid").write_text("4242")

    with patch.object(scheduler_runner.os, "kill", side_effect=OSError):  # pid is gone
        mock_spawn, _ = _run_auth_check(tmp_path, [_task(ActionType.DRIVE)], {"drive": False})

    assert mock_spawn.called


def test_spawning_records_the_pid_so_the_next_poll_can_dedupe(tmp_path):
    _run_auth_check(tmp_path, [_task(ActionType.DRIVE)], {"drive": False})

    assert (tmp_path / "reauth_drive.pid").read_text() == "4242"


# --- what the click actually does ----------------------------------------


def _run_prompt(choice, secret_path="/secrets/drive.json", credentials=MagicMock()):
    settings = MagicMock(google_drive_client_secret_path=secret_path)
    with patch.object(listener.Settings, "load", return_value=settings), \
         patch.object(listener, "ask_via_notification", return_value=choice), \
         patch.object(listener.drive_service, "authenticate", return_value=credentials) as mock_auth, \
         patch.object(listener, "send_notification") as mock_notify:
        listener.prompt_reauthentication("drive", "Log back in to run Domingo 2026-10-04.")
    return mock_auth, mock_notify


def test_clicking_log_in_actually_runs_the_interactive_login():
    """The whole point: allow_interactive=True is what opens the browser, with no app needed."""
    mock_auth, mock_notify = _run_prompt("Log In")

    mock_auth.assert_called_once_with("/secrets/drive.json", allow_interactive=True)
    assert "Logged back in" in mock_notify.call_args.args[1]


def test_dismissing_the_prompt_logs_nobody_in():
    mock_auth, mock_notify = _run_prompt("")

    assert not mock_auth.called
    assert not mock_notify.called


def test_a_login_that_does_not_take_is_reported_rather_than_claimed_as_success():
    """authenticate() returns None instead of raising on a bad secret -- the only failure signal there is."""
    _, mock_notify = _run_prompt("Log In", credentials=None)

    assert "didn't go through" in mock_notify.call_args.args[1]


def test_an_unconfigured_service_says_so_instead_of_offering_a_doomed_button():
    mock_auth, mock_notify = _run_prompt("Log In", secret_path="")

    assert not mock_auth.called
    assert "client secret" in mock_notify.call_args.args[1]
