"""
Playlist page for Setlist Builder.

The first screen the user sees, illustrated as a cassette. Two ALWAYS
visible entry points, the "Weekly"/"Monthly" mode picker (see
_mode_picker_large/_mode_picker_small), drive every import:
  - Weekly opens the single-screenshot OCR flow (unchanged from before),
    populating the base card (ReceiptDropView) behind the cassette.
  - Monthly opens the multi-week PDF import flow, populating the
    carousel panel in that same base-card rect instead.

Once a weekly screenshot's songs are loaded, the cassette's "logic pro"
/ "drive" / "youtube" labels open ONE SHARED front card (see
_show_front_card_tab / _make_front_card) -- raised in FRONT of the
cassette (unlike the base card, which stays behind it), positioned just
above that same label row so the three labels stay visible/clickable
underneath it for quick switching between tabs, with an X button
(_on_close_front_card_clicked) to dismiss it. All three tabs reuse the
SAME plain-title SongEntry list built from the weekly OCR pass (see
_current_entries) -- no second OCR pass per tab, and library/service
matching mutates those same entries in place (chart_match /
youtube_match / audio_match / logic_match), so switching tabs (or
closing and reopening the front card) never discards a manual
correction.

  - "drive" -- Charts matching/downloading/merging (ChartsPage).
  - "youtube" -- lookup-site matching + playlist update (YouTubePage).
  - "logic pro" -- Audio/Logic library matching (ReviewPage), then
    "Continue to Build" (inside that same tab) swaps to Copy Audio /
    Build Manifest / Run Logic Pro automation (BuildPage).

This page has no layout manager -- it's a fixed-size widget painted with
the cassette artwork as its background, with the card/star/region
widgets positioned absolutely on top of it, in cassette-relative
fractions of its size. Clicking blank cassette plastic no longer does
anything (importing now always goes through the mode picker) -- but
press-and-drag still moves the whole window, since there's no title bar
to do that for us once the window is frameless.

NOTE ON THREADING (a known limitation for now):
This runs OCR and matching synchronously on the UI thread. For a handful
of songs against a modestly-sized library, this is fast enough not to
matter in practice, and keeping it synchronous keeps this first working
version simple. If your libraries grow large enough that this becomes a
noticeable freeze, the fix is to move this work onto a QThread -- worth
flagging now so it doesn't come as a surprise later, but not worth
building before we know it's actually needed.
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QSize, Qt, Signal
from PySide6.QtGui import QCursor, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.models.scheduled_task import ActionType
from app.models.settings import Settings
from app.models.song_entry import SongEntry
from app.services.database_service import (
    add_song_entries,
    create_setlist,
    get_setlist,
    get_song_entries,
    replace_song_entries,
    set_setlist_target,
)
from app.services.library_scanner import scan_library
from app.services.ocr_service import extract_song_titles
from app.services.pdf_import_service import PdfSegmentationError, import_monthly_pdf
from app.services.scheduling_service import compute_default_schedule_datetime, schedule_task
from app.ui.assets import load_card_pixmap, load_cassette_pixmap, load_sheep_pixmap, load_task_manager_pixmap
from app.ui.pages.build_page import BuildPage
from app.ui.pages.charts_page import ChartsPage
from app.ui.pages.review_page import ReviewPage
from app.ui.pages.settings_dialog import SettingsDialog
from app.ui.pages.youtube_page import YouTubePage
from app.ui.widgets.monthly_carousel import MonthlyCarouselWidget
from app.ui.widgets.paper_panel import PaperPanel
from app.ui.widgets.receipt_view import ReceiptDropView
from app.ui.widgets.silhouette_button import SilhouetteButton
from app.ui.widgets.task_manager_dialog import show_task_manager_dialog
from app.ui.widgets.text_region_button import TextRegionButton

logger = logging.getLogger(__name__)


def _default_import_directory() -> str:
    """
    Where the Weekly/Monthly file pickers start browsing from -- your
    Downloads folder, per direct feedback, since that's where a
    screenshot or an emailed/exported monthly PDF actually lands in
    practice. Falls back to an empty string (Qt's own default, usually
    the last folder browsed) if Downloads doesn't exist for some reason,
    rather than pointing at a folder that isn't there.
    """
    downloads = Path.home() / "Downloads"
    return str(downloads) if downloads.is_dir() else ""


# Rendered cassette width in on-screen pixels -- everything else below is
# expressed as a fraction of this, so it scales together if this changes.
# Reduced from 780 (~18% smaller): at 780, the receipt widget (which is
# sized as a fraction of THIS width, not of its own native asset
# resolution) was being stretched noticeably past native resolution on
# HiDPI/Retina displays, reading as blurry.
_CASSETTE_TARGET_WIDTH = 640

# Fractions measured off the reference mockup: the shepherd (Task
# Manager) and its 3 sheep (Close/Minimize/Settings) sit as one tight
# flock in the cassette's bottom-left corner, below the label row --
# replacing the old star row that used to sit ABOVE the labels, spread
# across the full width. Left to right: shepherd, then the sheep in
# Close/Minimize/Settings order, per direct design decision.
#
# A shared BASELINE (not a center) -- the shepherd is taller than the
# sheep, so centering each independently left the shorter sheep floating
# above the ground the shepherd's own feet touch. Bottom-aligning
# everything to one fraction instead (see _bottom_align_on_cassette)
# keeps them standing on the same "ground line".
_FLOCK_BASELINE_Y_FRACTION = 0.97
_SHEEP_WIDTH_FRACTION = 0.045
_SHEPHERD_WIDTH_FRACTION = 0.05
_SHEPHERD_CENTER_X_FRACTION = 0.09
_CLOSE_SHEEP_CENTER_X_FRACTION = 0.15
_MINIMIZE_SHEEP_CENTER_X_FRACTION = 0.2
_SETTINGS_SHEEP_CENTER_X_FRACTION = 0.25

_LABEL_ROW_CENTER_Y_FRACTION = 0.74  # nudged up from 0.77, per direct feedback
_LOGIC_PRO_CENTER_X_FRACTION = 0.256
_DRIVE_CENTER_X_FRACTION = 0.501
_YOUTUBE_CENTER_X_FRACTION = 0.742
_REGION_FONT_POINT_SIZE = 40

# Extra breathing room above each region panel's content (Continue to
# Build / Add / Remove / table header row all sat flush against the
# paper's torn top edge otherwise) -- since these panels have a fixed
# height budget (see _make_panel), bottom stays tight.
# Widened from 50, then again from 75, per direct feedback that the
# front card's tabs (Target/Match row, table header) sat too close to
# the X button/top edge.
_PANEL_TOP_MARGIN = 90
# Left/right breathing room -- per direct feedback that "Target:"/"Order"
# text and the table's right edge were sitting flush against the card's
# own edges.
_PANEL_SIDE_MARGIN = 16

# Receipt/card placement: the card sits ON TOP of the cassette,
# horizontally centered (computed directly from both widths -- see
# __init__), overlapping its upper (title/reel) band and poking up above
# the top edge, with the star/label row staying clear below it.
#
# _RECEIPT_TARGET_WIDTH is a fixed pixel value -- what ReceiptDropView
# scales card_asset.png's width to (height follows from the asset's own
# landscape aspect ratio). Kept a little narrower than the cassette's
# own width (640) so it doesn't overhang sideways.
_RECEIPT_TARGET_WIDTH = 560
_RECEIPT_DY_FRACTION = -0.5  # receipt/card's top-left y, relative to the cassette's -- MORE NEGATIVE = higher (pokes up further above the cassette), LESS NEGATIVE = lower (sits closer/lower into the cassette). Nudged from -0.45 for a slight lift, per direct feedback.
_RECEIPT_SLIDE_DURATION_MS = 450

# The shared front card (drive/youtube/logic pro): ONE card, raised in
# FRONT of the cassette (unlike the base card/monthly panel, which stay
# LOWERED behind it), positioned just above the logic pro/drive/youtube
# label row so those three labels stay visible/clickable underneath it
# for quick switching between tabs -- see _show_front_card_tab. Bigger
# than the base card (per design decision) since it holds real tables/
# dropdowns, not just a song list. Real interactive widgets (combos,
# buttons, a table) live inside it -- NOT rotated for the same
# QGraphicsProxyWidget/macOS compositing bug reason the base card's
# straight (non-tilted) layout already works around.
_FRONT_CARD_TARGET_WIDTH = 620  # bigger = wider. Nudged from 600 for a slightly larger card, per direct feedback.
_FRONT_CARD_TARGET_HEIGHT = 500  # bigger = taller.  Nudged from 480, same reasoning.
# Bigger gap = the card's bottom edge sits further above the label row,
# which lifts the WHOLE card higher (its height is anchored from this
# bottom edge upward -- see __init__'s front_card_dy calculation).
# Nudged from 10 for a slight lift, per direct feedback.
_FRONT_CARD_BOTTOM_GAP_ABOVE_LABELS = 4

# A press that moves less than this many pixels before release still
# counts as a click (opens the file dialog) rather than a window drag.
_CLICK_DRAG_THRESHOLD_PX = 4

# View/tab identifiers -- _VIEW_MONTHLY is tracked via _current_view (see
# _reveal_receipt_with_ledger/_show_monthly_carousel, mutually exclusive
# with the plain base card); _VIEW_DRIVE/_VIEW_YOUTUBE/_VIEW_LOGIC_PRO are
# tracked separately via _current_front_tab (see _show_front_card_tab) --
# the front card is a raised overlay, not something that replaces the
# base card, so it doesn't participate in _current_view's mutual exclusion.
_VIEW_DRIVE = "drive"
_VIEW_YOUTUBE = "youtube"
_VIEW_LOGIC_PRO = "logic_pro"
_VIEW_MONTHLY = "monthly"


class PlaylistPage(QWidget):
    """
    First page of the app: load a playlist image, then pick a setup path.

    Emits `shape_changed()` whenever the receipt or one of the three
    region panels' visibility/position changes, so MainWindow knows to
    recompute the frameless window's mask. None of the three setup paths
    have a "ready" signal handed off elsewhere -- all of them (drive,
    youtube, logic pro/build) are handled entirely in-place on this
    page; MainWindow only ever shows this one page.
    """

    shape_changed = Signal()

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._settings = settings
        self._current_image_path: str | None = None
        self._current_entries: list[SongEntry] = []
        self._current_setlist_id: int | None = None  # the row _save_entries_to_database just created, if it succeeded
        # Monthly mode's counterpart to _current_entries/_current_setlist_id
        # -- there's no single "current" week the way Weekly mode has a
        # single current screenshot, so entries are cached per setlist_id
        # instead (see _active_entries_and_setlist_id), the same reasoning
        # as _current_entries itself: mutations (a manual match
        # correction) must survive switching tabs/weeks and back without
        # a round-trip to the database until Schedule explicitly persists
        # them. Each week's Drive folder / YouTube playlist pick is NOT
        # cached here -- it goes straight to the setlists table (see
        # _remember_target), since a pick that only lived in memory was
        # lost on restart, and a week whose pick was lost got silently
        # dropped from monthly bulk scheduling.
        self._monthly_entries_cache: dict[int, list[SongEntry]] = {}
        self._current_front_setlist_id: int | None = None  # whichever setlist_id _show_front_card_tab last prepared for
        self._drag_offset: QPoint | None = None
        self._press_global_pos: QPoint | None = None
        self._dragged_since_press = False
        self._current_view: str | None = None  # None = plain receipt/nothing; else _VIEW_MONTHLY
        self._current_front_tab: str | None = None  # None, or one of _VIEW_DRIVE/_VIEW_YOUTUBE/_VIEW_LOGIC_PRO
        self._receipt_slide_anim: QPropertyAnimation | None = None
        self._monthly_panel_slide_anim: QPropertyAnimation | None = None
        self._front_card_slide_anim: QPropertyAnimation | None = None

        native_cassette = load_cassette_pixmap()
        scale = _CASSETTE_TARGET_WIDTH / native_cassette.width()
        cassette_size = QSize(_CASSETTE_TARGET_WIDTH, round(native_cassette.height() * scale))
        self._cassette_pixmap = native_cassette.scaled(
            cassette_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )

        self._image_view = ReceiptDropView(
            target_width=_RECEIPT_TARGET_WIDTH,
            parent=self,
        )

        # Horizontally centered on the cassette (computed directly from
        # both widths, not a hand-picked fraction) -- the card asset is
        # wide enough now that even a small fraction-based offset visibly
        # off-centered it, per direct visual feedback.
        receipt_dx = round((cassette_size.width() - self._image_view.width()) / 2)
        receipt_dy = round(cassette_size.height() * _RECEIPT_DY_FRACTION)
        cassette_rect = QRect(QPoint(0, 0), cassette_size)
        receipt_rect = QRect(QPoint(receipt_dx, receipt_dy), self._image_view.size())

        # The monthly carousel panel occupies this same rect -- see the
        # constants block above.
        self._panel_size = receipt_rect.size()

        # The shared front card (drive/youtube/logic pro): centered
        # horizontally on the cassette (same approach as the base card),
        # with its BOTTOM edge sitting just above the label row's top
        # edge -- computed from the label row's own center-y fraction and
        # an estimate of its text height, both already used elsewhere in
        # this file for the label buttons themselves.
        front_card_size = QSize(_FRONT_CARD_TARGET_WIDTH, _FRONT_CARD_TARGET_HEIGHT)
        label_row_half_height = round(_REGION_FONT_POINT_SIZE * 1.3 / 2)
        label_row_top_y = round(cassette_size.height() * _LABEL_ROW_CENTER_Y_FRACTION) - label_row_half_height
        front_card_bottom_y = label_row_top_y - _FRONT_CARD_BOTTOM_GAP_ABOVE_LABELS
        front_card_dx = round((cassette_size.width() - front_card_size.width()) / 2)
        front_card_dy = front_card_bottom_y - front_card_size.height()
        front_card_rect = QRect(QPoint(front_card_dx, front_card_dy), front_card_size)

        union_rect = cassette_rect.united(receipt_rect).united(front_card_rect)
        shift = -union_rect.topLeft()

        self._cassette_pos = cassette_rect.topLeft() + shift
        # resize(), not setFixedSize(): setFixedSize() would pin this
        # page's minimumSize to its (large, receipt-overhang-inclusive)
        # size -- and QStackedWidget's own minimumSizeHint is the max
        # across ALL of its pages, so that floor would leak into
        # MainWindow and block the geometry animation from ever
        # shrinking the window for the OTHER (smaller) rectangular
        # pages. Explicitly zeroing minimumSize keeps this page's own
        # fixed-looking size (nothing here uses a layout that would
        # otherwise stretch it) without imposing that floor on siblings.
        self.resize(union_rect.size())
        self.setMinimumSize(0, 0)

        self._receipt_target_pos = receipt_rect.topLeft() + shift
        self._front_card_target_pos = front_card_rect.topLeft() + shift
        self._front_card_size = front_card_size
        self._image_view.move(self._receipt_target_pos)
        # Visible from launch, sitting behind the cassette -- unlike the
        # old tall receipt (which stayed hidden until the first OCR
        # result), the card is what the Weekly/Monthly picker sits on
        # top of, so it needs to already be there before anything's
        # imported. show_ledger_preview() later just updates its content
        # in place (a crossfade -- see _reveal_receipt_with_ledger), no
        # slide-up needed since it was never hidden to begin with.
        self._image_view.show()
        self._image_view.lower()

        # The cassette artwork as a REAL child widget, not drawn directly
        # in this page's own paintEvent -- Qt always renders a parent's
        # paintEvent behind all of its children, so as long as the
        # cassette was only ever the parent's background, no stacking
        # call on the receipt (a child) could ever put the cassette in
        # front of it. Mouse-transparent so cassette-body clicks/drags
        # still reach THIS widget's own mousePress/Move/ReleaseEvent
        # below, unchanged.
        self._cassette_view = QLabel(parent=self)
        self._cassette_view.setPixmap(self._cassette_pixmap)
        self._cassette_view.setFixedSize(self._cassette_pixmap.size())
        self._cassette_view.move(self._cassette_pos)
        self._cassette_view.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        def _make_flock_button(pixmap, width_fraction: float, tooltip: str) -> SilhouetteButton:
            """A shepherd/sheep button, sized from the cassette's own width and the pixmap's native aspect ratio."""
            width = round(cassette_size.width() * width_fraction)
            height = round(width * pixmap.height() / pixmap.width())
            return SilhouetteButton(pixmap, QSize(width, height), parent=self, tooltip=tooltip)

        # The shepherd -- the Task Manager button (was a separate bell
        # icon on the label row; now leads the flock, per direct design
        # decision). Replaces what used to be three separate "approval
        # queue" bells on the Drive/YouTube/Logic Pro tabs individually.
        self._task_manager_button = _make_flock_button(
            load_task_manager_pixmap(),
            _SHEPHERD_WIDTH_FRACTION,
            "View and manage scheduled tasks (Drive, YouTube, Logic Pro).",
        )
        self._task_manager_button.clicked.connect(self._on_task_manager_clicked)
        self._bottom_align_on_cassette(self._task_manager_button, _SHEPHERD_CENTER_X_FRACTION, _FLOCK_BASELINE_Y_FRACTION)

        self._close_sheep = _make_flock_button(load_sheep_pixmap("close.png"), _SHEEP_WIDTH_FRACTION, "Close")
        self._close_sheep.clicked.connect(lambda: self.window().close())
        self._bottom_align_on_cassette(self._close_sheep, _CLOSE_SHEEP_CENTER_X_FRACTION, _FLOCK_BASELINE_Y_FRACTION)

        self._minimize_sheep = _make_flock_button(load_sheep_pixmap("minimize.png"), _SHEEP_WIDTH_FRACTION, "Minimize")
        self._minimize_sheep.clicked.connect(lambda: self.window().showMinimized())
        self._bottom_align_on_cassette(self._minimize_sheep, _MINIMIZE_SHEEP_CENTER_X_FRACTION, _FLOCK_BASELINE_Y_FRACTION)

        self._settings_sheep = _make_flock_button(load_sheep_pixmap("settings.png"), _SHEEP_WIDTH_FRACTION, "Settings…")
        self._settings_sheep.clicked.connect(self._on_configure_clicked)
        self._bottom_align_on_cassette(self._settings_sheep, _SETTINGS_SHEEP_CENTER_X_FRACTION, _FLOCK_BASELINE_Y_FRACTION)

        self._setup_logic_button = TextRegionButton("logic pro", _REGION_FONT_POINT_SIZE, parent=self)
        self._setup_logic_button.setEnabled(False)
        self._setup_logic_button.clicked.connect(self._on_setup_logic_clicked)
        self._center_on_cassette(
            self._setup_logic_button, _LOGIC_PRO_CENTER_X_FRACTION, _LABEL_ROW_CENTER_Y_FRACTION
        )

        self._setup_charts_button = TextRegionButton("drive", _REGION_FONT_POINT_SIZE, parent=self)
        self._setup_charts_button.setEnabled(False)
        self._setup_charts_button.clicked.connect(self._on_setup_charts_clicked)
        self._center_on_cassette(
            self._setup_charts_button, _DRIVE_CENTER_X_FRACTION, _LABEL_ROW_CENTER_Y_FRACTION
        )

        self._setup_youtube_button = TextRegionButton("youtube", _REGION_FONT_POINT_SIZE, parent=self)
        self._setup_youtube_button.setEnabled(False)
        self._setup_youtube_button.clicked.connect(self._on_setup_youtube_clicked)
        self._center_on_cassette(
            self._setup_youtube_button, _YOUTUBE_CENTER_X_FRACTION, _LABEL_ROW_CENTER_Y_FRACTION
        )

        # The shared front card: ONE PaperPanel, skinned with the same
        # card_asset.png texture as the base card, holding all three of
        # drive/youtube/logic-pro's real, existing page widgets in a
        # QStackedWidget -- see _show_front_card_tab. Reuses each page's
        # existing internal layout completely as-is (only the outer
        # container/position changed, per design decision). NOT rotated,
        # for the same QGraphicsProxyWidget/macOS compositing bug reason
        # the base card's straight layout already works around.
        # All three pass the SAME explicit values -- rather than relying
        # on each page's own set_compact() defaults happening to agree
        # (they didn't: ReviewPage's defaults were margin=4/spacing=4,
        # and YouTubePage's row_height had a stray "-10" making its rows
        # visibly shorter than the other two) -- so the row height and
        # the gap between the icon row and the table read as one
        # consistent family across Drive/YouTube/Logic Pro. Bumped from
        # 22/6 to 28/14 per direct feedback for more breathing room.
        self._charts_page = ChartsPage(settings, parent=None)
        self._charts_page.set_compact(row_height=28, margin=0, spacing=14)
        self._charts_page.target_changed.connect(
            lambda name: self._remember_target(ActionType.DRIVE, name)
        )

        self._youtube_page = YouTubePage(settings, parent=None)
        self._youtube_page.set_compact(row_height=28, margin=0, spacing=14)
        self._youtube_page.target_changed.connect(
            lambda name: self._remember_target(ActionType.YOUTUBE, name)
        )

        self._review_page = ReviewPage(settings, parent=None)
        self._review_page.set_compact(row_height=28, margin=0, spacing=14)
        self._build_page = BuildPage(settings, parent=None)
        logic_pro_widget, self._logic_pro_stack = self._make_logic_pro_stage_widget(
            self._review_page, self._build_page
        )

        self._front_card, self._front_card_stack, self._close_front_card_button = self._make_front_card(
            drive_widget=self._wrap_in_scroll_area(self._charts_page),
            youtube_widget=self._wrap_in_scroll_area(self._youtube_page),
            logic_pro_widget=logic_pro_widget,
        )
        self._front_card_tab_index = {
            _VIEW_DRIVE: 0,
            _VIEW_YOUTUBE: 1,
            _VIEW_LOGIC_PRO: 2,
        }

        self._monthly_carousel = MonthlyCarouselWidget(target_width=_RECEIPT_TARGET_WIDTH, parent=None)
        self._monthly_carousel.bulk_schedule_requested.connect(self._on_bulk_schedule_clicked)
        self._monthly_panel = self._make_panel(self._monthly_carousel)

        # Only the monthly carousel still participates in _current_view's
        # mutual exclusion with the base card (see _reveal_receipt_with_ledger
        # / _show_monthly_carousel) -- the front card is a separate raised
        # overlay tracked via _current_front_tab instead (see
        # _show_front_card_tab), since it doesn't replace/hide the base card.
        self._panel_for_view = {
            _VIEW_MONTHLY: self._monthly_panel,
        }
        self._slide_anim_attr_for_view = {
            _VIEW_MONTHLY: "_monthly_panel_slide_anim",
        }

        # The "Weekly | Monthly" mode picker: unlike the card/panels
        # below it, this is ALWAYS visible, from the moment the app
        # launches, with nothing imported yet -- it's how you choose
        # which kind of import to run, every time, not just once.
        #
        # Two differently-sized instances of the same pair of buttons
        # (both calling the same click handlers), toggled by
        # _update_mode_picker_visibility depending on whether the card
        # is still blank or already showing content:
        #   - _mode_picker_large: centered in the middle of the blank
        #     card -- the only thing on it before anything's imported.
        #   - _mode_picker_small: a small pair tucked into the card's
        #     top-right corner, shown once a ledger/carousel/panel is
        #     showing, so the rest of the card is free for that content.
        # Plain serif text (TextRegionButton, no button chrome) in a
        # dark color -- the light cream color TextRegionButton defaults
        # to was tuned for the cassette's dark label strip and is
        # unreadable against this light paper card.
        self._has_content = False

        self._weekly_button_large = TextRegionButton(
            "Weekly", 26, parent=self, text_color="#2a2620", hover_color="#000000"
        )
        self._weekly_button_large.clicked.connect(self._on_weekly_mode_clicked)
        self._monthly_button_large = TextRegionButton(
            "Monthly", 26, parent=self, text_color="#2a2620", hover_color="#000000"
        )
        self._monthly_button_large.clicked.connect(self._on_monthly_mode_clicked)
        self._mode_picker_large = QWidget(parent=self)
        # Plain QWidgets pick up the app-wide stylesheet's opaque
        # background rule once WA_StyledBackground is active -- explicit
        # "background: transparent" here is what keeps this container
        # from painting a visible gray box behind the two buttons.
        self._mode_picker_large.setStyleSheet("background: transparent;")
        large_layout = QHBoxLayout(self._mode_picker_large)
        large_layout.setContentsMargins(0, 0, 0, 0)
        large_layout.setSpacing(28)
        large_layout.addWidget(self._weekly_button_large)
        large_layout.addWidget(self._monthly_button_large)

        self._weekly_button_small = TextRegionButton(
            "Weekly", 14, parent=self, text_color="#4a4640", hover_color="#000000"
        )
        self._weekly_button_small.clicked.connect(self._on_weekly_mode_clicked)
        self._monthly_button_small = TextRegionButton(
            "Monthly", 14, parent=self, text_color="#4a4640", hover_color="#000000"
        )
        self._monthly_button_small.clicked.connect(self._on_monthly_mode_clicked)
        self._mode_picker_small = QWidget(parent=self)
        self._mode_picker_small.setStyleSheet("background: transparent;")
        small_layout = QHBoxLayout(self._mode_picker_small)
        small_layout.setContentsMargins(0, 0, 0, 0)
        small_layout.setSpacing(10)
        small_layout.addWidget(self._weekly_button_small)
        small_layout.addWidget(self._monthly_button_small)

        self._position_mode_pickers()
        self._mode_picker_large.show()
        self._mode_picker_small.hide()
        self._mode_picker_large.raise_()
        self._mode_picker_small.raise_()

        self.setContextMenuPolicy(Qt.DefaultContextMenu)

    def _position_mode_pickers(self) -> None:
        """
        Position _mode_picker_large within the card's rect (horizontally
        centered, vertically a bit above dead-center per direct
        feedback), and tuck _mode_picker_small into its top-right corner.
        Only ever needs to run once (at construction) -- the card's rect
        itself never moves or resizes at runtime, only which picker is
        visible changes (see _update_mode_picker_visibility).
        """
        card_rect = QRect(self._receipt_target_pos, self._image_view.size())

        self._mode_picker_large.adjustSize()
        large_size = self._mode_picker_large.size()
        self._mode_picker_large.move(
            card_rect.x() + (card_rect.width() - large_size.width()) // 2,
            card_rect.y() + round(card_rect.height() * 0.36) - large_size.height() // 2,
        )

        self._mode_picker_small.adjustSize()
        small_size = self._mode_picker_small.size()
        small_margin_right = 12
        small_margin_top = 13  # was 12, shared with small_margin_right -- split out and increased per direct feedback ("a tiny bit lowered")
        self._mode_picker_small.move(
            card_rect.x() + card_rect.width() - small_size.width() - small_margin_right,
            card_rect.y() + small_margin_top,
        )

    def _update_mode_picker_visibility(self) -> None:
        """
        Called after anything is imported (weekly OCR or monthly PDF) --
        flips from the large centered picker to the small corner one,
        permanently for the rest of this app session (there's no path
        back to "nothing imported yet" without relaunching).
        """
        if self._has_content:
            return
        self._has_content = True
        self._mode_picker_large.hide()
        self._mode_picker_small.show()
        self._mode_picker_small.raise_()

    def _make_panel(self, content: QWidget) -> PaperPanel:
        """
        A card_asset-textured panel occupying the receipt's exact rect,
        with `content` sized to fill it EXACTLY (no scroll area, no
        margin) -- unlike the front card (which wraps arbitrary tables/
        forms that need their own scrolling and breathing room), this is
        only ever the monthly carousel, which does its own internal
        layout to imitate the base card's exact proportions (see
        MonthlyCarouselWidget/ledger_drawing.py) and needs to occupy this
        rect edge-to-edge for that alignment to hold.
        """
        panel = PaperPanel(parent=self, texture=load_card_pixmap())
        panel.setObjectName("ledgerPage")
        panel.setAttribute(Qt.WA_StyledBackground, True)
        content.setParent(panel)
        content.setGeometry(0, 0, self._panel_size.width(), self._panel_size.height())
        panel.setGeometry(QRect(self._receipt_target_pos, self._panel_size))
        panel.hide()
        return panel

    def _make_logic_pro_stage_widget(
        self, review_page: ReviewPage, build_page: BuildPage
    ) -> tuple[QWidget, QStackedWidget]:
        """
        The "logic pro" tab's content: a QStackedWidget with two stages --
        Review's matching table first, then Build's copy/manifest/
        automation controls once "Continue to Build" is clicked (an
        ordinary in-place stack swap, not a slide -- that motion is
        reserved for the front card's own open/close, not a sub-step
        within one of its tabs).

        Returns the stack ALREADY wrapped for direct use as one page of
        the shared front card's own QStackedWidget (see _make_front_card)
        -- unlike _make_panel's callers, this doesn't build its own
        PaperPanel anymore, since Logic Pro is now just one tab sharing
        the front card with Drive/YouTube rather than having its own card.
        """
        review_stage = QWidget()
        # Plain QWidgets pick up WA_StyledBackground once an app-wide
        # stylesheet is active, so the generic "QWidget { background-
        # color: $app_bg }" rule paints them solid -- unlike ChartsPage/
        # YouTubePage (which sit directly in a transparent QScrollArea
        # with no extra wrapper), this stage needs its own explicit
        # override to let the front card's texture show through underneath.
        review_stage.setStyleSheet("background: transparent;")
        review_stage_layout = QVBoxLayout(review_stage)
        # Qt's default QVBoxLayout margins/spacing (~11px/~6px) were
        # stacking on TOP of review_page's own set_compact() margins
        # below, making Logic Pro visibly more padded than Drive/YouTube
        # (which have no such wrapper layout in between) -- per direct
        # feedback, zeroed out to match exactly.
        review_stage_layout.setContentsMargins(0, 0, 0, 0)
        review_stage_layout.setSpacing(6)
        # Continue to Build now lives INSIDE review_page's own icon-button
        # row (after Match/Add/Remove/Schedule), per direct feedback --
        # ReviewPage owns the button/its placement (it's purely visual),
        # PlaylistPage just wires the click since moving to the Build
        # stage is PlaylistPage's own concern, not ReviewPage's.
        review_page._continue_to_build_button.clicked.connect(self._on_continue_to_build_clicked)
        review_stage_layout.addWidget(review_page)

        build_stage = QWidget()
        build_stage.setStyleSheet("background: transparent;")
        build_stage_layout = QVBoxLayout(build_stage)
        build_stage_layout.setContentsMargins(0, 0, 0, 0)
        build_stage_layout.setSpacing(6)
        back_to_review_button = QPushButton("← Back to Review")
        back_to_review_button.setCursor(Qt.PointingHandCursor)
        back_to_review_button.clicked.connect(lambda: self._logic_pro_stack.setCurrentIndex(0))
        # Wrapped in its own QHBoxLayout, with the SAME EXPLICIT side
        # margins as BuildPage's own top-level layout (9px -- see
        # build_page.py) -- added directly to this zero-margin
        # build_stage_layout instead, it spanned edge to edge, visibly
        # wider than Prepare/Build Logic Pro Project (which sit inset
        # inside BuildPage's own internal layout). This used to rely on
        # QHBoxLayout's platform-style-dependent DEFAULT margin to
        # happen to match BuildPage's own -- which it did under the
        # Fusion style this was tested with, but not under macOS's
        # native Aqua style, where the two styles' default margins
        # differ. Both sides now use an explicit, platform-independent
        # number instead. No addStretch() -- per direct feedback, it
        # should fill the row exactly like those other two, not stay
        # content-sized.
        back_to_review_row = QHBoxLayout()
        back_to_review_row.setContentsMargins(9, 0, 9, 0)
        back_to_review_row.addWidget(back_to_review_button)
        build_stage_layout.addLayout(back_to_review_row)
        build_stage_layout.addWidget(build_page)

        stack = QStackedWidget()
        stack.setStyleSheet("background: transparent;")
        stack.addWidget(self._wrap_in_scroll_area(review_stage))  # index 0
        stack.addWidget(self._wrap_in_scroll_area(build_stage))   # index 1
        return stack, stack

    def _make_front_card(
        self, drive_widget: QWidget, youtube_widget: QWidget, logic_pro_widget: QWidget
    ) -> tuple[PaperPanel, QStackedWidget, QPushButton]:
        """
        The ONE shared card for Drive/YouTube/Logic Pro: a PaperPanel
        skinned with card_asset.png (same texture as the base card),
        holding a QStackedWidget with each tab's existing page (index 0 =
        drive, 1 = youtube, 2 = logic pro -- see _front_card_tab_index),
        plus a close (X) button pinned to the top-right corner so it can
        be dismissed back to whatever's showing underneath (see
        _on_close_front_card_clicked).

        Sized/positioned per _FRONT_CARD_TARGET_WIDTH/HEIGHT and
        self._front_card_target_pos (computed in __init__, above the
        label row) -- bigger than the base card since it holds real
        tables/dropdowns, not just a song list.
        """
        card = PaperPanel(parent=self, texture=load_card_pixmap())
        card.setObjectName("ledgerPage")
        card.setAttribute(Qt.WA_StyledBackground, True)

        stack = QStackedWidget(parent=card)
        stack.setStyleSheet("background: transparent;")
        stack.addWidget(drive_widget)
        stack.addWidget(youtube_widget)
        stack.addWidget(logic_pro_widget)

        close_button = QPushButton("✕", parent=card)
        close_button.setFlat(True)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #4a4640; font-size: 16pt; }"
            "QPushButton:hover { color: #000000; }"
        )
        close_button.clicked.connect(self._on_close_front_card_clicked)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(_PANEL_SIDE_MARGIN, _PANEL_TOP_MARGIN, _PANEL_SIDE_MARGIN, 0)
        layout.addWidget(stack)

        card.setGeometry(QRect(self._front_card_target_pos, self._front_card_size))
        close_button.adjustSize()
        close_button.move(card.width() - close_button.width() - 12, 12)
        card.hide()
        return card, stack, close_button

    @staticmethod
    def _wrap_in_scroll_area(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(content)
        return scroll

    def sizeHint(self) -> QSize:  # noqa: D102 - trivial override
        return self.size()

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _center_on_cassette(self, widget: QWidget, center_x_fraction: float, center_y_fraction: float) -> None:
        widget.adjustSize()
        center = self._cassette_pos + QPoint(
            round(self._cassette_pixmap.width() * center_x_fraction),
            round(self._cassette_pixmap.height() * center_y_fraction),
        )
        widget.move(center.x() - widget.width() // 2, center.y() - widget.height() // 2)

    def _bottom_align_on_cassette(
        self, button: SilhouetteButton, center_x_fraction: float, baseline_y_fraction: float
    ) -> None:
        """
        Like _center_on_cassette, but aligns the button's actual VISIBLE
        artwork (button.idle_size, not the button's own bigger hover-
        scaled bounding box -- see SilhouetteButton) to a shared bottom
        baseline instead of centering it -- otherwise the shepherd
        (taller) and the sheep (shorter) would each be vertically
        centered on their own, leaving the shorter sheep floating above
        the ground the taller shepherd's feet touch.
        """
        baseline_y = self._cassette_pos.y() + round(self._cassette_pixmap.height() * baseline_y_fraction)
        center_x = self._cassette_pos.x() + round(self._cassette_pixmap.width() * center_x_fraction)
        # The idle-size artwork sits centered within the button's own
        # (larger, hover-scaled) box -- so its bottom edge is half the
        # size difference above the box's own bottom edge.
        idle_bottom_offset = (button.height() + button.idle_size.height()) // 2
        button.move(center_x - button.width() // 2, baseline_y - idle_bottom_offset)

    def shape_mask(self) -> QRegion:
        """
        The Playlist page's actual clickable/visible silhouette: the
        cassette body, unioned with whichever of the receipt / region
        panels is currently visible (none of them are visible on the
        home screen, so the mask is just the cassette there). Each stays
        "visible" (and so contributes to the mask) for its whole
        slide-down-and-out or slide-up-and-in animation, only actually
        hiding once a slide-down finishes -- so this naturally tracks
        those transitions frame-by-frame with no special-casing needed.
        Used by MainWindow to mask the frameless top-level window to
        match.
        """
        region = QRegion(self._cassette_pixmap.mask()).translated(
            self._cassette_pos.x(), self._cassette_pos.y()
        )
        # The mode picker (whichever of the two is currently showing) is
        # always visible/clickable, from launch -- unlike the
        # card/panels below it, it doesn't wait for anything to be
        # imported first.
        for picker in (self._mode_picker_large, self._mode_picker_small):
            if picker.isVisible():
                region = region.united(QRegion(picker.geometry()))
        if self._image_view.isVisible():
            receipt_region = self._image_view.mask_region().translated(
                self._image_view.pos().x(), self._image_view.pos().y()
            )
            region = region.united(receipt_region)
        for panel in self._panel_for_view.values():
            if panel.isVisible():
                region = region.united(QRegion(panel.geometry()))
        # The shared front card (drive/youtube/logic pro) isn't in
        # _panel_for_view -- it's a separate raised overlay, not part of
        # _current_view's mutual exclusion (see _show_front_card_tab).
        if self._front_card.isVisible():
            region = region.united(QRegion(self._front_card.geometry()))
        return region

    # ------------------------------------------------------------------
    # Window drag-to-move / cassette-body click (there's no title bar to
    # do either for free)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().pos()
            self._press_global_pos = event.globalPosition().toPoint()
            self._dragged_since_press = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            current = event.globalPosition().toPoint()
            if (current - self._press_global_pos).manhattanLength() > _CLICK_DRAG_THRESHOLD_PX:
                self._dragged_since_press = True
            self.window().move(current - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        # A plain cassette-body click no longer does anything -- importing
        # now always goes through the "Weekly"/"Monthly" mode header
        # buttons instead (see _on_weekly_mode_clicked/_on_monthly_mode_clicked),
        # so the cassette itself is purely decorative/drag-to-move here.
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        quit_action = menu.addAction("Quit Setlist Builder")
        quit_action.triggered.connect(QApplication.quit)
        menu.exec(self.mapToGlobal(event.pos()))

    # ------------------------------------------------------------------
    # Mode header: "Weekly" (single-screenshot) vs "Monthly" (PDF import)
    # ------------------------------------------------------------------

    def _on_weekly_mode_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Playlist Screenshot", _default_import_directory(), "Images (*.png *.jpg *.jpeg)"
        )
        if path:
            self._run_ocr_flow(path)

    def _on_monthly_mode_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Monthly PDF", _default_import_directory(), "PDF Files (*.pdf)"
        )
        if not path:
            return

        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            outcome = import_monthly_pdf(path)
        except PdfSegmentationError as error:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Import Failed", str(error))
            return
        except Exception:
            QApplication.restoreOverrideCursor()
            logger.exception("Failed to import monthly PDF: %s", path)
            QMessageBox.warning(
                self,
                "Import Failed",
                "Something went wrong importing this PDF. Check the log file for details.",
            )
            return
        QApplication.restoreOverrideCursor()

        logger.info("Imported %d week(s) from monthly PDF: %s", len(outcome.setlists), path)
        self._monthly_carousel.set_setlists(outcome.setlists)
        self._show_monthly_carousel()
        self._update_mode_picker_visibility()

        # Same three region buttons Weekly mode enables after its own
        # OCR pass (_run_ocr_flow) -- Monthly's own tabs now work off
        # whichever week the carousel is currently browsing (see
        # _active_entries_and_setlist_id), so there's no reason to leave
        # these disabled just because the import happened to come from a
        # PDF instead of a screenshot.
        self._setup_logic_button.setEnabled(True)
        self._setup_youtube_button.setEnabled(True)
        self._setup_charts_button.setEnabled(True)

        if outcome.warnings:
            QMessageBox.information(
                self,
                "Import Completed With Notes",
                f"Imported {len(outcome.setlists)} week(s), but a few things are worth checking:\n\n"
                + "\n".join(f"• {warning}" for warning in outcome.warnings),
            )

    def _show_monthly_carousel(self) -> None:
        """
        Bring the monthly carousel panel up into the receipt's rect,
        sliding down whatever's currently showing there first (mirrors
        _switch_to_view's approach, but isn't gated on a screenshot
        having been OCR'd -- Monthly mode has its own, separate data).
        """
        if self._current_view == _VIEW_MONTHLY:
            # Already showing -- set_setlists() already refreshed its
            # content in place, no slide needed.
            return

        def _bring_up_monthly() -> None:
            self._current_view = _VIEW_MONTHLY
            self._slide_widget_up(
                self._monthly_panel,
                self._receipt_target_pos,
                "_monthly_panel_slide_anim",
                after_show=self._monthly_panel.lower,
            )
            self._raise_mode_pickers()

        if self._current_view is not None:
            active_panel = self._panel_for_view[self._current_view]
            active_anim_attr = self._slide_anim_attr_for_view[self._current_view]
            self._slide_widget_down(active_panel, active_anim_attr, on_finished=_bring_up_monthly)
        else:
            self._slide_widget_down(self._image_view, "_receipt_slide_anim", on_finished=_bring_up_monthly)

    def _run_ocr_flow(self, path: str) -> None:
        self._current_image_path = path
        self._setup_logic_button.setEnabled(True)
        self._setup_youtube_button.setEnabled(True)
        self._setup_charts_button.setEnabled(True)
        logger.info("Playlist image selected: %s", path)

        titles = self._run_ocr()
        if not titles:
            return

        entries = [SongEntry(order=order, ocr_title=title) for order, title in enumerate(titles, start=1)]
        self._current_entries = entries
        logger.info("Built %d SongEntry object(s) from the initial OCR pass.", len(entries))
        self._save_entries_to_database(entries)
        self._reveal_receipt_with_ledger(entries)
        self._update_mode_picker_visibility()

    def _save_entries_to_database(self, entries: list[SongEntry]) -> None:
        """
        Record this screenshot import in the database for history/lookup
        purposes -- this does NOT change the existing behavior at all;
        the rest of the app keeps working from `self._current_entries`
        in memory exactly as before. If saving fails for any reason, the
        import still proceeds normally.

        No longer prompts for a service date (it used to) -- per direct
        feedback, that's redundant once per-list Schedule buttons exist
        (a later pass), since scheduling is what will actually assign a
        real date/service to a list. This just stamps today's date as a
        row-creation timestamp, not a claim about which service it's for.
        """
        try:
            setlist_id = create_setlist(source_type="single_screenshot", service_date=datetime.date.today())
            add_song_entries(setlist_id, entries)
            self._current_setlist_id = setlist_id
            logger.info("Saved %d song(s) to database as setlist %d.", len(entries), setlist_id)
        except Exception:
            self._current_setlist_id = None
            logger.exception("Failed to save this import to the database.")

    def _run_ocr(self) -> list[str]:
        """
        Shared OCR step. Returns the extracted titles, or an empty list
        (with a warning dialog already shown) if nothing was found.
        """
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            titles = extract_song_titles(self._current_image_path)
        finally:
            QApplication.restoreOverrideCursor()

        if not titles:
            QMessageBox.warning(
                self,
                "No Songs Found",
                "No song titles were found in this image. Check the log file for details.",
            )
        return titles

    # ------------------------------------------------------------------
    # Receipt reveal / slide-up + the in-place Charts (drive) panel
    #
    # Both the receipt and the Charts card share one transition style:
    # sliding down out of view (toward the window's bottom edge) to
    # leave, and sliding up from below into place to arrive -- the same
    # motion the receipt already uses for its very first appearance,
    # just reused as the SWAP mechanism between the two of them too, in
    # both directions. Simpler and more reliable than the rotation-based
    # "un-tilt" this used to do, which needed a whole stand-in widget to
    # avoid rotating the Charts card's real interactive widgets.
    # ------------------------------------------------------------------

    def _slide_widget_up(
        self, widget: QWidget, target_pos: QPoint, anim_attr: str, after_show=None, on_finished=None
    ) -> None:
        """
        Slide `widget` up from below the window's bottom edge into
        `target_pos`. `after_show` runs right after `.show()`, before the
        animation starts -- for z-order (raise_()/lower()) calls, which
        need to apply for the WHOLE slide, not just once it lands.
        `on_finished` runs once the slide completes.
        """
        start_pos = QPoint(target_pos.x(), self.window().height() + widget.height())

        existing_anim = getattr(self, anim_attr)
        if existing_anim is not None:
            existing_anim.stop()

        widget.move(start_pos)
        widget.show()
        if after_show is not None:
            after_show()

        anim = QPropertyAnimation(widget, b"pos", self)
        anim.setDuration(_RECEIPT_SLIDE_DURATION_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(start_pos)
        anim.setEndValue(target_pos)
        anim.valueChanged.connect(lambda _value: self.shape_changed.emit())

        def _on_up() -> None:
            self.shape_changed.emit()
            if on_finished is not None:
                on_finished()

        anim.finished.connect(_on_up)
        setattr(self, anim_attr, anim)
        anim.start()
        self.shape_changed.emit()
        # Every up-slide brings SOME content into the receipt's rect --
        # whichever mode picker is showing must stay in front of all of
        # it, for its whole lifetime, not just its own initial construction.
        self._raise_mode_pickers()

    def _raise_mode_pickers(self) -> None:
        self._mode_picker_large.raise_()
        self._mode_picker_small.raise_()

    def _slide_widget_down(self, widget: QWidget, anim_attr: str, on_finished=None) -> None:
        """Slide `widget` down off the window's bottom edge, then hide it."""
        if not widget.isVisible():
            if on_finished is not None:
                on_finished()
            return

        start_pos = widget.pos()
        end_pos = QPoint(start_pos.x(), self.window().height() + widget.height())

        existing_anim = getattr(self, anim_attr)
        if existing_anim is not None:
            existing_anim.stop()

        anim = QPropertyAnimation(widget, b"pos", self)
        anim.setDuration(_RECEIPT_SLIDE_DURATION_MS)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.setStartValue(start_pos)
        anim.setEndValue(end_pos)
        anim.valueChanged.connect(lambda _value: self.shape_changed.emit())

        def _on_down() -> None:
            widget.hide()
            self.shape_changed.emit()
            if on_finished is not None:
                on_finished()

        anim.finished.connect(_on_down)
        setattr(self, anim_attr, anim)
        anim.start()

    def _reveal_receipt_with_ledger(self, entries: list[SongEntry]) -> None:
        """
        Show the receipt's simple ledger view with `entries`. If a
        region panel is currently showing instead, it slides down and
        out first; the (fresh) receipt then slides up into view once
        it's gone. If the receipt itself is already up, its content just
        refreshes in place with no slide, same as before.
        """
        def _show_fresh_ledger() -> None:
            self._current_view = None
            filename = os.path.basename(self._current_image_path) if self._current_image_path else ""
            was_visible = self._image_view.isVisible()
            self._image_view.show_ledger_preview(filename, entries)
            if not was_visible:
                self._slide_receipt_into_view()
            else:
                # Defensive: re-assert the receipt-behind-cassette
                # stacking even when just refreshing content in place
                # (no slide), in case anything else touched z-order.
                self._image_view.lower()

        if self._current_view is not None:
            active_panel = self._panel_for_view[self._current_view]
            active_anim_attr = self._slide_anim_attr_for_view[self._current_view]
            self._slide_widget_down(active_panel, active_anim_attr, on_finished=_show_fresh_ledger)
        else:
            _show_fresh_ledger()

    def _slide_receipt_into_view(self) -> None:
        # lower(), NOT raise_(): the receipt must stay BEHIND the cassette
        # (self._cassette_view) for its WHOLE slide, so the cassette's own
        # body visually covers the receipt's bottom portion throughout,
        # per the reference mockup -- only the sliver poking up past the
        # cassette's top edge should ever read as fully visible.
        self._slide_widget_up(
            self._image_view, self._receipt_target_pos, "_receipt_slide_anim",
            after_show=self._image_view.lower,
        )

    def _remember_target(self, action_type: ActionType, target_name: str) -> None:
        """
        Persist the Drive folder / YouTube playlist you just picked
        against whichever week is currently open, the moment you pick it
        (see ChartsPage/YouTubePage.target_changed).

        Writes straight through to the setlists table rather than an
        in-memory dict, per direct feedback: each week needs to keep its
        OWN target ("one playlist is for Sunday, the other for
        Wednesday"), and the old in-memory version both lost every pick
        on restart and only recorded it if you happened to close the
        card with the X.
        """
        setlist_id = self._current_front_setlist_id
        if setlist_id is None:
            return
        set_setlist_target(setlist_id, action_type, target_name)
        logger.info("Remembered %s target %r for setlist %d.", action_type.value, target_name, setlist_id)

    def _target_for(self, setlist_id: int | None, action_type: ActionType) -> str:
        """
        This week's Drive folder / YouTube playlist: whatever you picked
        for it, or -- if you never picked one -- whichever configured
        target matches the week's own day name ("Domingo" -> your
        "domingo" playlist), matched case/accent-insensitively (see
        Settings.playlist_name_for_day).

        That day-name default is what lets monthly bulk scheduling do
        the right thing for a week you never explicitly set a target on,
        instead of silently dropping it from the plan.
        """
        if setlist_id is None:
            return ""

        setlist = get_setlist(setlist_id)
        if setlist is None:
            return ""

        if action_type is ActionType.DRIVE:
            return setlist.drive_target_name or self._settings.chart_folder_name_for_day(setlist.day_name) or ""
        if action_type is ActionType.YOUTUBE:
            return setlist.youtube_target_name or self._settings.playlist_name_for_day(setlist.day_name) or ""
        return ""

    def _active_entries_and_setlist_id(self) -> tuple[list[SongEntry], int | None]:
        """
        Whichever entries/setlist_id the Logic Pro/Drive/YouTube tabs
        should currently act on -- Weekly mode's single current
        screenshot (_current_entries/_current_setlist_id) if that's
        what's showing, otherwise whichever week the Monthly carousel is
        currently browsing (see MonthlyCarouselWidget.current_setlist).

        Monthly entries are cached in _monthly_entries_cache by
        setlist_id (fetched from the database once per week, on first
        visit) so a match made while browsing Week 2, then Week 3, then
        back to Week 2, is still there -- exactly like Weekly mode's
        single in-memory _current_entries list already behaves, and for
        the same reason (nothing here writes back to the database until
        Schedule explicitly calls replace_song_entries).
        """
        if self._current_view != _VIEW_MONTHLY:
            return self._current_entries, self._current_setlist_id

        setlist = self._monthly_carousel.current_setlist()
        if setlist is None:
            return [], None
        if setlist.id not in self._monthly_entries_cache:
            fetched = get_song_entries(setlist.id)
            self._monthly_entries_cache[setlist.id] = fetched
        return self._monthly_entries_cache[setlist.id], setlist.id

    @staticmethod
    def _describe_setlist_short(setlist) -> str:
        day_part = f"{setlist.day_name} " if setlist.day_name else ""
        return f"{day_part}{setlist.service_date.day}"

    def _on_bulk_schedule_clicked(self) -> None:
        """
        Schedules Drive, YouTube, and Logic Pro (Prepare + Build) for
        every week in the current monthly import that's ALREADY been
        reviewed this session (i.e. you've opened at least one of its
        tabs, so _monthly_entries_cache has its entries) -- deliberately
        does NOT auto-match or review anything on your behalf. A week
        you never visited is skipped and named in the summary, same for
        any action within a visited week that isn't actually ready
        (missing a match, or -- for Drive/YouTube -- no target folder/
        playlist picked yet).

        Each week's scheduled_for is computed automatically (see
        scheduling_service.compute_default_schedule_datetime: the Monday
        that starts that service week) rather than asked for one at a
        time -- the whole point of doing this in bulk.
        """
        setlists = self._monthly_carousel.all_setlists()
        if not setlists:
            return

        plan: list[tuple[object, datetime.datetime, list[SongEntry], dict[ActionType, str | None]]] = []
        unreviewed: list[object] = []

        for setlist in setlists:
            entries = self._monthly_entries_cache.get(setlist.id)
            if not entries:
                unreviewed.append(setlist)
                continue

            scheduled_for = max(
                compute_default_schedule_datetime(setlist.service_date, self._settings),
                datetime.datetime.now(),
            )

            # Each destination judges only the songs IT actually
            # includes (see SongEntry.excluded_from_*) -- a song you
            # removed from Drive shouldn't be able to block Drive from
            # being scheduled just because it has no chart match.
            drive_entries = [entry for entry in entries if not entry.excluded_from_drive]
            youtube_entries = [entry for entry in entries if not entry.excluded_from_youtube]
            logic_entries = [entry for entry in entries if not entry.excluded_from_logic]

            drive_target = self._target_for(setlist.id, ActionType.DRIVE)
            youtube_target = self._target_for(setlist.id, ActionType.YOUTUBE)

            ready: dict[ActionType, str | None] = {}
            if drive_entries and all(entry.chart_match is not None for entry in drive_entries) and drive_target:
                ready[ActionType.DRIVE] = drive_target
            if youtube_entries and all(entry.youtube_match is not None for entry in youtube_entries) and youtube_target:
                ready[ActionType.YOUTUBE] = youtube_target
            if logic_entries and all(
                entry.audio_match is not None and entry.logic_match is not None for entry in logic_entries
            ):
                ready[ActionType.PREPARE_LOGIC_PROJECT] = None
                ready[ActionType.LOGIC_AUTOMATION] = None

            plan.append((setlist, scheduled_for, entries, ready))

        if not plan:
            QMessageBox.information(
                self,
                "Bulk Schedule",
                "None of this month's weeks have been reviewed yet -- open each week's "
                "Drive/YouTube/Logic Pro tab at least once first.",
            )
            return

        summary_lines = []
        for setlist, scheduled_for, _entries, ready in plan:
            when = scheduled_for.strftime("%b %d, %Y %I:%M %p")
            actions = ", ".join(action.value.replace("_", " ").title() for action in ready) or "nothing ready yet"
            summary_lines.append(f"• {self._describe_setlist_short(setlist)} → {when}: {actions}")
        if unreviewed:
            summary_lines.append("")
            summary_lines.append("Not included (never opened this session):")
            summary_lines.extend(f"• {self._describe_setlist_short(setlist)}" for setlist in unreviewed)

        confirmed = QMessageBox.question(
            self,
            "Bulk Schedule This Month?",
            "This will schedule the following:\n\n" + "\n".join(summary_lines) + "\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return

        scheduled_count = 0
        for setlist, scheduled_for, entries, ready in plan:
            if not ready:
                continue
            replace_song_entries(setlist.id, entries)
            for action_type, target_name in ready.items():
                schedule_task(setlist.id, action_type, scheduled_for, target_name=target_name)
                scheduled_count += 1

        QMessageBox.information(
            self, "Bulk Schedule", f"Scheduled {scheduled_count} task(s) across {len(plan)} reviewed week(s)."
        )

    def _show_front_card_tab(self, view_name: str, prepare) -> None:
        """
        Show `view_name`'s (drive/youtube/logic-pro) content in the
        shared front card. `prepare` populates that tab's page with the
        current entries first.

        Unlike the base card/monthly carousel's mutual exclusion (see
        _reveal_receipt_with_ledger), the front card is a separate
        RAISED overlay above the label row -- it doesn't hide or replace
        whatever's showing in the receipt's own rect underneath, so
        nothing needs to slide away first. If the front card is already
        open (on any tab), switching to a different tab is instant --
        just a setCurrentIndex, no slide -- so bouncing between
        logic pro/drive/youtube is quick, per the design intent. The
        slide-up only happens the first time it opens; sliding back down
        only happens via its X button (_on_close_front_card_clicked).

        Requires either a Weekly screenshot already OCR'd, or a Monthly
        week actually loaded -- the region buttons are disabled until
        one of those is true anyway.
        """
        entries, setlist_id = self._active_entries_and_setlist_id()
        if not entries:
            return

        self._current_front_setlist_id = setlist_id
        prepare()
        self._front_card_stack.setCurrentIndex(self._front_card_tab_index[view_name])
        self._current_front_tab = view_name

        # The Weekly/Monthly mode picker belongs to the home card only --
        # hidden (not just lowered) while any front-card tab is open, per
        # direct feedback that it was showing up on top of drive/youtube/
        # logic pro too. Restored in _on_close_front_card_clicked.
        self._mode_picker_large.hide()
        self._mode_picker_small.hide()

        if self._front_card.isVisible():
            return

        self._slide_widget_up(
            self._front_card, self._front_card_target_pos, "_front_card_slide_anim",
            after_show=self._front_card.raise_,
        )

    def _on_close_front_card_clicked(self) -> None:
        # Each pick is already saved the moment it's made (see
        # _remember_target) -- this is just a belt-and-braces catch for
        # anything that changed the combo without going through
        # `activated`, before we lose track of which setlist was active.
        if self._current_front_setlist_id is not None:
            self._remember_target(ActionType.DRIVE, self._charts_page.current_folder_name())
            self._remember_target(ActionType.YOUTUBE, self._youtube_page.current_playlist_name())

        self._current_front_tab = None
        self._current_front_setlist_id = None
        self._slide_widget_down(self._front_card, "_front_card_slide_anim")
        # Restore the mode picker -- the front card can only ever be
        # opened once weekly entries exist, so _has_content is always
        # True here, meaning the small (not large) picker is always the
        # correct one to bring back.
        self._mode_picker_small.show()
        self._mode_picker_small.raise_()

    def _on_task_manager_clicked(self) -> None:
        show_task_manager_dialog(self._settings, parent=self)

    # ------------------------------------------------------------------
    # Setup handlers
    # ------------------------------------------------------------------

    def _on_configure_clicked(self) -> None:
        dialog = SettingsDialog(self._settings, parent=self)
        dialog.exec()

    def _on_setup_logic_clicked(self) -> None:
        if not self._settings.is_configured():
            QMessageBox.warning(
                self,
                "Libraries Not Configured",
                "Please configure your Audio and Logic library paths first "
                "(the leftmost star button opens Settings).",
            )
            return

        def _prepare() -> None:
            entries, setlist_id = self._active_entries_and_setlist_id()
            audio_library = scan_library(self._settings.audio_library_path, expect_bpm_suffix=True)
            logic_library = scan_library(self._settings.logic_library_path, expect_bpm_suffix=False)
            self._review_page.load_entries(entries, audio_library, logic_library, setlist_id)
            # Only computes a fresh match for entries that don't already
            # have one -- a manual correction (or a match from a
            # previous visit) survives switching away and back.
            self._review_page.match_entries()
            self._logic_pro_stack.setCurrentIndex(0)  # always re-enter on Review, not mid-Build
            logger.info("Showing in-place Logic Pro Review for %d song(s).", len(entries))

        self._show_front_card_tab(_VIEW_LOGIC_PRO, _prepare)

    def _on_continue_to_build_clicked(self) -> None:
        entries = self._review_page.get_entries()
        logger.info("Moving %d entries from Review to Build.", len(entries))
        self._build_page.set_entries(entries)
        self._logic_pro_stack.setCurrentIndex(1)

    def _on_setup_youtube_clicked(self) -> None:
        def _prepare() -> None:
            entries, setlist_id = self._active_entries_and_setlist_id()
            self._youtube_page.set_entries(entries, setlist_id)
            self._youtube_page.match_songs()
            remembered = self._target_for(setlist_id, ActionType.YOUTUBE)
            if remembered:
                self._youtube_page.select_playlist(remembered)
            logger.info("Showing in-place YouTube setup for %d song(s).", len(entries))

        self._show_front_card_tab(_VIEW_YOUTUBE, _prepare)

    def _on_setup_charts_clicked(self) -> None:
        """
        Show the full Charts editor in place, over the cassette -- reuses
        the titles already extracted by the initial cassette-body click
        (no second OCR pass), and reuses ChartsPage as-is (its matching,
        downloading, and merging logic is untouched).
        """
        def _prepare() -> None:
            entries, setlist_id = self._active_entries_and_setlist_id()
            self._charts_page.set_entries(entries, setlist_id)
            self._charts_page.match_charts()
            remembered = self._target_for(setlist_id, ActionType.DRIVE)
            if remembered:
                self._charts_page.select_folder(remembered)
            logger.info("Showing in-place Charts editor for %d song(s).", len(entries))

        self._show_front_card_tab(_VIEW_DRIVE, _prepare)
