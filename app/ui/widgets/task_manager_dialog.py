"""
Task Manager dialog -- opened from the single bell icon on the main
menu (see playlist_page.py's _task_manager_button), replacing the three
separate per-tab "approval queue" bells that used to live on Drive,
YouTube, and Logic Pro individually.

Lists every "active" ScheduledTask (PENDING, AWAITING_APPROVAL, or
DECLINED -- see database_service.list_active_scheduled_tasks) across
every setlist and action type at once, ordered by scheduled_for, with
Run Now / Reschedule / Delete on each row.

This exists specifically because declining a task must never be
equivalent to deleting it: before this, a declined task simply vanished
from view (still in the database, but with nowhere left to see or act
on it again). Now it stays listed here -- reschedulable or deletable --
until you explicitly choose to delete it.

Drive+YouTube pairs that share an approval_group_id are shown as one
row (Run Now / Reschedule / Delete act on both together), matching how
they notify/approve together elsewhere. Logic Pro's two pipeline steps
are never grouped, so they always show as their own row.
"""

from __future__ import annotations

import datetime
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.models.scheduled_task import ScheduledTask, TaskStatus
from app.models.settings import Settings
from app.services.database_service import get_setlist, get_task, list_active_scheduled_tasks
from app.services.scheduling_service import (
    approve_task,
    delete_group,
    delete_task,
    reschedule_group,
    reschedule_task,
)
from app.ui.widgets.card_dialog import CARD_BOTTOM_MARGIN, CARD_SIDE_MARGIN, CARD_WIDTH, setup_card_window
from app.ui.widgets.schedule_dialog import ask_for_schedule_datetime

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    TaskStatus.PENDING: "Pending",
    TaskStatus.AWAITING_APPROVAL: "Awaiting Approval",
    TaskStatus.DECLINED: "Declined",
    TaskStatus.FAILED: "Failed",
}


def _group_scheduled_for(tasks: list[ScheduledTask]) -> datetime.date:
    """The date this row's heading groups under -- see TaskManagerDialog._refresh."""
    return min(task.scheduled_for for task in tasks).date()


def _describe_group(tasks: list[ScheduledTask]) -> str:
    """
    Describes one row WITHOUT its "Scheduled for" date -- that's now
    shown once, as the heading over every row sharing it (see
    TaskManagerDialog._refresh), instead of repeated on every single
    row.
    """
    setlist = get_setlist(tasks[0].setlist_id)
    day_part = f"{setlist.day_name} " if setlist and setlist.day_name else ""
    # Short month/day only -- no year, no time. The exact time is still
    # shown (and adjustable) in the Reschedule dialog; repeating it here
    # on every row was more precision than this summary line needs.
    date_part = f"{setlist.service_date.month}/{setlist.service_date.day}" if setlist else "unknown date"
    action_names = ", ".join(task.action_type.value.replace("_", " ").title() for task in tasks)
    status = _STATUS_LABELS.get(tasks[0].status, tasks[0].status.value)
    description = f"{day_part}{date_part} -- {action_names}\n{status}"

    # Surface WHY it failed directly in the row -- otherwise "Failed"
    # alone tells you nothing about whether Run Now is even worth
    # trying again without first checking the log file.
    failure_reasons = [f"{task.action_type.value.replace('_', ' ').title()}: {task.error_message}" for task in tasks if task.status == TaskStatus.FAILED and task.error_message]
    if failure_reasons:
        description += "\n" + "; ".join(failure_reasons)

    return description


