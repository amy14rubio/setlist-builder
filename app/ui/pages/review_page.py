"""
Review page for Setlist Builder.

Displays the matched SongEntry list in a table, highlighting specific
problem cells (missing audio, missing logic, missing BPM, low confidence,
tempo mismatch) and letting the user fix problems by hand:

  - Matched Audio / Matched Logic: dropdowns listing every folder found in
    the scanned library, rather than free-text. A "match" always points
    at a real folder that actually exists, or explicitly at nothing.
  - BPM: free-text editable when there's an audio match, for the case
    where a folder's name didn't have a parseable BPM.
  - Logic Tempo: free-text editable, always. Type the project's tempo
    after checking it in Logic. Leave it BLANK if the project's tempo
    varies internally (no single number applies) -- a blank value is
    treated as "definitely needs a tempo copy," not "not checked yet."

Confidence highlighting stays (low scores get a red background), but
there's no separate Status column or "verified" button -- confidence and
the other per-cell highlights (missing BPM, tempo mismatch) already
carry that information directly.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.models.library_item import LibraryEntry
from app.models.scheduled_task import ActionType
from app.models.settings import Settings
from app.models.song_entry import LibraryMatch, SongEntry
from app.services.database_service import get_setlist, replace_song_entries
from app.services.matching_service import find_best_match
from app.services.scheduling_service import schedule_task
from app.ui.widgets.entry_table_row_ops import EntryTableRowOpsMixin
from app.ui.widgets.icon_button import make_icon_button
from app.ui.widgets.schedule_dialog import ask_for_schedule_datetime

logger = logging.getLogger(__name__)

_COLUMN_HEADERS = [
    # "Order",
    "Title",
    "Audio",
    "Logic",
    "BPM",
    # "Logic Tempo",
    # "Confidence",
]

_OCR_TITLE_COL, _AUDIO_COL, _LOGIC_COL, _BPM_COL = range(4)

# Order / Logic Tempo / Confidence no longer have real columns in the
# table above (see _COLUMN_HEADERS) -- kept as unreachable sentinels
# instead of deleted, since _on_item_changed and _refresh_computed_cells
# below still reference them by name.
_ORDER_COL = _LOGIC_TEMPO_COL = _CONFIDENCE_COL = -1

_NO_MATCH_LABEL = "— No match —"

# Soft, readable highlight color for flagged cells (low confidence,
# missing BPM, tempo mismatch) -- light enough that text stays legible,
# distinct enough to be noticeable at a glance in a table.
_PROBLEM_CELL_COLOR = QColor(255, 214, 214)  # light red


class ReviewPage(QWidget, EntryTableRowOpsMixin):
    """Editable table view of matched songs, with problem highlighting."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._entries: list[SongEntry] = []
        self._audio_library: list[LibraryEntry] = []
        self._logic_library: list[LibraryEntry] = []
        self._setlist_id: Optional[int] = None
        self._init_row_ops("excluded_from_logic")

        # Guards against the itemChanged signal firing while WE are the
        # ones setting cell content programmatically (e.g. when
        # recalculating BPM/Confidence/Status after an edit). Without
        # this, every programmatic update would be misinterpreted as a
        # fresh user edit, potentially recursing.
        self._is_refreshing = False

        self._table = QTableWidget()
        # QSS's "background-color: transparent" on QTableWidget (see
        # #ledgerPage QTableWidget in style.py) doesn't reach the table's
        # own viewport widget, which keeps auto-filling itself solid --
        # without this, the table hides PaperPanel's paper texture behind
        # a flat block, both under its rows AND the empty space below them.
        self._table.viewport().setAutoFillBackground(False)
        self._table.setColumnCount(len(_COLUMN_HEADERS))
        self._table.setHorizontalHeaderLabels(_COLUMN_HEADERS)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        # BPM never needs as much horizontal room as the other columns
        # (it's a short number, not a title/folder name) -- fixed and
        # narrow instead of sharing the stretch, per direct feedback.
        header.setSectionResizeMode(_BPM_COL, QHeaderView.Fixed)
        self._table.setColumnWidth(_BPM_COL, 60)
        # Left-align header text to match the left-aligned cell content
        # below it (Qt centers header labels by default) -- matches
        # ChartsPage's identical override, so all three tables read as
        # one consistent family.
        for col in (_OCR_TITLE_COL, _AUDIO_COL, _LOGIC_COL, _BPM_COL):
            header_item = self._table.horizontalHeaderItem(col)
            header_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed
        )
        self._table.itemChanged.connect(self._on_item_changed)

        self._match_button = make_icon_button(
            "match", "Re-match every song's audio folder and Logic Pro project."
        )
        self._match_button.clicked.connect(self._on_match_clicked)

        self._add_row_button = make_icon_button(
            "add",
            "Add a blank row for a song OCR missed. Pick its matches from the dropdowns afterward.",
        )
        self._add_row_button.clicked.connect(self._on_add_song_clicked)

        self._remove_row_button = make_icon_button(
            "remove",
            "Remove the currently selected row, for a song OCR mistakenly added "
            "(e.g. misread a line as a song that wasn't one).",
        )
        self._remove_row_button.clicked.connect(self._on_remove_row_clicked)

        # Moved here from BuildPage's "Continue to Build" page, per direct
        # feedback -- scheduling the Logic Pro pipeline doesn't need to
        # wait until Continue to Build; it can be scheduled as soon as
        # everything here has a match, one step earlier in the flow.
        self._schedule_button = make_icon_button(
            "schedule",
            "Schedule the whole Logic Pro pipeline (Prepare, then Build Logic Pro Project) for a future date "
            "instead of running it now -- both steps are scheduled for the same time.",
        )
        self._schedule_button.clicked.connect(self._on_schedule_clicked)

        # Built here (not in playlist_page.py) so it can sit in this same
        # icon-button row, after every icon -- but left UNconnected: moving
        # to the Build stage is PlaylistPage's own concern (it owns
        # BuildPage/the stage stack), so PlaylistPage wires the click
        # itself onto this public attribute.
        self._continue_to_build_button = QPushButton("Continue to Build →")
        self._continue_to_build_button.setCursor(Qt.PointingHandCursor)
        # Expanding (not a trailing addStretch) -- the button itself
        # fills whatever width the 4 fixed-size icon buttons leave
        # behind, per direct feedback, rather than staying at its own
        # natural size and just getting pushed to the right edge.
        self._continue_to_build_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        top_row = QHBoxLayout()
        # Tighter than the icons' own fixed 34px size would suggest --
        # per direct feedback, the default (unset) QHBoxLayout gap read
        # as too spread out.
        top_row.setSpacing(4)
        top_row.addWidget(self._match_button)
        top_row.addWidget(self._add_row_button)
        top_row.addWidget(self._remove_row_button)
        top_row.addWidget(self._schedule_button)
        top_row.addWidget(self._continue_to_build_button, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        # Stretch factor 1: see ChartsPage/YouTubePage's identical change
        # -- the table claims the container's full leftover height so
        # more rows fit before scrolling.
        layout.addWidget(self._table, 1)
        self._layout = layout

    def set_compact(self, row_height: int = 22, margin: int = 4, spacing: int = 4) -> None:
        """
        Tighten margins/spacing and shrink table rows -- purely a layout
        density change (no business logic touched), for contexts with
        much less vertical room than this page's original full-window
        home, e.g. PlaylistPage's in-place "logic pro" panel. Lets more
        table rows fit before scrolling without changing what's shown.
        """
        self._layout.setContentsMargins(margin, margin, margin, margin)
        self._layout.setSpacing(spacing)
        self._table.verticalHeader().setDefaultSectionSize(row_height)
        self._table.verticalHeader().setVisible(False)
        # Add/Remove are now fixed-size square icon buttons (see
        # make_icon_button) -- no setMaximumHeight call here, same
        # reasoning as ChartsPage/YouTubePage.set_compact.

    def get_entries(self) -> list[SongEntry]:
        """
        Return Logic Pro's own currently-included (possibly hand-edited)
        SongEntry list -- NOT the raw shared list, which may also hold
        songs Logic Pro has excluded (see _visible_entries). Called by
        PlaylistPage when moving from Review to Build (both embedded in
        its "logic pro" panel) -- a public accessor rather than reaching
        into self._entries directly keeps ReviewPage free to change its
        internal storage later without breaking callers.
        """
        return self._visible_entries()

    def load_entries(
        self,
        entries: list[SongEntry],
        audio_library: list[LibraryEntry],
        logic_library: list[LibraryEntry],
        setlist_id: Optional[int] = None,
    ) -> None:
        """
        Populate the table from a list of SongEntry objects.

        `setlist_id` is the persisted Setlist row this week's entries
        were saved under -- needed by the Schedule button. None if that
        save failed (a rare case; see
        PlaylistPage._save_entries_to_database).
        """
        self._entries = entries
        self._audio_library = audio_library
        self._logic_library = logic_library
        self._setlist_id = setlist_id

        self._rebuild_table()

    def match_entries(self) -> None:
        """
        Match every loaded song against the audio/Logic libraries already
        given to load_entries -- only fills in entries that don't already
        have a match, so a manual correction (or a match from a previous
        visit) survives. Public (not just the Match button's private
        click handler) so PlaylistPage can also trigger this
        automatically right when the tab opens, the same way
        ChartsPage.match_charts()/YouTubePage.match_songs() work.
        """
        for entry in self._entries:
            if entry.audio_match is None:
                entry.audio_match = find_best_match(entry.ocr_title, self._audio_library)
            if entry.logic_match is None:
                entry.logic_match = find_best_match(entry.ocr_title, self._logic_library)
        self._rebuild_table()

    def _on_match_clicked(self) -> None:
        self.match_entries()

    def _on_schedule_clicked(self) -> None:
        """
        One Schedule button/one date-time picker for the WHOLE Logic Pro
        pipeline -- schedules Prepare and Build Logic Pro Project for the
        exact same time, per direct feedback, rather than picking a
        separate time for each. They still run as two separate tasks
        with two separate notifications/approvals (Build Logic Pro
        Project isn't grouped with Prepare, since it's the more
        disruptive of the two -- see ActionType's docstring), and
        scheduling_service doesn't check the PREVIOUS step's status
        until a task actually runs (see schedule_task's docstring), so
        if Build Logic Pro Project's notification gets approved before
        Prepare's has actually finished executing, it fails clearly
        (asking you to run Prepare first) rather than guessing --
        approving Prepare's notification first avoids that.

        Ported here from BuildPage's own identically-named handler
        (moved per direct feedback -- Continue to Build no longer needs
        to happen first), with the confirmation now a QMessageBox
        instead of appending to a log view, since this page has none.
        """
        if self._setlist_id is None:
            QMessageBox.warning(
                self, "Can't Schedule", "This week's songs weren't saved to the database, so there's nothing to schedule."
            )
            return
        visible = self._visible_entries()
        if not visible:
            QMessageBox.warning(self, "Can't Schedule", "No songs loaded yet.")
            return
        if any(entry.audio_match is None or entry.logic_match is None for entry in visible):
            QMessageBox.warning(
                self, "Can't Schedule", "Every song needs both an audio match and a Logic Pro project match first."
            )
            return

        setlist = get_setlist(self._setlist_id)
        service_date = setlist.service_date if setlist else None
        scheduled_for = ask_for_schedule_datetime(
            "Prepare + Build Logic Pro Project", self._settings, parent=self, service_date=service_date
        )
        if scheduled_for is None:
            return

        # The FULL shared list, not just Logic's own visible entries --
        # replace_song_entries deletes every existing row for this
        # setlist first, so saving only Logic's subset would silently
        # erase Drive's/YouTube's own excluded-from-logic songs from the
        # database entirely.
        replace_song_entries(self._setlist_id, self._entries)
        schedule_task(self._setlist_id, ActionType.PREPARE_LOGIC_PROJECT, scheduled_for)
        schedule_task(self._setlist_id, ActionType.LOGIC_AUTOMATION, scheduled_for)
        QMessageBox.information(
            self,
            "Scheduled",
            f"Scheduled Prepare and Build Logic Pro Project for {scheduled_for.strftime('%b %d, %Y %I:%M %p')}.",
        )

    def _build_row(self, row: int, entry: SongEntry) -> None:
        # self._set_readonly_cell(row, _ORDER_COL, str(entry.order))

        title_item = QTableWidgetItem(entry.ocr_title)
        title_item.setFlags(title_item.flags() | Qt.ItemIsEditable)
        self._table.setItem(row, _OCR_TITLE_COL, title_item)

        audio_combo = self._make_match_combo(self._audio_library, entry.audio_match)
        audio_combo.currentIndexChanged.connect(
            lambda _index, r=row: self._on_match_changed(r, is_audio=True)
        )
        self._table.setCellWidget(row, _AUDIO_COL, audio_combo)

        logic_combo = self._make_match_combo(self._logic_library, entry.logic_match)
        logic_combo.currentIndexChanged.connect(
            lambda _index, r=row: self._on_match_changed(r, is_audio=False)
        )
        logic_combo.setStyleSheet("background: transparent; border: none;")
        self._table.setCellWidget(row, _LOGIC_COL, logic_combo)

        self._refresh_computed_cells(row)

    def _make_match_combo(
        self, library: list[LibraryEntry], current_match: LibraryMatch | None
    ) -> QComboBox:
        """
        Build a dropdown listing every folder in `library`, plus a
        "No match" option. userData on each item is the folder_path,
        which is what we use to look the entry back up later -- titles
        alone aren't guaranteed unique, folder paths are.
        """
        combo = QComboBox()
        combo.setCursor(Qt.PointingHandCursor)
        combo.addItem(_NO_MATCH_LABEL, userData=None)

        selected_index = 0
        sorted_library = sorted(library, key=lambda e: e.title.lower())
        for i, lib_entry in enumerate(sorted_library, start=1):
            combo.addItem(lib_entry.title, userData=lib_entry.folder_path)
            if current_match is not None and lib_entry.folder_path == current_match.folder_path:
                selected_index = i

        combo.setCurrentIndex(selected_index)
        return combo

    def _on_match_changed(self, row: int, is_audio: bool) -> None:
        combo = self._table.cellWidget(row, _AUDIO_COL if is_audio else _LOGIC_COL)
        selected_folder_path = combo.currentData()
        library = self._audio_library if is_audio else self._logic_library
        entry = self._visible_entries()[row]

        new_match: LibraryMatch | None = None
        if selected_folder_path is not None:
            lib_entry = next(
                (e for e in library if e.folder_path == selected_folder_path), None
            )
            if lib_entry is not None:
                # A manually-chosen match is a CONFIRMED match -- 100%
                # confidence, since a human looked at it and picked it.
                new_match = LibraryMatch(
                    title=lib_entry.title,
                    folder_path=lib_entry.folder_path,
                    confidence=100.0,
                    bpm=lib_entry.bpm,
                )

        if is_audio:
            entry.audio_match = new_match
        else:
            entry.logic_match = new_match

        logger.info(
            "Row %d: manually set %s match to %r",
            row,
            "audio" if is_audio else "logic",
            new_match.title if new_match else None,
        )
        self._refresh_computed_cells(row)

    def _on_add_song_clicked(self) -> None:
        """
        Add a blank row for a song OCR missed entirely -- inserted
        directly below the currently selected row (renumbering
        everything after it), or appended to the end if nothing's
        selected (see EntryTableRowOpsMixin._insert_new_entry_below_selected).
        The user fills in the title and picks matches from the dropdowns
        afterward -- the audio/Logic library lists are already
        available, so no new scan is needed.
        """
        _entry, insert_at = self._insert_new_entry_below_selected()
        logger.info(
            "User added a new blank song row to Logic Pro at position %d (now %d total).",
            insert_at,
            len(self._visible_entries()),
        )

    def _on_remove_row_clicked(self) -> None:
        """
        Remove the currently selected row, for a song OCR mistakenly
        added (e.g. misread a stray line as a song title that wasn't
        one) -- see EntryTableRowOpsMixin._remove_selected_entry.
        Renumbering matters here specifically since `order` drives
        sequencing in the Build Manifest and must stay contiguous.
        """
        removed = self._remove_selected_entry()
        if removed is None:
            logger.info("Remove Row clicked with no row selected -- nothing to do.")
            return
        logger.info("Removed song '%s' from Logic Pro.", removed.ocr_title)

    def _rebuild_table(self) -> None:
        """Fully redraw the table from this tab's own currently-included entries."""
        visible = self._visible_entries()
        self._table.setRowCount(len(visible))
        for row, entry in enumerate(visible):
            self._build_row(row, entry)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._is_refreshing:
            return  # this change came from our own code, not the user

        row = item.row()
        column = item.column()

        if column == _BPM_COL:
            self._handle_bpm_edit(row, item)
        elif column == _LOGIC_TEMPO_COL:
            self._handle_logic_tempo_edit(row, item)
        elif column == _OCR_TITLE_COL:
            self._visible_entries()[row].ocr_title = item.text().strip()
        # All other columns are read-only display cells; nothing to do.

    def _handle_bpm_edit(self, row: int, item: QTableWidgetItem) -> None:
        entry = self._visible_entries()[row]

        if entry.audio_match is None:
            # Shouldn't normally happen (cell isn't made editable in this
            # case), but if it does, there's nowhere to attach a manually
            # entered BPM. Revert silently.
            # self._refresh_computed_cells(row)
            return

        text = item.text().strip()
        if text == "" or text.lower() == "unknown":
            entry.audio_match.bpm = None
        else:
            try:
                entry.audio_match.bpm = int(text)
            except ValueError:
                logger.warning("Invalid BPM entered (%r) -- reverting.", text)
                # Fall through: _refresh_computed_cells will redraw using
                # the entry's actual (unchanged) value below.

        self._refresh_computed_cells(row)

    def _handle_logic_tempo_edit(self, row: int, item: QTableWidgetItem) -> None:
        entry = self._visible_entries()[row]

        text = item.text().strip()
        if text == "":
            # Blank is a valid, meaningful value here -- it means the
            # project's tempo varies internally. Not an error to revert.
            entry.logic_tempo = None
        else:
            try:
                entry.logic_tempo = int(text)
            except ValueError:
                logger.warning("Invalid Logic Tempo entered (%r) -- reverting.", text)
                # Fall through: redraw using the entry's actual value.

        # self._refresh_computed_cells(row)

    def _refresh_computed_cells(self, row: int) -> None:
        """
        Recompute and redraw the BPM cell for one row. Called both on
        initial load and after any edit, so it can never disagree with
        the entry's actual current state.

        Logic Tempo and Confidence are deliberately NOT rendered as
        their own columns anymore (see _COLUMN_HEADERS) -- that's a
        display choice only. Neither needs to be "computed" here to
        keep working: logic_tempo is a plain field on the entry, and
        confidence lives on entry.audio_match/logic_match, set upstream
        when the match was first made (PlaylistPage's find_best_match
        call) -- both stay fully intact on the entry itself (still
        readable via entry.is_low_confidence()/needs_tempo_copy) even
        though nothing here displays them.
        """
        entry = self._visible_entries()[row]
        threshold = self._settings.match_confidence_threshold

        self._is_refreshing = True
        try:
            bpm_text = str(entry.audio_bpm) if entry.audio_bpm is not None else "Unknown"
            bpm_item = QTableWidgetItem(bpm_text)
            if entry.audio_match is not None:
                bpm_item.setFlags(bpm_item.flags() | Qt.ItemIsEditable)
            else:
                bpm_item.setFlags(bpm_item.flags() & ~Qt.ItemIsEditable)
            if entry.is_missing_bpm:
                bpm_item.setBackground(_PROBLEM_CELL_COLOR)
            self._table.setItem(row, _BPM_COL, bpm_item)

            # tempo_text = str(entry.logic_tempo) if entry.logic_tempo is not None else ""
            # tempo_item = QTableWidgetItem(tempo_text)
            # tempo_item.setFlags(tempo_item.flags() | Qt.ItemIsEditable)
            # if entry.needs_tempo_copy is True:
            #     tempo_item.setBackground(_PROBLEM_CELL_COLOR)
            # self._table.setItem(row, _LOGIC_TEMPO_COL, tempo_item)

            # confidence_item = QTableWidgetItem(self._format_confidence(entry))
            # confidence_item.setFlags(confidence_item.flags() & ~Qt.ItemIsEditable)
            # if entry.is_low_confidence(threshold):
            #     confidence_item.setBackground(_PROBLEM_CELL_COLOR)
            # self._table.setItem(row, _CONFIDENCE_COL, confidence_item)
        finally:
            self._is_refreshing = False

    def _set_readonly_cell(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self._table.setItem(row, column, item)

    @staticmethod
    def _format_confidence(entry: SongEntry) -> str:
        scores = []
        if entry.audio_match is not None:
            scores.append(f"A:{entry.audio_match.confidence:.0f}")
        if entry.logic_match is not None:
            scores.append(f"L:{entry.logic_match.confidence:.0f}")
        return "  ".join(scores) if scores else "—"
