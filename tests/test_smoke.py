"""Sanity check that the test harness itself (Qt + isolated HOME) works before anything else relies on it."""

from app.models.settings import Settings
from app.services.database_service import initialize_database


def test_isolated_home_does_not_touch_real_data(isolated_home):
    initialize_database()
    db_file = isolated_home / "Library" / "Application Support" / "SetlistBuilder" / "setlist_builder.db"
    assert db_file.exists()


def test_qapp_fixture_provides_a_real_qapplication(qapp):
    from PySide6.QtWidgets import QApplication

    assert isinstance(qapp, QApplication)
