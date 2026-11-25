#!/bin/sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST="$HOME/Library/LaunchAgents/$(sh "$ROOT/scripts/scheduler-label.sh").plist"
launchctl bootout "gui/$(id -u)" "$DST" 2>/dev/null || true
rm -f "$DST"
echo "Scheduler removed."
