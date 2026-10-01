"""
Charts page for Setlist Builder.

Reached directly from the Playlist page's "Set Up Charts" button --
entirely independent of both the Logic Pro pipeline (Review/Build) and
the YouTube playlist workflow. It runs its own fresh OCR pass (via
PlaylistPage) and never touches output_folder or weekly_project_folder,
since those belong to the Logic Pro pipeline and this workflow has no
reason to depend on that pipeline having run at all.

Three steps, meant to be run in order (the first runs automatically on
arrival, same as the YouTube page):
  1. Match Songs Against Charts Folder -- lists the configured Google
     Drive folder (one Google Doc chart per song) and fuzzy-matches each
     song's title against it, same approach as the YouTube page matching
     against its configured lookup site.
  2. Download Matched Charts -- first REMOVES every PDF already in the
     selected Target Folder, then exports each song's matched Google Doc
     as a fresh PDF into that folder. Same "empty it, then fill it"
     behavior as youtube_service.py's clear_playlist() +
     add_videos_to_playlist(), so stale charts from a previous week's
     setlist never linger alongside the current one.

TARGET FOLDER SELECTION
-------------------------
Mirrors the YouTube page's playlist dropdown: settings.chart_folders
maps a name (e.g. "Miércoles") to a folder path, so each weekly service
folder only ever needs to be entered once. Picking one from the
dropdown also auto-fills "Setlist PDF Name" with that same name, since
in practice the merged PDF is named after which service it's for.
  3. Merge Charts into Setlist PDF -- combines every downloaded chart
     into one PDF, in setlist order, under the name typed into the
     "Setlist PDF Name" field (editable any time before clicking Merge --
     re-merging under a new name is cheap since the source charts are
     already downloaded), written to settings.charts_merged_output_folder.

MATCH PERSISTENCE
-------------------
Same reasoning as the YouTube page: a chart match lives on
`entry.chart_match` itself, not a page-level dictionary keyed by row
number, so a manual correction survives a table rebuild.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.models.settings import Settings
from app.models.song_entry import ChartMatch, SongEntry
from app.services.drive_service import (
    DriveDoc,
    authenticate,
    export_doc_as_pdf,
    find_chart_for_song,
    get_drive_client,
    list_docs_in_folder,
)
from app.models.scheduled_task import ActionType
from app.services.database_service import get_setlist, replace_song_entries
from app.services.manifest_service import build_manifest
from app.services.pdf_merge_service import merge_charts_from_manifest
from app.services.scheduling_service import schedule_task
from app.ui.widgets.entry_table_row_ops import EntryTableRowOpsMixin
from app.ui.widgets.icon_button import make_icon_button
from app.ui.widgets.schedule_dialog import ask_for_schedule_datetime

logger = logging.getLogger(__name__)

_COLUMN_HEADERS = ["Order", "Song", "Matched"]
_ORDER_COL, _TITLE_COL, _CHART_COL, _CONFIDENCE_COL, _STATUS_COL = range(5)

_NO_MATCH_LABEL = "— No match —"

_PROBLEM_CELL_COLOR = QColor(255, 214, 214)
_OK_ROW_STATUS_COLOR = QColor(214, 255, 214)

_DEFAULT_MERGED_PDF_NAME = "Setlist Charts"
_ADD_NEW_FOLDER_LABEL = "+ Add New Folder…"
_NO_FOLDER_SELECTED_LABEL = "--"


class ChartsPage(QWidget, EntryTableRowOpsMixin):
    # Emitted with the newly-picked Target folder name whenever YOU pick
    # one (QComboBox.activated fires on user interaction only, never on
    # the programmatic select_folder/_refresh_folder_combo calls) --
    # PlaylistPage persists it against the current week so the choice
    # survives a restart. See PlaylistPage._remember_target.
    target_changed = Signal(str)

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._entries: list[SongEntry] = []
        self._setlist_id: Optional[int] = None
        self._charts_library: list[DriveDoc] = []
        self._init_row_ops("excluded_from_drive")

        self._folder_combo = QComboBox()
        self._folder_combo.setCursor(Qt.PointingHandCursor)
        self._folder_combo.currentIndexChanged.connect(self._on_folder_combo_changed)
        # activated (not currentIndexChanged) -- see target_changed.
        self._folder_combo.activated.connect(lambda _index: self.target_changed.emit(self.current_folder_name()))
        self._refresh_folder_combo()

        self._match_button = make_icon_button(
            "match", "Re-match every song's chart against the Drive charts folder."
        )
        self._match_button.clicked.connect(self._on_match_clicked)

        self._add_row_button = make_icon_button("add", "Add a blank row for a song OCR missed.")
        self._add_row_button.clicked.connect(self._on_add_song_clicked)

        self._remove_row_button = make_icon_button("remove", "Remove the currently selected row.")
        self._remove_row_button.clicked.connect(self._on_remove_row_clicked)

        self._download_and_merge_button = make_icon_button(
            "merge",
            "Removes every PDF currently in the Charts Download Folder, downloads "
            "this week's matched charts, and merges them into one setlist PDF -- "
            "all in one step.",
        )
        self._download_and_merge_button.clicked.connect(self._on_download_and_merge_clicked)

        self._schedule_button = make_icon_button(
            "schedule", "Schedule this week's Drive download/merge for a future date instead of running it now."
        )
        self._schedule_button.clicked.connect(self._on_schedule_clicked)

        self._summary_label = QLabel("No songs loaded yet.")
        self._summary_label.setWordWrap(True)

        self._table = QTableWidget()
        # QSS's "background-color: transparent" on QTableWidget (see
        # #ledgerPage QTableWidget in style.py) doesn't reach the table's
        # own viewport widget, which keeps auto-filling itself solid --
        # without this, the table hides PaperPanel's paper texture behind
        # a flat block, both under its rows AND the empty space below them.
        self._table.viewport().setAutoFillBackground(False)
        self._table.setColumnCount(len(_COLUMN_HEADERS))
        self._table.setHorizontalHeaderLabels(_COLUMN_HEADERS)

        # "Song" gets a fixed width instead of also Stretch-ing -- with
        # TWO Stretch columns Qt just splits the leftover space 50/50
        # (it doesn't preserve whatever width ratio was set beforehand),
        # which shortchanges "Matched" even though chart names + artist
        # need more room than the plain OCR'd song title. With only
        # "Matched" left as Stretch, it's guaranteed to take 100% of
        # whatever space "Order" and "Song" don't use.
        self._table.setColumnWidth(_TITLE_COL, 150)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(_ORDER_COL, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_TITLE_COL, QHeaderView.Interactive)
        header.setSectionResizeMode(_CHART_COL, QHeaderView.Stretch)

        # Left-align header text to match the left-aligned cell content
        # below it (Qt centers header labels by default).
        for col in (_ORDER_COL, _TITLE_COL, _CHART_COL):
            header_item = self._table.horizontalHeaderItem(col)
            header_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._table.setEditTriggers(QTableWidget.NoEditTriggers)

        top_row = QHBoxLayout()
        # Tighter than the icons' own fixed 34px size would suggest --
        # per direct feedback, the default (unset) QHBoxLayout gap read
        # as too spread out.
        top_row.setSpacing(4)
        top_row.addWidget(QLabel("Target:"))
        top_row.addWidget(self._folder_combo)
        top_row.addWidget(self._match_button)
        top_row.addWidget(self._add_row_button)
        top_row.addWidget(self._remove_row_button)
        # Schedule right before the immediate action (Download+Merge),
        # which stays LAST -- per direct feedback, the last icon in the
        # row should always be the immediate action, schedule right
        # before it.
        top_row.addWidget(self._schedule_button)
        top_row.addWidget(self._download_and_merge_button)
        top_row.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        # Stretch factor 1: the table itself claims whatever leftover
        # height the container gives it (the embedded "drive" card is
        # sized to the receipt's own tall rect, well beyond the rows
        # above's natural height) -- so it can show more rows without an
        # internal scrollbar, rather than staying at a small natural
        # size while a trailing addStretch collects that same space as
        # wasted blank area below it. The rows above stay pinned to
        # their own natural size since they have no stretch factor.
        layout.addWidget(self._table, 1)
        self._layout = layout

        # self._update_table_height()

    def set_compact(self, row_height: int = 22, margin: int = 0, spacing: int = 6) -> None:
        """
        Tighten margins/spacing and shrink table rows -- purely a layout
        density change (no business logic touched), for contexts with
        much less vertical room than this page's original full-window
        home, e.g. PlaylistPage's in-place "drive" panel. Lets more table
        rows fit before scrolling without changing what's shown.
        """
        self._layout.setContentsMargins(margin, margin, margin, margin)
        self._layout.setSpacing(spacing)
        self._table.verticalHeader().setDefaultSectionSize(row_height)
        self._table.verticalHeader().setVisible(False)
        self._folder_combo.setMaximumHeight(row_height + 6)
        # Match/Add/Remove/Download and Merge are now fixed-size square
        # icon buttons (see make_icon_button) -- deliberately left out of
        # the loop above; capping their height here without also capping
        # their width would break the "always square" requirement, and
        # ICON_BUTTON_SIZE is already compact enough not to need it.

        # self._update_table_height()

    # def _update_table_height(self) -> None:
    #     """
    #     Cap the table's height to exactly fit its current row count,
    #     instead of letting it stretch to fill whatever space the layout
    #     has available -- so "+ Add Song"/"Remove" sit directly below the
    #     last row rather than at the bottom of a mostly-empty table.
    #     Called whenever the row count (or row height, via set_compact())
    #     changes.
    #     """
    #     header_height = self._table.horizontalHeader().height()
    #     row_height = self._table.verticalHeader().defaultSectionSize()
    #     content_height = header_height + self._table.rowCount() * row_height + 2 * self._table.frameWidth()
    #     self._table.setMaximumHeight(content_height)

    def set_entries(self, entries: list[SongEntry], setlist_id: Optional[int] = None) -> None:
        """
        Called by MainWindow when the user chooses 'Set Up Charts'.

        `setlist_id` is the persisted Setlist row this week's entries
        were saved under (see PlaylistPage._save_entries_to_database) --
        needed by the Schedule button, since scheduling_service works
        against a Setlist id, not an in-memory entries list. None for a
        rare case where saving to the database itself failed.
        """
        self._entries = entries
        self._setlist_id = setlist_id
        self._table.setRowCount(0)
        # self._update_table_height()
        self._summary_label.setText(
            f"{len(entries)} song(s) ready. Click 'Match Songs' to look them up."
        )
        # Re-read Settings here, not just at construction time -- folders
        # may have been added since this page was first built (e.g. via
        # the Settings dialog, opened from a completely different page).
        self._refresh_folder_combo()

    # ------------------------------------------------------------------
    # Target folder selection
    # ------------------------------------------------------------------

    def _refresh_folder_combo(self) -> None:
        """
        Rebuilds the dropdown, preserving your current pick by name if it
        still exists -- otherwise (including the very first time, before
        you've ever chosen one) falls back to the "--" placeholder, so
        Target never silently defaults to some folder you didn't
        actually pick.
        """
        previous_selection = self._folder_combo.currentText()

        self._folder_combo.blockSignals(True)
        self._folder_combo.clear()
        self._folder_combo.addItem(_NO_FOLDER_SELECTED_LABEL)
        for name in sorted(self._settings.chart_folders.keys()):
            self._folder_combo.addItem(name)
        self._folder_combo.addItem(_ADD_NEW_FOLDER_LABEL)

        restored_index = self._folder_combo.findText(previous_selection)
        self._folder_combo.setCurrentIndex(restored_index if restored_index >= 0 else 0)
        self._folder_combo.blockSignals(False)

    def _on_folder_combo_changed(self, _index: int) -> None:
        if self._folder_combo.currentText() == _ADD_NEW_FOLDER_LABEL:
            self._prompt_add_folder()
            return
        # The merged PDF's filename is now derived directly from
        # whichever folder is selected (see _merged_pdf_filename) -- no
        # separate field to keep in sync here anymore.

    def _prompt_add_folder(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Add Chart Folder", "Folder name (e.g. 'Miércoles'):"
        )
        if not ok or not name.strip():
            self._refresh_folder_combo()  # revert the combo back off "+ Add New..."
            return

        folder_path = QFileDialog.getExistingDirectory(self, "Select Chart Folder")
        if not folder_path:
            self._refresh_folder_combo()
            return

        self._settings.chart_folders[name.strip()] = folder_path
        self._settings.save()
        logger.info("Saved new chart folder '%s' -> %s.", name.strip(), folder_path)

        self._refresh_folder_combo()
        index = self._folder_combo.findText(name.strip())
        if index >= 0:
            self._folder_combo.setCurrentIndex(index)

    def _current_folder_path(self) -> Optional[str]:
        name = self._folder_combo.currentText()
        return self._settings.chart_folders.get(name)

    def current_folder_name(self) -> str:
        """
        The Target combo's current text -- used by PlaylistPage to
        remember your pick per Monthly week. Returns "" for the "--"
        placeholder or "+ Add New Folder…" (rather than that literal
        label), so PlaylistPage's `if remembered:` check never re-selects
        either one later -- re-selecting "+ Add New Folder…" would
        actually re-trigger the add-folder prompt via
        _on_folder_combo_changed, popping up unexpectedly on a tab you
        just opened.
        """
        text = self._folder_combo.currentText()
        if text in (_NO_FOLDER_SELECTED_LABEL, _ADD_NEW_FOLDER_LABEL):
            return ""
        return text

    def select_folder(self, name: str) -> None:
        """Selects `name` in the Target combo if it's present -- a no-op if it isn't (e.g. deleted since)."""
        index = self._folder_combo.findText(name)
        if index >= 0:
            self._folder_combo.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _on_match_clicked(self) -> None:
        self.match_charts()

    def match_charts(self) -> None:
        """
        Match every loaded song against the configured Drive charts
        folder.

        Public (not just a private click handler) so MainWindow can
        trigger this automatically right after Playlist hands off
        entries -- same reasoning as YouTubePage.match_songs().
        """
        if not self._visible_entries():
            self._summary_label.setText("No songs loaded yet.")
            return

        client_secret_path = self._settings.google_drive_client_secret_path
        folder_id = self._settings.google_drive_charts_folder_id
        if not client_secret_path or not folder_id:
            self._summary_label.setText(
                "Configure the Google Drive Client Secret File and Charts Folder ID "
                "in Settings first."
            )
            return

        self._summary_label.setText("Authenticating with Google Drive...")
        self._summary_label.repaint()
        creds = authenticate(client_secret_path)
        if creds is None:
            self._summary_label.setText(
                "Google Drive authentication failed -- check the log file for details."
            )
            return

        self._summary_label.setText("Listing charts folder...")
        self._summary_label.repaint()
        drive = get_drive_client(creds)
        self._charts_library = list_docs_in_folder(drive, folder_id)

        if not self._charts_library:
            self._summary_label.setText(
                "No chart docs found -- check the Charts Folder ID in Settings."
            )
            return

        self._rebuild_table()
        self._summary_label.setText(
            f"Matched {len(self._visible_entries())} song(s) against {len(self._charts_library)} chart(s)."
        )

    def _build_row(self, row: int, entry: SongEntry) -> None:
        title = entry.audio_match.title if entry.audio_match else entry.ocr_title

        order_item = QTableWidgetItem(str(entry.order))
        order_item.setFlags(order_item.flags() & ~Qt.ItemIsEditable)
        self._table.setItem(row, _ORDER_COL, order_item)

        title_item = QTableWidgetItem(title)
        title_item.setFlags(title_item.flags() & ~Qt.ItemIsEditable)
        self._table.setItem(row, _TITLE_COL, title_item)

        # Only compute a fresh match if this song doesn't already have
        # one -- same reasoning as the YouTube page: a manual correction
        # must survive a table rebuild untouched.
        if entry.chart_match is None and self._charts_library:
            result = find_chart_for_song(title, self._charts_library)
            if result is not None:
                matched_doc, score = result
                entry.chart_match = ChartMatch(
                    title=matched_doc.name, doc_id=matched_doc.id, confidence=score
                )

        combo = self._make_chart_combo(entry.chart_match)
        combo.currentIndexChanged.connect(lambda _i, r=row: self._on_match_changed(r))
        self._table.setCellWidget(row, _CHART_COL, combo)

        # self._refresh_row_status(row, entry)

    def _make_chart_combo(self, current_match: Optional[ChartMatch]) -> QComboBox:
        combo = QComboBox()
        combo.setCursor(Qt.PointingHandCursor)
        combo.addItem(_NO_MATCH_LABEL, userData=None)

        selected_index = 0
        sorted_library = sorted(self._charts_library, key=lambda d: d.name.lower())
        for i, doc in enumerate(sorted_library, start=1):
            combo.addItem(doc.name, userData=doc.id)
            if current_match is not None and doc.id == current_match.doc_id:
                selected_index = i

        combo.setCurrentIndex(selected_index)
        return combo

    def _on_match_changed(self, row: int) -> None:
        combo = self._table.cellWidget(row, _CHART_COL)
        doc_id = combo.currentData()
        entry = self._visible_entries()[row]

        if doc_id is None:
            entry.chart_match = None
            # self._refresh_row_status(row, entry)
            return

        matched_doc = next((d for d in self._charts_library if d.id == doc_id), None)
        if matched_doc is not None:
            # A manual pick is a CONFIRMED match -- 100%, same convention
            # as the YouTube page's match dropdown.
            entry.chart_match = ChartMatch(title=matched_doc.name, doc_id=matched_doc.id, confidence=100.0)

    # def _refresh_row_status(self, row: int, entry: SongEntry) -> None:
        # threshold = self._settings.match_confidence_threshold
        # match = entry.chart_match

        # score = match.confidence if match else 0.0
        # confidence_item = QTableWidgetItem(f"{score:.0f}" if match else "—")
        # confidence_item.setFlags(confidence_item.flags() & ~Qt.ItemIsEditable)
        # if match is None or score < threshold:
        #     confidence_item.setBackground(_PROBLEM_CELL_COLOR)
        # self._table.setItem(row, _CONFIDENCE_COL, confidence_item)

        # is_ok = match is not None and score >= threshold
        # status_item = QTableWidgetItem("OK" if is_ok else "Needs Review")
        # status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
        # status_item.setBackground(_OK_ROW_STATUS_COLOR if is_ok else _PROBLEM_CELL_COLOR)
        # self._table.setItem(row, _STATUS_COL, status_item)

    def _rebuild_table(self) -> None:
        visible = self._visible_entries()
        self._table.setRowCount(len(visible))
        for row, entry in enumerate(visible):
            self._build_row(row, entry)
        # self._update_table_height()

    # ------------------------------------------------------------------
    # Row management
    # ------------------------------------------------------------------

    def _on_add_song_clicked(self) -> None:
        """
        Add a blank row for a song OCR missed -- inserted directly below
        the currently selected row (renumbering everything after it), or
        appended to the end if nothing's selected (see
        EntryTableRowOpsMixin._insert_new_entry_below_selected). Its
        chart_match starts as None, so it gets auto-matched on the next
        rebuild if the charts folder has already been listed -- every
        OTHER row's existing match is left completely untouched.
        """
        _entry, insert_at = self._insert_new_entry_below_selected()
        logger.info(
            "User added a new blank Charts row at position %d (now %d total).",
            insert_at,
            len(self._visible_entries()),
        )

    def _on_remove_row_clicked(self) -> None:
        """
        Remove the currently selected row (see
        EntryTableRowOpsMixin._remove_selected_entry). Every remaining
        song's chart_match is untouched -- removal doesn't trigger any
        re-matching.
        """
        removed = self._remove_selected_entry()
        if removed is None:
            logger.info("Remove Row clicked with no row selected -- nothing to do.")
            return
        logger.info("Removed song '%s' from Charts.", removed.ocr_title)

    # ------------------------------------------------------------------
    # Downloading matched charts
    # ------------------------------------------------------------------

    def _clear_download_folder(self, folder_path: str) -> int:
        """
        Remove every existing PDF from the charts download folder, the
        same "wipe it, then fill it" approach the YouTube page uses for
        playlists (clear_playlist() then add_videos_to_playlist()).

        Only touches "*.pdf" files directly in the folder -- never
        subfolders, never other file types -- so anything else you keep
        in that folder is left alone even though the PDFs get replaced
        wholesale every run.

        Returns the number of files removed. Logs and skips (rather than
        raising) any individual deletion that fails -- e.g. a file
        that's open elsewhere -- so one stuck file doesn't block
        clearing the rest.
        """
        folder = Path(folder_path)
        if not folder.exists():
            return 0

        removed_count = 0
        for pdf_path in folder.glob("*.pdf"):
            try:
                pdf_path.unlink()
                removed_count += 1
            except OSError as error:
                logger.error("Failed to remove old chart PDF %s: %s", pdf_path, error)

        logger.info("Removed %d old chart PDF(s) from %s", removed_count, folder)
        return removed_count

    def _merged_pdf_filename(self) -> str:
        """
        The merged PDF's filename, taken directly from whichever Target
        folder is selected (no separate name field anymore -- per direct
        feedback, the two always matched anyway). Path(...).name strips
        out any "/" a folder name might contain, so this can never write
        outside the configured merged-output folder; ".pdf" is guaranteed.
        """
        typed_name = Path(self._folder_combo.currentText().strip()).name
        if not typed_name:
            typed_name = _DEFAULT_MERGED_PDF_NAME
        if not typed_name.lower().endswith(".pdf"):
            typed_name += ".pdf"
        return typed_name

    def _on_download_and_merge_clicked(self) -> None:
        """
        Download this week's matched charts AND merge them into one
        setlist PDF, in one click -- per direct feedback that having
        Download and Merge as two separate button presses was pure
        friction (you always did both, back to back, anyway). Matches
        how the scheduled/automatic Drive task already behaves (see
        scheduling_service._execute_drive), just now available as a
        single interactive button too.

        Validates BOTH steps' preconditions up front, before showing
        the one combined confirmation -- so confirming never leads
        straight into a second failure (e.g. discovering the merged
        output folder isn't configured only after charts already
        downloaded).
        """
        visible = self._visible_entries()
        if not visible or self._table.rowCount() == 0:
            self._summary_label.setText("Match songs first before downloading charts.")
            return

        client_secret_path = self._settings.google_drive_client_secret_path
        charts_download_folder = self._current_folder_path()
        if not client_secret_path or not charts_download_folder:
            self._summary_label.setText(
                "Configure the Google Drive Client Secret File in Settings, and "
                "select (or add) a Target Folder above, first."
            )
            return
        if not self._settings.charts_merged_output_folder:
            self._summary_label.setText("Configure the Charts Merged PDF Folder in Settings first.")
            return

        downloadable = [e for e in visible if e.chart_match is not None]
        skipped_no_match = sum(1 for e in visible if e.chart_match is None)
        if not downloadable:
            self._summary_label.setText("Nothing to download -- match songs first.")
            return

        output_path = Path(self._settings.charts_merged_output_folder) / self._merged_pdf_filename()

        confirmed = QMessageBox.question(
            self,
            "Download and Merge Charts?",
            f"This will REMOVE every PDF currently in:\n\n{charts_download_folder}\n\n"
            f"download {len(downloadable)} new chart(s), and merge them, in setlist "
            f"order, into:\n\n{output_path}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return

        self._summary_label.setText("Authenticating with Google Drive...")
        self._summary_label.repaint()
        creds = authenticate(client_secret_path)
        if creds is None:
            self._summary_label.setText(
                "Google Drive authentication failed -- check the log file for details."
            )
            return

        removed_count = self._clear_download_folder(charts_download_folder)

        drive = get_drive_client(creds)
        downloaded_count = 0
        for entry in downloadable:
            title = entry.audio_match.title if entry.audio_match else entry.ocr_title
            download_path = Path(charts_download_folder) / f"{entry.order:02d} - {title}.pdf"
            result = export_doc_as_pdf(drive, entry.chart_match.doc_id, str(download_path))
            if result is not None:
                entry.chart_pdf_path = result
                downloaded_count += 1

        manifest = build_manifest(visible, self._settings)
        merged_count = merge_charts_from_manifest(manifest, str(output_path))

        message = (
            f"Removed {removed_count} old chart(s). "
            f"Downloaded {downloaded_count}/{len(downloadable)} new chart(s). "
        )
        if skipped_no_match:
            message += f"{skipped_no_match} song(s) skipped (no match). "
        message += f"Merged {merged_count} chart(s) into {output_path}." if merged_count else "Merge failed -- check the log file."
        self._summary_label.setText(message)

    def _on_schedule_clicked(self) -> None:
        if self._setlist_id is None:
            QMessageBox.warning(
                self, "Can't Schedule", "This week's songs weren't saved to the database, so there's nothing to schedule."
            )
            return
        visible = self._visible_entries()
        if not visible or any(entry.chart_match is None for entry in visible):
            QMessageBox.warning(self, "Can't Schedule", "Every song needs a chart match first -- click Match.")
            return
        if self._folder_combo.currentText() == _NO_FOLDER_SELECTED_LABEL:
            QMessageBox.warning(self, "Can't Schedule", "Select (or add) a Target folder above first.")
            return

        setlist = get_setlist(self._setlist_id)
        service_date = setlist.service_date if setlist else None
        scheduled_for = ask_for_schedule_datetime(
            "Drive chart download + merge", self._settings, parent=self, service_date=service_date
        )
        if scheduled_for is None:
            return

        # Persist the in-memory matches (Match/Add/Remove edits) so the
        # future execution reads the same matches you just reviewed --
        # this page never writes back to the database on its own
        # otherwise (see the module docstring's MATCH PERSISTENCE note).
        # The FULL shared list, not just this tab's own visible entries --
        # replace_song_entries deletes every existing row for this
        # setlist first, so saving only Drive's subset would silently
        # erase YouTube's/Logic Pro's own excluded-from-drive songs from
        # the database entirely.
        replace_song_entries(self._setlist_id, self._entries)
        schedule_task(self._setlist_id, ActionType.DRIVE, scheduled_for, target_name=self._folder_combo.currentText())
        self._summary_label.setText(f"Scheduled for {scheduled_for.strftime('%b %d, %Y %I:%M %p')}.")
