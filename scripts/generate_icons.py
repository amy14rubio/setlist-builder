"""
One-off script: draws the square line-icons used by the Match/Add/
Remove/Merge buttons (Charts, YouTube, Review pages) and the Settings
dialog's folder Browse buttons, all at a uniform size and stroke weight,
in the app's own paper-ink color.

Not part of the running app -- run by hand whenever an icon needs to be
regenerated (a different size, color, or stroke weight):

    venv/bin/python3 scripts/generate_icons.py

Output goes to resources/icons/*.png, already sized well above what the
buttons display them at (see playlist_page/charts_page/youtube_page/
review_page's ICON_BUTTON_SIZE) so they stay crisp on a Retina display --
same reasoning as receipt_view.py's device-pixel-ratio rendering.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "resources" / "icons"

_CANVAS_SIZE = 256  # generated resolution; displayed much smaller (see ICON_BUTTON_SIZE)
_INK = (0x33, 0x30, 0x2A, 255)  # matches app/ui/style.py's PAPER_INK ("#33302a")
_STROKE_WIDTH = 16
_MARGIN = 40  # padding from the canvas edge, so strokes never touch the button's own border


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (_CANVAS_SIZE, _CANVAS_SIZE), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def _draw_add() -> Image.Image:
    """A plus sign."""
    image, draw = _canvas()
    center = _CANVAS_SIZE // 2
    half = (_CANVAS_SIZE - 2 * _MARGIN) // 2
    draw.line((center - half, center, center + half, center), fill=_INK, width=_STROKE_WIDTH)
    draw.line((center, center - half, center, center + half), fill=_INK, width=_STROKE_WIDTH)
    return image


def _draw_remove() -> Image.Image:
    """A trash can: a lid, a body, and two vertical ribs."""
    image, draw = _canvas()
    body_left, body_right = _MARGIN + 20, _CANVAS_SIZE - _MARGIN - 20
    body_top, body_bottom = _MARGIN + 50, _CANVAS_SIZE - _MARGIN
    draw.line((body_left, body_top, body_left, body_bottom), fill=_INK, width=_STROKE_WIDTH)
    draw.line((body_right, body_top, body_right, body_bottom), fill=_INK, width=_STROKE_WIDTH)
    draw.line((body_left, body_bottom, body_right, body_bottom), fill=_INK, width=_STROKE_WIDTH)

    lid_left, lid_right = _MARGIN, _CANVAS_SIZE - _MARGIN
    draw.line((lid_left, body_top, lid_right, body_top), fill=_INK, width=_STROKE_WIDTH)
    # The little handle on top of the lid.
    handle_left, handle_right = _CANVAS_SIZE // 2 - 28, _CANVAS_SIZE // 2 + 28
    draw.line((handle_left, body_top, handle_left, body_top - 24), fill=_INK, width=_STROKE_WIDTH)
    draw.line((handle_right, body_top, handle_right, body_top - 24), fill=_INK, width=_STROKE_WIDTH)
    draw.line((handle_left, body_top - 24, handle_right, body_top - 24), fill=_INK, width=_STROKE_WIDTH)

    # Two ribs down the body.
    rib_top, rib_bottom = body_top + 24, body_bottom - 16
    for x in (body_left + (body_right - body_left) // 3, body_left + 2 * (body_right - body_left) // 3):
        draw.line((x, rib_top, x, rib_bottom), fill=_INK, width=_STROKE_WIDTH - 4)
    return image


def _draw_folder() -> Image.Image:
    """A file folder outline -- used for the Settings dialog's Browse buttons."""
    image, draw = _canvas()
    left, right = _MARGIN, _CANVAS_SIZE - _MARGIN
    top, bottom = _MARGIN + 40, _CANVAS_SIZE - _MARGIN
    tab_right = left + (right - left) // 2

    draw.line((left, top, left, bottom), fill=_INK, width=_STROKE_WIDTH)
    draw.line((left, bottom, right, bottom), fill=_INK, width=_STROKE_WIDTH)
    draw.line((right, bottom, right, top + 24), fill=_INK, width=_STROKE_WIDTH)
    draw.line((right, top + 24, tab_right + 20, top + 24), fill=_INK, width=_STROKE_WIDTH)
    draw.line((tab_right + 20, top + 24, tab_right, top), fill=_INK, width=_STROKE_WIDTH)
    draw.line((tab_right, top, left, top), fill=_INK, width=_STROKE_WIDTH)
    return image


