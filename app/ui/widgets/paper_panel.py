"""
Paper-textured card for Setlist Builder's in-place "drive" (Charts)
editor -- a real interactive widget (unlike ReceiptDropView, which is a
single rotated, painted pixmap with no children), so real child widgets
(ChartsPage's buttons/table) can sit on top of it via a normal layout.

Deliberately NOT rotated (unlike the receipt): real interactive widgets
(the table, buttons) can't rotate along with a tilted background without
hitting a reproducible Qt/macOS bug (see playlist_page.py's history on
this) -- so the background stays straight, letting the content align
cleanly with it, even though that means it isn't pixel-identical to the
receipt's tilted look.

Otherwise this uses the EXACT same texture and cropping as
ReceiptDropView (see its _cap_aspect) so the paper itself reads as the
same material -- just not tilted, and with no torn edge, since the
receipt's "torn" silhouette was only ever a side effect of rotating a
plain rectangle; a straight rectangle has no natural torn edge to match.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget

# Same cap as ReceiptDropView._cap_aspect -- keeping both in sync is what
# makes this read as the same paper material, just laid out straight.
# A no-op for any texture that's already wider than tall (e.g.
# card_asset.png), so passing a landscape texture in via `texture` below
# is unaffected by this legacy correction.
_MAX_ASPECT_RATIO = 1.3


class PaperPanel(QWidget):
    def __init__(self, texture: QPixmap, parent: Optional[QWidget] = None) -> None:
        """
        `texture` is required -- both real call sites (PlaylistPage's
        monthly panel and shared front card) always pass one explicitly
        (load_card_pixmap()) anyway. This used to default to a
        now-deleted torn-paper asset (paper_asset.png) for a texture no
        caller ever actually used, once the app moved to the card-based
        look everywhere.
        """
        super().__init__(parent)
        self._texture = self._cap_aspect(texture)

    @staticmethod
    def _cap_aspect(texture: QPixmap) -> QPixmap:
        max_height = round(texture.width() * _MAX_ASPECT_RATIO)
        if texture.height() <= max_height:
            return texture
        return texture.copy(0, 0, texture.width(), max_height)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        scaled = self._texture.scaled(self.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        painter.drawPixmap(0, 0, scaled)
