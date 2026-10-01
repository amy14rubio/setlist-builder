"""
Accessibility-focused tests for the frontend.

This app leans heavily on icon-only buttons (Match/Add/Remove/Download/
Merge/Schedule/etc.) with no visible text -- which means the tooltip AND
the accessible name (what VoiceOver/screen readers actually announce)
are the ONLY way anyone using assistive technology, or even just
hovering to figure out what a button does, can tell what it is. These
tests sweep every real page in the app and assert that invariant holds,
rather than trusting each page's author remembered it by hand.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QPushButton

from app.models.settings import Settings
from app.ui.pages.charts_page import ChartsPage
from app.ui.pages.playlist_page import PlaylistPage
from app.ui.pages.review_page import ReviewPage
from app.ui.pages.youtube_page import YouTubePage
from app.ui.widgets.icon_button import ICON_BUTTON_SIZE, make_icon_button
from app.ui.widgets.monthly_carousel import MonthlyCarouselWidget

# A reasonable floor for a desktop pointer target -- not a touchscreen
# guideline (that would be ~44pt), but small enough to catch an
# accidental near-zero size while not being so strict it fights the
# monthly carousel's deliberately smaller Previous/Next buttons (20px).
_MIN_CLICK_TARGET_PX = 18


def _all_icon_buttons(widget) -> list[QPushButton]:
    """Every QPushButton descendant that has an icon set (i.e. was built via make_icon_button)."""
    return [button for button in widget.findChildren(QPushButton) if not button.icon().isNull()]


@pytest.fixture(params=[ChartsPage, YouTubePage, ReviewPage, PlaylistPage, MonthlyCarouselWidget])
def page(request, qapp):
    cls = request.param
    if cls is MonthlyCarouselWidget:
        return cls()
    return cls(Settings())


def test_every_icon_button_has_a_non_empty_tooltip(page):
    icon_buttons = _all_icon_buttons(page)
    assert icon_buttons, f"{type(page).__name__} has no icon buttons to check -- update this test if that's now expected"

    for button in icon_buttons:
        assert button.toolTip().strip(), f"{type(page).__name__} has an icon button with no tooltip"


def test_every_icon_button_has_a_matching_accessible_name(page):
    """The accessible name is what VoiceOver actually reads -- must exist and match the tooltip, not just default to Qt's blank fallback."""
    for button in _all_icon_buttons(page):
        assert button.accessibleName().strip(), f"{type(page).__name__} has an icon button with no accessible name"
        assert button.accessibleName() == button.toolTip()


def test_every_icon_button_meets_the_minimum_click_target_size(page):
    for button in _all_icon_buttons(page):
        assert button.width() >= _MIN_CLICK_TARGET_PX
        assert button.height() >= _MIN_CLICK_TARGET_PX


def test_make_icon_button_rejects_an_empty_tooltip(qapp):
    """Enforced at the source, not just caught by sweeping tests after the fact."""
    with pytest.raises(ValueError):
        make_icon_button("add", "")


def test_setup_buttons_start_disabled_until_something_is_loaded(qapp):
    """
    Accessibility matters here too: a screen reader announces a
    disabled control differently, and a sighted user relies on the
    greyed-out state -- these three must not be independently clickable
    before there's anything for Logic Pro/Drive/YouTube to act on.
    """
    page = PlaylistPage(Settings())

    assert not page._setup_logic_button.isEnabled()
    assert not page._setup_charts_button.isEnabled()
    assert not page._setup_youtube_button.isEnabled()


def test_mode_picker_buttons_have_non_empty_visible_text(qapp):
    """Weekly/Monthly and logic pro/drive/youtube rely on their own visible text as their accessible name -- confirm it's actually there."""
    page = PlaylistPage(Settings())

    for button in (
        page._weekly_button_large,
        page._monthly_button_large,
        page._setup_logic_button,
        page._setup_charts_button,
        page._setup_youtube_button,
    ):
        assert button.text().strip()
