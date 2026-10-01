"""
LibraryEntry model for Setlist Builder.

Represents ONE folder found while scanning either the Audio Library or
the Logic Library -- before any matching has happened. This is
deliberately a plainer, simpler shape than LibraryMatch (in song_entry.py):
a LibraryEntry has no "confidence" score, because confidence only makes
sense once it's been compared against something. It's just "here's a
folder that exists, with its title and BPM parsed out of the name."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class LibraryEntry:
    """One scanned folder from a library, before any matching occurs."""

    title: str
    folder_path: str
    bpm: Optional[int] = None
