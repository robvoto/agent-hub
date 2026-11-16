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

## Operational lessons from real-form reviews (17 November 2026)

These are **review rules for callers and future signature support**, not claims that the v0.1.1 CLI can sign or email documents.

- **Find the source first.** When the requester identifies an email attachment or existing file, retrieve that exact original and preserve it. Never recreate the form from a screenshot or silently substitute an earlier draft.
- **Reuse reviewed content.** Change only the requested field, date, or signature. Keep all other pages, layout, text, and metadata as intact as the PDF engine permits. Use PDF-coordinate editing on the original instead of exporting whole pages to images and rebuilding a raster PDF.
- **Signature is a distinct authorized action.** For Rob's own documents, look up his existing **authorized original transparent signature** in the private personal reference/Drive location; do not generate a replacement or use typed cursive. Never infer authorization to sign another person's form. Do not commit signature bytes or private file links to this repository.
- **Size and placement are explicit.** Preserve image aspect ratio, trim excess transparent margins without modifying ink, start conservatively small, and fit within the blank signature field. The **visible ink**, not just the image rectangle, must clear the last paragraph, field labels, printed signature line, name, and date. Keep a modest margin. Do not obscure the form's wording.
- **Preview before delivery.** Render and inspect all pages at readable zoom, especially the signature area. Check for text/date overlap, signature encroachment, unintended scale changes, page-count changes, and PDF file-size blowups. If a placement fails, adjust only that page/overlay and re-render. A valid PDF file alone is not visual acceptance.
- **Use one current deliverable.** If a corrected file is to replace an email draft attachment, update the existing draft when supported; otherwise disclose that a new draft was created and identify obsolete drafts for cleanup. Do not claim a newer attachment is in the draft until verified. Do not send without the user's exact authorization.

**Keep the CLI examples above:** `inspect` and `fill` remain real v0.1.1 commands. No signature, image, Gmail, or draft-replacement command exists in this package; do not present a hypothetical command as executable. This checklist is for reviewers until the signing/attachment capabilities and tests actually exist.

## Required next acceptance work

1. Compare printed appearances in at least two renderers for real PDFs and add layout-sensitive assertions.
2. Cover multi-line text, radio/button groups and choice fields with verified appearance values.
3. Test the previously failed TPG original (not accessible from the provided old public URL) with **private** sample data and the exact mandatory section checklist.
4. Add explicit signer approval and target-document digest binding before signature placement; separate signing from drafting.
5. Publish a tested installation/adapter path for Agent Hub/OpenCode, and a separately authorized route for ChatGPT cloud use.

This is only a standalone engine slice, not the shared execution broker or final production release. Track integration and acceptance in Agent Hub backlog rather than a second ledger.
