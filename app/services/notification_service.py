"""
Notification service for Setlist Builder.

Fires native macOS notification banners via `osascript` -- the
background scheduler check (see scheduler_runner.py) uses this to tell
you a task is ready for approval, or that a Google login needs
refreshing, without needing the Qt app to be open at all (osascript
talks to macOS directly, not through this app's own window).
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

# launchd runs background processes with a minimal PATH
# (/usr/bin:/bin:/usr/sbin:/sbin -- confirmed via `launchctl print`),
# which does NOT include Homebrew's /opt/homebrew/bin -- a bare
# "terminal-notifier" command name silently failed with
# FileNotFoundError the first time this ran under launchd (invisible,
# since the caller redirects its own stderr to a log file).
# shutil.which() covers being run by hand (a normal interactive shell's
# PATH already has Homebrew on it); the explicit fallback paths cover
# being run by launchd.
_TERMINAL_NOTIFIER_CANDIDATES = (
    "/opt/homebrew/bin/terminal-notifier",  # Apple Silicon Homebrew
    "/usr/local/bin/terminal-notifier",  # Intel Homebrew
)


def find_terminal_notifier() -> str:
    """Absolute path to terminal-notifier, or raise if it isn't installed."""
    found = shutil.which("terminal-notifier")
    if found:
        return found
    for candidate in _TERMINAL_NOTIFIER_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError(
        "terminal-notifier isn't installed (or isn't on PATH) -- install it with 'brew install terminal-notifier'."
    )


def ask_via_notification(title: str, message: str, actions: list[str]) -> str:
    """
    Show a notification with clickable action buttons and BLOCK until
    one is clicked, returning the clicked button's title.

    Blocks indefinitely (terminal-notifier's own `-action` behavior:
    "Waits for a response and prints the chosen title"), which is why
    every caller runs as its own detached process rather than inside
    the short-lived scheduler run -- see notification_action_listener.py.

    Returns "" if the notification was dismissed rather than answered
    (terminal-notifier reports "@CLOSED"/"@TIMEOUT" in that case), so
    callers can treat "no decision" uniformly.
    """
    result = subprocess.run(
        [find_terminal_notifier(), "-title", title, "-message", message, "-action", ",".join(actions)],
        capture_output=True,
        text=True,
    )
    choice = result.stdout.strip()
    return choice if choice in actions else ""


def send_notification(title: str, message: str) -> bool:
    """
    Show a native macOS notification banner.

    Returns True on success, False (logged, never raised) if osascript
    itself fails -- a failed notification shouldn't crash the
    background check that's calling this.
    """
    script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)
        return True
    except Exception as error:
        logger.error("Failed to send notification (%s): %s", title, error)
        return False


def _applescript_string(text: str) -> str:
    """
    Quote `text` as an AppleScript string literal.

    Escapes backslashes and double quotes so a song title (or anything
    else derived from imported data) can't break out of the quoted
    string and inject extra AppleScript into the -e argument.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
