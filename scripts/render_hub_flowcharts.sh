#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

echo "Rendering 07-HUB-LANGGRAPH-TOOLS.*"
uv run python scripts/generate_hub_langgraph_diagram.py

echo "Rendering 07B-HUB-LANGGRAPH-NODE-FLOW.svg"
npx --yes @mermaid-js/mermaid-cli \
  -i docs/diagrams/07B-HUB-LANGGRAPH-NODE-FLOW.mmd \
  -o docs/diagrams/07B-HUB-LANGGRAPH-NODE-FLOW.svg
