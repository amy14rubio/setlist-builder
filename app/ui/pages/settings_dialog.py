"""
Settings dialog for Setlist Builder.

Configures the paths and thresholds the app needs, grouped into four
horizontal tabs (General, Logic Pro, YouTube, Drive) so it's clear at a
glance which settings belong to which feature, without needing to scroll
past three other features' settings to find the one you want.

Styled as a frameless "card" popup -- same PaperPanel + card_asset.png
texture, ledgerPage theming, and top-right X close button as the shared
front card in playlist_page.py -- rather than a plain OS dialog window.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.models.settings import Settings
from app.ui.style import PAPER_BORDER, PAPER_FONT_FAMILY, PAPER_INK
from app.ui.widgets.card_dialog import CARD_WIDTH, setup_card_window
from app.ui.widgets.icon_button import make_icon_button

# Minimum width for path/URL/ID text fields -- a floor only; the actual
# width comes from _tighten_label_column() shrinking each tab's label
# column to its own longest label, then letting the field expand into
# whatever's left of the card's width.
_FIELD_MIN_WIDTH = 150


class SettingsDialog(QDialog):
    """
    Dialog for editing and saving Settings.

    The dialog is handed the live Settings object, edits it in place, and
    calls settings.save() itself when the user clicks Save. This keeps
    "how settings get persisted" in one place (Settings.save()) rather
    than duplicating file-writing logic here in the UI layer.

    The YouTube playlist list and chart folder list are both exceptions:
    add/remove there save IMMEDIATELY (not batched with the main Save
    button), since it's the same dict.pop()/dict[name]=value pattern
    the YouTube page and Charts page themselves use when you add a new
    entry from their own "+ Add New…" option -- keeping every entry
    point behave identically avoids a confusing "did that save or not"
    moment.
    """

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self._settings = settings

        _, card_layout, close_button = setup_card_window(self, CARD_WIDTH)

        self._audio_path_field = QLineEdit(settings.audio_library_path)
        self._logic_path_field = QLineEdit(settings.logic_library_path)
        self._output_path_field = QLineEdit(settings.desktop_output_folder)

        self._confidence_spinbox = QSpinBox()
        self._confidence_spinbox.setRange(0, 100)
        self._confidence_spinbox.setSuffix("%")
        self._confidence_spinbox.setValue(settings.match_confidence_threshold)
        self._confidence_spinbox.setFixedWidth(80)
        self._confidence_spinbox.setToolTip(
            "Matches scoring below this value are flagged as 'Needs Review' "
            "on the Review screen. Lower this if the app is flagging too many "
            "correct matches; raise it if it's letting bad matches through."
        )

        self._youtube_client_secret_field = QLineEdit(settings.youtube_client_secret_path)

        self._video_lookup_url_field = QLineEdit(settings.video_lookup_url)
        self._video_lookup_url_field.setToolTip(
            "The site scraped to look up each song's YouTube video. Blank until "
            "you configure it -- and even then, this only finds matches if the "
            "site lists songs as a plain list of song-title links to youtu.be "
            "videos. If your songs aren't listed anywhere with that structure, "
            "this feature won't find matches regardless of the URL."
        )

        self._drive_client_secret_field = QLineEdit(settings.google_drive_client_secret_path)

        self._drive_charts_folder_field = QLineEdit(settings.google_drive_charts_folder_id)
        self._drive_charts_folder_field.setToolTip(
            "The Drive folder containing one Google Doc chart per song. Copy the ID "
            "from the folder's URL -- the part after 'folders/'."
        )

        self._charts_merged_output_folder_field = QLineEdit(settings.charts_merged_output_folder)
        self._charts_merged_output_folder_field.setToolTip(
            "Where the final merged setlist PDF is written -- can be a different "
            "folder than the downloads above (e.g. a shared team folder)."
        )
        for field in (self._video_lookup_url_field, self._drive_charts_folder_field):
            field.setMinimumWidth(_FIELD_MIN_WIDTH)
            field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # -- General (shared across Logic Pro, YouTube, and Drive) --
        general_form = self._new_form()
        general_form.addRow("Match Confidence Threshold:", self._confidence_spinbox)

        # -- Logic Pro --
        logic_form = self._new_form()
        logic_form.addRow("Audio Library:", self._build_path_row(self._audio_path_field))
        logic_form.addRow("Logic Library:", self._build_path_row(self._logic_path_field))
        logic_form.addRow(
            "Desktop Output Folder:", self._build_path_row(self._output_path_field)
        )
        self._tighten_label_column(logic_form)

        # -- YouTube --
        youtube_form = self._new_form()
        youtube_form.addRow(
            "YouTube Client Secret File:",
            self._build_file_row(self._youtube_client_secret_field, "JSON Files (*.json)"),
        )
        youtube_form.addRow("Video Lookup Site:", self._video_lookup_url_field)
        self._tighten_label_column(youtube_form)

        # Saved YouTube playlists (Sunday, Wednesday, Youth Group, ...)
        self._playlists_list = QListWidget()
        self._refresh_playlists_list()
        self._cap_list_height(self._playlists_list)

        add_playlist_button = make_icon_button("add", "Add a playlist")
        add_playlist_button.clicked.connect(self._on_add_playlist_clicked)
        remove_playlist_button = make_icon_button("remove", "Remove the selected playlist")
        remove_playlist_button.clicked.connect(self._on_remove_playlist_clicked)

        playlist_buttons_row = QHBoxLayout()
        playlist_buttons_row.addWidget(add_playlist_button)
        playlist_buttons_row.addWidget(remove_playlist_button)
        playlist_buttons_row.addStretch()

        youtube_layout = QVBoxLayout()
        youtube_layout.addLayout(youtube_form)
        youtube_layout.addWidget(QLabel("Saved YouTube Playlists:"))
        youtube_layout.addWidget(self._playlists_list)
        youtube_layout.addLayout(playlist_buttons_row)
        youtube_layout.addStretch()

        # -- Drive --
        drive_form = self._new_form()
        drive_form.addRow(
            "Google Drive Client Secret File:",
            self._build_file_row(self._drive_client_secret_field, "JSON Files (*.json)"),
        )
        drive_form.addRow("Google Drive Charts Folder ID:", self._drive_charts_folder_field)
        drive_form.addRow(
            "Charts Merged PDF Folder:",
            self._build_path_row(self._charts_merged_output_folder_field),
        )
        self._tighten_label_column(drive_form)

        # Saved chart folders (Miércoles, Domingo, Youth Group, ...)
        self._chart_folders_list = QListWidget()
        self._refresh_chart_folders_list()
        self._cap_list_height(self._chart_folders_list)

        add_chart_folder_button = make_icon_button("add", "Add a chart folder")
        add_chart_folder_button.clicked.connect(self._on_add_chart_folder_clicked)
        remove_chart_folder_button = make_icon_button("remove", "Remove the selected chart folder")
        remove_chart_folder_button.clicked.connect(self._on_remove_chart_folder_clicked)

        chart_folder_buttons_row = QHBoxLayout()
        chart_folder_buttons_row.addWidget(add_chart_folder_button)
        chart_folder_buttons_row.addWidget(remove_chart_folder_button)
        chart_folder_buttons_row.addStretch()

        drive_layout = QVBoxLayout()
        drive_layout.addLayout(drive_form)
        drive_layout.addWidget(QLabel("Saved Chart Folders:"))
        drive_layout.addWidget(self._chart_folders_list)
        drive_layout.addLayout(chart_folder_buttons_row)
        drive_layout.addStretch()

        tabs = QTabWidget()
        # Transparent, plain-text tabs (no chip background) -- matching
        # the paper-ink look of the Weekly/Monthly TextRegionButtons
        # elsewhere in the app. Since those buttons have no persistent
        # "selected" look of their own to copy (only a hover underline),
        # the active tab is instead marked the way a real paper folder
        # tab reads: a border on three sides (top/left/right) that stops
        # at the pane below it, per direct feedback.
        tabs.setStyleSheet(
            f"QTabWidget::pane {{ background: transparent; border: none; top: -1px; padding-top: 18px; }}"
            f"QTabBar {{ background: transparent; font-family: {PAPER_FONT_FAMILY}; }}"
            f"QTabBar::tab {{ background: transparent; border: none; padding: 8px 18px;"
            f" margin-right: 4px; color: {PAPER_INK}; }}"
            f"QTabBar::tab:hover {{ color: #000000; }}"
            f"QTabBar::tab:selected {{ background: transparent;"
            f" border-top: 1px solid {PAPER_BORDER}; border-left: 1px solid {PAPER_BORDER};"
            f" border-right: 1px solid {PAPER_BORDER}; border-top-left-radius: 6px;"
            f" border-top-right-radius: 6px; }}"
        )
        # QSS has no working "cursor" property in this Qt build (silently
        # a no-op, not just unstyled) -- every clickable widget needs its
        # cursor set directly in code instead.
        tabs.tabBar().setCursor(Qt.PointingHandCursor)
        tabs.addTab(self._build_tab(general_form), "General")
        tabs.addTab(self._build_tab(logic_form), "Logic Pro")
        tabs.addTab(self._build_tab(youtube_layout), "YouTube")
        tabs.addTab(self._build_tab(drive_layout), "Drive")

        # QTabWidget holds its pages in an internal QStackedWidget that
        # Qt creates automatically -- the "::pane" rule above only styles
        # the tab widget's own frame, not this separate child widget, so
        # it was still picking up the app-wide opaque QWidget background
        # and hiding the card texture behind every tab's content.
        tab_stack = tabs.findChild(QStackedWidget)
        if tab_stack is not None:
            tab_stack.setStyleSheet("background: transparent;")

        save_button = QPushButton("Save")
        save_button.setCursor(Qt.PointingHandCursor)
        save_button.clicked.connect(self._on_save)
        cancel_button = QPushButton("Cancel")
        cancel_button.setCursor(Qt.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)

        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(cancel_button)
        button_row.addWidget(save_button)

        card_layout.addWidget(tabs)
        card_layout.addLayout(button_row)
        close_button.raise_()

    def _new_form(self) -> QFormLayout:
        """
        A QFormLayout with left-aligned labels and tighter row spacing --
        per direct feedback that the default (right-aligned, roomy) rows
        felt loose and misaligned with the rest of the app's left-aligned
        text.
        """
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setVerticalSpacing(4)
        form.setHorizontalSpacing(12)
        # Default policy only grows a field up to its own sizeHint --
        # since every field here is meant to fill whatever room the
        # (now-tightened) label column leaves it, force it to actually do
        # that instead of sitting at content width.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        return form

    def _tighten_label_column(self, form: QFormLayout) -> None:
        """
        Shrinks every label in `form` down to the width of its own
        longest label (rather than whatever width QFormLayout's default
        column sizing leaves it at) -- per direct feedback that the
        fields should get as much of the remaining width as possible.
        """
        labels = []
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.LabelRole)
            if item is not None and item.widget() is not None:
                labels.append(item.widget())
        if not labels:
            return
        width = max(label.sizeHint().width() for label in labels)
        for label in labels:
            label.setFixedWidth(width)

    def _cap_list_height(self, list_widget: QListWidget, max_visible_rows: int = 10) -> None:
        """
        Caps a saved-playlists/saved-folders list to roughly
        `max_visible_rows` tall instead of letting it default to
        Expanding -- per direct feedback that a near-empty list was
        stretching to fill the whole tab, pushing the Add/Remove buttons
        far below the last real row instead of sitting right under it.
        """
        row_height = list_widget.sizeHintForRow(0) if list_widget.count() else 24
        visible_rows = max(list_widget.count(), 1)
        # A flat "+6" padding was too tight once the app's real serif font
        # (not available in every environment, e.g. headless testing --
        # Qt then falls back to a slightly taller default) was actually
        # loaded, clipping the last row and forcing a scrollbar for a
        # list short enough that it should never need one. Padding by a
        # full extra row height instead of a few flat pixels is generous
        # enough to absorb that kind of font-metrics difference.
        list_widget.setMaximumHeight(row_height * min(visible_rows, max_visible_rows) + row_height)
        list_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

    def _build_tab(self, content) -> QWidget:
        """
        Wraps a tab's form/layout in a scroll area (the card is shorter
        than some tabs' full content, e.g. Drive's chart-folder list).
        Both the tab page and the scroll area stay fully transparent so
        the card's own texture shows straight through, matching how
        every other ledgerPage-themed page in the app works.
        """
        content_widget = QWidget()
        content_widget.setStyleSheet("background: transparent;")
        content_widget.setLayout(content)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(content_widget)
        return scroll

    def _build_path_row(self, line_edit: QLineEdit) -> QHBoxLayout:
        """A text field + folder-icon Browse button, wired to open a folder picker."""
        line_edit.setMinimumWidth(_FIELD_MIN_WIDTH)
        line_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        browse_button = make_icon_button("folder", "Browse for a folder…")
        browse_button.clicked.connect(lambda: self._browse_for_folder(line_edit))

        row = QHBoxLayout()
        row.addWidget(line_edit)
        row.addWidget(browse_button)
        return row

    def _build_file_row(self, line_edit: QLineEdit, file_filter: str) -> QHBoxLayout:
        """A text field + folder-icon Browse button, wired to open a FILE picker (not a folder)."""
        line_edit.setMinimumWidth(_FIELD_MIN_WIDTH)
        line_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        browse_button = make_icon_button("folder", "Browse for a file…")
        browse_button.clicked.connect(lambda: self._browse_for_file(line_edit, file_filter))

        row = QHBoxLayout()
        row.addWidget(line_edit)
        row.addWidget(browse_button)
        return row

    def _browse_for_file(self, line_edit: QLineEdit, file_filter: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select File", line_edit.text(), file_filter)
        if path:
            line_edit.setText(path)

    def _browse_for_folder(self, line_edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", line_edit.text())
        if folder:
            line_edit.setText(folder)

    def _refresh_playlists_list(self) -> None:
        self._playlists_list.clear()
        for name in sorted(self._settings.youtube_playlists.keys()):
            playlist_id = self._settings.youtube_playlists[name]
            item = QListWidgetItem(f"{name}  —  {playlist_id}")
            item.setData(Qt.UserRole, name)  # store the real name, not the display text
            self._playlists_list.addItem(item)

    def _on_add_playlist_clicked(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Add Playlist", "Playlist name (e.g. 'Lista del Miércoles'):"
        )
        if not ok or not name.strip():
            return

        playlist_id, ok = QInputDialog.getText(
            self, "Add Playlist", "Playlist ID (from its URL, the part after 'list='):"
        )
        if not ok or not playlist_id.strip():
            return

        self._settings.youtube_playlists[name.strip()] = playlist_id.strip()
        self._settings.save()
        self._refresh_playlists_list()

    def _on_remove_playlist_clicked(self) -> None:
        item = self._playlists_list.currentItem()
        if item is None:
            return

        name = item.data(Qt.UserRole)
        self._settings.youtube_playlists.pop(name, None)
        self._settings.save()
        self._refresh_playlists_list()

    def _refresh_chart_folders_list(self) -> None:
        self._chart_folders_list.clear()
        for name in sorted(self._settings.chart_folders.keys()):
            folder_path = self._settings.chart_folders[name]
            item = QListWidgetItem(f"{name}  —  {folder_path}")
            item.setData(Qt.UserRole, name)  # store the real name, not the display text
            self._chart_folders_list.addItem(item)

    def _on_add_chart_folder_clicked(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Add Chart Folder", "Folder name (e.g. 'Miércoles'):"
        )
        if not ok or not name.strip():
            return

        folder_path = QFileDialog.getExistingDirectory(self, "Select Chart Folder")
        if not folder_path:
            return

        self._settings.chart_folders[name.strip()] = folder_path
        self._settings.save()
        self._refresh_chart_folders_list()

    def _on_remove_chart_folder_clicked(self) -> None:
        item = self._chart_folders_list.currentItem()
        if item is None:
            return

        name = item.data(Qt.UserRole)
        self._settings.chart_folders.pop(name, None)
        self._settings.save()
        self._refresh_chart_folders_list()

    def _on_save(self) -> None:
        self._settings.audio_library_path = self._audio_path_field.text().strip()
        self._settings.logic_library_path = self._logic_path_field.text().strip()
        self._settings.desktop_output_folder = self._output_path_field.text().strip()
        self._settings.match_confidence_threshold = self._confidence_spinbox.value()
        self._settings.youtube_client_secret_path = self._youtube_client_secret_field.text().strip()
        self._settings.video_lookup_url = self._video_lookup_url_field.text().strip()
        self._settings.google_drive_client_secret_path = self._drive_client_secret_field.text().strip()
        self._settings.google_drive_charts_folder_id = self._drive_charts_folder_field.text().strip()
        self._settings.charts_merged_output_folder = (
            self._charts_merged_output_folder_field.text().strip()
        )
        self._settings.save()
        self.accept()
