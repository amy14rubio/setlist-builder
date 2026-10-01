"""
Build page for Setlist Builder.

The final page: Prepare (copies matched audio folders into the weekly
destination, then writes the JSON handoff manifest from those files --
merged into one button/one step since a manifest can never meaningfully
be built without the audio having just been copied first, per direct
feedback) and Build Logic Pro Project (drives Logic Pro directly via
logic_automation.py, kept separate since it takes over your keyboard
and screen). This is the ONE place in the whole app that builds the
actual QMessageBox conflict dialog -- copy_service.py itself has no
idea a dialog exists; it just calls the on_conflict function we hand it
here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget

from app.models.settings import Settings
from app.models.song_entry import SongEntry
from app.services.copy_service import OVERWRITE, SKIP, copy_audio_folders
from app.services.logic_automation import AutomationCancelled, build_full_rehearsal_project, request_cancel
from app.services.manifest_service import build_manifest, write_manifest

logger = logging.getLogger(__name__)

_MANIFEST_FILENAME = "build_manifest.json"

# Only lines from logic_automation's own logger get streamed into the
# Build page's log view -- not every log line the whole app produces
# while the automation happens to be running.
_AUTOMATION_LOGGER_NAME = "app.services.logic_automation"


class _LogSignalBridge(QObject):
    """
    Just a Signal, isolated on a plain QObject of its own.

    _QtLogHandler can't carry this Signal directly: logging.Handler
    already defines a method named emit(), and multiply-inheriting from
    both logging.Handler AND QObject on the SAME class makes that name
    collide with Qt's own signal-emission machinery (confirmed by
    testing -- it raises a bogus "takes 2 positional arguments but 3
    were given" TypeError from deep inside Shiboken, not from this
    code). Keeping the Signal on a separate QObject that never defines
    an emit() method of its own sidesteps the collision entirely.
    """

    log_emitted = Signal(str)


class _QtLogHandler(logging.Handler):
    """
    A logging.Handler that re-emits every log record as a Qt signal
    (via self.bridge.log_emitted).

    Needed because logic_automation.build_full_rehearsal_project() runs
    on a background QThread and logs its progress the normal way
    (logger.info(...)) -- this is the bridge that gets those lines into
    the Build page's log view in real time, in addition to wherever the
    app's normal logging configuration already sends them (console,
    log file).

    Qt's signal/slot mechanism auto-queues delivery across threads based
    on the RECEIVING object's thread affinity, so emitting this signal
    from the background thread (inside emit(), called by the logging
    module while build_full_rehearsal_project() runs) safely reaches a
    main-thread slot without any manual locking here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.bridge = _LogSignalBridge()

    def emit(self, record: logging.LogRecord) -> None:
        self.bridge.log_emitted.emit(self.format(record))


class _LogicAutomationThread(QThread):
    """
    Runs build_full_rehearsal_project() on a background thread so
    Logic Pro's multi-minute automation never blocks the UI event loop.

    Exactly one of finished_ok / cancelled / failed fires when the run
    ends, so the UI can show the right final message and re-enable its
    buttons. log_message fires once per log line logic_automation.py
    emits while the run is in progress.
    """

    log_message = Signal(str)
    finished_ok = Signal()
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, manifest_path: str, parent=None) -> None:
        super().__init__(parent)
        self._manifest_path = manifest_path

    def run(self) -> None:
        automation_logger = logging.getLogger(_AUTOMATION_LOGGER_NAME)
        handler = _QtLogHandler()
        handler.setLevel(logging.INFO)
        handler.bridge.log_emitted.connect(self.log_message)
        automation_logger.addHandler(handler)

        try:
            build_full_rehearsal_project(self._manifest_path)
            self.finished_ok.emit()
        except AutomationCancelled:
            self.cancelled.emit()
        except Exception as error:  # surface ANY failure to the UI, not just RuntimeError
            logger.exception("Logic Pro automation failed")
            self.failed.emit(str(error))
        finally:
            automation_logger.removeHandler(handler)


