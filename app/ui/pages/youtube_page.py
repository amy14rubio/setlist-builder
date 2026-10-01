"""
YouTube Playlist page for Setlist Builder.

Independent from the Logic Pro build steps -- lets you review and
correct which YouTube video each song matched to (sourced from the
external site configured in Settings.video_lookup_url) before actually
touching a real playlist.

Multiple named playlists (Sunday, Wednesday, Youth Group, ...) are
supported: pick one from the dropdown, or add a new one. Once saved, its
ID is remembered permanently -- you only ever paste a playlist ID in once.

MATCH PERSISTENCE
-------------------
Each song's chosen video match is stored directly on its SongEntry
(`entry.youtube_match`), the same way audio/Logic matches work on the
Review page -- NOT in a page-level dictionary keyed by row number. This
matters: row numbers shift whenever a row is added or removed, so a
row-indexed lookup would silently lose track of (or misattribute) a
manual correction the moment the table changed shape. Storing the match
on the entry itself means a rebuild can simply check "does this song
already have a match?" and leave it alone if so -- only genuinely new
rows (which start with youtube_match=None) get auto-matched.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
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

from app.models.scheduled_task import ActionType
from app.models.settings import Settings
from app.models.song_entry import SongEntry, YouTubeMatch
from app.services.database_service import get_setlist, replace_song_entries
from app.services.scheduling_service import schedule_task
from app.services.video_lookup_service import VideoLookupEntry, fetch_video_lookup_library, find_video_for_song
from app.services.youtube_service import add_videos_to_playlist, authenticate, clear_playlist, get_youtube_client
from app.ui.widgets.entry_table_row_ops import EntryTableRowOpsMixin
from app.ui.widgets.icon_button import make_icon_button
from app.ui.widgets.schedule_dialog import ask_for_schedule_datetime

logger = logging.getLogger(__name__)

_COLUMN_HEADERS = ["Order", "Song", "Matched"]
_ORDER_COL, _TITLE_COL, _VIDEO_COL, _CONFIDENCE_COL, _STATUS_COL = range(5)

_NO_MATCH_LABEL = "— No match —"
_ADD_NEW_PLAYLIST_LABEL = "+ Add New Playlist…"

_PROBLEM_CELL_COLOR = QColor(255, 214, 214)
_OK_ROW_STATUS_COLOR = QColor(214, 255, 214)


class YouTubePage(QWidget, EntryTableRowOpsMixin):
    # Emitted with the newly-picked Target playlist name whenever YOU
    # pick one (QComboBox.activated fires on user interaction only,
    # never on the programmatic select_playlist/_refresh_playlist_combo
    # calls) -- PlaylistPage persists it against the current week so the
    # choice survives a restart. See PlaylistPage._remember_target.
    target_changed = Signal(str)

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._entries: list[SongEntry] = []
        self._setlist_id: Optional[int] = None
        self._video_lookup_library: list[VideoLookupEntry] = []
        self._init_row_ops("excluded_from_youtube")

        self._playlist_combo = QComboBox()
        self._playlist_combo.setCursor(Qt.PointingHandCursor)
        self._playlist_combo.currentIndexChanged.connect(self._on_playlist_combo_changed)
        # activated (not currentIndexChanged) -- see target_changed.
        self._playlist_combo.activated.connect(lambda _index: self.target_changed.emit(self.current_playlist_name()))
        self._playlist_combo.setMinimumWidth(10)
        self._refresh_playlist_combo()


        self._match_button = make_icon_button(
            "match", "Re-match every song's video against the configured lookup site."
        )
        self._match_button.clicked.connect(self._on_match_clicked)

        self._add_row_button = make_icon_button("add", "Add a blank row for a song OCR missed.")
        self._add_row_button.clicked.connect(self._on_add_song_clicked)

        self._remove_row_button = make_icon_button("remove", "Remove the currently selected row.")
        self._remove_row_button.clicked.connect(self._on_remove_row_clicked)

        self._update_button = make_icon_button("update", "Replace the YouTube playlist with this week's matched videos.")
        self._update_button.clicked.connect(self._on_update_clicked)

        self._schedule_button = make_icon_button(
            "schedule", "Schedule this week's YouTube playlist update for a future date instead of running it now."
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
        self._table.setColumnWidth(_TITLE_COL, 180)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(_ORDER_COL, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_TITLE_COL, QHeaderView.Interactive)
        header.setSectionResizeMode(_VIDEO_COL, QHeaderView.Stretch)

        # Left-align header text to match the left-aligned cell content
        # below it (Qt centers header labels by default) -- matches
        # ChartsPage's identical override, so all three tables read as
        # one consistent family. This loop existed already but its body
        # was left commented out, so it was never actually doing anything.
        for col in (_ORDER_COL, _TITLE_COL, _VIDEO_COL):
            header_item = self._table.horizontalHeaderItem(col)
            header_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._table.setEditTriggers(QTableWidget.NoEditTriggers)

        top_row = QHBoxLayout()
        # Tighter than the icons' own fixed 34px size would suggest --
        # per direct feedback, the default (unset) QHBoxLayout gap read
        # as too spread out.
        top_row.setSpacing(4)
        top_row.addWidget(QLabel("Target:"))
        top_row.addWidget(self._playlist_combo)
        top_row.addWidget(self._match_button)
        top_row.addWidget(self._add_row_button)
        top_row.addWidget(self._remove_row_button)
        # Schedule right before the immediate action (Update), which
        # stays LAST -- per direct feedback, the last icon in the row
        # should always be the immediate action, schedule right before it.
        top_row.addWidget(self._schedule_button)
        top_row.addWidget(self._update_button)

        top_row.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        # layout.addLayout(row_button_row)
        # layout.addWidget(self._update_button)
        # Stretch factor 1: see ChartsPage's identical change for why --
        # the table itself now claims the container's full leftover
        # height (PlaylistPage's in-place panels are all sized to the
        # receipt's own tall rect) so more rows fit before scrolling,
        # instead of a trailing addStretch collecting that same space as
        # wasted blank area below a small, fixed-height table.
        layout.addWidget(self._table, 1)
        self._layout = layout

    def set_compact(self, row_height: int = 22, margin: int = 0, spacing: int = 6) -> None:
        """
        Tighten margins/spacing and shrink table rows -- purely a layout
        density change (no business logic touched), for contexts with
        much less vertical room than this page's original full-window
        home, e.g. PlaylistPage's in-place "youtube" panel. Lets more
        table rows fit before scrolling without changing what's shown.
        """
        self._layout.setContentsMargins(margin, margin, margin, margin)
        self._layout.setSpacing(spacing)
        # Was "row_height - 10" -- a leftover that made YouTube's rows
        # noticeably shorter than Charts'/Review's identical row_height,
        # despite all three being meant to read as one consistent family
        # of tables. Per direct feedback, all three must match exactly.
        self._table.verticalHeader().setDefaultSectionSize(row_height)
        self._table.verticalHeader().setVisible(False)
        self._playlist_combo.setMaximumHeight(row_height + 6)
        # Match/Add/Remove/Update are now fixed-size square icon buttons
        # (see make_icon_button) -- left out of the loop above, same
        # reasoning as ChartsPage.set_compact: capping height alone would
        # break their "always square" sizing.

    def set_entries(self, entries: list[SongEntry], setlist_id: Optional[int] = None) -> None:
        """
        Called by MainWindow when the user chooses 'Set Up YouTube Playlist'.

        `setlist_id` is the persisted Setlist row this week's entries
        were saved under -- needed by the Schedule button. None if that
        save failed (a rare case; see PlaylistPage._save_entries_to_database).
        """
        self._entries = entries
        self._setlist_id = setlist_id
        self._table.setRowCount(0)
        self._summary_label.setText(
            f"{len(entries)} song(s) ready. Click 'Match Songs' to look them up."
        )
        # Re-read Settings here, not just at construction time -- playlists
        # may have been added since this page was first built (e.g. via
        # the Settings dialog, opened from a completely different page).
        self._refresh_playlist_combo()

    # ------------------------------------------------------------------
    # Playlist selection
    # ------------------------------------------------------------------

    def _refresh_playlist_combo(self) -> None:
        self._playlist_combo.blockSignals(True)
        self._playlist_combo.clear()
        for name in sorted(self._settings.youtube_playlists.keys()):
            self._playlist_combo.addItem(name)
        self._playlist_combo.addItem(_ADD_NEW_PLAYLIST_LABEL)
        self._playlist_combo.blockSignals(False)

    def _on_playlist_combo_changed(self, _index: int) -> None:
        if self._playlist_combo.currentText() == _ADD_NEW_PLAYLIST_LABEL:
            self._prompt_add_playlist()

    def _prompt_add_playlist(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Add Playlist", "Playlist name (e.g. 'Lista del Miércoles'):"
        )
        if not ok or not name.strip():
            self._refresh_playlist_combo()  # revert the combo back off "+ Add New..."
            return

        playlist_id, ok = QInputDialog.getText(
            self, "Add Playlist", "Playlist ID (from its URL, the part after 'list='):"
        )
        if not ok or not playlist_id.strip():
            self._refresh_playlist_combo()
            return

        self._settings.youtube_playlists[name.strip()] = playlist_id.strip()
        self._settings.save()
        logger.info("Saved new YouTube playlist '%s'.", name.strip())

        self._refresh_playlist_combo()
        index = self._playlist_combo.findText(name.strip())
        if index >= 0:
            self._playlist_combo.setCurrentIndex(index)

    def _current_playlist_id(self) -> Optional[str]:
        name = self._playlist_combo.currentText()
        return self._settings.youtube_playlists.get(name)

    def current_playlist_name(self) -> str:
        """
        The Target combo's current text -- used by PlaylistPage to
        remember your pick per Monthly week. Returns "" for
        "+ Add New Playlist…" (rather than that literal label), so
        PlaylistPage's `if remembered:` check never re-selects it later
        -- doing so would actually re-trigger the add-playlist prompt via
        _on_playlist_combo_changed, popping up unexpectedly on a tab you
        just opened.
        """
        text = self._playlist_combo.currentText()
        if text == _ADD_NEW_PLAYLIST_LABEL:
            return ""
        return text

    def select_playlist(self, name: str) -> None:
        """Selects `name` in the Target combo if it's present -- a no-op if it isn't (e.g. deleted since)."""
        index = self._playlist_combo.findText(name)
        if index >= 0:
            self._playlist_combo.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _on_match_clicked(self) -> None:
        self.match_songs()

    def match_songs(self) -> None:
        """
        Fetch the configured lookup site (if not already fetched this
        session) and build/refresh the table.

        Public (not just a private click handler) so MainWindow can
        trigger this automatically right after the YouTube setup path
        hands off entries -- per the design, YouTube setup matches
        immediately without requiring an extra manual click.
        """
        if not self._visible_entries():
            self._summary_label.setText("No songs loaded yet.")
            return

        if not self._settings.video_lookup_url:
            self._summary_label.setText("Configure a Video Lookup Site in Settings first.")
            return

        self._summary_label.setText(f"Fetching song list from {self._settings.video_lookup_url}...")
        self._summary_label.repaint()

        self._video_lookup_library = fetch_video_lookup_library(self._settings.video_lookup_url)
        if not self._video_lookup_library:
            self._summary_label.setText(
                f"Could not fetch {self._settings.video_lookup_url} -- check your internet connection."
            )
            return

        self._rebuild_table()
        self._summary_label.setText(
            f"Matched {len(self._visible_entries())} song(s) against the configured lookup site."
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
        # one. This is the key to not losing manual corrections: a song
        # that was already matched (automatically or by hand) keeps
        # exactly what it had, even when the table gets rebuilt because
        # some OTHER row was added or removed.
        if entry.youtube_match is None and self._video_lookup_library:
            result = find_video_for_song(title, self._video_lookup_library)
            if result is not None:
                matched_entry, score = result
                entry.youtube_match = YouTubeMatch(
                    title=matched_entry.title,
                    video_id=matched_entry.video_id,
                    confidence=score,
                )

        combo = self._make_video_combo(entry.youtube_match)
        combo.currentIndexChanged.connect(lambda _i, r=row: self._on_match_changed(r))
        self._table.setCellWidget(row, _VIDEO_COL, combo)

        # self._refresh_row_status(row, entry)

    def _make_video_combo(self, current_match: Optional[YouTubeMatch]) -> QComboBox:
        combo = QComboBox()
        combo.setCursor(Qt.PointingHandCursor)
        combo.addItem(_NO_MATCH_LABEL, userData=None)

        selected_index = 0
        sorted_library = sorted(self._video_lookup_library, key=lambda e: e.title.lower())
        for i, entry in enumerate(sorted_library, start=1):
            combo.addItem(entry.title, userData=entry.video_id)
            if current_match is not None and entry.video_id == current_match.video_id:
                selected_index = i

        combo.setCurrentIndex(selected_index)
        return combo

    def _on_match_changed(self, row: int) -> None:
        combo = self._table.cellWidget(row, _VIDEO_COL)
        video_id = combo.currentData()
        entry = self._visible_entries()[row]

        if video_id is None:
            entry.youtube_match = None
            # self._refresh_row_status(row, entry)
            return

        matched_lib_entry = next(
            (e for e in self._video_lookup_library if e.video_id == video_id), None
        )
        if matched_lib_entry is not None:
            # A manual pick is a CONFIRMED match -- 100%, same convention
            # as the audio/Logic match dropdowns on the Review page.
            entry.youtube_match = YouTubeMatch(
                title=matched_lib_entry.title, video_id=matched_lib_entry.video_id, confidence=100.0
            )

        # self._refresh_row_status(row, entry)

    # def _refresh_row_status(self, row: int, entry: SongEntry) -> None:
    #     threshold = self._settings.match_confidence_threshold
    #     match = entry.youtube_match

    #     score = match.confidence if match else 0.0
    #     confidence_item = QTableWidgetItem(f"{score:.0f}" if match else "—")
    #     confidence_item.setFlags(confidence_item.flags() & ~Qt.ItemIsEditable)
    #     if match is None or score < threshold:
    #         confidence_item.setBackground(_PROBLEM_CELL_COLOR)
    #     self._table.setItem(row, _CONFIDENCE_COL, confidence_item)

    #     is_ok = match is not None and score >= threshold
    #     status_item = QTableWidgetItem("OK" if is_ok else "Needs Review")
    #     status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
    #     status_item.setBackground(_OK_ROW_STATUS_COLOR if is_ok else _PROBLEM_CELL_COLOR)
    #     self._table.setItem(row, _STATUS_COL, status_item)

    # ------------------------------------------------------------------
    # Row management
    # ------------------------------------------------------------------

    def _on_add_song_clicked(self) -> None:
        """
        Add a blank row for a song OCR missed -- inserted directly below
        the currently selected row (renumbering everything after it), or
        appended to the end if nothing's selected (see
        EntryTableRowOpsMixin._insert_new_entry_below_selected). Its
        youtube_match starts as None, so it gets auto-matched on the
        next rebuild if the lookup site has already been fetched -- every
        OTHER row's existing match is left completely untouched.
        """
        _entry, insert_at = self._insert_new_entry_below_selected()
        logger.info(
            "User added a new blank YouTube row at position %d (now %d total).",
            insert_at,
            len(self._visible_entries()),
        )

    def _on_remove_row_clicked(self) -> None:
        """
        Remove the currently selected row (see
        EntryTableRowOpsMixin._remove_selected_entry). Every remaining
        song's youtube_match is untouched -- removal doesn't trigger any
        re-matching.
        """
        removed = self._remove_selected_entry()
        if removed is None:
            logger.info("Remove Row clicked with no row selected -- nothing to do.")
            return
        logger.info("Removed song '%s' from YouTube.", removed.ocr_title)

    def _rebuild_table(self) -> None:
        """
        Fully redraw the table from this tab's own currently-included
        entries. Safe to call after any add/remove: _build_row only
        computes a fresh match for entries that don't already have one,
        so existing rows' matches (auto or manual) are preserved exactly
        as they were.
        """
        visible = self._visible_entries()
        self._table.setRowCount(len(visible))
        for row, entry in enumerate(visible):
            self._build_row(row, entry)

    # ------------------------------------------------------------------
    # Updating the real playlist
    # ------------------------------------------------------------------

    def _on_update_clicked(self) -> None:
        visible = self._visible_entries()
        if not visible or self._table.rowCount() == 0:
            self._summary_label.setText("Match songs first before updating the playlist.")
            return

        playlist_id = self._current_playlist_id()
        if not playlist_id:
            self._summary_label.setText("Select or add a target playlist first.")
            return

        client_secret_path = self._settings.youtube_client_secret_path
        if not client_secret_path:
            self._summary_label.setText(
                "Configure the YouTube Client Secret File in Settings first."
            )
            return

        video_ids: list[str] = []
        missing_titles: list[str] = []
        for entry in visible:
            title = entry.audio_match.title if entry.audio_match else entry.ocr_title
            if entry.youtube_match is None:
                missing_titles.append(title)
                continue
            video_ids.append(entry.youtube_match.video_id)

        if not video_ids:
            self._summary_label.setText("No matched videos to add -- nothing to update.")
            return

        warning_text = ""
        if missing_titles:
            warning_text = "\n\nThese songs have NO match and will be SKIPPED:\n" + "\n".join(
                f"  - {t}" for t in missing_titles
            )

        confirmed = QMessageBox.question(
            self,
            "Update YouTube Playlist?",
            f"This will REMOVE all current videos from "
            f"'{self._playlist_combo.currentText()}' and replace them with "
            f"{len(video_ids)} video(s).{warning_text}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return

        self._summary_label.setText("Authenticating with YouTube...")
        self._summary_label.repaint()
        creds = authenticate(client_secret_path)
        if creds is None:
            self._summary_label.setText(
                "YouTube authentication failed -- check the log file for details."
            )
            return

        youtube = get_youtube_client(creds)
        removed = clear_playlist(youtube, playlist_id)
        added = add_videos_to_playlist(youtube, playlist_id, video_ids)
        self._summary_label.setText(
            f"Done. Removed {removed} old video(s), added {added} new one(s)."
        )

    def _on_schedule_clicked(self) -> None:
        if self._setlist_id is None:
            QMessageBox.warning(
                self, "Can't Schedule", "This week's songs weren't saved to the database, so there's nothing to schedule."
            )
            return
        visible = self._visible_entries()
        if not visible or any(entry.youtube_match is None for entry in visible):
            QMessageBox.warning(self, "Can't Schedule", "Every song needs a video match first -- click Match.")
            return
        if not self._current_playlist_id():
            QMessageBox.warning(self, "Can't Schedule", "Select or add a target playlist first.")
            return

        setlist = get_setlist(self._setlist_id)
        service_date = setlist.service_date if setlist else None
        scheduled_for = ask_for_schedule_datetime(
            "YouTube playlist update", self._settings, parent=self, service_date=service_date
        )
        if scheduled_for is None:
            return

        # Persist the in-memory matches so the future execution reads the
        # same matches you just reviewed (see the module's MATCH
        # PERSISTENCE note -- this page never writes back on its own otherwise).
        # The FULL shared list, not just this tab's own visible entries --
        # replace_song_entries deletes every existing row for this
        # setlist first, so saving only YouTube's subset would silently
        # erase Drive's/Logic Pro's own excluded-from-youtube songs from
        # the database entirely.
        replace_song_entries(self._setlist_id, self._entries)
        schedule_task(self._setlist_id, ActionType.YOUTUBE, scheduled_for, target_name=self._playlist_combo.currentText())
        self._summary_label.setText(f"Scheduled for {scheduled_for.strftime('%b %d, %Y %I:%M %p')}.")
