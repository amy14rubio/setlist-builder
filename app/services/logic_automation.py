"""
Native Logic Pro automation for Setlist Builder -- builds a rehearsal
project directly from a Build Manifest, with no Keyboard Maestro
license required.

HOW THIS WORKS
---------------
Every action (keystrokes, waiting for a window, clicking a button) is
built on AppleScript's "System Events" application, which can control
any app's UI -- the same underlying mechanism Keyboard Maestro itself
uses internally. This talks to it directly via `osascript`.

Called from the UI via build_page.py, which runs
build_full_rehearsal_project() on a background QThread -- a full
setlist run takes several minutes of exclusive keyboard/screen control,
far too long to block the UI event loop.

STOPPING A RUN IN PROGRESS
----------------------------
Create the file `~/.setlist_builder_stop` at any time (from a separate
Terminal window, a macOS Shortcut bound to a keyboard shortcut that runs
`touch ~/.setlist_builder_stop`, or the Cancel button in the app, which
calls request_cancel() below) and the run will stop within about a
second. No special permissions needed for this -- it's just a
file-existence check, not global keyboard monitoring.

BEFORE RUNNING ANYTHING
-------------------------
1. This only works on macOS (uses `osascript`).
2. Whatever runs this (the Setlist Builder app itself) needs
   Accessibility AND Automation permission to control Logic Pro and
   System Events. macOS should prompt for this the first time; if not,
   grant it manually in System Settings > Privacy & Security >
   Accessibility, and also > Automation.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


# ======================================================================
# STOPPING A RUN IN PROGRESS
# ======================================================================

# Create this file from ANYWHERE (a separate Terminal window, a macOS
# Shortcut bound to a keyboard shortcut, anything that can run a shell
# command) to stop the script. No special permission needed -- checking
# for a file's existence isn't a privileged operation the way global
# keyboard monitoring is.
_STOP_FILE = Path.home() / ".setlist_builder_stop"


class AutomationCancelled(Exception):
    """Raised when the user creates the stop file."""


def request_cancel() -> None:
    """
    Create the stop file, requesting that a running
    build_full_rehearsal_project() call stop as soon as it next checks
    (within about a second). This is the same file-existence mechanism
    documented above for external cancellation (a separate Terminal, a
    macOS Shortcut) -- the Cancel button in the UI just uses this
    identical path, rather than a separate in-process mechanism.
    """
    _STOP_FILE.touch()


def check_cancelled() -> None:
    """Raise AutomationCancelled if the stop file exists (and clean it up)."""
    if _STOP_FILE.exists():
        _STOP_FILE.unlink()
        raise AutomationCancelled(f"Cancelled by user (stop file: {_STOP_FILE}).")


def cancellable_sleep(seconds: float) -> None:
    """
    Like time.sleep(), but checks the stop file every ~0.1s instead of
    blocking uninterruptibly for the whole duration -- so creating the
    stop file stops the script within a fraction of a second, not up to
    `seconds` later. Used everywhere in place of time.sleep().
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        check_cancelled()
        time.sleep(min(0.1, max(0.0, deadline - time.time())))
    check_cancelled()


# ======================================================================
# LOW-LEVEL PRIMITIVES
# Every one of these talks to "System Events" via JavaScript for
# Automation (JXA).
# ======================================================================