class BuildPage(QWidget):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._entries: list[SongEntry] = []
        self._manifest_path: str | None = None
        self._automation_thread: Optional[_LogicAutomationThread] = None

        self._prepare_button = QPushButton("Prepare (Copy Audio + Build Manifest)")
        self._prepare_button.setCursor(Qt.PointingHandCursor)
        self._prepare_button.clicked.connect(self._on_prepare_clicked)

        self._run_automation_button = QPushButton("▶ Build Logic Pro Project")
        self._run_automation_button.setCursor(Qt.PointingHandCursor)
        self._run_automation_button.setToolTip(
            "This takes over your keyboard and screen to drive Logic Pro. "
            "Make sure Logic is open with a blank project ready before clicking."
        )
        self._run_automation_button.clicked.connect(self._on_run_automation_clicked)

        self._cancel_automation_button = QPushButton("⏹ Cancel")
        self._cancel_automation_button.setCursor(Qt.PointingHandCursor)
        self._cancel_automation_button.setToolTip(
            "Stops the automation within about a second."
        )
        self._cancel_automation_button.clicked.connect(self._on_cancel_automation_clicked)
        self._cancel_automation_button.setVisible(False)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)

        prepare_row = QHBoxLayout()
        prepare_row.setContentsMargins(0, 0, 0, 0)
        prepare_row.addWidget(self._prepare_button)

        automation_row = QHBoxLayout()
        automation_row.setContentsMargins(0, 0, 0, 0)
        automation_row.addWidget(self._run_automation_button)

        layout = QVBoxLayout(self)
        # Explicit, not left at Qt's platform-style default -- that
        # default comes from QStyle::pixelMetric and can genuinely differ
        # between styles (confirmed: Fusion vs macOS's native Aqua style
        # gave different values), which is exactly what made "Back to
        # Review" (playlist_page.py, explicitly zero-margin by our own
        # code) match this layout's margin on one style but not the
        # other. Pinning this to a fixed number removes that platform
        # dependency entirely -- playlist_page.py's back_to_review_row
        # is set to match this same number.
        layout.setContentsMargins(9, 9, 9, 9)
        layout.addLayout(prepare_row)
        layout.addLayout(automation_row)
        layout.addWidget(self._cancel_automation_button)
        layout.addWidget(self._log_view)

    def set_entries(self, entries: list[SongEntry]) -> None:
        """Called by MainWindow when the user moves from Review to Build."""
        self._entries = entries
        self._log_view.clear()
        self._append_log(f"{len(entries)} song(s) ready. Destination folder: {self._destination_folder()}")

    def start_build(self, entries: list[SongEntry]) -> None:
        """
        Sets the entries AND immediately runs Prepare (Copy Audio +
        Build Manifest).

        This is what "Continue to Build" triggers -- the whole point of
        reaching this page is to finish the weekly setup, so there's no
        reason to make the user press another button for a normal run.
        The Prepare button below stays available for re-running it by
        itself later (e.g. you went back to Review to fix something, or
        a copy needs retrying).
        """
        self.set_entries(entries)
        self._on_prepare_clicked()

    def _destination_folder(self) -> str:
        """
        Where Copy Audio places files and where the manifest gets written.

        Falls back to ~/Desktop/Secuencias if the user hasn't explicitly
        configured a Desktop Output Folder in Settings -- matches the
        original spec's default location.
        """
        if self._settings.desktop_output_folder:
            return self._settings.desktop_output_folder
        return str(Path.home() / "Desktop" / "Secuencias")

    def _on_prepare_clicked(self) -> None:
        """Copies matched audio into place, then builds the manifest from those files -- see the module docstring."""
        if not self._entries:
            self._append_log("Nothing to prepare -- no songs loaded.")
            return

        destination = self._destination_folder()
        self._append_log(f"Copying audio folders into {destination} ...")

        copy_audio_folders(self._entries, destination, on_conflict=self._ask_conflict)

        copied = sum(1 for e in self._entries if e.output_folder is not None)
        self._append_log(f"Done. {copied}/{len(self._entries)} song(s) have files in place.")

        self._on_build_manifest_clicked()

    def _ask_conflict(self, song_title: str) -> str:
        """
        The ONE place a real dialog gets built. copy_service.py calls
        this function without knowing anything about QMessageBox --
        it just expects back the string "overwrite" or "skip".
        """
        response = QMessageBox.question(
            self,
            "Folder Already Exists",
            f'"{song_title}" already exists in the destination folder.\n\n'
            f"Overwrite it with the version from your library?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,  # default button, in case Enter gets pressed accidentally
        )
        decision = OVERWRITE if response == QMessageBox.Yes else SKIP
        logger.info("User chose '%s' for conflict on '%s'.", decision, song_title)
        return decision

    def _on_build_manifest_clicked(self) -> None:
        if not self._entries:
            self._append_log("Nothing to build a manifest from -- no songs loaded.")
            return

        manifest = build_manifest(self._entries, self._settings)
        manifest_path = Path(self._destination_folder()) / _MANIFEST_FILENAME
        write_manifest(manifest, str(manifest_path))
        self._manifest_path = str(manifest_path)

        self._append_log(f"Build Manifest written to {manifest_path}")

    def _on_run_automation_clicked(self) -> None:
        if self._manifest_path is None:
            self._append_log("Build the manifest first -- nothing to build from yet.")
            return

        if self._automation_thread is not None:
            self._append_log("A build is already running.")
            return

        confirmed = QMessageBox.question(
            self,
            "Build Logic Pro Project?",
            "This will take over your keyboard and screen to build the "
            "Logic Pro project.\n\n"
            "Make sure Logic Pro is open with a blank project ready, with "
            "the Click and Guia tracks already created.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return

        self._set_automation_running(True)
        self._append_log("Starting Logic Pro automation...")

        self._automation_thread = _LogicAutomationThread(self._manifest_path, parent=self)
        self._automation_thread.log_message.connect(self._append_log)
        self._automation_thread.finished_ok.connect(self._on_automation_finished_ok)
        self._automation_thread.cancelled.connect(self._on_automation_cancelled)
        self._automation_thread.failed.connect(self._on_automation_failed)
        self._automation_thread.start()

    def _on_cancel_automation_clicked(self) -> None:
        self._append_log("Cancelling... (this can take up to a second)")
        self._cancel_automation_button.setEnabled(False)
        request_cancel()

    def _on_automation_finished_ok(self) -> None:
        self._append_log("Logic Pro project build complete.")
        self._teardown_automation_thread()

    def _on_automation_cancelled(self) -> None:
        self._append_log("Cancelled by user.")
        self._teardown_automation_thread()

    def _on_automation_failed(self, message: str) -> None:
        self._append_log(f"Failed: {message}")
        self._teardown_automation_thread()

    def _teardown_automation_thread(self) -> None:
        self._set_automation_running(False)
        self._automation_thread = None

    def _set_automation_running(self, running: bool) -> None:
        self._prepare_button.setEnabled(not running)
        self._run_automation_button.setEnabled(not running)
        self._cancel_automation_button.setVisible(running)
        self._cancel_automation_button.setEnabled(running)

    def _append_log(self, text: str) -> None:
        self._log_view.append(text)
