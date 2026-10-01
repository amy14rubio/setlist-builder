"""
Global styling for Setlist Builder.

Qt doesn't use real CSS -- it has its own similar-but-simpler language
called QSS (Qt Style Sheets). Same basic idea (selectors + properties,
things like :hover and :disabled states) but a much smaller feature set:
no flexbox/grid, no JavaScript, and selectors mostly target widget TYPES
(QPushButton, QLineEdit, ...) rather than arbitrary classes.

This one stylesheet is applied ONCE, globally, to the whole QApplication
in main.py -- every widget in the app picks it up automatically, so
individual pages/widgets never need their own styling code.

ON COLOR VARIABLES
-------------------
QSS has no equivalent of CSS custom properties (no `:root { --x: ... }`
you can reference elsewhere) -- there is no variable syntax in QSS
itself. The THEME PALETTE section below is the substitute: plain Python
constants are the single source of truth, and string.Template fills them
into the QSS text below (using $name placeholders rather than an
f-string, since f-strings would require escaping every literal `{`/`}`
in the QSS -- Template's $-syntax doesn't collide with QSS's braces at
all). Change a color once here and it propagates everywhere that
placeholder is used.
"""

from string import Template

# ======================================================================
# THEME PALETTE -- the "$name" placeholders in the templates below pull
# from here. This is the one place to change a color.
# ======================================================================

# Neutral app-wide theme (STYLESHEET) -- the default look for every
# widget that isn't inside a "#ledgerPage" (paper-themed) container.
APP_BG = "#f5f5f5"
APP_TEXT = "#333333"
APP_SURFACE = "#ffffff"
APP_BORDER = "#cccccc"
APP_BORDER_LIGHT = "#e0e0e0"
APP_HOVER = "#ececec"
APP_PRESSED = "#dcdcdc"
APP_DISABLED_BG = "#f0f0f0"
APP_DISABLED_TEXT = "#aaaaaa"
APP_FOCUS_BORDER = "#999999"
APP_GRIDLINE = "#e5e5e5"
APP_HEADER_BG = "#e8e8e8"
APP_HEADER_BORDER = "#d5d5d5"
APP_SCROLLBAR_TRACK = "#b8b8b8"
APP_SCROLLBAR_HANDLE = "#707070"
APP_SCROLLBAR_HANDLE_HOVER = "#555555"

# Paper/ledger theme ("#ledgerPage" containers: Review/Build/YouTube/
# Charts, and PlaylistPage's in-place "drive"/"youtube"/"logic pro" cards
# -- see the comment on PAPER_LEDGER_TEMPLATE for why the cassette
# Playlist page itself is excluded).
PAPER_BG = "#f0efed"
PAPER_SURFACE = "#f7f7f7"
PAPER_SURFACE_TRANSPARENT = "transparent"
PAPER_HOVER = "#ededed"
PAPER_SELECTION_BG = "#c9c9c9"
PAPER_INK = "#33302a"
PAPER_BORDER = "#242424"
PAPER_FONT_FAMILY = '"Garogier", Georgia, "Times New Roman", serif'


APP_TEMPLATE = Template("""
QWidget {
    background-color: $app_bg;
    color: $app_text;
    font-size: 18px;
    padding: 0px;
}

QMainWindow, QDialog {
    background-color: $app_bg;
}

QLabel {
    background: transparent;
}

QPushButton {
    background-color: $app_surface;
    border: 1px solid $app_border;
    border-radius: 5px;
    padding: 6px 14px;
    color: $app_text;
}

QPushButton:hover {
    background-color: $app_hover;
}

QPushButton:pressed {
    background-color: $app_pressed;
}

QPushButton:disabled {
    background-color: $app_disabled_bg;
    color: $app_disabled_text;
    border: 1px solid $app_border_light;
}

QLineEdit, QComboBox, QSpinBox, QTextEdit {
    background-color: $app_surface;
    border: 1px solid $app_border;
    border-radius: 5px;
    padding: 4px 6px;
}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {
    border: 1px solid $app_focus_border;
}

QComboBox::drop-down {
    border: none;
    width: 20px;
}

QTableWidget {
    border: none;

    gridline-color: $app_gridline;
}

QHeaderView::section {
    background-color: $app_header_bg;
    color: $app_text;
    border: none;
    font-weight: bold;
}

QListWidget {
    background-color: $app_surface;
    border: none;
}

/* Thin, minimalist scrollbars everywhere: no arrow buttons, a slim
   rounded handle in a subtle light-gray track.

   The track is a deliberate light gray, NOT "transparent" -- Qt's style
   sheet engine doesn't reliably make a QScrollBar's empty track
   (::add-page/::sub-page) genuinely transparent once any app-wide
   QWidget background-color rule exists (confirmed: this isn't a typo or
   a specificity issue -- it survives explicit "background: transparent"
   on every relevant selector, !important, ID selectors, direct palette
   overrides, and even a QProxyStyle that skips painting those
   subcontrols entirely; the fill still leaks through). Rather than ship
   a stray white/gray block nobody asked for, the track gets an
   intentional light-gray fill instead, so the whole scrollbar reads as
   one deliberate two-tone shape. */
QScrollBar:vertical {
    background: $app_scrollbar_track;
    width: 8px;
    margin: 0px;
    border-radius: 4px;
}

QScrollBar:horizontal {
    background: $app_scrollbar_track;
    height: 8px;
    margin: 0px;
    border-radius: 4px;
}

QScrollBar::handle {
    background: $app_scrollbar_handle;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    min-height: 24px;
}

QScrollBar::handle:horizontal {
    min-width: 24px;
}

QScrollBar::handle:hover {
    background: $app_scrollbar_handle_hover;
}

QScrollBar::add-line, QScrollBar::sub-line {
    width: 0px;
    height: 0px;
    border: none;
    background: transparent;
}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
    border: none;
}
""")

