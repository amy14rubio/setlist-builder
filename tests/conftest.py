"""
Shared pytest fixtures for Setlist Builder's test suite.

TWO THINGS EVERY TEST IN THIS SUITE RELIES ON, BOTH HANDLED HERE:

1. Qt needs a platform plugin to even import PySide6 widgets, and CI/dev
   machines won't always have a real display -- QT_QPA_PLATFORM is set
   to "offscreen" before anything imports PySide6 (must happen at
   import time, at the top of this file, not inside a fixture function,
   since PySide6 picks its platform plugin the first time any of its
   modules are imported).
2. NEVER touch the real ~/Library/Application Support/SetlistBuilder/ --
   that's the developer's actual production database, settings, and
   OAuth tokens. The `isolated_home` fixture below redirects HOME to a
   fresh temp directory for every test, the same pattern used
   throughout manual verification all session (see e.g. the various
   /tmp/fake_home_* directories in this project's development history).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    """
    One QApplication for the whole test session -- Qt only allows a
    single QApplication instance per process, and widgets can't be
    constructed without one existing first.
    """
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """
    Redirects HOME to a fresh temp directory for the duration of one
    test, so app/models/settings.py and app/services/database_service.py
    (both of which resolve paths via Path.home()) read/write there
    instead of the developer's real Application Support folder.

    Returns the temp path, in case a test wants to inspect files
    directly (e.g. to assert a migration produced the expected schema).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path
