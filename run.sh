#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-telegram}"

case "$MODE" in
  chat)
    uv run agent-hub chat "${@:2}"
    ;;
  telegram)
    uv run agent-hub telegram "${@:2}"
    ;;
  *)
    echo "Usage: $0 [chat|telegram] [--verbose|--debug|--log-level LEVEL]"
    exit 1
    ;;
esac
