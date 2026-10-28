"""Fail-closed original-PDF inspection, filling, and validation.

This module is intentionally independent of Agent Hub, MCP, Gmail, and auth.
Only deterministic PDF operations are exposed; no signatures or sending.
Non-form overlays require explicit reviewer-supplied PDF-space rectangles.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any
import json
import math

import fitz
from PIL import Image, ImageChops, ImageDraw

MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_PAGES = 30


class PDFToolError(RuntimeError):
    """Operation cannot safely proceed; original remains unchanged."""


def _source(path: str | Path) -> Path:
    p = Path(path).expanduser().resolve(strict=True)
    if not p.is_file() or p.suffix.lower() != ".pdf":
        raise PDFToolError("Input must be an existing PDF file")
    if p.stat().st_size > MAX_SOURCE_BYTES:
        raise PDFToolError("Original exceeds 40 MiB processing limit")
    return p


def _open(path: Path) -> fitz.Document:
    try:
        d = fitz.open(path)
    except Exception as exc:
        raise PDFToolError(f"Cannot open PDF: {type(exc).__name__}") from exc
    if d.needs_pass:
        d.close()
        raise PDFToolError("Encrypted PDF requires authorization; not processed")
    if d.page_count < 1 or d.page_count > MAX_PAGES:
        d.close()
        raise PDFToolError("PDF must contain 1 to 30 pages for this MVP")
    return d


def render_original(path: str | Path, output_dir: str | Path, dpi: int = 144) -> list[str]:
    """Render all pages of the original PDF to a new/empty private directory."""
    source = _source(path)
    destination = Path(output_dir).expanduser().resolve()
    if not 72 <= dpi <= 200:
        raise PDFToolError("DPI must be between 72 and 200")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise PDFToolError("Original preview directory must be empty")
    with _open(source) as doc:
        destination.mkdir(parents=True, exist_ok=True)
        names = []
        for page_i, page in enumerate(doc):
            name = f"page-{page_i + 1:03d}.png"
            page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False).save(destination / name)
            names.append(name)
    return names


def _all_widgets(doc: fitz.Document) -> dict[str, list[tuple[int, Any]]]:
    found: dict[str, list[tuple[int, Any]]] = {}
    for idx, page in enumerate(doc):
        for w in page.widgets() or []:
            if w.field_name:
                found.setdefault(w.field_name, []).append((idx, w))
    return found


def inspect_pdf(path: str | Path) -> dict[str, Any]:
    p = _source(path)
    digest = sha256(p.read_bytes()).hexdigest()
    with _open(p) as doc:
        widgets = _all_widgets(doc)
        pages = []
        for i, page in enumerate(doc):
            rect = page.rect
            text = page.get_text("text")
            pages.append({
                "page": i + 1, "width_pt": round(rect.width, 2),
                "height_pt": round(rect.height, 2),
                "has_extractable_text": bool(text.strip()),
                "images": len(page.get_images()),
                "widgets": len(list(page.widgets() or [])),
            })
        fields = []
        for name, entries in sorted(widgets.items()):
            for page_i, w in entries:
                fields.append({"name": name, "page": page_i + 1,
                               "type": int(w.field_type), "type_name": w.field_type_string,
                               "rect": [round(float(v), 2) for v in w.rect],
                               "current_value_present": bool(w.field_value)})
        kind = "fillable" if fields else ("scanned_or_image" if all(not x["has_extractable_text"] for x in pages) else "static")
        return {"source_sha256": digest, "page_count": len(pages),
                "kind": kind, "pages": pages, "fields": fields,
                "note": "Field requirement and value meaning cannot be inferred; explicit reviewed specification required."}


def _rect(values: Any, page: fitz.Page, name: str) -> fitz.Rect:
    if not isinstance(values, list) or len(values) != 4:
        raise PDFToolError(f"{name}: rect must be four PDF point coordinates [x0,y0,x1,y1], top-left origin")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise PDFToolError(f"{name}: invalid rectangle coordinates")
    box = fitz.Rect(values)
    if box.is_empty or box.is_infinite or not page.rect.contains(box):
        raise PDFToolError(f"{name}: rectangle is empty or lies outside original page")
    return box


def _text_font_size(value: str, box: fitz.Rect, size: float, min_size: float, shrink: bool, name: str) -> float:
    if not isinstance(size, (float, int)) or not isinstance(min_size, (float, int)) or size <= 0 or min_size <= 0:
        raise PDFToolError(f"{name}: invalid font size")
    if "\n" in value or "\r" in value:
        raise PDFToolError(f"{name}: multiline insertion is not supported in MVP")
    point_size = float(size)
    while True:
        width = fitz.get_text_length(value, fontname="helv", fontsize=point_size)
        if width <= box.width - 2 and point_size * 1.3 <= box.height:
            return point_size
        if not shrink or point_size - .5 < min_size:
            raise PDFToolError(f"{name}: text cannot fit in declared box; review placement or use explicit shrink_to_fit")
        point_size -= .5


def _checks_for_overlaps(page: fitz.Page, box: fitz.Rect, name: str, placed: list[fitz.Rect]) -> None:
    if any(box.intersects(other) for other in placed):
        raise PDFToolError(f"{name}: overlapping output rectangles")
    # Existing text inside a declared fill region means the original may be overwritten.
    # Drawn rules/lines are not text; they may still need visual review.
    for word in page.get_text("words"):
        original_word = fitz.Rect(word[:4])
        intersection = box & original_word
        if intersection.is_empty:
            continue
        overlap = intersection.get_area() / max(original_word.get_area(), 1)
        if overlap > .15:
            raise PDFToolError(f"{name}: insertion overlaps text in the original PDF")


def _visual_diff_check(original: Path, output: Path,
                       allowed: dict[int, list[fitz.Rect]]) -> list[dict[str, Any]]:
    """Fail if output pixels change outside declared field/overlay boxes (72dpi).

    This is a regression guard, not a substitute for semantic visual review.
    """
    pages: list[dict[str, Any]] = []
    with _open(original) as before, _open(output) as after:
        for i in range(len(before)):
            src = before[i].get_pixmap(alpha=False)
            dst = after[i].get_pixmap(alpha=False)
            if (src.width, src.height) != (dst.width, dst.height):
                raise PDFToolError(f"Page {i + 1}: render dimensions changed")
            one = Image.frombytes("RGB", (src.width, src.height), src.samples)
            two = Image.frombytes("RGB", (dst.width, dst.height), dst.samples)
            difference = ImageChops.difference(one, two)
            mask = Image.new("L", one.size, 0)
            draw = ImageDraw.Draw(mask)
            for rect in allowed.get(i, []):
                draw.rectangle((max(0, int(rect.x0) - 3), max(0, int(rect.y0) - 3),
                                min(src.width, int(math.ceil(rect.x1)) + 3),
                                min(src.height, int(math.ceil(rect.y1)) + 3)), fill=255)
            # White mask = explicitly approved changed area. Everything else must match.
            outside = Image.composite(Image.new("RGB", one.size, "black"), difference, mask)
            if outside.getbbox():
                raise PDFToolError(f"Page {i + 1}: render changed outside reviewed fill rectangles")
            pages.append({"page": i + 1, "outside_declared_change": False})
    return pages


def _validated_spec(info: dict[str, Any], spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(spec, dict):
        raise PDFToolError("Specification must be a JSON object")
    allowed = {"source_sha256", "expected_page_count", "entries", "required_fields"}
    if set(spec) - allowed:
        raise PDFToolError("Unknown specification keys: " + ", ".join(sorted(set(spec) - allowed)))
    if spec.get("source_sha256") != info["source_sha256"]:
        raise PDFToolError("Original PDF hash does not match reviewed specification")
    if spec.get("expected_page_count") != info["page_count"]:
        raise PDFToolError("Original page count differs from reviewed specification")
    entries = spec.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PDFToolError("Specification requires a non-empty entries list")
    mandatory = spec.get("required_fields", [])
    if not isinstance(mandatory, list) or not all(isinstance(x, str) and x for x in mandatory):
        raise PDFToolError("required_fields must be a list of names")
    names = [x.get("name") for x in entries if isinstance(x, dict)]
    if len(names) != len(entries) or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        raise PDFToolError("Each entry needs a unique non-empty name")
    missing = sorted(set(mandatory) - set(names))
    if missing:
        raise PDFToolError("Required fields missing from spec: " + ", ".join(missing))
    for entry in entries:
        if set(entry) - {"name", "kind", "value", "page", "rect", "font_size", "min_font_size", "shrink_to_fit"}:
            raise PDFToolError(f"{entry['name']}: unknown entry option")
        if entry["name"] in mandatory and (entry.get("value") is None or entry.get("value") == ""):
            raise PDFToolError(f"{entry['name']}: required value is missing")
    return entries, {"required_fields": mandatory, "missing_required": missing}


def fill_pdf(source: str | Path, spec: dict[str, Any], output: str | Path,
             preview_dir: str | Path, dpi: int = 144) -> dict[str, Any]:
    """Edit a copy of original PDF. Fail closed; return NEEDS_VISUAL_REVIEW, never 'complete'.

    No signature use is implemented here; broker must add signer authorization before it.
    """
    original = _source(source)
    target = Path(output).expanduser().resolve()
    previews = Path(preview_dir).expanduser().resolve()
    if target == original:
        raise PDFToolError("Cannot overwrite original PDF")
    if target.suffix.lower() != ".pdf":
        raise PDFToolError("Output must end in .pdf")
    if target.exists():
        raise PDFToolError("Output already exists; no overwrite permitted")
    if not 72 <= dpi <= 200:
        raise PDFToolError("DPI must be between 72 and 200")
    if previews.exists() and any(previews.iterdir()):
        raise PDFToolError("Preview directory must be empty")
    info = inspect_pdf(original)
    entries, checks = _validated_spec(info, spec)
    warnings: list[str] = []
    placement: list[dict[str, Any]] = []
    try:
        with _open(original) as doc:
            widgets = _all_widgets(doc)
            occupied: dict[int, list[fitz.Rect]] = {}
            approved_boxes: dict[int, list[fitz.Rect]] = {}
            for item in entries:
                name = item["name"]
                kind = item.get("kind", "field")
                value = item.get("value")
                if value is None and name not in checks["required_fields"]:
                    continue
                if kind == "field":
                    matches = widgets.get(name, [])
                    if not matches:
                        raise PDFToolError(f"{name}: field not present in original PDF; no invented placement")
                    if not isinstance(value, (str, bool, int, float)):
                        raise PDFToolError(f"{name}: unsupported field value")
                    for page_i in sorted({idx for idx, _widget in matches}):
                        # Widget handles require an open corresponding Page instance.
                        live_page = doc[page_i]
                        live_widgets = [w for w in live_page.widgets() or [] if w.field_name == name]
                        for w in live_widgets:
                            if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                                if not isinstance(value, bool):
                                    raise PDFToolError(f"{name}: checkbox value must be boolean")
                                w.field_value = w.on_state() if value else "Off"
                            elif w.field_type in (fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
                                if not isinstance(value, (str, int, float)):
                                    raise PDFToolError(f"{name}: text value must be text or number")
                                w.field_value = str(value)
                            else:
                                raise PDFToolError(f"{name}: widget type requires explicit reviewed mapping; unsupported in MVP")
                            try:
                                w.update()
                            except Exception as exc:
                                raise PDFToolError(f"{name}: widget update failed ({type(exc).__name__})") from exc
                            placement.append({"name": name, "page": page_i + 1, "method": "existing_widget"})
                            approved_boxes.setdefault(page_i, []).append(fitz.Rect(w.rect))
                elif kind in ("text", "check"):
                    if not isinstance(item.get("page"), int) or not 1 <= item["page"] <= len(doc):
                        raise PDFToolError(f"{name}: explicit valid 1-based page required")
                    page_i = item["page"] - 1
                    page = doc[page_i]
                    box = _rect(item.get("rect"), page, name)
                    _checks_for_overlaps(page, box, name, occupied.setdefault(page_i, []))
                    if kind == "text":
                        if not isinstance(value, str):
                            raise PDFToolError(f"{name}: text overlay requires a string")
                        fontsize = _text_font_size(value, box, item.get("font_size", 10), item.get("min_font_size", 7), bool(item.get("shrink_to_fit", False)), name)
                        # Use ascender-based baseline with a safety margin; clipping guarded by width/height.
                        baseline = fitz.Point(box.x0 + 1, box.y0 + (box.height - fontsize) / 2 + fontsize * .8)
                        page.insert_text(baseline, value, fontname="helv", fontsize=fontsize, color=(0, 0, 0), overlay=True)
                    else:
                        if not isinstance(value, bool):
                            raise PDFToolError(f"{name}: checkbox overlay requires true/false")
                        if value:
                            inset = min(box.width, box.height) * .18
                            a, b = fitz.Point(box.x0 + inset, box.y0 + box.height * .5), fitz.Point(box.x0 + box.width * .43, box.y1 - inset)
                            c = fitz.Point(box.x1 - inset, box.y0 + inset)
                            page.draw_polyline([a, b, c], color=(0, 0, 0), width=1.4, overlay=True)
                    occupied[page_i].append(box)
                    approved_boxes.setdefault(page_i, []).append(box)
                    placement.append({"name": name, "page": item["page"], "method": "reviewed_overlay"})
                else:
                    raise PDFToolError(f"{name}: unknown kind (expected field, text or check)")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Create output only after every input, permission-independent geometric check passes.
            doc.save(target, garbage=3, deflate=True)
        check = validate_pdf(original, target, entries, info)
        check["render_regression"] = _visual_diff_check(original, target, approved_boxes)
        if check["errors"]:
            target.unlink(missing_ok=True)
            raise PDFToolError("Output validation failed: " + "; ".join(check["errors"]))
        previews.mkdir(parents=True, exist_ok=True)
        with _open(target) as result_doc:
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            for page_i, page in enumerate(result_doc):
                image = page.get_pixmap(matrix=mat, alpha=False)
                image.save(previews / f"page-{page_i + 1:03d}.png")
        return {"status": "NEEDS_VISUAL_REVIEW", "source_sha256": info["source_sha256"],
                "output_sha256": sha256(target.read_bytes()).hexdigest(),
                "page_count": info["page_count"], "placements": placement,
                "validation": check, "warnings": warnings,
                "preview_pages": [f"page-{i + 1:03d}.png" for i in range(info["page_count"])],
                "human_review_required": True,
                "note": "Original kept intact. Inspect every preview. No signature or email send performed."}
    except Exception:
        # A failed workflow must not leave a misleading 'completed' artifact.
        target.unlink(missing_ok=True)
        raise


def validate_pdf(source: str | Path, output: str | Path,
                 entries: list[dict[str, Any]], info: dict[str, Any] | None = None) -> dict[str, Any]:
    info = info or inspect_pdf(source)
    errors: list[str] = []
    with _open(_source(output)) as doc:
        if len(doc) != info["page_count"]:
            errors.append("Page count changed")
        # Extract immutable values while each page is still strongly referenced.
        values: dict[str, list[tuple[str, str]]] = {}
        for page_i in range(len(doc)):
            page = doc[page_i]
            for widget in page.widgets() or []:
                if not widget.field_name:
                    continue
                state = widget.on_state() if widget.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX else ""
                values.setdefault(widget.field_name, []).append((str(widget.field_value), state))
        for entry in entries:
            if entry.get("value") is None:
                continue
            name = entry["name"]
            kind = entry.get("kind", "field")
            if kind == "field":
                found = values.get(name, [])
                if not found:
                    errors.append(f"{name}: original field not retained")
                for actual, state in found:
                    wanted = entry["value"]
                    expected = (state if wanted else "Off") if isinstance(wanted, bool) else str(wanted)
                    if actual != str(expected):
                        errors.append(f"{name}: structural value mismatch")
            elif kind == "text":
                # Do not mistake a successful insert_text call for rendered/extractable output.
                # This catches lost glyphs, wrong-page placements and missing field text.
                page_index = entry.get("page", 0) - 1
                if not 0 <= page_index < len(doc):
                    errors.append(f"{name}: text placement page missing")
                    continue
                rectangle = fitz.Rect(entry["rect"])
                actual = " ".join(doc[page_index].get_text("text", clip=rectangle).split())
                expected = " ".join(entry["value"].split())
                if actual != expected:
                    errors.append(f"{name}: text overlay not accurately extractable inside declared box")
        for i, p in enumerate(doc):
            if (abs(p.rect.width - info["pages"][i]["width_pt"]) > 1 or
                abs(p.rect.height - info["pages"][i]["height_pt"]) > 1):
                errors.append(f"Page {i + 1}: geometry changed")
    return {"errors": errors, "structural_checks_passed": not bool(errors),
            "visual_review_complete": False,
            "note": "Structural checks cannot prove semantic correctness or viewer-independent visual placement."}
