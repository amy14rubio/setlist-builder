"""
Regression tests for two real bugs, both the same root cause: a
double-clicked .app bundle (and launchd) get a minimal PATH
(/usr/bin:/bin:/usr/sbin:/sbin) that doesn't include Homebrew's
/opt/homebrew/bin -- so a bare command name silently fails to resolve,
even though the exact same code works fine from a Terminal (whose PATH
does include Homebrew).

1. app/services/ocr_service.py called pytesseract with the bare command
   "tesseract" -- worked from Terminal, silently returned "no song
   titles found" when launched from the double-click .app launcher.
2. The notification listener called the bare command
   "terminal-notifier" -- worked when run by hand, crashed with
   FileNotFoundError (invisibly, since its own stderr went to /dev/null
   at the time) every time launchd's scheduler_runner.py spawned it.
   (That lookup now lives in notification_service, shared by every
   notification the background scheduler sends.)

Both were fixed the same way: resolve an absolute path once via
shutil.which(), falling back to the known Homebrew install locations,
instead of ever trusting PATH.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services import notification_service, ocr_service


def test_ocr_service_resolves_tesseract_via_path_when_available():
    with patch("app.services.ocr_service.shutil.which", return_value="/usr/bin/tesseract"):
        assert ocr_service._resolve_tesseract_cmd() == "/usr/bin/tesseract"


def test_ocr_service_falls_back_to_homebrew_path_when_not_on_path():
    """The actual bug: a minimal launcher PATH means shutil.which('tesseract') returns None."""
    with patch("app.services.ocr_service.shutil.which", return_value=None), \
         patch("app.services.ocr_service.Path.exists", return_value=True):
        resolved = ocr_service._resolve_tesseract_cmd()

    assert resolved in ocr_service._TESSERACT_CANDIDATES


def test_ocr_service_falls_back_to_bare_command_if_nothing_found():
    """Never crash at import time -- if truly nowhere to be found, let pytesseract raise its own clear error later."""
    with patch("app.services.ocr_service.shutil.which", return_value=None), \
         patch("app.services.ocr_service.Path.exists", return_value=False):
        assert ocr_service._resolve_tesseract_cmd() == "tesseract"


def test_notification_service_resolves_terminal_notifier_via_path_when_available():
    with patch("app.services.notification_service.shutil.which", return_value="/usr/bin/terminal-notifier"):
        assert notification_service.find_terminal_notifier() == "/usr/bin/terminal-notifier"


def test_notification_service_falls_back_to_homebrew_path_when_not_on_path():
    """The actual bug: launchd's minimal PATH means a bare 'terminal-notifier' lookup fails."""
    def fake_which(name):
        return "/opt/homebrew/bin/terminal-notifier" if name == "/opt/homebrew/bin/terminal-notifier" else None

    with patch("app.services.notification_service.shutil.which", side_effect=fake_which):
        assert notification_service.find_terminal_notifier() == "/opt/homebrew/bin/terminal-notifier"


def test_notification_service_raises_a_clear_error_if_truly_not_installed():
    with patch("app.services.notification_service.shutil.which", return_value=None):
        try:
            notification_service.find_terminal_notifier()
            assert False, "expected a RuntimeError"
        except RuntimeError as error:
            assert "brew install terminal-notifier" in str(error)
