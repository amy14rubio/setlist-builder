"""
Monthly carousel widget for Setlist Builder.

Shows one Setlist ("week," e.g. Domingo 4 or Miércoles 7) at a time from
a monthly PDF import, with Previous/Next navigation between them.

Deliberately imitates the weekly base card's look exactly (title
position, two-column song list) via the same draw_ledger_content
function both use -- see app/ui/widgets/ledger_drawing.py -- rather than
its own separate native-widget layout, per direct feedback that the two
views had drifted apart visually. No position counter ("1 of 8") and no
text on the Previous/Next buttons -- the icons alone are enough, sitting
side by side in the top-left corner.

SCOPE OF THIS FIRST VERSION
----------------------------
This only browses a month's weeks and their song lists -- it does not
yet have the per-tab (Logic Pro / Drive / YouTube) "Update now" /
"Schedule" controls described in the architecture discussion. Those
depend on this basic browsing view working first, and are a deliberate
next step rather than part of this pass.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QWidget

from app.models.scheduled_task import Setlist
from app.services.database_service import get_song_entries
from app.ui.widgets.icon_button import make_icon_button
from app.ui.widgets.ledger_drawing import DEFAULT_SMALL_PICKER_TOP_MARGIN_PX, draw_ledger_content


class _CardLedgerOverlay(QWidget):
    """
    Live-painted title + two-column song list -- sits transparently on
    top of MonthlyCarouselWidget's own PaperPanel background (see
    playlist_page._make_panel), unlike ReceiptDropView, which
    pre-renders its own copy of the card texture underneath the same
    content. No crossfade animation is needed here (the carousel just
    jumps between weeks), so painting live each frame is simpler --
    and, being a live QPainter call rather than a pre-rendered-to-a-
    small-pixmap one, it's automatically crisp on a Retina display with
    no manual devicePixelRatio handling (see ReceiptDropView._render for
    why THAT one needs it and this one doesn't).
    """

    def __init__(self, target_width: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target_width = target_width
        self._title = ""
        self._entries: list = []

    def set_content(self, title: str, entries: list) -> None:
        self._title = title
        self._entries = entries
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        draw_ledger_content(painter, self.width(), self.height(), self._title, self._entries, self._target_width)
        painter.end()


class MonthlyCarouselWidget(QWidget):
    """One-week-at-a-time browser over a list of Setlist rows."""

    # Emitted when the Bulk Schedule button is clicked -- PlaylistPage
    # connects to this, since it (not this simple browsing widget) owns
    # the per-week entries cache and target-folder memory bulk
    # scheduling needs to act on.
    bulk_schedule_requested = Signal()

    def __init__(self, target_width: int = 560, parent=None) -> None:
        super().__init__(parent)
        self._setlists: list[Setlist] = []
        self._index = 0

        self._card = _CardLedgerOverlay(target_width, parent=self)

        # Smaller than the shared ICON_BUTTON_SIZE, side by side in the
        # top-LEFT corner -- clear of the title (centered) and
        # PlaylistPage's small mode-picker (top-right), per direct
        # feedback (an earlier bottom-corners placement is what that
        # feedback was replacing). Bulk Schedule joins the same cluster
        # (not the top-right corner, which stays reserved for the mode
        # picker). Bumped from 20 to 26, then again to 28, per direct
        # feedback that they kept reading too small.
        self._prev_button = make_icon_button("previous", "Previous week", parent=self, size=28)
        self._prev_button.clicked.connect(self._go_previous)
        self._next_button = make_icon_button("next", "Next week", parent=self, size=28)
        self._next_button.clicked.connect(self._go_next)
        self._bulk_schedule_button = make_icon_button(
            "schedule",
            "Schedule Drive, YouTube, and Logic Pro for every week in this month you've already reviewed.",
            parent=self,
            size=28,
        )
        self._bulk_schedule_button.clicked.connect(self.bulk_schedule_requested.emit)

        self._empty_label = QLabel("No monthly list imported yet.", parent=self)
        self._empty_label.setAlignment(Qt.AlignCenter)

        self._refresh()

    def set_setlists(self, setlists: list[Setlist]) -> None:
        """Load a fresh batch of weeks (e.g. after a new monthly import) and jump to the first one."""
        self._setlists = setlists
        self._index = 0
        self._refresh()

    def current_setlist(self) -> Setlist | None:
        """The week currently being browsed -- what PlaylistPage's Logic Pro/Drive/YouTube tabs act on, in Monthly mode."""
        if not self._setlists:
            return None
        return self._setlists[self._index]

    def all_setlists(self) -> list[Setlist]:
        """Every week loaded from the current monthly import, in order -- what bulk scheduling iterates over."""
        return list(self._setlists)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._card.setGeometry(0, 0, self.width(), self.height())
        self._empty_label.setGeometry(0, 0, self.width(), self.height())

        left_margin = 25  # was 6 -- per direct feedback, too close to the left edge
        button_gap = 2
        # Vertically centered on the SAME point draw_ledger_content()
        # centers the title on (see its own identical calculation) --
        # both this widget and _card fill the exact same rect (see
        # setGeometry above), so they share one coordinate space. Without
        # this, the icons sat flush against the top edge while the title
        # (a much taller font) read as vertically centered in the same
        # header band, leaving the two looking misaligned.
        picker_center = round(DEFAULT_SMALL_PICKER_TOP_MARGIN_PX * (self.width() / self._card._target_width))
        icon_top = max(0, picker_center - self._prev_button.height() // 2)

        self._prev_button.move(left_margin, icon_top)
        self._next_button.move(left_margin + self._prev_button.width() + button_gap, icon_top)
        self._bulk_schedule_button.move(
            left_margin + self._prev_button.width() + button_gap + self._next_button.width() + button_gap,
            icon_top,
        )

    def _go_previous(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._refresh()

    def _go_next(self) -> None:
        if self._index < len(self._setlists) - 1:
            self._index += 1
            self._refresh()

    def _refresh(self) -> None:
        has_weeks = bool(self._setlists)

        self._prev_button.setEnabled(has_weeks and self._index > 0)
        self._next_button.setEnabled(has_weeks and self._index < len(self._setlists) - 1)
        self._bulk_schedule_button.setEnabled(has_weeks)
        self._card.setVisible(has_weeks)
        self._empty_label.setVisible(not has_weeks)

        if not has_weeks:
            return

        setlist = self._setlists[self._index]
        # Just the day-of-month (e.g. "Domingo 4"), not the full
        # year-month-day -- per direct feedback, the month/year are
        # already implied by which monthly PDF you just imported, so
        # spelling them out on every single week's title was redundant.
        day_part = f"{setlist.day_name} " if setlist.day_name else ""
        title = f"{day_part}{setlist.service_date.day}"
        if setlist.note:
            title += f" ({setlist.note})"

        self._card.set_content(title, get_song_entries(setlist.id))
