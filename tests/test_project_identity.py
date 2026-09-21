"""Identity checks for the Agent Hub rename."""

from __future__ import annotations

from pathlib import Path
from tomllib import loads

from agent_hub.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]


def test_project_metadata_uses_agent_hub_script_name() -> None:
    pyproject = loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert project["name"] == "agent-hub"
    assert project["scripts"] == {"agent-hub": "agent_hub.cli:main"}


def test_cli_parser_uses_agent_hub_prog_name() -> None:
    parser = build_parser()

    assert parser.prog == "agent-hub"
    assert parser.parse_args(["chat"]).command == "chat"
    assert parser.parse_args(["telegram"]).command == "telegram"


def test_tracked_project_files_do_not_reference_retired_identity() -> None:
    tracked_files = [ROOT / "README.md", ROOT / "src" / "agent_hub" / "cli.py"]
    tracked_files.extend(sorted((ROOT / "docs").rglob("*")))
    tracked_files.extend(sorted((ROOT / ".agents" / "skills").rglob("SKILL.md")))

    retired_identity = "ar" + "my"

    for path in tracked_files:
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8")
        assert retired_identity not in text.lower(), path
