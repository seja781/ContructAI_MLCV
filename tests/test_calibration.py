"""Scale calibration: only the drawing's own dimensions may establish a scale."""

from decimal import Decimal

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app.backend.main import app
from constructai.extraction.calibration import LinearReference, calibrate, measure_reference
from constructai.extraction.ocr import tesseract_status


def _chain(image: np.ndarray, row: int, ticks: list[int], tick_half: int = 10) -> None:
    image[row - 1: row + 1, ticks[0]: ticks[-1] + 1] = 0
    for x in ticks:
        image[row - tick_half: row + tick_half, x - 1: x + 1] = 0


def _blank(width=1200, height=400) -> np.ndarray:
    return np.full((height, width), 255, dtype=np.uint8)


def test_segment_between_ticks_is_measured_not_whole_chain():
    image = _blank()
    _chain(image, 200, [100, 300, 700])
    # text box sits above the 300..700 segment
    reference = LinearReference("A-1", "4.0 m", Decimal("4.0"), 0, (450, 160, 550, 180))
    length, how = measure_reference(image < 128, reference)
    assert how == "ticks"
    assert length == pytest.approx(400, abs=2)


def test_agreeing_references_give_verified_scale():
    image = _blank()
    _chain(image, 200, [100, 300, 700])  # 100 px per metre drawing
    refs = [
        LinearReference("A-1", "2 m", Decimal("2"), 0, (170, 160, 230, 180)),
        LinearReference("A-2", "4 m", Decimal("4"), 0, (470, 160, 530, 180)),
    ]
    scale = calibrate(1, {0: image}, refs)
    assert scale.status == "verified"
    assert scale.pixels_per_metre == pytest.approx(100, rel=0.02)
    assert all(r.agrees_with_consensus for r in scale.references)


def test_not_to_scale_dimensions_are_conflicting_and_give_no_scale():
    image = _blank()
    _chain(image, 200, [100, 300, 700])
    refs = [
        LinearReference("A-1", "2 m", Decimal("2"), 0, (170, 160, 230, 180)),
        LinearReference("A-2", "9 m", Decimal("9"), 0, (470, 160, 530, 180)),  # drawn 4 m long
    ]
    scale = calibrate(1, {0: image}, refs)
    assert scale.status == "conflicting"
    assert scale.pixels_per_metre is None


def test_single_reference_is_not_enough():
    image = _blank()
    _chain(image, 200, [100, 300])
    scale = calibrate(1, {0: image}, [LinearReference("A-1", "2 m", Decimal("2"), 0, (170, 160, 230, 180))])
    assert scale.status == "single_reference"
    assert scale.pixels_per_metre is None


def test_no_dimension_line_means_calibration_unavailable():
    scale = calibrate(1, {0: _blank()}, [LinearReference("A-1", "2 m", Decimal("2"), 0, (170, 160, 230, 180))])
    assert scale.status == "unavailable"
    assert scale.pixels_per_metre is None


def test_majority_outlier_is_flagged_but_scale_still_verified():
    image = _blank(1600)
    _chain(image, 200, [100, 300, 500, 900, 1300])
    refs = [
        LinearReference("A-1", "2 m", Decimal("2"), 0, (170, 160, 230, 180)),
        LinearReference("A-2", "2 m", Decimal("2"), 0, (370, 160, 430, 180)),
        LinearReference("A-3", "4 m", Decimal("4"), 0, (670, 160, 730, 180)),
        LinearReference("A-4", "1 m", Decimal("1"), 0, (1070, 160, 1130, 180)),  # misread: drawn 4 m
    ]
    scale = calibrate(1, {0: image}, refs)
    assert scale.status == "verified"
    flagged = [r.annotation_id for r in scale.references if not r.agrees_with_consensus]
    assert flagged == ["A-4"]


@pytest.mark.skipif(not tesseract_status().available, reason="Tesseract OCR executable not installed")
def test_end_to_end_ocr_calibration_and_misread_flag(tmp_path):
    """A rendered dimension chain is calibrated from its own text; a dimension
    whose number disagrees with its drawn length is flagged unverified."""

    font = ImageFont.load_default(size=30)
    image = Image.new("RGB", (1500, 400), "white")
    draw = ImageDraw.Draw(image)
    ticks = [100, 400, 700, 1300]  # 150 px per metre => 2 m, 2 m, 4 m segments
    draw.line((ticks[0], 250, ticks[-1], 250), fill="black", width=2)
    for x in ticks:
        draw.line((x, 235, x, 265), fill="black", width=2)
    for (left, right), text in zip(zip(ticks, ticks[1:]), ["2.0 m", "2.0 m", "9.0 m"]):
        width = draw.textlength(text, font=font)
        draw.text(((left + right) / 2 - width / 2, 195), text, fill="black", font=font)
    path = tmp_path / "chain.png"
    image.save(path)

    body = TestClient(app).post("/extractions", files={"file": (path.name, path.read_bytes(), "image/png")}).json()
    scale = body["scale_calibration"][0]

    assert scale["status"] == "verified"
    assert scale["pixels_per_metre"] == pytest.approx(150, rel=0.03)
    misread = [a for a in body["dimension_annotations"] if a["raw_text"].replace(" ", "") == "9.0m"]
    assert misread and misread[0]["status"] == "unverified"
    assert any("not drawn to scale" in w or "misread" in w for w in body["warnings"])
    assert body["elements"] == []  # linear dimensions alone never become elements
