#!/bin/bash
# Auto-restarts Setlist Builder whenever a .py file changes -- the closest
# equivalent to Vite/Electron's live-reload that a PySide6 (Qt Widgets)
# app can have. Not true hot-reload: the app fully restarts on each save,
# so any in-progress state (an open front card, an imported file) resets
# each time -- fine for visual/layout iteration, not for testing a
# multi-step workflow without re-clicking through it.
#
# Usage:
#   ./dev_watch.sh
# (run this AFTER activating your venv: source venv/bin/activate)

watchmedo auto-restart \
    --directory=. \
    --pattern="*.py" \
    --ignore-pattern="venv/*" \
    --recursive \
    --debounce-interval=0.3 \
    -- python main.py
