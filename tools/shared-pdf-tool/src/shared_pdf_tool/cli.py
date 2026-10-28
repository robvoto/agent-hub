"""CLI for deterministic PDF operations without requiring a running Hub/MCP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import inspect_pdf, fill_pdf, render_original, PDFToolError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shared-pdf", description="Inspect or fill the supplied original PDF; no network, email or signature access")
    sub = parser.add_subparsers(dest="cmd", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("original_pdf")
    inspect.add_argument("--out", help="Report JSON path; stdout if omitted")
    inspect.add_argument("--previews", help="New/empty folder to render EVERY original page for inspection")
    fill = sub.add_parser("fill")
    fill.add_argument("original_pdf")
    fill.add_argument("spec_json")
    fill.add_argument("--output", required=True)
    fill.add_argument("--previews", required=True)
    fill.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "inspect":
            result = inspect_pdf(args.original_pdf)
            if args.previews:
                result["original_preview_pages"] = render_original(args.original_pdf, args.previews)
        else:
            result = fill_pdf(args.original_pdf, json.loads(Path(args.spec_json).read_text(encoding="utf-8")), args.output, args.previews)
        report = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        out = args.out if args.cmd == "inspect" else args.report
        if out:
            p = Path(out)
            if p.exists():
                raise PDFToolError("Refusing to overwrite existing report")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(report, encoding="utf-8")
        else:
            print(report, end="")
        return 0
    except (PDFToolError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
