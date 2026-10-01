"""
Shared "pick a date/time" dialog for the Schedule buttons on the Drive,
YouTube, and Logic Pro tabs.

Deliberately a tiny, generic dialog -- it has no idea which action it's
scheduling (that's decided by whichever tab opened it, via
scheduling_service.schedule_task) and just hands back a datetime, or
None if the user cancels.

Shows a full QCalendarWidget grid directly (not tucked behind a
dropdown/popup) plus a separate time field, per direct feedback that
seeing the month at a glance is easier than a click-to-open popup.
"""

from __future__ import annotations

import datetime
from typing import Optional

from PySide6.QtCore import QDate, QDateTime, Qt, QTime
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QTimeEdit,
    QVBoxLayout,
)

from app.models.settings import Settings
from app.services import scheduling_service

# A plain, minimal light look for the calendar grid, independent of
# whatever light/dark system appearance is active -- QCalendarWidget's
# default styling otherwise picks up the OS palette directly, which
# rendered as near-black cells with low-contrast text under macOS dark
# mode. Reuses the app's own text/border colors (see app/ui/style.py)
# rather than introducing a new palette.
_CALENDAR_STYLESHEET = """
    QCalendarWidget QWidget {
        background-color: #ffffff;
        color: #333333;
    }
    QCalendarWidget QAbstractItemView:enabled {
        color: #333333;
        background-color: #ffffff;
        selection-background-color: #333333;
        selection-color: #ffffff;
        outline: none;
    }
    QCalendarWidget QAbstractItemView:disabled {
        color: #cccccc;
    }
    QCalendarWidget QToolButton {
        background-color: transparent;
        color: #333333;
        icon-size: 16px;
    }
    QCalendarWidget QToolButton:hover {
        background-color: #ececec;
    }
    QCalendarWidget QMenu {
        background-color: #ffffff;
        color: #333333;
    }
"""

# The weekday header row + Sat/Sun date numbers default to a harsh red
# -- toned down to the same plain text color as every other day, for a
# more minimal look with less visual noise.
_WEEKEND_TEXT_COLOR = QColor("#333333")


class ScheduleDialog(QDialog):
    def __init__(
        self,
        action_label: str,
        settings: Settings,
        parent=None,
        service_date: Optional[datetime.date] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Schedule")

        default_dt = self._default_datetime(service_date)

        self._calendar = QCalendarWidget(self)
        self._calendar.setMinimumDate(QDate.currentDate())
        self._calendar.setSelectedDate(default_dt.date())
        self._calendar.setGridVisible(False)
        self._calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)  # hides the ISO week-number column
        self._calendar.setStyleSheet(_CALENDAR_STYLESHEET)
        weekend_format = QTextCharFormat()
        weekend_format.setForeground(_WEEKEND_TEXT_COLOR)
        self._calendar.setWeekdayTextFormat(Qt.Saturday, weekend_format)
        self._calendar.setWeekdayTextFormat(Qt.Sunday, weekend_format)

        self._time_edit = QTimeEdit(default_dt.time(), self)
        self._time_edit.setDisplayFormat("h:mm AP")

        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Time:", self))
        time_row.addWidget(self._time_edit)
        time_row.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        for standard_button in (QDialogButtonBox.Ok, QDialogButtonBox.Cancel):
            buttons.button(standard_button).setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Run {action_label} at:", self))
        layout.addWidget(self._calendar)
        layout.addLayout(time_row)
        layout.addWidget(buttons)

    def _default_datetime(self, service_date: Optional[datetime.date]) -> QDateTime:
        """
        When `service_date` is known (every real Schedule button always
        knows its own setlist's service_date), defaults to the Monday
        that starts that service week, at Settings.default_schedule_time
        -- see scheduling_service.compute_default_schedule_datetime.
        Clamped to "now" if that computed moment has already passed
        (e.g. you're scheduling late in the week) -- scheduling for a
        moment already in the past would just mean it's immediately due
        anyway, so "now" is the honest equivalent rather than silently
        picking some other arbitrary future time.

        Without a service_date, falls back to the last date/time picked
        in ANY Schedule dialog (Settings.last_scheduled_at), or now + 1
        hour the very first time -- unchanged from before this rule
        existed, kept for any future caller that doesn't have a single
        settled service_date to compute from.
        """
        now = datetime.datetime.now()
        if service_date is not None:
            computed = scheduling_service.compute_default_schedule_datetime(service_date, self._settings)
            return QDateTime(max(computed, now))

        if self._settings.last_scheduled_at:
            try:
                return QDateTime(datetime.datetime.fromisoformat(self._settings.last_scheduled_at))
            except ValueError:
                pass
        return QDateTime.currentDateTime().addSecs(3600)

    def selected_datetime(self) -> datetime.datetime:
        combined = QDateTime(self._calendar.selectedDate(), self._time_edit.time())
        return combined.toPython()


def ask_for_schedule_datetime(
    action_label: str,
    settings: Settings,
    parent=None,
    service_date: Optional[datetime.date] = None,
) -> Optional[datetime.datetime]:
    """
    Show the dialog; returns the chosen datetime, or None if cancelled.

    `service_date` (the setlist's own service date, when known) lets the
    dialog suggest the Monday that starts that service week as the
    default -- see ScheduleDialog._default_datetime. Also saves the
    chosen datetime back to Settings on acceptance, for the rarer case
    where a future caller doesn't have a service_date to compute from.
    """
    dialog = ScheduleDialog(action_label, settings, parent=parent, service_date=service_date)
    if dialog.exec() != QDialog.Accepted:
        return None

    chosen = dialog.selected_datetime()
    settings.last_scheduled_at = chosen.isoformat()
    settings.save()
    return chosen
