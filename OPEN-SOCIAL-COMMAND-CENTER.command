#!/bin/sh
# Opens the dashboard. If it's already running, just brings it up in the browser.
cd "$(dirname "$0")"
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8787/; then
  open http://127.0.0.1:8787/
  exit 0
fi
exec /Users/you/.local/bin/uv run scc serve
