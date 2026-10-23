#!/bin/sh
DST="$HOME/Library/LaunchAgents/com.creator.social-command-center.plist"
launchctl bootout "gui/$(id -u)" "$DST" 2>/dev/null || true
rm -f "$DST"
echo "Scheduler removed."