def _draw_checkmark() -> Image.Image:
    """A simple checkmark -- used for the YouTube Update button only."""
    image, draw = _canvas()
    draw.line(
        (
            _MARGIN + 10, _CANVAS_SIZE // 2 + 10,
            _CANVAS_SIZE // 2 - 10, _CANVAS_SIZE - _MARGIN - 10,
            _CANVAS_SIZE - _MARGIN - 10, _MARGIN + 10,
        ),
        fill=_INK, width=_STROKE_WIDTH, joint="curve",
    )
    return image


def _draw_match() -> Image.Image:
    """Two curved arrows forming a refresh/re-run loop -- reverted back per direct feedback (the checkmark swap was a misunderstanding)."""
    image, draw = _canvas()
    box = (_MARGIN, _MARGIN, _CANVAS_SIZE - _MARGIN, _CANVAS_SIZE - _MARGIN)

    draw.arc(box, start=-30, end=160, fill=_INK, width=_STROKE_WIDTH)
    draw.arc(box, start=150, end=340, fill=_INK, width=_STROKE_WIDTH)

    # Arrowhead at the end of the top arc (~160 degrees).
    import math
    cx, cy = _CANVAS_SIZE / 2, _CANVAS_SIZE / 2
    r = (_CANVAS_SIZE - 2 * _MARGIN) / 2
    angle = math.radians(160)
    tip_x, tip_y = cx + r * math.cos(angle), cy + r * math.sin(angle)
    draw.polygon(
        [
            (tip_x, tip_y),
            (tip_x - 40, tip_y - 10),
            (tip_x - 10, tip_y + 38),
        ],
        fill=_INK,
    )

    # Arrowhead at the end of the bottom arc (~340 degrees).
    angle2 = math.radians(340)
    tip_x2, tip_y2 = cx + r * math.cos(angle2), cy + r * math.sin(angle2)
    draw.polygon(
        [
            (tip_x2, tip_y2),
            (tip_x2 + 40, tip_y2 + 10),
            (tip_x2 + 10, tip_y2 - 38),
        ],
        fill=_INK,
    )
    return image


def _draw_merge() -> Image.Image:
    """Two overlapping page outlines, representing several docs combined into one."""
    image, draw = _canvas()
    corner = 18

    back_box = (_MARGIN + 34, _MARGIN, _CANVAS_SIZE - _MARGIN, _CANVAS_SIZE - _MARGIN - 34)
    draw.rounded_rectangle(back_box, radius=corner, outline=_INK, width=_STROKE_WIDTH - 4)

    front_box = (_MARGIN, _MARGIN + 34, _CANVAS_SIZE - _MARGIN - 34, _CANVAS_SIZE - _MARGIN)
    # Erase where the front page overlaps the back page's outline, so the
    # front page reads as sitting ON TOP, not just two crossed outlines.
    draw.rectangle(front_box, fill=(0, 0, 0, 0))
    draw.rounded_rectangle(front_box, radius=corner, outline=_INK, width=_STROKE_WIDTH)
    return image


def _draw_schedule() -> Image.Image:
    """A clock face -- used for the new per-tab Schedule buttons."""
    image, draw = _canvas()
    box = (_MARGIN, _MARGIN, _CANVAS_SIZE - _MARGIN, _CANVAS_SIZE - _MARGIN)
    draw.ellipse(box, outline=_INK, width=_STROKE_WIDTH)
    cx = cy = _CANVAS_SIZE // 2
    radius = (_CANVAS_SIZE - 2 * _MARGIN) / 2
    # Hour hand pointing up-ish, minute hand pointing right -- reads
    # clearly as a clock even at small button sizes.
    draw.line((cx, cy, cx, cy - radius * 0.5), fill=_INK, width=_STROKE_WIDTH)
    draw.line((cx, cy, cx + radius * 0.7, cy), fill=_INK, width=_STROKE_WIDTH)
    return image


