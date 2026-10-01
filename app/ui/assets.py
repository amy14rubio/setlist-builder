"""
Illustrated-asset loading for Setlist Builder's cassette-themed UI.

Every source PNG in resources/ is a sprite sitting somewhere inside a
1920x1080 canvas (that's just how the artwork was exported) -- the actual
cassette/receipt shape only occupies a sub-rect of it, surrounded by
transparent padding. The crop rects below were measured directly from
each PNG's alpha channel and are fixed properties of these specific,
final asset files, not something worth recomputing at runtime.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtGui import QPixmap

RESOURCES_DIR = Path(__file__).resolve().parent.parent.parent / "resources"

# (left, top, right, bottom) alpha-channel bounding boxes, measured from
# the source PNGs (see the PR/commit notes for how -- Pillow's
# Image.getbbox() on the alpha channel).
_CASSETTE_BBOX = QRect(186, 41, 1734 - 186, 1038 - 41)

# card_asset.png's landscape index-card look, measured the same way.
_CARD_BBOX = QRect(210, 88, 1710 - 210, 992 - 88)


def _load_cropped(filename: str, bbox: QRect) -> QPixmap:
    pixmap = QPixmap(str(RESOURCES_DIR / filename))
    if pixmap.isNull():
        raise FileNotFoundError(f"Could not load asset: {filename}")
    return pixmap.copy(bbox)


def load_cassette_pixmap() -> QPixmap:
    """The cassette shell, cropped tight to its own silhouette."""
    return _load_cropped("cassette_asset.png", _CASSETTE_BBOX)


def load_card_pixmap() -> QPixmap:
    """
    The wide, landscape index-card texture (card_asset.png) that
    replaces the old tall receipt look -- see CardView (formerly
    ReceiptDropView) in app/ui/widgets/receipt_view.py.
    """
    return _load_cropped("card_asset.png", _CARD_BBOX)


def _load_pixmap(filename: str) -> QPixmap:
    pixmap = QPixmap(str(RESOURCES_DIR / filename))
    if pixmap.isNull():
        raise FileNotFoundError(f"Could not load asset: {filename}")
    return pixmap


def load_task_manager_pixmap() -> QPixmap:
    """The shepherd figure -- the cassette's Task Manager button."""
    return _load_pixmap("task-manager.png")


def load_sheep_pixmap(filename: str) -> QPixmap:
    """
    One of the three sheep silhouettes (close.png/minimize.png/
    settings.png) -- visually identical art, so callers just pick which
    file by the button's actual function.
    """
    return _load_pixmap(filename)
