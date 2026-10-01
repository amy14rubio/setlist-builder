"""
Logging configuration for Setlist Builder.

Call setup_logging() ONCE, near the very start of main.py, before any other
module logs anything. After that, every other file in the app just does:

    import logging
    logger = logging.getLogger(__name__)
    logger.info("something happened")

...and it will automatically be written to both the console and the log
file configured here. Modules don't need to know anything about *where*
logs go — that's the whole point of Python's logging system.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path | None = None) -> None:
    """
    Configure the root logger for the whole application.

    Parameters
    ----------
    log_dir:
        Folder to write log files into. Defaults to a `logs/` folder
        alongside this project (NOT Application Support — unlike settings,
        logs are debugging artifacts you'll want to find sitting right next
        to the code when something goes wrong, per the original project
        layout).
    """
    if log_dir is None:
        # Path(__file__) is THIS file (logging_setup.py).
        # .parent three times walks: utils/ -> app/ -> project root.
        # This way the log folder is found correctly no matter what
        # directory you happen to run `python main.py` from.
        project_root = Path(__file__).resolve().parent.parent.parent
        log_dir = project_root / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "setlist_builder.log"

    # A "formatter" defines what each log line looks like.
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # RotatingFileHandler prevents the log file from growing forever.
    # Once it hits maxBytes, it renames the old one (.log.1, .log.2, ...)
    # and starts a fresh file. backupCount=5 means we keep 5 old logs
    # around before the oldest gets deleted.
    file_handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    # Also print logs to the terminal while developing, at a less noisy
    # level (INFO and above) so debugging details don't clutter the console
    # but are still captured in the file.
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    # The ROOT logger is the ancestor of every logger created anywhere in
    # the app via logging.getLogger(__name__). Configuring it here once
    # means every module's logger inherits these handlers automatically.
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.getLogger(__name__).info("Logging initialized. Writing to %s", log_file)
