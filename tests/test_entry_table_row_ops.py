"""
Regression tests for app/ui/widgets/entry_table_row_ops.py (the mixin
shared by ChartsPage, YouTubePage, and ReviewPage) -- covering:

1. Add inserts directly below the selected VISIBLE row (not always at
   the end), renumbering everything after it (globally -- `order` is
   the song's position in the setlist overall, shared by all three
   tabs, not per-tab).
2. Remove excludes the selected row from THIS TAB ONLY, instead of
   deleting it from the shared entries list outright.
3. THE BUG THIS FILE EXISTS TO CATCH: Drive/YouTube/Logic Pro all share
   the same underlying SongEntry objects for a given week (so a manual
   match correction survives switching tabs), but each tracks its own
   inclusion independently. Removing a song from Drive's list must NOT
   remove it from YouTube's or Logic Pro's -- previously it did, because
   Remove used to `list.pop()` the SHARED list outright. Reported bug:
   "if I set drive to 8 songs and then go to youtube and set 5 songs
   while deleting the rest, drive would also only change to having 5
   songs."
4. Symmetrically, adding a new blank row in one tab must not silently
   add it to the other two -- it starts included ONLY in the tab it was
   added to.
5. Match/re-match no longer undoes removals or additions -- inclusion
   is now a real, persistent decision (like a match correction), not a
   temporary edit Match resets.

Exercised directly against ChartsPage, YouTubePage, and ReviewPage
(rather than only the mixin in isolation) specifically because the
point of extracting the mixin was to guarantee identical behavior
across all three -- a bug in only one page's wiring wouldn't show up in
a mixin-only test.
"""

from __future__ import annotations

import pytest

from app.models.settings import Settings
from app.models.song_entry import SongEntry
from app.ui.pages.charts_page import ChartsPage
from app.ui.pages.review_page import ReviewPage
from app.ui.pages.youtube_page import YouTubePage


def _load(page, entries: list[SongEntry]) -> None:
    if isinstance(page, ReviewPage):
        page.load_entries(entries, [], [])
    else:
        page.set_entries(entries)
    page._rebuild_table()


def _make_page(cls, entries: list[SongEntry] | None = None):
    page = cls(Settings())
    _load(page, entries if entries is not None else [SongEntry(order=i, ocr_title=f"Song {i}") for i in range(1, 4)])
    return page


@pytest.fixture(params=[ChartsPage, YouTubePage, ReviewPage], ids=["ChartsPage", "YouTubePage", "ReviewPage"])
def page(request, qapp):
    return _make_page(request.param)


def test_add_inserts_below_selected_row_and_renumbers(page):
    page._table.selectRow(0)  # "Song 1"

    page._on_add_song_clicked()

    titles_in_order = [e.ocr_title for e in page._visible_entries()]
    assert titles_in_order == ["Song 1", "New Song", "Song 2", "Song 3"]
    assert [e.order for e in page._entries] == [1, 2, 3, 4]


def test_add_appends_to_end_when_nothing_selected(page):
    page._table.clearSelection()
    page._table.setCurrentCell(-1, -1)

    page._on_add_song_clicked()

    assert [e.ocr_title for e in page._visible_entries()][-1] == "New Song"


def test_remove_excludes_selected_row_from_this_tab_and_renumbers(page):
    page._table.selectRow(1)  # "Song 2"

    page._on_remove_row_clicked()

    titles_in_order = [e.ocr_title for e in page._visible_entries()]
    assert titles_in_order == ["Song 1", "Song 3"]
    # Renumbered globally, across the full shared list -- Song 2 is
    # still IN that list (just excluded from this tab), so `order`
    # still accounts for it.
    assert [e.order for e in page._entries] == [1, 2, 3]


