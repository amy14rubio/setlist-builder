#!/bin/bash
# Installs the background scheduler as a macOS launchd agent, so
# scheduled tasks notify you even when the app isn't open.
#
# Run it once, from anywhere, with the venv already set up (see
# SETUP.md):
#     bash scripts/install_scheduler.sh
#
# To stop and remove it again:
#     bash scripts/install_scheduler.sh --uninstall
#
# Everything is derived from where this script actually lives, so it
# works from any clone, any username, any folder name -- nothing is
# hardcoded. If you move the project folder, just run it again.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.setlistbuilder.scheduler"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/SetlistBuilder"

# launchd's modern bootstrap/bootout commands need the GUI domain for
# the logged-in user; `launchctl load` still works but is deprecated.
DOMAIN="gui/$(id -u)"

unload_if_loaded() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
}

if [ "$1" = "--uninstall" ]; then
    unload_if_loaded
    rm -f "$PLIST"
    echo "Removed the background scheduler. Scheduled tasks will now only"
    echo "be noticed while the app itself is open."
    exit 0
fi

if [ -n "$1" ]; then
    echo "Unknown option: $1 (the only one is --uninstall)"
    exit 1
fi

if [ ! -x "$PROJECT_DIR/venv/bin/python3" ]; then
    echo "No venv found at $PROJECT_DIR/venv -- set that up first (see SETUP.md step 2), then run this again."
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

# 900 seconds = 15 minutes. Don't raise this much without reading
# _REAUTH_LEAD_TIME in scheduler_runner.py: the "your Google login
# expired" prompt is designed to still reach you with real time to
# spare given a 15-minute poll, and a slower poll eats into that
# margin. Lowering it is harmless, just chattier.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/venv/bin/python3</string>
        <string>$PROJECT_DIR/scheduler_runner.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StartInterval</key>
    <integer>900</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/scheduler.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/scheduler.err.log</string>
</dict>
</plist>
EOF

# Boot out any previous copy first, so re-running this after moving the
# project folder replaces the old agent instead of erroring out.
unload_if_loaded
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "Installed: $PLIST"
echo "It runs $PROJECT_DIR/scheduler_runner.py every 15 minutes."
echo
echo "Check it's registered:   launchctl list | grep $LABEL"
echo "Watch what it does:      tail -f $LOG_DIR/scheduler.err.log"
echo "Remove it later:         bash scripts/install_scheduler.sh --uninstall"
echo
echo "The first notification it sends will make macOS ask for permission."
echo "Approve it, or nothing will reach you (System Settings > Notifications)."
