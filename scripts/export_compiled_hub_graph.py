#!/usr/bin/env python3
"""Export the real compiled Agent Hub LangGraph to Mermaid and SVG."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agent_hub.studio_graph import graph

ROOT = Path(__file__).resolve().parents[1]
DIAGRAM_DIR = ROOT / "docs" / "diagrams"
MMD_PATH = DIAGRAM_DIR / "07-HUB-COMPILED-LANGGRAPH.mmd"
SVG_PATH = DIAGRAM_DIR / "07-HUB-COMPILED-LANGGRAPH.svg"


def main() -> None:
    DIAGRAM_DIR.mkdir(parents=True, exist_ok=True)
    mermaid = graph.get_graph().draw_mermaid()
    MMD_PATH.write_text(mermaid, encoding="utf-8")
    print(f"Wrote real compiled graph Mermaid to {MMD_PATH}")

    npx = shutil.which("npx")
    if npx is None:
        print("npx not found; Mermaid source written, SVG render skipped.")
        return

    subprocess.run(
        [
            npx,
            "--yes",
            "@mermaid-js/mermaid-cli",
            "-i",
            str(MMD_PATH),
            "-o",
            str(SVG_PATH),
        ],
        check=True,
    )
    print(f"Wrote real compiled graph SVG to {SVG_PATH}")


if __name__ == "__main__":
    main()