class TaskManagerDialog(QDialog):
    """
    Styled as the same frameless "card" popup as SettingsDialog (see
    app/ui/widgets/card_dialog.py) -- same fixed card size, so it's
    never taller than the settings card either. Since the task list has
    no natural cap (there's no tab structure to split it across, unlike
    Settings), the WHOLE card scrolls once the list grows past that
    fixed height, rather than the dialog window itself growing without
    bound as more tasks pile up.
    """

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Task Manager")

        _, card_layout, close_button = setup_card_window(self, CARD_WIDTH)
        # A bit more top margin than the shared default -- unlike
        # SettingsDialog, there's no tab bar above this content to
        # separate it from the close button, so the default margin left
        # the first row's text sitting right up against the card's top
        # edge/the X button.
        card_layout.setContentsMargins(CARD_SIDE_MARGIN, 70, CARD_SIDE_MARGIN, CARD_BOTTOM_MARGIN)

        self._list_layout = QVBoxLayout()
        self._list_layout.setSpacing(10)

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        container.setLayout(self._list_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(container)

        card_layout.addWidget(scroll)
        close_button.raise_()

        self._refresh()

    def _refresh(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        tasks = list_active_scheduled_tasks()  # already ordered by scheduled_for
        groups: dict[str, list[ScheduledTask]] = {}
        rows: list[tuple[str | None, list[ScheduledTask]]] = []
        seen_groups: set[str] = set()
        for task in tasks:
            if task.approval_group_id:
                if task.approval_group_id in seen_groups:
                    continue
                seen_groups.add(task.approval_group_id)
                groups[task.approval_group_id] = [t for t in tasks if t.approval_group_id == task.approval_group_id]
                rows.append((task.approval_group_id, groups[task.approval_group_id]))
            else:
                rows.append((None, [task]))

        if not rows:
            self._list_layout.addWidget(QLabel("No active tasks.", self))
            self._list_layout.addStretch()
            return

        # rows is already ordered by scheduled_for (from
        # list_active_scheduled_tasks), so every row sharing the same
        # scheduled date is already contiguous -- a heading only needs
        # to go in front of the FIRST row of each new date, rather than
        # repeating "Scheduled for X" on every single row.
        last_heading_date: datetime.date | None = None
        for group_id, group_tasks in rows:
            heading_date = _group_scheduled_for(group_tasks)
            if heading_date != last_heading_date:
                self._list_layout.addWidget(self._build_date_heading(heading_date))
                last_heading_date = heading_date
            self._list_layout.addWidget(self._build_row(group_id, group_tasks))
        # Without this, a short list centers itself in the fixed card
        # height instead of anchoring to the top -- there's no longer a
        # tight-fitting dialog height to prevent that now that the card
        # is a fixed size regardless of how many tasks there are.
        self._list_layout.addStretch()

    def _build_date_heading(self, heading_date: datetime.date) -> QLabel:
        label = QLabel(f"Scheduled for {heading_date.month}/{heading_date.day}", self)
        label.setStyleSheet("font-size: 25px; text-decoration: underline;")
        return label

    def _build_row(self, group_id: str | None, tasks: list[ScheduledTask]) -> QWidget:
        row = QWidget(self)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        label = QLabel(_describe_group(tasks), row)
        label.setWordWrap(True)
        row_layout.addWidget(label, stretch=1)

        run_button = QPushButton("Run Now", row)
        run_button.setCursor(Qt.PointingHandCursor)
        run_button.clicked.connect(lambda: self._on_run_now(group_id, tasks))
        row_layout.addWidget(run_button)

        reschedule_button = QPushButton("Reschedule", row)
        reschedule_button.setCursor(Qt.PointingHandCursor)
        reschedule_button.clicked.connect(lambda: self._on_reschedule(group_id, tasks))
        row_layout.addWidget(reschedule_button)

        delete_button = QPushButton("Delete", row)
        delete_button.setCursor(Qt.PointingHandCursor)
        delete_button.clicked.connect(lambda: self._on_delete(group_id, tasks))
        row_layout.addWidget(delete_button)

        return row

    def _on_run_now(self, group_id: str | None, tasks: list[ScheduledTask]) -> None:
        """
        Runs immediately regardless of scheduled_for -- approve_task
        executes right away with no due-time check of its own, so "Run
        Now" is just calling the normal approval path on demand rather
        than waiting for the background scheduler check to flag it
        AWAITING_APPROVAL first.

        Deliberately calls approve_task on each of THIS row's own
        `tasks` (all active, per list_active_scheduled_tasks) rather
        than approve_group -- a Drive+YouTube pair can have one side
        DONE and the other FAILED (Drive succeeds, YouTube fails, say),
        and approve_group would blindly re-run EVERY task sharing the
        group id, including the one that already succeeded. Re-running
        only what's actually still active avoids that.
        """
        try:
            for task in tasks:
                approve_task(task.id, self._settings)
        except Exception:
            logger.exception("Failed to run task(s).")
            QMessageBox.warning(self, "Run Failed", "Something went wrong running this task -- check the log file.")
            self._refresh()
            return

        # execute_task never raises -- it always leaves DONE or FAILED
        # with error_message set, so a failure would otherwise pass
        # silently here. Re-fetch and surface it directly.
        finished = [get_task(task.id) for task in tasks]
        failures = [task for task in finished if task is not None and task.status == TaskStatus.FAILED]
        if failures:
            details = "\n".join(f"- {task.action_type.value.replace('_', ' ').title()}: {task.error_message}" for task in failures)
            QMessageBox.warning(self, "Task Failed", f"This didn't complete successfully:\n\n{details}")

        self._refresh()

    def _on_reschedule(self, group_id: str | None, tasks: list[ScheduledTask]) -> None:
        setlist = get_setlist(tasks[0].setlist_id)
        service_date = setlist.service_date if setlist else None
        scheduled_for = ask_for_schedule_datetime(
            ", ".join(task.action_type.value.replace("_", " ").title() for task in tasks),
            self._settings,
            parent=self,
            service_date=service_date,
        )
        if scheduled_for is None:
            return

        if group_id is not None:
            reschedule_group(group_id, scheduled_for)
        else:
            reschedule_task(tasks[0].id, scheduled_for)
        self._refresh()

    def _on_delete(self, group_id: str | None, tasks: list[ScheduledTask]) -> None:
        description = _describe_group(tasks).splitlines()[0]
        confirmed = QMessageBox.question(
            self,
            "Delete Task?",
            f"Permanently delete this scheduled task?\n\n{description}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return

        if group_id is not None:
            delete_group(group_id)
        else:
            delete_task(tasks[0].id)
        self._refresh()


def show_task_manager_dialog(settings: Settings, parent=None) -> None:
    dialog = TaskManagerDialog(settings, parent=parent)
    dialog.exec()