def test_remove_does_not_delete_the_entry_from_the_shared_list(page):
    """The core fix: Remove must exclude, never pop -- the entry still exists for other tabs to see."""
    page._table.selectRow(1)  # "Song 2"

    removed = page._remove_selected_entry()

    assert removed is not None
    assert removed in page._entries  # still present in the shared list
    assert len(page._entries) == 3  # nothing was actually deleted
    assert len(page._visible_entries()) == 2  # just excluded from THIS tab's own view


def test_remove_with_nothing_selected_is_a_no_op(page):
    page._table.clearSelection()
    page._table.setCurrentCell(-1, -1)
    original = list(page._entries)

    page._on_remove_row_clicked()

    assert page._entries == original


def test_match_does_not_undo_a_removal_or_an_addition(page, monkeypatch):
    """
    Inclusion is now a real, persistent decision -- Match/re-match must
    NOT bring back a removed song or drop a manually-added one, unlike
    the old "temporary until you re-match" behavior.
    """
    match_method_name = {"ChartsPage": "match_charts", "YouTubePage": "match_songs", "ReviewPage": "match_entries"}[
        type(page).__name__
    ]
    # Stub out the actual network/library matching -- only the
    # inclusion-survives-Match behavior is under test here.
    monkeypatch.setattr(page, match_method_name, lambda: page._rebuild_table())

    page._table.selectRow(1)
    page._on_remove_row_clicked()  # "Song 2" excluded from this tab
    page._table.selectRow(0)
    page._on_add_song_clicked()  # "New Song" included only in this tab

    page._on_match_clicked()

    titles = [e.ocr_title for e in page._visible_entries()]
    assert "Song 2" not in titles
    assert "New Song" in titles


# ----------------------------------------------------------------------
# Cross-tab independence -- the exact scenario reported: removing a song
# from one tab (e.g. Drive) must not remove it from the others (YouTube,
# Logic Pro), even though all three share the same underlying entries.
# ----------------------------------------------------------------------


def test_removing_from_one_tab_does_not_affect_the_others(qapp):
    shared_entries = [SongEntry(order=i, ocr_title=f"Song {i}") for i in range(1, 9)]  # 8 songs

    drive = _make_page(ChartsPage, shared_entries)
    youtube = _make_page(YouTubePage, shared_entries)
    logic = _make_page(ReviewPage, shared_entries)

    # All three see all 8 songs to start.
    assert len(drive._visible_entries()) == 8
    assert len(youtube._visible_entries()) == 8
    assert len(logic._visible_entries()) == 8

    # On YouTube, keep only the first 5 -- remove the last 3, one at a
    # time from the end (matching how a user would actually do this).
    for _ in range(3):
        youtube._table.selectRow(youtube._table.rowCount() - 1)
        youtube._on_remove_row_clicked()

    assert len(youtube._visible_entries()) == 5
    # THE BUG: Drive and Logic Pro must be completely unaffected.
    assert len(drive._visible_entries()) == 8
    assert len(logic._visible_entries()) == 8
    assert [e.ocr_title for e in drive._visible_entries()] == [f"Song {i}" for i in range(1, 9)]

    # The underlying shared list itself still has all 8 -- nothing was
    # actually deleted, only excluded from YouTube's own view.
    assert len(shared_entries) == 8


def test_adding_to_one_tab_does_not_affect_the_others(qapp):
    shared_entries = [SongEntry(order=i, ocr_title=f"Song {i}") for i in range(1, 4)]

    drive = _make_page(ChartsPage, shared_entries)
    youtube = _make_page(YouTubePage, shared_entries)

    drive._table.selectRow(drive._table.rowCount() - 1)
    drive._on_add_song_clicked()

    assert "New Song" in [e.ocr_title for e in drive._visible_entries()]
    assert "New Song" not in [e.ocr_title for e in youtube._visible_entries()]
    # It DOES exist in the shared list (YouTube could add it too later),
    # just excluded from YouTube's own view by default.
    assert any(e.ocr_title == "New Song" for e in shared_entries)
