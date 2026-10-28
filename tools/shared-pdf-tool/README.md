# Shared PDF Tool (0.1.1 proof of concept)

Strict deterministic operations on the **original** PDF: inspect, fill existing AcroForm widgets or manually mapped locations in static/scanned PDFs, structurally validate and render **every page**. Uses PyMuPDF. No network, Gmail, stored personal data or direct access to signatures.

This is an independently importable Python package, not tied to LangGraph, Agent Hub, OpenCode or MCP internals. Initial CLI interface is suitable for those callers that can install and run the package locally. Cloud agents, including ChatGPT, must have the code available and be granted their own file access; a repository file is **not** a remotely callable hosted tool.

## Commands

```sh
python -m pip install -e .
shared-pdf inspect original.pdf --out inspection.json
shared-pdf fill original.pdf reviewed-spec.json --output filled.pdf --previews previews/ --report report.json
```

Reviewed spec shape (rectangles in PyMuPDF top-left-origin PDF points, NOT pixel coordinates):

```json
{
  "source_sha256": "sha256 of original PDF from inspect",
  "expected_page_count": 1,
  "required_fields": ["first_name", "accept"],
  "entries": [
    {"name": "first_name", "kind": "field", "value": "Ada"},
    {"name": "accept", "kind": "check", "page": 1, "rect": [100, 150, 113, 163], "value": true}
  ]
}
```

- A `field` uses the exact original AcroForm field name; it refuses arbitrary missing names.
- `text` and `check` require explicit page and reviewed box in the original. No automatic guessing or layout re-creation.
- `required_fields` must list every field deemed mandatory by the verified document/user requirements. There is no safe general rule to infer semantic requirements from PDF pixels.
- Failing placement, missing mandatory values, unknown fields, box outside page, overlap with existing text, or page mismatch stops without output.
- Static text entries are re-extracted **from the saved output and clipped to their reviewed boxes**; incorrect or missing glyphs fail validation rather than silently passing.
- Output is `NEEDS_VISUAL_REVIEW` until **a human or separate genuinely authorized viewer reviews every page**. A successful render is not a guarantee of aesthetic or legal correctness.
- No signature application, cryptographic digital signing, Gmail dispatch, or password-protected PDF processing in MVP. Those require separate approvals and broker integration.
- Output and source must be separate; no overwrite allowed. Reports never contain supplied field values.

## Required next acceptance work

1. Compare printed appearances in at least two renderers for real PDFs and add layout-sensitive assertions.
2. Cover multi-line text, radio/button groups and choice fields with verified appearance values.
3. Test the previously failed TPG original (not accessible from the provided old public URL) with **private** sample data and the exact mandatory section checklist.
4. Add explicit signer approval and target-document digest binding before signature placement; separate signing from drafting.
5. Publish a tested installation/adapter path for Agent Hub/OpenCode, and a separately authorized route for ChatGPT cloud use.

This is only a standalone engine slice, not the shared execution broker or final production release. Track integration and acceptance in Agent Hub backlog rather than a second ledger.
