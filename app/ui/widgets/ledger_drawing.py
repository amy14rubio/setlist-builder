"""
Shared "index card" content drawing for Setlist Builder.

Both the weekly base card (ReceiptDropView, which pre-renders this onto
a pixmap so it can crossfade between old/new content) and the monthly
carousel (MonthlyCarouselWidget's overlay, which just paints live --
there's no crossfade between weeks, so no pixmap pre-render is needed)
draw the exact same layout: a title line, vertically aligned with
PlaylistPage's small "Weekly/Monthly" corner picker, followed by a
two-column numbered song list. Factored out here specifically so a
change to one (spacing, font size, column split) can never quietly
drift out of sync with the other -- direct feedback flagged that drift
once already (the monthly view wasn't matching the weekly view's
layout).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen

from app.models.song_entry import SongEntry

MAX_LEDGER_ROWS = 20  # up to 10 per column -- a real setlist rarely runs past ~10-12 songs
HEADER_COLOR = QColor(35, 32, 28)
ROW_COLOR = QColor(45, 42, 38)
DOTTED_LINE_COLOR = QColor(120, 115, 105)

# Matches playlist_page._position_mode_pickers' `small_margin_top` (the
# small "Weekly/Monthly" corner picker's own top margin, in DISPLAYED
# logical pixels) -- duplicated here rather than imported, since this
# module has no business knowing about PlaylistPage's layout; keep these
# two numbers in sync by hand if either changes. Used to vertically
# align the title with that picker.
DEFAULT_SMALL_PICKER_TOP_MARGIN_PX = 25



def draw_ledger_content(
    painter: QPainter,
    width: int,
    height: int,
    title: str,
    entries: list[SongEntry],
    target_width: int,
    small_picker_top_margin_px: float = DEFAULT_SMALL_PICKER_TOP_MARGIN_PX,
) -> None:
    """
    Draws a title + two-column numbered song list into `painter`'s
    current paint device, using `width`/`height` as the coordinate space
    to lay everything out in (proportionally -- every measurement below
    is a fraction of one of these two, so this reads identically whether
    called on a small live-painted widget or a large pre-rendered
    native-resolution canvas).

    `target_width` is the width the CALLER treats as its own "logical"
    size (see ReceiptDropView._render, which scales a native texture
    down to this before drawing) -- used only to keep the title's
    vertical alignment with the small mode-picker consistent regardless
    of the caller's actual resolution.
    """
    painter.setRenderHint(QPainter.Antialiasing)
    margin = round(width * 0.05)
    content_width = width - 2 * margin

    # Smaller than before (was 0.08) and centered across the FULL content
    # width, not shifted -- per direct feedback. A smaller font also
    # naturally needs less horizontal room, which is most of what keeps
    # it clear of PlaylistPage's small "Weekly/Monthly" corner picker for
    # any realistic title; an extreme title (long date + a long note)
    # can still reach that corner, a known remaining edge case rather
    # than something this centers around avoiding.
    title_font = QFont("Garogier", round(height * 0.06), QFont.Bold)
    row_font = QFont("Garogier", round(height * 0.06))

    painter.setPen(QPen(HEADER_COLOR))
    painter.setFont(title_font)
    title_metrics = QFontMetrics(title_font)

    picker_center_native = round(small_picker_top_margin_px * (width / target_width))
    y = max(0, picker_center_native - title_metrics.height() // 2) + title_metrics.ascent()

    elided_title = title_metrics.elidedText(title, Qt.ElideMiddle, content_width)
    painter.drawText(
        QRectF(margin, y - title_metrics.ascent(), content_width, title_metrics.height()),
        Qt.AlignHCenter,
        elided_title,
    )
    y += title_metrics.descent() + 15

    _draw_dotted_line(painter, margin, width - margin, y)
    y += 21

    shown = entries[:MAX_LEDGER_ROWS]
    half = (len(shown) + 1) // 2
    left_column, right_column = shown[:half], shown[half:]

    column_gap = round(content_width * 0.06)
    column_width = (content_width - column_gap) // 2
    left_x = margin
    right_x = margin + column_width + column_gap

    painter.setFont(row_font)
    row_metrics = QFontMetrics(row_font)
    row_height = round(row_metrics.height() * 1.4)
    rows_start_y = y

    for column_x, column_entries in ((left_x, left_column), (right_x, right_column)):
        row_y = rows_start_y
        for entry in column_entries:
            painter.setPen(QPen(ROW_COLOR))
            baseline = row_y + row_metrics.ascent()
            painter.drawText(column_x, baseline, f"{entry.order:02d}")
            number_width = row_metrics.horizontalAdvance("00") + round(column_width * 0.06)
            elided_song_title = row_metrics.elidedText(entry.ocr_title, Qt.ElideRight, column_width - number_width)
            painter.drawText(column_x + number_width, baseline, elided_song_title)
            row_y += row_height

    if len(entries) > len(shown):
        more_y = rows_start_y + max(len(left_column), len(right_column)) * row_height
        painter.setFont(row_font)
        painter.drawText(
            QRectF(margin, more_y, content_width, row_metrics.height()),
            Qt.AlignHCenter,
            f"+ {len(entries) - len(shown)} more…",
        )


def _draw_dotted_line(painter: QPainter, x1: int, x2: int, y: int) -> None:
    pen = QPen(DOTTED_LINE_COLOR)
    pen.setStyle(Qt.DotLine)
    pen.setWidth(1)
    painter.setPen(pen)
    painter.drawLine(x1, y, x2, y)
