"""
Shared "frameless paper card" window chrome for popup dialogs styled like
playlist_page.py's shared front card: PaperPanel + card_asset.png +
ledgerPage theming + a top-right X close button.

Used by SettingsDialog and TaskManagerDialog so both popups read as the
same family of card and don't duplicate (or drift out of sync on) this
setup.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout, QWidget

from app.ui.assets import load_card_pixmap
from app.ui.style import PAPER_BG
from app.ui.widgets.paper_panel import PaperPanel

# Every card popup shares this one width -- height comes from
# card_asset.png's own aspect ratio (see setup_card_window), so it's
# never stretched or letterboxed.
CARD_WIDTH = 760

# Margins inside the card -- the space between the card's own edges and
# whatever content a caller puts in the returned card_layout.
CARD_SIDE_MARGIN = 16
CARD_TOP_MARGIN = 20
CARD_BOTTOM_MARGIN = 16


class DraggableCard(PaperPanel):
    """
    A PaperPanel that lets the user drag the whole (frameless) window by
    pressing anywhere on its background -- necessary because a frameless
    QDialog has no title bar to drag by otherwise.
    """

    def __init__(self, texture, parent: QWidget | None = None) -> None:
        super().__init__(texture, parent)
        self._drag_offset: QPoint | None = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None


def setup_card_window(dialog: QDialog, card_width: int = CARD_WIDTH) -> tuple[DraggableCard, QVBoxLayout, QPushButton]:
    """
    Turns `dialog` into a frameless paper-card popup: card_asset.png
    background (scaled to `card_width`, height from its own aspect
    ratio), draggable by its background, and a top-right X close button
    that rejects the dialog.

    Returns (card, card_layout, close_button) -- the caller adds its own
    content into `card_layout`, then MUST call `close_button.raise_()`
    once that's done. The close button is created (and stacked) before
    that content exists, so anything added afterward -- a scroll area's
    native scrollbar chrome especially -- would otherwise paint over it;
    raising it again after is what keeps it clickable on top.
    """
    dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
    # NOT WA_TranslucentBackground -- the card texture's own corners have
    # a few fully-transparent pixels (from its source crop), and on a
    # translucent top-level window those show up as a solid black wedge
    # instead of the desktop (no real compositing happens for a window
    # this small/short-lived). A plain paper-colored fallback background
    # is barely noticeable there and never looks broken.
    dialog.setStyleSheet(f"QDialog {{ background-color: {PAPER_BG}; }}")

    card_texture = load_card_pixmap()
    card_height = round(card_texture.height() * (card_width / card_texture.width()))
    dialog.setFixedSize(card_width, card_height)

    card = DraggableCard(texture=card_texture, parent=dialog)
    card.setObjectName("ledgerPage")
    card.setAttribute(Qt.WA_StyledBackground, True)
    card.setGeometry(dialog.rect())

    close_button = QPushButton("✕", parent=card)
    close_button.setFlat(True)
    close_button.setCursor(Qt.PointingHandCursor)
    close_button.setStyleSheet(
        "QPushButton { border: none; background: transparent; color: #4a4640; font-size: 16pt; }"
        "QPushButton:hover { color: #000000; }"
    )
    close_button.clicked.connect(dialog.reject)

    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(
        CARD_SIDE_MARGIN, CARD_TOP_MARGIN, CARD_SIDE_MARGIN, CARD_BOTTOM_MARGIN
    )

    # Close button position only depends on the card's (already-fixed)
    # width, so it can be finalized here regardless of what content the
    # caller adds into card_layout afterward.
    close_button.adjustSize()
    close_button.move(card.width() - close_button.width() - 12, 12)

    return card, card_layout, close_button
