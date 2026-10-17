"""End-to-end tests for real drawing extraction (SVG text, PDF text layer, OCR).

Raster/OCR tests are skipped automatically when the Tesseract executable is
not installed, so the rest of the suite still runs.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.backend.main import app
from constructai.boq.generator import BOQGenerator
from constructai.extraction import ocr as ocr_module
from constructai.extraction.agent import ExtractionAgent
from constructai.extraction.annotations import interpret_page
from constructai.extraction.local_provider import LocalExtractionProvider
from constructai.extraction.ocr import TextLine
from constructai.extraction.schemas import ExtractionResult

SAMPLE_PLAN = Path("data/sample/second_floor_plan.png")
needs_tesseract = pytest.mark.skipif(
    not ocr_module.tesseract_status().available, reason="Tesseract OCR executable not installed"
)


def _post(client, path: Path, content_type: str):
    with path.open("rb") as handle:
        return client.post("/extractions", files={"file": (path.name, handle, content_type)})


def _by_label(result: dict) -> dict:
    return {e["description"]: e for e in result["elements"]}


# ------------------------------------------------------------------ association logic (no OCR)


def _line(text, box, conf=0.95, frame=0):
    return TextLine(text, box, box, frame, conf, 1, "tesseract_ocr", "test")


def _lines_twice(*specs):
    # Two independent "passes" reading the same text, as the OCR session produces.
    return [_line(t, b) for t, b in specs] + [TextLine(t, b, b, 0, 0.9, 1, "tesseract_ocr", "test2") for t, b in specs]


def test_dimension_under_label_becomes_room_with_provenance():
    lines = _lines_twice(("BEDROOM", (100, 100, 220, 120)), ("4.5 x 3.6 m", (100, 130, 220, 145)))
    page = interpret_page(lines, 1)
    assert len(page.elements) == 1
    room = page.elements[0]
    assert room.description == "BEDROOM"
    assert room.room_type == "bedroom"
    assert (room.dimensions.length, room.dimensions.width) == (Decimal("4.5"), Decimal("3.6"))
    assert room.measurement_source == "dimension_annotation"
    assert room.raw_dimension_text == "4.5 x 3.6 m"
    assert room.annotation_ids


def test_label_without_dimension_is_reported_not_filled():
    lines = _lines_twice(("BEDROOM", (100, 100, 220, 120)), ("KITCHEN", (400, 100, 520, 120)), ("4.5 x 3.6 m", (100, 130, 220, 145)))
    page = interpret_page(lines, 1)
    assert [e.description for e in page.elements] == ["BEDROOM"]
    assert any(u.text == "KITCHEN" for u in page.unresolved)


def test_single_unconfirmed_ocr_reading_does_not_become_an_element():
    lines = [_line("BEDROOM", (100, 100, 220, 120)), _line("4.5 x 3.6 m", (100, 130, 220, 145))]
    page = interpret_page(lines, 1)
    assert page.elements == []
    assert page.annotations[0].status == "unverified"


def test_unitless_dimensions_are_not_converted():
    lines = _lines_twice(("STORE", (100, 100, 220, 120)), ("3500 x 4200", (100, 130, 220, 145)))
    page = interpret_page(lines, 1)
    assert page.elements == []
    assert any("no unit" in u.reason for u in page.unresolved)


def test_declared_unit_note_allows_conversion():
    lines = _lines_twice(
        ("ALL DIMENSIONS ARE IN MM", (10, 10, 300, 25)),
        ("STORE", (100, 100, 220, 120)),
        ("3500 x 4200", (100, 130, 220, 145)),
    )
    page = interpret_page(lines, 1)
    assert page.elements[0].dimensions.length == Decimal("3.5")


def test_two_equally_close_labels_make_association_uncertain():
    lines = _lines_twice(
        ("BEDROOM", (100, 100, 200, 120)),
        ("STUDY", (205, 100, 300, 120)),
        ("4.5 x 3.6 m", (150, 130, 250, 145)),
    )
    page = interpret_page(lines, 1)
    assert page.elements == []
    assert page.annotations[0].association_status == "uncertain"


# ------------------------------------------------------------------ SVG / PDF exact text paths


def _write_svg(path: Path, texts: list[tuple[str, float, float, str]]) -> Path:
    body = "\n".join(
        f'<text x="{x}" y="{y}" font-size="14" text-anchor="middle"{extra}>{t}</text>' for t, x, y, extra in texts
    )
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 800 600">{body}</svg>')
    return path


def test_svg_text_metric_drawing_end_to_end(tmp_path):
    svg = _write_svg(
        tmp_path / "metric.svg",
        [
            ("LIVING ROOM", 200, 100, ""),
            ("5400 x 4200 mm", 200, 120, ""),
            ("KITCHEN", 500, 100, ""),
            ("3.0 x 2.7 m", 500, 120, ""),
            ("TOILET", 650, 300, ""),  # no dimension printed
        ],
    )
    response = _post(TestClient(app), svg, "image/svg+xml")
    assert response.status_code == 200
    body = response.json()
    rooms = _by_label(body)
    assert set(rooms) == {"LIVING ROOM", "KITCHEN"}
    assert rooms["LIVING ROOM"]["dimensions"]["length"] == pytest.approx(5.4)
    assert rooms["LIVING ROOM"]["original_unit"] == "mm"
    assert rooms["KITCHEN"]["dimensions"]["width"] == pytest.approx(2.7)
    assert any(u["text"] == "TOILET" for u in body["unresolved"])
    assert body["extraction_method"] == "svg_text"
    assert "not established" in body["real_world_scale"]


def test_changing_the_drawing_changes_the_result(tmp_path):
    first = _write_svg(tmp_path / "a.svg", [("BEDROOM", 200, 100, ""), ("4.5 x 3.6 m", 200, 120, "")])
    second = _write_svg(tmp_path / "b.svg", [("STUDY", 200, 100, ""), ("12'-0\" x 10'-6\"", 200, 120, "")])
    client = TestClient(app)
    a = _post(client, first, "image/svg+xml").json()
    b = _post(client, second, "image/svg+xml").json()
    assert [e["description"] for e in a["elements"]] == ["BEDROOM"]
    assert [e["description"] for e in b["elements"]] == ["STUDY"]
    assert b["elements"][0]["dimensions"] == {"length": pytest.approx(3.6576), "width": pytest.approx(3.2004), "height": None}
    assert b["source_name"] == "b.svg"


def test_svg_rotated_text_is_associated_in_its_own_frame(tmp_path):
    svg = _write_svg(
        tmp_path / "rotated.svg",
        [
            ("KITCHEN", 300, 300, ' transform="rotate(-90 300 300)"'),
            ("5'-9&quot; x 10'-0&quot;", 320, 300, ' transform="rotate(-90 320 300)"'),
        ],
    )
    body = _post(TestClient(app), svg, "image/svg+xml").json()
    assert [e["description"] for e in body["elements"]] == ["KITCHEN"]
    assert body["elements"][0]["raw_dimension_text"] == "5'-9\" x 10'-0\""


def test_svg_without_any_dimensions_returns_no_measurements(tmp_path):
    svg = _write_svg(tmp_path / "nodims.svg", [("BEDROOM", 200, 100, ""), ("KITCHEN", 500, 100, "")])
    body = _post(TestClient(app), svg, "image/svg+xml").json()
    assert body["elements"] == []
    assert body["status"] == "no_measurements"
    assert body["warnings"]


def test_pdf_text_layer_feet_inches(tmp_path):
    import fitz

    pdf_path = tmp_path / "plan.pdf"
    document = fitz.open()
    page = document.new_page(width=600, height=400)
    page.insert_text((100, 100), "DINING", fontsize=14)
    page.insert_text((100, 120), "9'-6\" x 11'-0\"", fontsize=12)
    page.insert_text((300, 100), "BALCONY", fontsize=14)
    page.insert_text((300, 120), "18'-9\" x 4'-0\"", fontsize=12)
    document.save(pdf_path)
    body = _post(TestClient(app), pdf_path, "application/pdf").json()
    rooms = _by_label(body)
    assert rooms["DINING"]["dimensions"]["length"] == pytest.approx(2.8956)
    assert rooms["BALCONY"]["dimensions"]["length"] == pytest.approx(5.715)
    assert body["extraction_method"] == "pdf_text_layer"


# ------------------------------------------------------------------ raster OCR path


def _font(size):
    return ImageFont.load_default(size=size)


def _draw_plan(path: Path, rooms: list[tuple[str, str | None, tuple[int, int]]], degrade=False) -> Path:
    image = Image.new("RGB", (1500, 900), "white")
    draw = ImageDraw.Draw(image)
    for label, dims, (x, y) in rooms:
        draw.rectangle((x - 120, y - 90, x + 420, y + 140), outline="black", width=6)
        draw.text((x, y), label, fill="black", font=_font(38))
        if dims:
            draw.text((x, y + 55), dims, fill="black", font=_font(34))
    if degrade:  # scanned / low-contrast copy
        image = image.point(lambda p: 110 + p * 0.45).rotate(1.5, fillcolor=(225, 225, 225), expand=False)
        image = image.filter(ImageFilter.GaussianBlur(0.8))
    image.save(path)
    return path


@needs_tesseract
@pytest.mark.parametrize("degrade", [False, True])
def test_png_ocr_metric_and_feet(tmp_path, degrade):
    png = _draw_plan(
        tmp_path / "plan.png",
        [("BEDROOM", "4500 x 3600 mm", (250, 200)), ("KITCHEN", "3.0 x 2.7 m", (800, 200)), ("STUDY", "12'-0\" x 10'-6\"", (250, 600)), ("STORE", None, (800, 600))],
        degrade=degrade,
    )
    body = _post(TestClient(app), png, "image/png").json()
    rooms = _by_label(body)
    assert rooms["BEDROOM"]["dimensions"]["length"] == pytest.approx(4.5)
    assert rooms["KITCHEN"]["dimensions"]["width"] == pytest.approx(2.7)
    assert rooms["STUDY"]["dimensions"]["length"] == pytest.approx(3.6576)
    assert "STORE" not in rooms  # no dimension printed -> never filled in
    assert any(u["text"] == "STORE" for u in body["unresolved"])


@needs_tesseract
def test_blank_image_returns_nothing_rather_than_defaults(tmp_path):
    png = tmp_path / "blank.png"
    Image.new("RGB", (800, 600), "white").save(png)
    body = _post(TestClient(app), png, "image/png").json()
    assert body["elements"] == []
    assert body["status"] == "no_measurements"


@needs_tesseract
@pytest.mark.skipif(not SAMPLE_PLAN.exists(), reason="sample plan missing")
def test_sample_second_floor_plan_reads_values_printed_on_the_drawing():
    body = _post(TestClient(app), SAMPLE_PLAN, "image/png").json()
    rooms = {e["description"]: e["raw_dimension_text"] for e in body["elements"]}
    # Values printed on the sheet. Not every room must be found, but anything
    # returned must match the drawing exactly.
    printed = {
        "ENTRY FOYER": "3'-9\" x 10'-0\"",
        "KITCHEN": "5'-9\" x 10'-0\"",
        "DINING": "9'-6\" x 11'-0\"",
        "ATT. TOILET": "5'-0\" x 6'-0\"",
        "C. TOILET": "5'-0\" x 6'-0\"",
        "LIVING (NE)": "9'-6\" x 13'-0\"",
        "BEDROOM 2 (NW)": "9'-3\" x 11'-0\"",
        "BALCONY (N)": "18'-9\" x 4'-0\"",
        "POOJA": "3'-6\" x 3'-6\"",
        "MASTER BED (SW)": "9'-3\" x 10'-0\"",
        "Roof slab": "19'-3\" x 34'-6\"",
    }
    assert len(rooms) >= 6
    for label, raw in rooms.items():
        assert label in printed, label
        assert raw.replace("\"", "").replace("'", "") == printed[label].replace("\"", "").replace("'", "")
    # Spaces with no dimension on the drawing are never given one.
    assert "PASSAGE" not in rooms and "STAIR" not in rooms
    client = TestClient(app)
    for index in range(len(body["elements"])):
        reviewed = client.post(f"/extractions/{body['extraction_id']}/elements/{index}/review", json={"decision": "approved"})
        assert reviewed.status_code == 200
    boq = client.post(f"/extractions/{body['extraction_id']}/boq")
    assert boq.status_code == 200, boq.text
    assert len(boq.json()["items"]) == len(body["elements"])


# ------------------------------------------------------------------ failure handling


def test_missing_tesseract_returns_503_not_empty_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ocr_module, "tesseract_status", lambda: ocr_module.TesseractStatus(False, error="Tesseract OCR executable not found")
    )
    png = tmp_path / "plan.png"
    Image.new("RGB", (100, 100), "white").save(png)
    response = _post(TestClient(app), png, "image/png")
    assert response.status_code == 503
    assert "Tesseract" in response.json()["detail"]


def test_corrupt_image_is_rejected(tmp_path):
    png = tmp_path / "broken.png"
    png.write_bytes(b"not really a png")
    assert _post(TestClient(app), png, "image/png").status_code == 422








# ------------------------------------------------------------------ downstream compatibility


def test_extraction_with_provenance_fields_flows_into_boq(tmp_path):
    svg = _write_svg(tmp_path / "boq.svg", [("BEDROOM", 200, 100, ""), ("4.5 x 3.6 m", 200, 120, "")])
    result = LocalExtractionProvider().extract(svg)
    round_tripped = ExtractionResult.model_validate_json(result.model_dump_json())
    boq = BOQGenerator().generate(round_tripped)
    assert boq.items[0].quantity == Decimal("16.20")
    assert boq.items[0].unit == "m2"
    assert boq.items[0].calculation == "4.5 m x 3.6 m = 16.20 m2"


# ------------------------------------------------------------------ SVG robustness (regressions)


def test_svg_with_invalid_numbers_never_returns_bare_500(tmp_path):
    svg = tmp_path / "odd.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 800 600">'
        '<text x="100" y="100" font-size="1e999">KITCHEN</text>'
        '<text x="300" y="100" font-size="NaN">TOILET</text>'
        '<text x="300" y="120" font-size="12">1500 x 1200 mm</text></svg>'
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/extractions", files={"file": (svg.name, svg.read_bytes(), "image/svg+xml")}
    )
    assert response.status_code == 200
    assert _by_label(response.json())["TOILET"]["dimensions"]["length"] == pytest.approx(1.5)


def test_svg_nested_scale_matrix_and_rotate_transforms(tmp_path):
    svg = tmp_path / "cad_export.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">'
        '<g transform="matrix(2 0 0 2 10 10)"><g transform="scale(0.5)">'
        '<text x="300" y="300" font-size="20" text-anchor="middle">STUDY</text>'
        '<text x="300" y="330" font-size="16" text-anchor="middle">3000 x 2700 mm</text></g></g>'
        '<g transform="translate(800 500) rotate(-90)">'
        '<text x="0" y="0" font-size="20">STORE</text><text x="0" y="30" font-size="16">1.8 x 1.5 m</text></g></svg>'
    )
    body = _post(TestClient(app), svg, "image/svg+xml").json()
    rooms = _by_label(body)
    assert rooms["STUDY"]["dimensions"]["length"] == pytest.approx(3)
    assert rooms["STORE"]["dimensions"]["width"] == pytest.approx(1.5)


def test_location_comes_only_from_text_printed_on_the_drawing():
    lines = _lines_twice(
        ("SECOND FLOOR PLAN", (300, 10, 700, 50)),
        ("LIVING (NE)", (100, 100, 260, 120)),
        ("9'-6\" x 13'-0\"", (100, 130, 260, 145)),
        ("STUDY", (500, 100, 620, 120)),
        ("3.0 x 2.7 m", (500, 130, 620, 145)),
    )
    page = interpret_page(lines, 1)
    rooms = {e.description: e for e in page.elements}
    assert rooms["LIVING (NE)"].location == "SECOND FLOOR PLAN, NE zone"
    assert rooms["STUDY"].location == "SECOND FLOOR PLAN"
    assert rooms["STUDY"].dimensions.height is None  # never assumed


def test_no_title_and_no_zone_leaves_location_empty():
    page = interpret_page(_lines_twice(("STUDY", (500, 100, 620, 120)), ("3.0 x 2.7 m", (500, 130, 620, 145))), 1)
    assert page.elements[0].location is None


def _plan_with_directions(extra_label="LIVING (NE)"):
    # South at the top, north at the bottom, east on the left, west on the right
    return _lines_twice(
        ("GROUND FLOOR PLAN", (300, 0, 700, 40)),
        ("SOUTH", (480, 60, 540, 80)),
        ("NORTH", (480, 940, 540, 960)),
        ("EAST", (20, 500, 70, 520)),
        ("WEST", (930, 500, 990, 520)),
        ("KITCHEN", (150, 150, 260, 170)),
        ("3.0 x 2.7 m", (150, 180, 260, 195)),
        (extra_label, (150, 800, 300, 820)),
        ("4.5 x 3.6 m", (150, 830, 300, 845)),
        ("BEDROOM", (750, 800, 860, 820)),
        ("4.0 x 3.0 m", (750, 830, 860, 845)),
    )


def test_room_zone_from_printed_direction_labels():
    rooms = {e.description: e.location for e in interpret_page(_plan_with_directions(), 1).elements}
    assert rooms["LIVING (NE)"] == "GROUND FLOOR PLAN, NE zone"
    assert rooms["KITCHEN"] == "GROUND FLOOR PLAN, SE zone (by position)"
    assert rooms["BEDROOM"] == "GROUND FLOOR PLAN, NW zone (by position)"


def test_position_zones_dropped_when_printed_zone_disagrees():
    page = interpret_page(_plan_with_directions("LIVING (SW)"), 1)
    rooms = {e.description: e.location for e in page.elements}
    assert rooms["KITCHEN"] == "GROUND FLOOR PLAN"  # no derived zone
    assert any("Position-based zones not used" in w for w in page.warnings)
