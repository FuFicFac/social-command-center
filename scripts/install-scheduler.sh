#!/bin/sh
# Installs the every-minute scheduler as a LaunchAgent (posts approved, due shorts).
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)/launchd/com.creator.social-command-center.plist"
DST="$HOME/Library/LaunchAgents/com.creator.social-command-center.plist"
cp "$SRC" "$DST"
launchctl bootout "gui/$(id -u)" "$DST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
echo "Scheduler installed. Log: data/scheduler.log   Remove with: scripts/uninstall-scheduler.sh"
