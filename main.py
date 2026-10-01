"""
Setlist Builder — application entry point.

Run with:
    python main.py
(after activating your virtual environment)
"""

import logging
import sys

from PySide6.QtWidgets import QApplication

from app.services.database_service import initialize_database, purge_completed_tasks
from app.ui.main_window import MainWindow
from app.ui.style import load_stylesheet
from app.utils.logging_setup import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    # Logging must be configured before anything else logs a message,
    # otherwise those early log calls go nowhere (or use Python's bare
    # default configuration, which we don't want).
    setup_logging()
    logger.info("Setlist Builder starting up.")

    initialize_database()
    # Clears out task rows that already ran and can never run again --
    # see purge_completed_tasks for the one row it deliberately keeps.
    purge_completed_tasks()

    app = QApplication(sys.argv)
    app.setStyleSheet(load_stylesheet())
    window = MainWindow()
    window.show()

    logger.info("Main window shown. Entering Qt event loop.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
