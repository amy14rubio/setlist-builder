"""
Transparent serif-text button for the "logic pro" / "drive" / "youtube"
regions on Setlist Builder's cassette Playlist page.

cassette_asset.png has no baked-in text for these -- they're drawn here
as real, clickable text so they can call straight into the existing
setup handlers, styled to sit naturally on the cassette's label strip.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPushButton, QWidget

_STYLESHEET_TEMPLATE = """
QPushButton {{
    border: none;
    background: transparent;
    color: {text_color};
    font-size: {font_point_size}pt;
}}
QPushButton:hover:!disabled {{
    color: {hover_color};
    text-decoration: underline;
}}
QPushButton:disabled {{
    color: rgba(255, 255, 255, 90);
}}
"""


class TextRegionButton(QPushButton):
    def __init__(
        self,
        text: str,
        font_point_size: int = 16,
        parent: QWidget | None = None,
        text_color: str = "#e8e6e1",
        hover_color: str = "#ffffff",
    ) -> None:
        """
        `text_color`/`hover_color` default to the light cream shade this
        was originally built for -- sitting on the cassette's dark label
        strip (logic pro/drive/youtube). Pass darker colors when placing
        one of these on a light paper/card background instead (see the
        Weekly/Monthly mode picker on PlaylistPage's card), where the
        default light color would be nearly invisible.
        """
        super().__init__(text, parent)

        font = QFont("Garogier", font_point_size)
        font.setStyleHint(QFont.Serif)
        self.setFont(font)

        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        # The QFont set above alone isn't enough: the app-wide stylesheet
        # sets "font-size: 18px" on every QWidget, and a QSS rule always
        # wins over a QFont set in code -- so font_point_size needs to be
        # baked into THIS widget's own stylesheet to actually take effect.
        self.setStyleSheet(
            _STYLESHEET_TEMPLATE.format(font_point_size=font_point_size, text_color=text_color, hover_color=hover_color)
        )
