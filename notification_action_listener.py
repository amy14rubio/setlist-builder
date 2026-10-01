"""
Notification action listener -- makes a notification itself clickable,
instead of only alerting you that you need to go open the app. Two
modes, both spawned by scheduler_runner.py:

  - Approve / Decline a group of tasks that just came due.
  - Log In, when a Google login has expired and a scheduled task is
    about to need it. Clicking runs the normal Google sign-in flow (it
    just opens your browser), so re-authenticating never requires
    opening the app at all.

Spawned as its own DETACHED process. It has to be detached because
terminal-notifier's `-action` flag blocks until you actually click
something (per its own docs: "Waits for a response and prints the
chosen title") -- which could be hours later -- while scheduler_runner.py
itself needs to exit right away, since launchd re-invokes it on its own
short interval rather than keeping it running.

If this process (or the whole Mac) restarts before you click, the click
is simply never seen -- the task just stays AWAITING_APPROVAL, still
fully approvable the normal way via the bell icon in the app, and the
login can still be refreshed from inside the app. This is a convenience
shortcut on top of those paths, not a replacement for them.

Not meant to be run by hand normally. For manual testing:
    python notification_action_listener.py <task_id> [<task_id> ...]
    python notification_action_listener.py --reauth <drive|youtube> "<message>"
"""

from __future__ import annotations

import logging
import sys

from app.models.scheduled_task import ScheduledTask, TaskStatus
from app.models.settings import Settings
from app.services import drive_service, youtube_service
from app.services.database_service import get_setlist, get_task, initialize_database
from app.services.notification_service import ask_via_notification, send_notification
from app.services.scheduling_service import approve_task, decline_task
from app.utils.logging_setup import setup_logging

logger = logging.getLogger(__name__)

# Each re-authenticatable service, mapped to how to find its configured
# client secret and which module performs the sign-in. The module is
# held rather than its authenticate function so the lookup stays late
# bound. Both authenticate() functions open a browser against an
# ephemeral local server (InstalledAppFlow.run_local_server), which
# works just as well from this background process as from the Qt app.
_REAUTH_SERVICES = {
    "youtube": (lambda settings: settings.youtube_client_secret_path, youtube_service),
    "drive": (lambda settings: settings.google_drive_client_secret_path, drive_service),
}


def _describe(tasks: list[ScheduledTask], message: str) -> tuple[str, str]:
    setlist = get_setlist(tasks[0].setlist_id)
    day_part = f"{setlist.day_name} " if setlist and setlist.day_name else ""
    date_part = setlist.service_date.isoformat() if setlist else "unknown date"
    title = f"Setlist Builder: {day_part}{date_part}".strip()
    return title, message


def _action_names(tasks: list[ScheduledTask]) -> str:
    return ", ".join(task.action_type.value.replace("_", " ").title() for task in tasks)


def listen_and_act(task_ids: list[int]) -> None:
    tasks = [task for task in (get_task(task_id) for task_id in task_ids) if task is not None]
    if not tasks:
        logger.warning("None of these task ids exist any more: %s", task_ids)
        return

    title, message = _describe(tasks, f"Ready for approval: {_action_names(tasks)}")
    choice = ask_via_notification(title, message, ["Approve", "Decline"])
    settings = Settings.load()

    if choice == "Approve":
        for task in tasks:
            approve_task(task.id, settings)
        logger.info("Approved via notification: task(s) %s", task_ids)
        _send_completion_notification(tasks)
    elif choice == "Decline":
        for task in tasks:
            decline_task(task.id)
        logger.info("Declined via notification: task(s) %s", task_ids)
    else:
        # Dismissed rather than answered -- leave the task exactly as it
        # is; it's still sitting in the bell's queue.
        logger.info("Notification for task(s) %s closed without a decision.", task_ids)


def _send_completion_notification(tasks: list[ScheduledTask]) -> None:
    """
    A second, passive (non-actionable) banner confirming what actually
    happened once approve_task finished running -- approve_task never
    raises (see scheduling_service.execute_task), it always leaves each
    task DONE or FAILED with error_message set, so re-reading each
    task's final status here is the only way to know which one it was.
    """
    finished = [task for task in (get_task(task.id) for task in tasks) if task is not None]
    failed = [task for task in finished if task.status == TaskStatus.FAILED]

    if failed:
        details = "; ".join(f"{task.action_type.value.replace('_', ' ').title()}: {task.error_message}" for task in failed)
        title, message = _describe(tasks, f"Failed -- {details}")
    else:
        title, message = _describe(tasks, f"Done: {_action_names(finished)}")

    send_notification(title, message)


def prompt_reauthentication(service_name: str, message: str) -> None:
    """
    Offer to re-run the Google sign-in for one expired service, and do
    it right here if the button is clicked -- no need to open the app.

    Called when a scheduled task that needs this service is close enough
    to its run time to be worth interrupting you over (see
    scheduler_runner._notify_auth_health).
    """
    secret_path_of, service = _REAUTH_SERVICES[service_name]
    settings = Settings.load()
    client_secret_path = secret_path_of(settings)

    if not client_secret_path:
        # Nothing to log into yet -- this service was never set up, so a
        # "Log In" button would just fail. Say so passively instead.
        send_notification(
            f"Setlist Builder: {service_name.title()}",
            f"{message} -- but no {service_name.title()} client secret is set in Settings yet.",
        )
        return

    choice = ask_via_notification(f"Setlist Builder: {service_name.title()} login expired", message, ["Log In"])
    if choice != "Log In":
        logger.info("%s re-authentication prompt dismissed without logging in.", service_name)
        return

    credentials = service.authenticate(client_secret_path, allow_interactive=True)
    if credentials:
        logger.info("Re-authenticated %s from the notification.", service_name)
        send_notification(
            f"Setlist Builder: {service_name.title()}",
            f"Logged back in -- your scheduled {service_name.title()} task can run now.",
        )
    else:
        # authenticate() returns None rather than raising on a bad or
        # missing client secret (see its docstring), so this is the only
        # signal that the login didn't actually take.
        logger.error("Re-authentication for %s failed.", service_name)
        send_notification(
            f"Setlist Builder: {service_name.title()}",
            "That login didn't go through -- try again from Settings in the app.",
        )


if __name__ == "__main__":
    setup_logging()
    initialize_database()
    arguments = sys.argv[1:]
    try:
        if arguments[:1] == ["--reauth"]:
            prompt_reauthentication(arguments[1], arguments[2])
        else:
            listen_and_act([int(argument) for argument in arguments])
    except Exception:
        logger.exception("notification_action_listener failed for %s", arguments)
