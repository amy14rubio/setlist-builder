"""
Regression tests for notification_action_listener.py's Approve/Decline/
dismiss routing -- the logic behind the actionable macOS notification's
buttons. Verified manually during development (including a real live
click-through), captured here as a real test.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.models.scheduled_task import TaskStatus
import notification_action_listener as listener


def _fake_task(task_id=1, status=TaskStatus.AWAITING_APPROVAL, error_message=None):
    task = MagicMock()
    task.id = task_id
    task.setlist_id = 1
    task.status = status
    task.error_message = error_message
    task.action_type.value = "drive"
    return task


def test_approve_choice_calls_approve_task_and_sends_completion_notification():
    task = _fake_task()
    with patch.object(listener, "get_task", return_value=task), \
         patch.object(listener, "get_setlist", return_value=None), \
         patch.object(listener, "ask_via_notification", return_value="Approve"), \
         patch.object(listener, "approve_task") as mock_approve, \
         patch.object(listener, "decline_task") as mock_decline, \
         patch.object(listener, "send_notification") as mock_notify, \
         patch.object(listener.Settings, "load", return_value=MagicMock()):
        listener.listen_and_act([1])

    assert mock_approve.called
    assert not mock_decline.called
    assert mock_notify.called  # the completion banner


def test_decline_choice_calls_decline_task_not_approve():
    task = _fake_task()
    with patch.object(listener, "get_task", return_value=task), \
         patch.object(listener, "get_setlist", return_value=None), \
         patch.object(listener, "ask_via_notification", return_value="Decline"), \
         patch.object(listener, "approve_task") as mock_approve, \
         patch.object(listener, "decline_task") as mock_decline, \
         patch.object(listener, "send_notification") as mock_notify:
        listener.listen_and_act([1])

    assert mock_decline.called
    assert not mock_approve.called
    assert not mock_notify.called  # no completion banner for a decline -- nothing ran


def test_dismissing_the_notification_does_neither():
    task = _fake_task()
    with patch.object(listener, "get_task", return_value=task), \
         patch.object(listener, "get_setlist", return_value=None), \
         patch.object(listener, "ask_via_notification", return_value=""), \
         patch.object(listener, "approve_task") as mock_approve, \
         patch.object(listener, "decline_task") as mock_decline:
        listener.listen_and_act([1])

    assert not mock_approve.called
    assert not mock_decline.called


def test_completion_notification_reports_failure_when_task_ends_failed():
    """Approve_task never raises (see scheduling_service) -- a failure must be surfaced by re-checking status, not by an exception."""
    task = _fake_task(status=TaskStatus.FAILED, error_message="login expired")
    with patch.object(listener, "get_task", return_value=task), \
         patch.object(listener, "get_setlist", return_value=None), \
         patch.object(listener, "send_notification") as mock_notify:
        listener._send_completion_notification([task])

    title, message = mock_notify.call_args.args
    assert "Failed" in message
    assert "login expired" in message
