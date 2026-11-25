#!/bin/sh
# Installs the every-minute scheduler as a LaunchAgent (posts approved, due shorts).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="$(sh "$ROOT/scripts/scheduler-label.sh")"
UV="$(PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" command -v uv)"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
sed -e "s|__LABEL__|$LABEL|g" -e "s|__UV__|$UV|g" -e "s|__ROOT__|$ROOT|g" \
  "$ROOT/launchd/social-command-center.plist.template" > "$DST"
launchctl bootout "gui/$(id -u)" "$DST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
echo "Scheduler installed. Log: data/scheduler.log   Remove with: scripts/uninstall-scheduler.sh"
