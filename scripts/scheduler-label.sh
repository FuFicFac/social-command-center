#!/bin/sh
# Prints this installation's scheduler label: <general.label_prefix>.social-command-center
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="$(sed -n 's/^label_prefix *= *"\(.*\)"/\1/p' "$ROOT/config.toml" 2>/dev/null | head -1)"
echo "${PREFIX:-local}.social-command-center"
