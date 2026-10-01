"""
Scheduler runner -- Setlist Builder's background check.

This is the piece from the architecture discussion that runs
independently of the main app being open: point a launchd agent (or, to
test it by hand, just run this directly) at this script on an interval,
and it will:

  1. Flag every ScheduledTask whose `scheduled_for` has arrived, moving
     it from PENDING to AWAITING_APPROVAL (see
     scheduling_service.flag_due_tasks), and send one native macOS
     notification per task (Drive+YouTube pairs notify together, Logic
     Pro always alone).
  2. When a Drive/YouTube task is about to come due, check whether the
     login it needs is still valid (see
     scheduling_service.check_auth_health) and, if not, send a
     notification you can log back in directly from -- catching
     Google's Testing-mode 7-day refresh token expiry BEFORE it
     silently blocks an approved task, not after.

  3. Clear out task rows that already ran successfully and can never
     run again (see database_service.purge_completed_tasks), so the
     database doesn't just grow forever.

This script never touches YouTube, Drive, or Logic Pro itself -- it
only announces work. Approving a notified task (which actually runs it)
happens through the main app, via scheduling_service.approve_task /
approve_group.

Run manually to test:
    python scheduler_runner.py
"""

from __future__ import annotations

import datetime
import logging
import os
import subprocess
import sys
from pathlib import Path

from app.models.scheduled_task import ActionType, ScheduledTask
from app.models.settings import Settings
from app.services.database_service import (
    get_setlist,
    initialize_database,
    list_tasks_due_within,
    purge_completed_tasks,
)
from app.services.scheduling_service import check_auth_health, flag_due_tasks
from app.utils.logging_setup import setup_logging

logger = logging.getLogger(__name__)

_LISTENER_SCRIPT = Path(__file__).resolve().parent / "notification_action_listener.py"

# Maps check_auth_health's service-name keys to the ActionType that
# depends on that login, so a stale-login notification can name exactly
# which scheduled task(s) it will actually block -- Logic Pro has no
# entry here since neither of its steps touches Drive or YouTube auth.
_ACTION_TYPE_FOR_SERVICE = {"youtube": ActionType.YOUTUBE, "drive": ActionType.DRIVE}
_SERVICE_FOR_ACTION_TYPE = {action: service for service, action in _ACTION_TYPE_FOR_SERVICE.items()}

# How far ahead of a task's run time a stale login is worth interrupting
# you over. Deliberately DOUBLE launchd's own 15-minute interval: a
# 15-minute window polled every 15 minutes could land with barely a
# minute to spare, whereas 30 guarantees at least one check fires with
# a quarter hour of real warning left. Outside this window the login
# isn't checked at all -- a token that's expired today but whose task
# isn't until Sunday is not something to be nagged about now.
_REAUTH_LEAD_TIME = datetime.timedelta(minutes=30)

# One marker file per service, holding the pid of the re-auth listener
# currently waiting on a click. Without this, every 15-minute poll
# would stack up another identical unanswered notification for as long
# as the login stayed stale.
_MARKER_DIR = Path.home() / "Library" / "Application Support" / "SetlistBuilder"


def _spawn_listener(arguments: list[str]) -> int:
    """
    Launches notification_action_listener.py as its own detached
    process, so its notification's buttons work directly -- no need to
    open the app. Detached (start_new_session=True) because that
    listener blocks on terminal-notifier's own click prompt for as long
    as it takes you to respond, which this short-lived script
    (re-invoked by launchd on its own interval) can't afford to wait
    around for. Returns the new process's pid.
    """
    log_dir = Path.home() / "Library" / "Logs" / "SetlistBuilder"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Appended, not overwritten -- multiple listeners can be alive at
    # once, and each run's own app-level log line (via
    # app.utils.logging_setup) already goes to the main
    # logs/setlist_builder.log; this file only exists to catch a crash
    # that happens BEFORE logging is even set up, which silently
    # vanished into /dev/null the first time this ran under launchd.
    with (log_dir / "notification_listener.log").open("a") as log_file:
        process = subprocess.Popen(
            [sys.executable, str(_LISTENER_SCRIPT), *arguments],
            start_new_session=True,
            stdout=log_file,
            stderr=log_file,
        )
    return process.pid


def _spawn_actionable_notification(group: list[ScheduledTask]) -> None:
    """One Approve/Decline notification for a group of just-due tasks."""
    _spawn_listener([str(task.id) for task in group])


def _notify_due_tasks() -> None:
    groups = flag_due_tasks()
    if not groups:
        logger.info("No tasks due.")
        return

    for group in groups:
        _spawn_actionable_notification(group)
        logger.info("Notified (actionable): task(s) %s", [task.id for task in group])


def _describe_task(task: ScheduledTask) -> str:
    setlist = get_setlist(task.setlist_id)
    day_part = f"{setlist.day_name} " if setlist and setlist.day_name else ""
    date_part = setlist.service_date.isoformat() if setlist else "unknown date"
    return f"{day_part}{date_part}".strip()


def _reauth_listener_already_waiting(marker: Path) -> bool:
    """
    True if a previous run's re-auth listener for this service is still
    alive and waiting on your click, so we don't pile on a duplicate.

    A marker left behind by a listener that has since exited (answered,
    dismissed, or killed by a restart) reads as "not waiting" -- its pid
    is simply gone -- which is what lets the prompt come back on the
    next poll if the login is still stale.
    """
    try:
        pid = int(marker.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)  # signal 0 tests for existence without touching the process
    except OSError:
        return False
    return True


def _notify_auth_health(settings: Settings) -> None:
    """
    Check the Google logins that imminent tasks actually need, and offer
    to refresh any stale one right from the notification.

    Scoped two ways on purpose: only within _REAUTH_LEAD_TIME of a
    task's run time, and only for the services those particular tasks
    use -- so a stale Drive token never nags you on a week where
    nothing needs Drive.
    """
    cutoff = datetime.datetime.now() + _REAUTH_LEAD_TIME
    imminent = list_tasks_due_within(cutoff)

    tasks_by_service: dict[str, list[ScheduledTask]] = {}
    for task in imminent:
        service_name = _SERVICE_FOR_ACTION_TYPE.get(task.action_type)
        if service_name is not None:  # Logic Pro steps need no Google login
            tasks_by_service.setdefault(service_name, []).append(task)

    if not tasks_by_service:
        logger.info("No Drive/YouTube task due within %s -- skipping the login check.", _REAUTH_LEAD_TIME)
        return

    health = check_auth_health(settings)
    for service_name, tasks in sorted(tasks_by_service.items()):
        if health.get(service_name, False):
            continue

        marker = _MARKER_DIR / f"reauth_{service_name}.pid"
        if _reauth_listener_already_waiting(marker):
            logger.info("A %s re-authentication prompt is already waiting -- not sending another.", service_name)
            continue

        # dict.fromkeys de-duplicates while keeping run order: a
        # Drive+YouTube pair for one week shares a description, and
        # naming the same service twice reads like a mistake.
        affected = ", ".join(dict.fromkeys(_describe_task(task) for task in tasks))
        message = f"Log back in to run {affected}."

        pid = _spawn_listener(["--reauth", service_name, message])
        _MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(pid))
        logger.warning("%s login is not valid and %s is due soon -- prompted to log in.", service_name, affected)


def run() -> None:
    setup_logging()
    initialize_database()

    settings = Settings.load()
    _notify_due_tasks()
    _notify_auth_health(settings)
    # Housekeeping, so it runs after the time-sensitive notifications
    # rather than delaying them.
    purge_completed_tasks()


if __name__ == "__main__":
    run()
