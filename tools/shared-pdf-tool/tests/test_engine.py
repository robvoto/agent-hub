import hashlib
from pathlib import Path

import fitz
import pytest
from reportlab.pdfgen import canvas

from shared_pdf_tool import PDFToolError, inspect_pdf, fill_pdf, validate_pdf


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _fillable(path):
    c = canvas.Canvas(str(path), pagesize=(612, 792))
    c.drawString(40, 740, "Personal details — name and consent")
    c.acroForm.textfield(name="first_name", tooltip="First name", x=100, y=680, width=240, height=25)
    c.acroForm.checkbox(name="accept_terms", tooltip="Accept terms", x=100, y=640, buttonStyle="check")
    c.showPage()
    c.drawString(40, 740, "Second page — fixed original")
    c.save()


def _static(path):
    c = canvas.Canvas(str(path), pagesize=(612, 792))
    c.drawString(40, 740, "Ownership change — original layout")
    c.drawString(40, 690, "First name:")
    c.line(160, 675, 430, 675)
    c.drawString(40, 620, "Agreement")
    c.rect(130, 613, 13, 13)
    c.showPage()
    c.drawString(40, 740, "Original second page")
    c.save()


def test_fillable_original_unchanged_and_all_pages_render(tmp_path):
    src = tmp_path / "form.pdf"; _fillable(src)
    before = src.read_bytes()
    info = inspect_pdf(src)
    assert info["kind"] == "fillable" and info["page_count"] == 2
    assert {x["name"] for x in info["fields"]} == {"first_name", "accept_terms"}
    spec = {"source_sha256": info["source_sha256"], "expected_page_count": 2,
            "required_fields": ["first_name", "accept_terms"],
            "entries": [{"name": "first_name", "kind": "field", "value": "Ada"},
                        {"name": "accept_terms", "kind": "field", "value": True}]}
    target = tmp_path / "filled.pdf"; previews = tmp_path / "previews"
    report = fill_pdf(src, spec, target, previews, dpi=100)
    assert src.read_bytes() == before
    assert report["status"] == "NEEDS_VISUAL_REVIEW"
    assert report["validation"]["structural_checks_passed"]
    assert len(list(previews.glob("*.png"))) == 2
    with fitz.open(target) as doc:
        fields = {w.field_name: w.field_value for page in doc for w in page.widgets() or []}
    assert fields["first_name"] == "Ada"
    assert fields["accept_terms"] not in ("Off", "")
    assert "Ada" not in str(report)


def test_static_placement_and_required_validation(tmp_path):
    src = tmp_path / "static.pdf"; _static(src)
    info = inspect_pdf(src)
    assert info["kind"] == "static"
    spec = {"source_sha256": info["source_sha256"], "expected_page_count": 2,
            "required_fields": ["name", "checked"],
            "entries": [{"name": "name", "kind": "text", "value": "Ada Lovelace", "page": 1,
                         "rect": [160, 100, 400, 122], "font_size": 11},
                        {"name": "checked", "kind": "check", "value": True, "page": 1,
                         "rect": [130, 165, 143, 178]}]}
    target = tmp_path / "filled.pdf"
    report = fill_pdf(src, spec, target, tmp_path / "renders", dpi=100)
    assert report["status"] == "NEEDS_VISUAL_REVIEW"
    with fitz.open(target) as doc:
        assert "Ada Lovelace" in doc[0].get_text()
        assert doc[1].get_text() == fitz.open(src)[1].get_text()
    spec["entries"][0]["value"] = ""
    with pytest.raises(PDFToolError, match="required value is missing"):
        fill_pdf(src, spec, tmp_path / "fail.pdf", tmp_path / "renders2")
    assert not (tmp_path / "fail.pdf").exists()