def run_jxa(script: str, timeout: float = 10) -> str:
    """
    Run a JavaScript for Automation (JXA) script and return its stdout,
    stripped of trailing whitespace. Raises RuntimeError with the real
    error text if the script fails.

    `timeout` bounds this ONE subprocess call. Without it, a single
    stuck osascript invocation (Logic Pro's main thread genuinely busy,
    or an unexpected dialog silently waiting for input) can block
    forever -- and since polling loops like
    wait_until_window_title_contains() only check THEIR OWN deadline
    between calls to this function, one hung call defeats that outer
    deadline entirely.
    """
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"JXA script did not respond within {timeout}s -- Logic Pro may be "
            f"busy, or an unexpected dialog may be waiting for input."
        )
    if result.returncode != 0:
        raise RuntimeError(f"JXA script failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _jxa_string_literal(value: str) -> str:
    """
    Escapes `value` for safe interpolation inside a double-quoted JXA
    (JavaScript) string literal -- every function below that builds a
    script via an f-string must pass dynamic text through this first.

    Not currently reachable by untrusted input (every call site today
    passes fixed literals like "Logic Pro" or a single keyboard key --
    real song/file text always goes through paste_text()'s
    pbcopy-then-Cmd+V path instead, which never touches a JXA string at
    all), but escaping unconditionally here is what keeps that true if
    this code is ever extended to accept a dynamic name. Mirrors
    notification_service.py's _applescript_string, same reasoning.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def activate_app(app_name: str) -> None:
    """Bring an application to the front, launching it if needed."""
    run_jxa(f'Application("{_jxa_string_literal(app_name)}").activate();')


# Special (non-character) keys, mapped to their macOS virtual key codes.
# Needed because System Events treats "type this letter" and "press this
# named key" as two different operations (keystroke vs. key code).
_SPECIAL_KEY_CODES = {
    "return": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "escape": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "forward_slash": 44,  # for Cmd+Option+/ (Forward by Division Value)
}


def simulate_keystroke(key: str, modifiers: list[str] | None = None) -> None:
    """
    Send a single keystroke to whatever app is currently frontmost.

    `key` is either a literal character (e.g. "c", "d", "a") or one of
    the special key names in _SPECIAL_KEY_CODES (e.g. "return", "up").
    `modifiers` is any combination of "command", "shift", "option",
    "control".

    Examples:
        simulate_keystroke("c", ["command"])           # Cmd+C
        simulate_keystroke("return")                     # plain Return
        simulate_keystroke("up", ["shift"])              # Shift+Up Arrow
        simulate_keystroke("d", ["command", "shift"])    # Cmd+Shift+D
    """
    modifiers = modifiers or []
    mods_js = ", ".join(f'"{_jxa_string_literal(m)} down"' for m in modifiers)

    key_lower = key.lower()
    if key_lower in _SPECIAL_KEY_CODES:
        code = _SPECIAL_KEY_CODES[key_lower]
        script = f'''
            var se = Application("System Events");
            se.keyCode({code}, {{using: [{mods_js}]}});
        '''
    else:
        script = f'''
            var se = Application("System Events");
            se.keystroke("{_jxa_string_literal(key)}", {{using: [{mods_js}]}});
        '''
    run_jxa(script)


def paste_text(text: str) -> None:
    """
    Put `text` on the clipboard, then simulate Cmd+V to paste it.
    """
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    simulate_keystroke("v", ["command"])


def front_window_title(app_name: str) -> str | None:
    """Return the frontmost window's title for the given app, or None."""
    script = f'''
        var se = Application("System Events");
        var proc = se.processes["{_jxa_string_literal(app_name)}"];
        proc.windows[0].title();
    '''
    try:
        return run_jxa(script)
    except RuntimeError:
        return None


def wait_until_window_title_contains(app_name: str, substring: str, timeout: float = 30) -> bool:
    """Poll until the app's front window title contains `substring`, or give up after `timeout` seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = front_window_title(app_name)
        if title and substring in title:
            return True
        cancellable_sleep(0.3)
    return False


def button_exists(app_name: str, button_name_fragment: str) -> bool:
    """
    Check whether a button whose name, title, OR description CONTAINS
    `button_name_fragment` exists in the front window.

    Checks multiple attributes (not just .name()) because some buttons
    return null for .name() via raw System Events even though they're
    real, clickable buttons -- checking name, title, and description
    together catches those too. Substring match, not exact equality,
    since macOS often renders button labels with a curly/typographic
    apostrophe (') rather than a plain straight one (').

    NOTE: this reliably works for Logic's own native dialogs (e.g.
    "Don't Close"). It does NOT reliably work for the shared system
    Open File panel or the "Which track stack type" dialog -- both
    proved unreadable via System Events even with this broader check,
    likely a macOS accessibility-exposure limitation specific to those
    panels. Those two steps use timed pauses instead (see _import_file
    and the grouping step in build_one_song).
    """
    script = f'''
        var se = Application("System Events");
        var proc = se.processes["{_jxa_string_literal(app_name)}"];
        var win = proc.windows[0];
        var allButtons = win.buttons();
        var found = false;
        for (var i = 0; i < allButtons.length; i++) {{
            var b = allButtons[i];
            var text = "";
            try {{ text += (b.name() || ""); }} catch (e) {{}}
            try {{ text += (b.title() || ""); }} catch (e) {{}}
            try {{ text += (b.description() || ""); }} catch (e) {{}}
            if (text.indexOf("{_jxa_string_literal(button_name_fragment)}") !== -1) {{
                found = true;
                break;
            }}
        }}
        found;
    '''
    try:
        return run_jxa(script) == "true"
    except RuntimeError:
        return False


def wait_until_button_exists(app_name: str, button_name_fragment: str, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if button_exists(app_name, button_name_fragment):
            return True
        cancellable_sleep(0.3)
    return False


def press_button(app_name: str, button_name_fragment: str) -> None:
    """Click the first button whose name, title, OR description CONTAINS `button_name_fragment`."""
    script = f'''
        var se = Application("System Events");
        var proc = se.processes["{_jxa_string_literal(app_name)}"];
        var win = proc.windows[0];
        var allButtons = win.buttons();
        for (var i = 0; i < allButtons.length; i++) {{
            var b = allButtons[i];
            var text = "";
            try {{ text += (b.name() || ""); }} catch (e) {{}}
            try {{ text += (b.title() || ""); }} catch (e) {{}}
            try {{ text += (b.description() || ""); }} catch (e) {{}}
            if (text.indexOf("{_jxa_string_literal(button_name_fragment)}") !== -1) {{
                b.click();
                break;
            }}
        }}
    '''
    run_jxa(script)


def open_file(path: str) -> None:
    """Open a file with its default application (e.g. a .logicx project)."""
    subprocess.run(["open", path], check=True)


def read_manifest(manifest_path: str) -> dict:
    """Read and parse the Build Manifest JSON."""
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def derive_track_name(file_path: str) -> str:
    """
    Take the filename (after the last "/"), then drop everything from
    the last "." onward (the extension), leaving e.g.
    "3 BGVS + Choir - Yeshua MSM".
    """
    filename = file_path.rsplit("/", 1)[-1]
    if "." in filename:
        return filename.rsplit(".", 1)[0]
    return filename


def validate_song_files_exist(song: dict) -> list[str]:
    """
    Check that every audio file path this song references ACTUALLY
    EXISTS on disk, before we ever touch Logic Pro's UI.

    Returns a list of problem descriptions (empty list = all good).
    This exists specifically because a stale/mismatched path (e.g. the
    manifest was generated before a folder got reorganized) produces a
    confusing, hard-to-diagnose freeze deep inside Logic's file picker --
    far better to catch it here, instantly, with a clear message.
    """
    problems = []
    for field in ("clickFile", "guideFile"):
        path = song.get(field)
        if path and not Path(path).exists():
            problems.append(f"{field} does not exist on disk: {path}")

    for i, path in enumerate(song.get("stackFiles", [])):
        if not Path(path).exists():
            problems.append(f"stackFiles[{i}] does not exist on disk: {path}")

    return problems


def validate_manifest_files_exist(manifest: dict) -> bool:
    """
    Validate every song in the manifest. Logs a clear report and
    returns False if ANY file is missing.
    """
    all_ok = True
    for song in manifest["songs"]:
        problems = validate_song_files_exist(song)
        if problems:
            all_ok = False
            logger.error("Problems found for '%s':", song["title"])
            for problem in problems:
                logger.error("    %s", problem)
    if all_ok:
        logger.info("All file paths in the manifest exist on disk. Safe to proceed.")
    return all_ok


def require(condition: bool, message: str) -> None:
    """
    Raise a clear, immediate RuntimeError if `condition` is False,
    instead of letting the script silently continue through an
    already-broken state.
    """
    if not condition:
        raise RuntimeError(message)


def _import_file(file_path: str) -> None:
    """
    Paste the file path and confirm the import with two timed Returns.

    The shared system Open File panel's actual controls aren't
    reliably readable via System Events (confirmed by testing), so this
    uses a timed pause rather than detecting an "Open" button.

    Selects all existing text in the "Go to Folder" field with Cmd+A
    BEFORE pasting -- that field can retain whatever text was typed into
    it the last time it was opened, so without clearing it first, a new
    path could get inserted alongside old leftover text rather than
    cleanly replacing it, producing a garbled/unresolvable path.

    Logs the exact path being pasted every time, for troubleshooting.
    """
    logger.debug("    (pasting exactly this path: %s)", file_path)
    simulate_keystroke("a", ["command"])  # clear any leftover text in the field first
    paste_text(file_path)
    cancellable_sleep(1.5)
    simulate_keystroke("return")
    cancellable_sleep(1.5)
    simulate_keystroke("return")
    found_tracks = wait_until_window_title_contains("Logic Pro", "Tracks", timeout=15)
    require(found_tracks, f"'Tracks' window never reappeared after importing: {file_path}")


def build_one_song(song: dict, is_first_song: bool) -> None:
    """Run the complete import sequence for a single song."""
    logger.info("  Building: %s", song["title"])
    check_cancelled()

    # --- Tempo placeholder + positioning ---
    activate_app("Logic Pro")
    if is_first_song:
        simulate_keystroke("t", ["command", "option"])  # create tempo point
    else:
        logger.info("  Homing to Click track and positioning...")
        for _ in range(10):
            simulate_keystroke("up")
        simulate_keystroke("return", ["option"])  # Go to End of Last Region
        for _ in range(60):
            simulate_keystroke("forward_slash", ["command", "option"])
        simulate_keystroke("t", ["command", "option"])  # create tempo point

    cancellable_sleep(1)

    # --- Click track ---
    if song.get("clickFile"):
        logger.info("  Importing Click file...")
        activate_app("Logic Pro")
        simulate_keystroke("i", ["command", "shift"])
        simulate_keystroke("g", ["command", "shift"])
        cancellable_sleep(1)  # let the Go to Folder sheet actually appear/gain focus first
        _import_file(song["clickFile"])

    # --- Guide track ---
    if song.get("guideFile"):
        logger.info("  Importing Guide file...")
        activate_app("Logic Pro")
        simulate_keystroke("down")
        simulate_keystroke("i", ["command", "shift"])
        simulate_keystroke("g", ["command", "shift"])
        cancellable_sleep(1)  # let the Go to Folder sheet actually appear/gain focus first
        _import_file(song["guideFile"])

    # --- Stack files: import each one onto its own new track, then
    #     rename that track from the file path directly ---
    stack_files = song.get("stackFiles", [])
    for i, stack_file_path in enumerate(stack_files):
        check_cancelled()
        logger.info("  Importing stack file %d/%d...", i + 1, len(stack_files))
        activate_app("Logic Pro")
        track_name = derive_track_name(stack_file_path)

        simulate_keystroke("a", ["command", "option"])  # new empty audio track
        cancellable_sleep(1)
        simulate_keystroke("i", ["command", "shift"])
        simulate_keystroke("g", ["command", "shift"])
        cancellable_sleep(1)  # let the Go to Folder sheet actually appear/gain focus first
        _import_file(stack_file_path)

        # Rename the newly-created track
        simulate_keystroke("return", ["shift"])
        paste_text(track_name)
        simulate_keystroke("return")

    # --- Group the stack files into a Track Stack ---
    if stack_files:
        logger.info("  Grouping stack into a Track Stack...")
        activate_app("Logic Pro")
        for _ in range(len(stack_files) - 1):
            simulate_keystroke("up", ["shift"])
        simulate_keystroke("d", ["command", "shift"])
        # The "Which track stack type" dialog's buttons also proved
        # unreadable via System Events (same class of problem as the
        # Open File panel) -- timed pause instead of button detection.
        cancellable_sleep(2)
        simulate_keystroke("return")
        cancellable_sleep(1)
        simulate_keystroke("return", ["shift"])
        paste_text(song["title"])
        simulate_keystroke("return")
        simulate_keystroke("left", ["command", "control"])  # collapse

    # --- Tempo/marker copy from the original project -- LAST, after
    #     everything else for this song is done ---
    if song.get("logicProject"):
        logger.info("  Opening original project for tempo/marker copy...")
        open_file(song["logicProject"])
        found_dont_close = wait_until_button_exists("Logic Pro", "Don", timeout=15)
        require(found_dont_close, "'Don't Close' button never appeared after opening the original project.")
        press_button("Logic Pro", "Don")
        if song.get("logicProjectWindowTitle"):
            found_original = wait_until_window_title_contains(
                "Logic Pro", song["logicProjectWindowTitle"], timeout=15
            )
            require(found_original, f"Original project window never appeared: {song['logicProjectWindowTitle']}")

        cancellable_sleep(1)
        simulate_keystroke("c", ["command"])  # Copy whatever's already selected
        simulate_keystroke("w", ["command"])  # Close the original project
        found_untitled = wait_until_window_title_contains("Logic Pro", "Untitled", timeout=15)
        require(found_untitled, "Setlist project window ('Untitled') never reappeared after closing.")

        logger.info("  Re-activating Logic Pro before pasting tempo/markers...")
        activate_app("Logic Pro")
        cancellable_sleep(1)
        simulate_keystroke("v", ["command"])  # Paste into the Setlist project

    logger.info("  Finished: %s", song["title"])


def build_full_rehearsal_project(manifest_path: str) -> None:
    """
    Build every song in the given Build Manifest into a running Logic
    Pro project. This is the single public entry point for the whole
    automation -- called by build_page.py on a background thread.

    Validates every file path in the manifest BEFORE touching Logic
    Pro's UI at all (see validate_manifest_files_exist()), raising
    RuntimeError immediately if anything's missing rather than starting
    a run that would get stuck partway through.

    Raises
    ------
    RuntimeError
        If validation fails, or if Logic Pro's UI doesn't respond the
        way this automation expects at any step (see require() and the
        wait_until_* helpers).
    AutomationCancelled
        If the stop file is created at any point during the run (see
        request_cancel() / check_cancelled()).
    """
    _STOP_FILE.unlink(missing_ok=True)  # clear any leftover stop signal from a previous run

    logger.info("--- Full setlist run ---")
    manifest = read_manifest(manifest_path)

    if not validate_manifest_files_exist(manifest):
        raise RuntimeError(
            "Cannot proceed -- fix the missing file paths above first "
            "(likely: regenerate the manifest with the correct Desktop "
            "Output Folder in Settings)."
        )

    activate_app("Logic Pro")
    cancellable_sleep(1)

    for index, song in enumerate(manifest["songs"]):
        check_cancelled()
        logger.info("Building song %d/%d: %s", index + 1, len(manifest["songs"]), song["title"])
        build_one_song(song, is_first_song=(index == 0))

    logger.info("Full run complete.")