# Applied only to the "#ledgerPage" wrapper containers built by
# MainWindow._wrap_with_navigation() (Review/Build/YouTube) and
# PlaylistPage's in-place "drive" card -- the cassette Playlist page
# itself must NOT pick this up, since it's illustrated, not themed. A
# flat paper-cream background + serif type + dotted "perforation" rules
# stand in for the receipt's paper texture -- tiling the actual
# (non-seamless) photographic texture as a repeating QSS background-
# image would look obviously repeated, so this is a deliberate
# simplification rather than a literal texture reuse (PlaylistPage's
# own drive card instead paints the real texture directly, see
# PaperPanel -- this stylesheet only needs to leave ITS OWN background
# transparent there so that texture shows through).
PAPER_LEDGER_TEMPLATE = Template("""
#ledgerPage {
    background-color: $paper_surface_transparent;
}

#ledgerPage QLabel,
#ledgerPage QPushButton,
#ledgerPage QComboBox,
#ledgerPage QLineEdit,
#ledgerPage QTableWidget,
#ledgerPage QHeaderView::section,
#ledgerPage QGroupBox,
#ledgerPage QTextEdit {
    font-family: $paper_font_family;
}

/* Tighter padding than the app-wide QPushButton rule (padding: 6px 14px)
   -- these panels are fixed-width (matched to the receipt's own rect),
   so button rows have much less room to work with than the main app's
   full-width pages, and the wider padding was pushing rows like Charts'
   Download/Merge past the panel's right edge.

   Transparent, not $paper_surface: matches the initial receipt, which
   has no solid-colored boxes at all -- just ink drawn straight onto the
   paper texture. The border alone is enough to read as a button. */
#ledgerPage QPushButton {
    background-color: $paper_surface_transparent;
    border: 1px solid $paper_border;
    color: $paper_ink;
    padding: 4px 8px;
}

#ledgerPage QPushButton:hover {
    background-color: $paper_hover;
}

/* Transparent, not $paper_surface: this table sits directly on top of
   PlaylistPage's painted paper texture (PaperPanel) -- an opaque body
   here would hide that texture behind a flat color. */
#ledgerPage QTableWidget {
    background-color: $paper_surface_transparent;
    border: none;
    gridline-color: transparent;
}

#ledgerPage QTableWidget::item {
    background-color: $paper_surface_transparent;
    border-bottom: 1px dotted $paper_border;
}

/* The one deliberate exception to "transparent so the paper shows
   through": a selected row still needs its own visible highlight,
   otherwise clicking a row gives no feedback at all. */
#ledgerPage QTableWidget::item:selected {
    background-color: $paper_selection_bg;
    color: $paper_ink;
}

#ledgerPage QHeaderView::section {
    background-color: $paper_surface_transparent;
    color: $paper_ink;
    border: none;
    border-bottom: 1px dotted $paper_border;
    font-weight: bold;
}

#ledgerPage QComboBox,
#ledgerPage QLineEdit {
    background-color: $paper_surface;
    border: 1px solid $paper_border;
}

/* More specific than the QComboBox rule above, so it wins for combos
   INSIDE the table (the "Matched Chart" cells) without touching combos
   elsewhere (e.g. the "Target Folder" dropdown), which should keep
   their solid paper-surface look. */
#ledgerPage QTableWidget QComboBox {
    background-color: $paper_surface_transparent;
    border: none;
}

""")


def load_stylesheet() -> str:
    """Return the global QSS stylesheet text, with the theme palette above filled in."""
    app_stylesheet = APP_TEMPLATE.substitute(
        app_bg=APP_BG,
        app_text=APP_TEXT,
        app_surface=APP_SURFACE,
        app_border=APP_BORDER,
        app_border_light=APP_BORDER_LIGHT,
        app_hover=APP_HOVER,
        app_pressed=APP_PRESSED,
        app_disabled_bg=APP_DISABLED_BG,
        app_disabled_text=APP_DISABLED_TEXT,
        app_focus_border=APP_FOCUS_BORDER,
        app_gridline=APP_GRIDLINE,
        app_header_bg=APP_HEADER_BG,
        app_header_border=APP_HEADER_BORDER,
        app_scrollbar_track=APP_SCROLLBAR_TRACK,
        app_scrollbar_handle=APP_SCROLLBAR_HANDLE,
        app_scrollbar_handle_hover=APP_SCROLLBAR_HANDLE_HOVER,
    )
    paper_stylesheet = PAPER_LEDGER_TEMPLATE.substitute(
        paper_bg=PAPER_BG,
        paper_surface=PAPER_SURFACE,
        paper_surface_transparent=PAPER_SURFACE_TRANSPARENT,
        paper_hover=PAPER_HOVER,
        paper_selection_bg=PAPER_SELECTION_BG,
        paper_ink=PAPER_INK,
        paper_border=PAPER_BORDER,
        paper_font_family=PAPER_FONT_FAMILY,
    )
    return app_stylesheet + paper_stylesheet
