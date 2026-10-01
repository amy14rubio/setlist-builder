"""
External video lookup for Setlist Builder.

Fetches a song list from a configured external website (see
Settings.video_lookup_url) and builds a lookup from song title to
YouTube video ID. Used to find each of this week's songs' videos, so
they can be added to a YouTube playlist automatically.

There is no default URL baked in here -- this only works once you've
configured Settings.video_lookup_url to point at a site that lists
songs with linked YouTube videos.

WHY SCRAPE INSTEAD OF USING AN API
------------------------------------
The site this was originally built against has no search API of its
own -- a page's own client-side search box is pure JavaScript filtering
elements already present in the page's HTML. That means every song is
always present in one page load; there's nothing to paginate or search
server-side. A single fetch of the page gives us everything.

HTML STRUCTURE THIS PARSER EXPECTS
---------------------------------------
Each song is expected to be a plain anchor tag:

    <a href="https://youtu.be/VIDEO_ID" ...><span>Song Title</span></a>

Some songs may wrap this in a <p>, and some anchors may be missing the
target/rel attributes -- neither matters, since matching is done purely
on the href pattern. Any link that doesn't match a single-video
youtu.be URL (e.g. a link to a whole playlist instead) is automatically
excluded.

This parser is shaped for one specific site's page structure -- pointing
Settings.video_lookup_url at a different site only works if that site
happens to share this same structure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

from app.utils.text_normalize import normalize_for_matching

logger = logging.getLogger(__name__)

_YOUTUBE_LINK_PATTERN = re.compile(r"^https://youtu\.be/([\w-]+)")


@dataclass
class VideoLookupEntry:
    """One song listed on the configured lookup site: its title and YouTube video ID."""

    title: str
    video_id: str


def _parse_video_lookup_html(html: str) -> list[VideoLookupEntry]:
    """
    Parse the raw HTML of the lookup site's page into a list of
    VideoLookupEntry objects. Split out from fetch_video_lookup_library()
    specifically so this parsing logic can be tested against a saved
    copy of the page's HTML, without needing a live network request.
    """
    soup = BeautifulSoup(html, "html.parser")

    entries: list[VideoLookupEntry] = []
    for link in soup.find_all("a", href=True):
        match = _YOUTUBE_LINK_PATTERN.match(link["href"])
        if not match:
            continue  # not a single-video link (e.g. a playlist promo button)

        title = link.get_text(strip=True)
        if not title:
            continue

        entries.append(VideoLookupEntry(title=title, video_id=match.group(1)))

    return entries


def fetch_video_lookup_library(url: str) -> list[VideoLookupEntry]:
    """
    Fetch and parse the full song list from `url` (Settings.video_lookup_url).

    Returns an empty list (logged, not raised) if `url` is blank (not
    configured yet), unreachable, or its structure doesn't match what
    _parse_video_lookup_html expects -- a missing setting, a network
    hiccup, or a website redesign shouldn't crash the whole app, just
    mean this particular feature can't find matches this run.
    """
    if not url:
        logger.info("No video lookup URL configured (Settings.video_lookup_url is blank) -- skipping.")
        return []

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except requests.RequestException as error:
        logger.error("Failed to fetch %s: %s", url, error)
        return []

    entries = _parse_video_lookup_html(response.text)
    logger.info("Fetched %d song(s) from %s", len(entries), url)
    return entries


def find_video_for_song(
    song_title: str, library: list[VideoLookupEntry]
) -> Optional[tuple[VideoLookupEntry, float]]:
    """
    Fuzzy-match a song title (e.g. from the Build Manifest) against the
    fetched lookup library -- the same normalization + RapidFuzz
    approach used for matching audio/Logic library folders.

    Returns (best_matching_entry, confidence_score), or None if the
    library itself is empty. As with the audio/Logic matching, this
    always returns SOMETHING if the library is non-empty, even a
    low-confidence guess -- the caller decides whether the score is good
    enough to actually use.
    """
    if not library:
        return None

    normalized_query = normalize_for_matching(song_title)
    normalized_choices = [normalize_for_matching(entry.title) for entry in library]

    result = process.extractOne(normalized_query, normalized_choices, scorer=fuzz.token_sort_ratio)
    if result is None:
        return None

    _matched_text, score, index = result
    logger.debug("Matched '%s' -> '%s' (score=%.1f)", song_title, library[index].title, score)
    return library[index], score
