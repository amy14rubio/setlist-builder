"""
Regression test for a real crash bug: an earlier edit to
app/ui/pages/build_page.py left a stray `logger.info(text)` at the end
of what was then `_on_approvals_clicked` (the per-tab bell button's
handler, since superseded by the single Task Manager bell on the main
menu -- see playlist_page.py) -- referencing a variable named `text`
that was never defined anywhere in that method. A NameError on every
single click, only caught because the user hit it live and pasted the
traceback back.

That exact method no longer exists (the bell moved), but the class of
bug -- a stray reference slipping into a button handler -- is exactly
what a trivial "call every handler, confirm it doesn't raise" smoke
test catches immediately. This file is deliberately not a deep test of
what each handler DOES, just that clicking it doesn't blow up.
"""

from __future__ import annotations

from unittest.mock import patch

from app.models.settings import Settings
from app.ui.pages.build_page import BuildPage


def test_prepare_button_with_no_songs_loaded_does_not_raise(qapp):
    page = BuildPage(Settings())
    page._on_prepare_clicked()  # nothing loaded -- should log and return, not crash


def test_schedule_button_with_no_setlist_does_not_raise(qapp):
    """
    The Schedule button moved from BuildPage to ReviewPage (per direct
    feedback -- scheduling doesn't need to wait for Continue to Build),
    so this now exercises ReviewPage's own _on_schedule_clicked.
    """
    from app.ui.pages.review_page import ReviewPage

    page = ReviewPage(Settings())
    with patch("app.ui.pages.review_page.QMessageBox") as mock_box:
        page._on_schedule_clicked()
    assert mock_box.warning.called  # "can't schedule" message, not a crash


def test_run_automation_button_with_no_manifest_does_not_raise(qapp):
    page = BuildPage(Settings())
    page._on_run_automation_clicked()  # no manifest built yet -- should log and return, not crash


def test_cancel_automation_button_does_not_raise(qapp):
    page = BuildPage(Settings())
    page._on_cancel_automation_clicked()


def test_task_manager_bell_does_not_raise(qapp):
    """
    The CURRENT home of the bell feature (see playlist_page.py) --
    the same class of "stray reference in a button handler" bug this
    whole file guards against, at its present location.
    """
    from app.ui.pages.playlist_page import PlaylistPage

    page = PlaylistPage(Settings())
    with patch("app.ui.pages.playlist_page.show_task_manager_dialog"):
        page._on_task_manager_clicked()  # must not raise
