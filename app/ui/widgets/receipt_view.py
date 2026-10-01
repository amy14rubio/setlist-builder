"""
Card/ledger widget for Setlist Builder's cassette Playlist page.

This is the "base" view sitting behind the cassette: the Weekly/Monthly
mode picker before anything's imported, and a SETLIST header + two-column
numbered song list once something has been (see show_ledger_preview).
Drawn on card_asset.png, a wide landscape index-card texture -- a
deliberate redesign away from the original tall receipt-roll look,
which read as harder to scan at a glance.

Purely a display widget -- PlaylistPage owns the actual OCR run (kicked
off via the Weekly mode picker button, not by clicking this card) and
calls show_ledger_preview() with the results; this widget only ever
renders whatever it's told to.

Two visual states, both drawn onto the SAME base card texture (plain
card art, no baked-in text) so they never need to resize the widget:
  - idle: the blank card texture, as-is (shown only for the brief moment
    between "widget exists" and "first OCR result" while it's already
    visible, e.g. mid-transition)
  - ledger preview: that same blank texture, with the SETLIST/filename/
    two-column title list drawn on top via QPainter (this is dynamic,
    per-run data -- it can't be pre-baked artwork)
Both are scaled once (baked into the pixmap, not a live widget transform)
so the widget itself is a plain axis-aligned QWidget; a click in the
transparent corner of that bounding box still counts as "clicked the
card" -- an accepted simplification rather than pixel-precise
hit-testing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QVariantAnimation
from PySide6.QtGui import QGuiApplication, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QWidget

from app.models.song_entry import SongEntry
from app.ui.assets import load_card_pixmap
from app.ui.widgets.ledger_drawing import draw_ledger_content


class ReceiptDropView(QWidget):
    """
    NOTE ON THE NAME: still called ReceiptDropView (matching its module,
    receipt_view.py) even though it now renders the landscape card
    texture rather than a receipt -- renaming the class/file is a purely
    cosmetic follow-up, not done here to keep this change's diff focused
    on the actual visual redesign.

    No longer interactive on its own (no click-to-browse, no drag-and-
    drop) -- importing goes exclusively through PlaylistPage's Weekly/
    Monthly mode picker now. This widget only ever renders whatever
    show_ledger_preview() tells it to.
    """

    def __init__(
        self,
        target_width: int = 550,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._target_width = target_width

        self._idle_texture = load_card_pixmap()
        self._blank_texture = load_card_pixmap()

        self._idle_pixmap = self._render(self._idle_texture)
        # .size() on a pixmap with a devicePixelRatio > 1 (see _render)
        # returns its PHYSICAL pixel count, not the logical size we want
        # the widget itself to occupy -- deviceIndependentSize() is the
        # one that matches what every other geometry calculation in
        # PlaylistPage expects (target_width and friends).
        self.setFixedSize(self._idle_pixmap.deviceIndependentSize().toSize())
        # A plain 1x-scale rendering, used ONLY for mask_region()'s hit-
        # testing shape below -- QPixmap.mask() returns a bitmap sized in
        # PHYSICAL pixels, and a QRegion built from it has no idea about
        # devicePixelRatio, so building it from the (now higher-
        # resolution) display pixmap would make the window's mask/click
        # region come out too large on a Retina display.
        self._mask_source_pixmap = self._render_at_1x(self._idle_texture)

        self._current_pixmap = self._idle_pixmap
        self._next_pixmap: QPixmap | None = None
        self._transition_progress = 0.0
        self._transition_anim: QVariantAnimation | None = None

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _render(self, content_texture: QPixmap) -> QPixmap:
        """
        Scale a native-resolution content pixmap down to our target
        WIDTH (not height, unlike the old tall receipt) -- card_asset.png
        is landscape, so pinning width is what keeps it a sensible size
        relative to the cassette (roughly as wide, noticeably shorter)
        rather than either overflowing sideways or rendering tiny.

        Rendered at the SCREEN's actual device pixel ratio (2x/3x on a
        Retina display), not a flat 1x -- confirmed by a direct
        screenshot that rendering the ledger text at 1x and letting Qt
        upscale the result for display was producing visibly fuzzy text,
        since Qt has no extra pixel detail to draw on a HiDPI screen
        otherwise. setDevicePixelRatio() tells Qt this pixmap represents
        `self._target_width` LOGICAL points despite having more actual
        pixel data, so it displays crisp instead of stretched.
        """
        ratio = self._device_pixel_ratio()
        physical_width = round(self._target_width * ratio)
        scale = physical_width / content_texture.width()
        scaled = content_texture.scaled(
            physical_width,
            round(content_texture.height() * scale),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        return scaled

    def _render_at_1x(self, content_texture: QPixmap) -> QPixmap:
        """Same scaling as _render, but always at a flat 1x -- see mask_region()."""
        scale = self._target_width / content_texture.width()
        return content_texture.scaled(
            self._target_width,
            round(content_texture.height() * scale),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

    @staticmethod
    def _device_pixel_ratio() -> float:
        screen = QGuiApplication.primaryScreen()
        return screen.devicePixelRatio() if screen else 1.0

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        if self._next_pixmap is not None:
            painter.setOpacity(1.0 - self._transition_progress)
            painter.drawPixmap(0, 0, self._current_pixmap)
            painter.setOpacity(self._transition_progress)
            painter.drawPixmap(0, 0, self._next_pixmap)
        else:
            painter.setOpacity(1.0)
            painter.drawPixmap(0, 0, self._current_pixmap)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_ledger_preview(self, filename: str, entries: list[SongEntry]) -> None:
        """Crossfade from the current look into the populated ledger."""
        ledger_texture = self._draw_ledger(filename, entries)
        ledger_pixmap = self._render(ledger_texture)
        self._set_content(ledger_pixmap, animate=True)

    def mask_region(self) -> QRegion:
        """
        This widget's silhouette, in its own local (logical) coordinates
        -- the card's shape, not its (larger, axis-aligned) bounding
        rect. Built from _mask_source_pixmap (always a flat 1x
        rendering, see __init__), NOT the higher-resolution
        _idle_pixmap used for display -- a mask built from a
        devicePixelRatio > 1 pixmap would come out too large, since
        QRegion has no concept of device pixel ratio. Used by
        PlaylistPage to build the frameless window's mask.
        """
        return QRegion(self._mask_source_pixmap.mask())

    # ------------------------------------------------------------------
    # Ledger drawing
    # ------------------------------------------------------------------

    def _draw_ledger(self, filename: str, entries: list[SongEntry]) -> QPixmap:
        """
        Title (the source image/PDF's filename) + a two-column numbered
        song list, via the shared draw_ledger_content -- see
        app/ui/widgets/ledger_drawing.py, also used by the monthly
        carousel so both views stay pixel-identical in layout.
        """
        canvas = self._blank_texture.copy()
        painter = QPainter(canvas)
        draw_ledger_content(painter, canvas.width(), canvas.height(), filename, entries, self._target_width)
        painter.end()
        return canvas

    # ------------------------------------------------------------------
    # Transition animation
    # ------------------------------------------------------------------

    def _set_content(self, pixmap: QPixmap, animate: bool) -> None:
        if self._transition_anim is not None:
            self._transition_anim.stop()
            self._transition_anim = None

        if not animate:
            self._current_pixmap = pixmap
            self._next_pixmap = None
            self._transition_progress = 0.0
            self.update()
            return

        self._next_pixmap = pixmap
        self._transition_progress = 0.0

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(400)
        anim.valueChanged.connect(self._on_transition_value_changed)
        anim.finished.connect(self._on_transition_finished)
        self._transition_anim = anim
        anim.start()

    def _on_transition_value_changed(self, value: float) -> None:
        self._transition_progress = value
        self.update()

    def _on_transition_finished(self) -> None:
        if self._next_pixmap is not None:
            self._current_pixmap = self._next_pixmap
        self._next_pixmap = None
        self._transition_progress = 0.0
        self._transition_anim = None
        self.update()

