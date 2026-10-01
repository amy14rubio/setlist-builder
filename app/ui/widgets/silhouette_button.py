"""
Single-image silhouette button for Setlist Builder's cassette Playlist
page -- the shepherd (Task Manager) and three sheep (Close/Minimize/
Settings) in the cassette's bottom-left corner.

Unlike StarButton (three separate idle/hover/pressed art files), each of
these is only ONE image -- so hover/press feedback is done by scaling
that same image up/down instead of swapping it out, per direct
preference over the dimming/opacity approach used elsewhere.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QPushButton, QWidget

# How far the image grows on hover / shrinks when pressed, relative to
# its own natural (idle) size -- kept subtle (barely noticeable) per
# direct feedback that the original jump was too large.
_HOVER_SCALE = 1.1
_PRESSED_SCALE = 0.95


class SilhouetteButton(QPushButton):
    def __init__(
        self, pixmap: QPixmap, idle_size: QSize, parent: QWidget | None = None, tooltip: str = ""
    ) -> None:
        super().__init__(parent)
        self._pixmap = pixmap
        self._idle_size = idle_size

        # Fixed at the HOVER size (the biggest the image ever gets) --
        # idle/pressed just draw smaller within that same box, so growing
        # on hover never gets clipped by the widget's own bounds.
        self.setFixedSize(
            round(idle_size.width() * _HOVER_SCALE), round(idle_size.height() * _HOVER_SCALE)
        )
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")
        if tooltip:
            self.setToolTip(tooltip)
            self.setAccessibleName(tooltip)

        self.pressed.connect(self.update)
        self.released.connect(self.update)

    @property
    def idle_size(self) -> QSize:
        """
        The size the artwork is actually drawn at when idle -- smaller
        than this widget's own (hover-scaled) fixed size, so callers that
        need to align the VISIBLE image to something (e.g. a shared
        ground line the shepherd/sheep should all stand on) can't just
        use this widget's own geometry.
        """
        return self._idle_size

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event) -> None:
        if self.isDown():
            scale = _PRESSED_SCALE
        elif self.underMouse():
            scale = _HOVER_SCALE
        else:
            scale = 1.0

        width = round(self._idle_size.width() * scale)
        height = round(self._idle_size.height() * scale)
        x = (self.width() - width) // 2
        y = (self.height() - height) // 2

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(x, y, width, height, self._pixmap)
