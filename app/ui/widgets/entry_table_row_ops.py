"""
Shared Add/Remove row logic for ChartsPage, YouTubePage, and ReviewPage --
the three "menu option" tabs (Drive, YouTube, Logic Pro) that each keep a
QTableWidget of SongEntry rows.

Extracted here because all three pages had implemented this same two
things nearly identically, copy-pasted as each new tab was built:

  1. Add a blank row directly below whichever row is selected (not
     always at the end), renumbering everything after it.
  2. Remove the selected row.

ALL THREE TABS SHARE THE SAME SongEntry OBJECTS for a given week (so a
manual match correction -- audio_match, chart_match, etc. -- survives
switching tabs), but each tab tracks its OWN inclusion independently:
Remove doesn't delete the SongEntry from the shared list, it just flags
THIS tab's own exclusion field on it (see SongEntry.excluded_from_drive/
_youtube/_logic), and every method here works against the tab's own
FILTERED view (`_visible_entries()`), not the raw shared list. This is
what fixes a real bug -- removing a song from Drive's list used to also
remove it from YouTube's and Logic Pro's, since all three read from the
exact same Python list object and Remove used to `list.pop()` it
outright.

A mixin, not a base class, since ChartsPage/YouTubePage/ReviewPage each
already subclass QWidget for unrelated reasons (different constructors,
different table columns) -- this only assumes the host class provides
`self._entries` (the shared `list[SongEntry]`), `self._table` (a
QTableWidget), and a `self._rebuild_table()` method that redraws the
table from `self._visible_entries()`, which all three already have.
"""

from __future__ import annotations

from typing import Optional

from app.models.song_entry import SongEntry

# Every per-destination exclusion field SongEntry has -- used when a new
# row is added, to exclude it from every OTHER destination by default
# (see _insert_new_entry_below_selected): a song typed in while setting
# up Drive shouldn't silently also appear on YouTube/Logic Pro until you
# deliberately add it there too.
_ALL_EXCLUSION_FIELDS = ("excluded_from_drive", "excluded_from_youtube", "excluded_from_logic")


class EntryTableRowOpsMixin:
    """
    Mix into a QWidget subclass that has `self._entries: list[SongEntry]`,
    `self._table: QTableWidget`, and `self._rebuild_table() -> None`.

    Call `self._init_row_ops(exclusion_field)` once in `__init__` (after
    `self._entries` is assigned) before using any of the methods below --
    `exclusion_field` is which of SongEntry's three per-destination flags
    this particular tab owns (e.g. "excluded_from_drive" for ChartsPage).
    """

    def _init_row_ops(self, exclusion_field: str) -> None:
        if exclusion_field not in _ALL_EXCLUSION_FIELDS:
            raise ValueError(f"_init_row_ops got an unknown exclusion field: {exclusion_field!r}")
        self._exclusion_field = exclusion_field

    def _visible_entries(self) -> list[SongEntry]:
        """This tab's own entries -- the shared list, minus whatever it has excluded."""
        return [entry for entry in self._entries if not getattr(entry, self._exclusion_field)]

    def _renumber_entries(self) -> None:
        """
        Keep `order` contiguous (1, 2, 3, ...) after any add/remove --
        across the FULL shared list, not just this tab's own visible
        subset, since `order` is the song's position in the setlist
        overall (see SongEntry's docstring), a single value every tab
        (and the Build Manifest, chart filenames, etc.) reads from the
        same underlying entry.
        """
        for index, entry in enumerate(self._entries, start=1):
            entry.order = index

    def _insert_new_entry_below_selected(self) -> tuple[SongEntry, int]:
        """
        Inserts a blank "New Song" entry directly below the currently
        selected VISIBLE row (or appends to the end of the shared list if
        nothing's selected), renumbers, rebuilds the table, and selects
        the new row. Returns the new entry and the row index it landed
        on (within this tab's own visible table), for the caller's own
        logging.

        Excluded from the other two destinations by default (see
        _ALL_EXCLUSION_FIELDS) -- only included in THIS tab until you
        deliberately add it to the others too.
        """
        visible = self._visible_entries()
        selected_row = self._table.currentRow()
        if 0 <= selected_row < len(visible):
            insert_at = self._entries.index(visible[selected_row]) + 1
        else:
            insert_at = len(self._entries)

        new_entry = SongEntry(order=insert_at + 1, ocr_title="New Song")
        for field in _ALL_EXCLUSION_FIELDS:
            setattr(new_entry, field, field != self._exclusion_field)
        self._entries.insert(insert_at, new_entry)
        self._renumber_entries()
        self._rebuild_table()

        new_row = self._visible_entries().index(new_entry)
        self._table.selectRow(new_row)
        return new_entry, new_row

    def _remove_selected_entry(self) -> Optional[SongEntry]:
        """
        Excludes the currently selected VISIBLE row from THIS tab only
        (see module docstring) and rebuilds the table. Returns the
        removed entry, or None if nothing was selected (the caller
        decides how to log that case).
        """
        row = self._table.currentRow()
        visible = self._visible_entries()
        if not (0 <= row < len(visible)):
            return None

        entry = visible[row]
        setattr(entry, self._exclusion_field, True)
        self._rebuild_table()
        return entry
