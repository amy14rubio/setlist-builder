"""
Main window for Setlist Builder.

Hosts a single page: the Playlist page, illustrated as a cassette. Charts
("drive"), YouTube, and Logic Pro (Review + Build) setup all now live
ENTIRELY inside the Playlist page itself, as in-place paper panels
overlaid on the cassette -- MainWindow never navigates to a separate page
or grows/shrinks the window for any of them.

The window itself is frameless and translucent, masked to the Playlist
page's exact silhouette (the cassette, plus whichever of the receipt or a
region panel it's currently showing, since both deliberately poke out
past the cassette's edges).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QApplication, QMainWindow

from app.models.settings import Settings
from app.ui.pages.playlist_page import PlaylistPage


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Setlist Builder")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(QApplication.quit)
        self.addAction(quit_action)

        self._settings = Settings.load()

        self._playlist_page = PlaylistPage(self._settings)
        self._playlist_page.shape_changed.connect(self._show_cassette_shape)
        self.setCentralWidget(self._playlist_page)

        self.resize(self._playlist_page.size())
        self._center_on_screen()
        self._show_cassette_shape()

    def showEvent(self, event) -> None:
        """
        Recompute the mask once the window is ACTUALLY shown (main.py
        calls window.show() after MainWindow() is constructed) -- the
        mask computed in __init__ was silently wrong for anything that
        depends on isVisible() (like the card, always shown from
        launch): a widget's isVisible() is False until its top-level
        window itself is shown, even after its own .show() has been
        called, so that first mask always excluded the card entirely.
        """
        super().showEvent(event)
        self._show_cassette_shape()

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = available.x() + (available.width() - self.width()) // 2
        y = available.y() + (available.height() - self.height()) // 3
        self.move(x, y)

    def _show_cassette_shape(self) -> None:
        self.setMask(self._playlist_page.shape_mask())