def test_declines_guessing_and_overlap(tmp_path):
    src = tmp_path / "original.pdf"; _static(src)
    info = inspect_pdf(src)
    base = {"source_sha256": info["source_sha256"], "expected_page_count": 2,
            "required_fields": ["name"], "entries": [{"name": "name", "kind": "field", "value": "Ada"}]}
    with pytest.raises(PDFToolError, match="not present in original"):
        fill_pdf(src, base, tmp_path / "no.pdf", tmp_path / "p")
    base["entries"] = [{"name": "name", "kind": "text", "value": "Ada", "page": 1,
                        "rect": [40, 96, 165, 125]}]
    with pytest.raises(PDFToolError, match="overlaps text"):
        fill_pdf(src, base, tmp_path / "no.pdf", tmp_path / "p")
    base["entries"][0]["rect"] = [800, 900, 820, 940]
    with pytest.raises(PDFToolError, match="outside original page"):
        fill_pdf(src, base, tmp_path / "no.pdf", tmp_path / "p")
    assert not (tmp_path / "no.pdf").exists()


def test_guard_stale_spec_and_clipping(tmp_path):
    src = tmp_path / "original.pdf"; _static(src)
    info = inspect_pdf(src)
    spec = {"source_sha256": "0" * 64, "expected_page_count": 2,
            "required_fields": [], "entries": [{"name": "name", "kind": "text", "value": "Ada", "page": 1,
            "rect": [160, 100, 210, 120]}]}
    with pytest.raises(PDFToolError, match="hash does not match"):
        fill_pdf(src, spec, tmp_path / "out.pdf", tmp_path / "preview")
    spec["source_sha256"] = info["source_sha256"]
    spec["entries"][0]["value"] = "A name too long for the requested rectangle"
    with pytest.raises(PDFToolError, match="cannot fit"):
        fill_pdf(src, spec, tmp_path / "out.pdf", tmp_path / "preview")
    assert not (tmp_path / "out.pdf").exists()
    with pytest.raises(PDFToolError, match="overwrite original"):
        fill_pdf(src, spec, src, tmp_path / "preview")


def test_scanned_identified_and_preserves_source(tmp_path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (560, 720), "white")
    dr = ImageDraw.Draw(img)
    dr.text((10, 40), "Scanned form", fill="black")
    img_path = tmp_path / "scan.png"; img.save(img_path)
    doc = fitz.open();page=doc.new_page(width=560,height=720)
    page.insert_image(page.rect, filename=str(img_path))
    path=tmp_path/"scan.pdf";doc.save(path);doc.close()
    info=inspect_pdf(path)
    assert info["kind"] == "scanned_or_image" and info["pages"][0]["images"] > 0
    before=_sha(path)
    spec={"source_sha256":info["source_sha256"],"expected_page_count":1,
          "required_fields":["name"],"entries":[{"name":"name","kind":"text","value":"Ada", "page":1,"rect":[100,100,240,124]}]}
    result=fill_pdf(path,spec,tmp_path/"out.pdf",tmp_path/"render",dpi=100)
    assert result["human_review_required"] and _sha(path)==before


def test_static_overlay_validation_detects_missing_text(tmp_path):
    """Saved PDF must contain exact expected characters inside the reviewed rectangle."""
    src = tmp_path / 'form.pdf'
    _static(src)
    info = inspect_pdf(src)
    spec = {'source_sha256': info['source_sha256'], 'expected_page_count': 2,
            'required_fields': ['name'],
            'entries': [{'name': 'name', 'kind': 'text', 'page': 1,
                         'rect': [160, 100, 400, 122], 'value': 'Ada Lovelace'}]}
    out = tmp_path / 'filled.pdf'
    report = fill_pdf(src, spec, out, tmp_path / 'pages')
    assert report['validation']['structural_checks_passed']
    # Original contains no overlaid values; validation of a fabricated success must fail.
    tampered = validate_pdf(src, src, spec['entries'], info)
    assert any('not accurately extractable' in x for x in tampered['errors'])


def test_saved_pdf_rejects_unsupported_glyphs_not_silently_dropped(tmp_path):
    src = tmp_path / 'form.pdf'
    _static(src)
    info = inspect_pdf(src)
    spec = {'source_sha256': info['source_sha256'], 'expected_page_count': 2,
            'required_fields': ['name'],
            'entries': [{'name': 'name', 'kind': 'text', 'page': 1,
                         'rect': [160, 100, 400, 122], 'value': 'A\u0930\u092c'}]}
    with pytest.raises(PDFToolError, match='not accurately extractable'):
        fill_pdf(src, spec, tmp_path / 'filled.pdf', tmp_path / 'pages')
    assert not (tmp_path / 'filled.pdf').exists()