def _draw_bell(color: tuple = _INK) -> Image.Image:
    """
    A notification bell -- used for the Task Manager button.

    `color` defaults to the app's usual paper-ink color (every other
    icon), but the Task Manager bell sits directly on the cassette's
    dark label row instead of a paper panel, so it's generated a second
    time in white (see main()) to read against that dark background.
    """
    image, draw = _canvas()
    cx = _CANVAS_SIZE // 2
    # Taller/narrower than the first pass, which read as "squished" (96
    # tall x 156 wide -- wider than tall). This one is ~130 tall x 116
    # wide, closer to a real bell's proportions.
    top = _MARGIN + 6
    bell_top = top + 20
    bell_bottom = _CANVAS_SIZE - _MARGIN - 20
    width_at_bottom = _CANVAS_SIZE // 2 - _MARGIN - 30

    # The little loop at the very top.
    draw.arc((cx - 16, top - 10, cx + 16, top + 16), start=200, end=340, fill=color, width=_STROKE_WIDTH - 6)

    # The bell body: a dome that widens toward the bottom.
    draw.arc(
        (cx - width_at_bottom, bell_top, cx + width_at_bottom, bell_bottom),
        start=180, end=360, fill=color, width=_STROKE_WIDTH,
    )
    draw.line((cx - width_at_bottom, (bell_top + bell_bottom) // 2, cx - width_at_bottom - 14, bell_bottom), fill=color, width=_STROKE_WIDTH)
    draw.line((cx + width_at_bottom, (bell_top + bell_bottom) // 2, cx + width_at_bottom + 14, bell_bottom), fill=color, width=_STROKE_WIDTH)
    draw.line((cx - width_at_bottom - 14, bell_bottom, cx + width_at_bottom + 14, bell_bottom), fill=color, width=_STROKE_WIDTH)

    # The clapper hanging below.
    draw.ellipse((cx - 16, bell_bottom + 8, cx + 16, bell_bottom + 40), fill=color)
    return image


def _draw_previous() -> Image.Image:
    """A left-pointing chevron."""
    image, draw = _canvas()
    cx = _CANVAS_SIZE // 2
    draw.line(
        (cx + 30, _MARGIN + 10, cx - 30, _CANVAS_SIZE // 2, cx + 30, _CANVAS_SIZE - _MARGIN - 10),
        fill=_INK, width=_STROKE_WIDTH, joint="curve",
    )
    return image


def _draw_next() -> Image.Image:
    """A right-pointing chevron."""
    image, draw = _canvas()
    cx = _CANVAS_SIZE // 2
    draw.line(
        (cx - 30, _MARGIN + 10, cx + 30, _CANVAS_SIZE // 2, cx - 30, _CANVAS_SIZE - _MARGIN - 10),
        fill=_INK, width=_STROKE_WIDTH, joint="curve",
    )
    return image


def main() -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    checkmark = _draw_checkmark()
    icons = {
        "add.png": _draw_add(),
        "remove.png": _draw_remove(),
        "match.png": _draw_match(),  # stays as the refresh-loop arrows
        "update.png": checkmark,  # YouTube's Update button
        "merge.png": checkmark,  # Charts' combined Download+Merge button
        "previous.png": _draw_previous(),
        "next.png": _draw_next(),
        "schedule.png": _draw_schedule(),
        "folder.png": _draw_folder(),  # Settings dialog's Browse buttons
        # Only the white variant is actually used (the Task Manager bell
        # sits on the cassette's dark label row) -- the dark-ink default
        # was generated for a while but nothing ever referenced it.
        "bell_white.png": _draw_bell(color=(255, 255, 255, 255)),
    }
    for filename, image in icons.items():
        path = _OUTPUT_DIR / filename
        image.save(path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
