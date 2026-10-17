from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.backend.main import app
from constructai.extraction.agent import ExtractionAgent
from constructai.extraction.ocr import tesseract_status
from constructai.extraction.text_providers import SvgTextProvider


SAMPLE_DRAWING = Path("data/sample/sample_drawing.svg")


def test_extraction_agent_returns_structured_elements() -> None:
    result = ExtractionAgent([SvgTextProvider()]).extract(SAMPLE_DRAWING)

    assert result.source_name == "sample_drawing.svg"
    assert len(result.elements) == 1
    assert result.elements[0].element_type == "wall"
    assert result.elements[0].element_id == "W-001"
    assert result.elements[0].quantity == 13.5
    assert result.elements[0].source_page == 1


def test_extraction_endpoint_accepts_drawing_upload() -> None:
    client = TestClient(app)
    with SAMPLE_DRAWING.open("rb") as drawing:
        response = client.post(
            "/extractions",
            files={"file": (SAMPLE_DRAWING.name, drawing, "image/svg+xml")},
        )

    assert response.status_code == 200
    assert response.json()["elements"][0]["specification"] == "Illustrative 230mm masonry wall assumption"


@pytest.mark.skipif(not tesseract_status().available, reason="Tesseract OCR executable not installed")
def test_floorplan_image_without_text_returns_no_fabricated_elements() -> None:
    """Shapes alone (no labels, no dimension text) must not become measured elements.

    Previously this test expected rectangles to be returned as walls/doors/windows
    classified only by their aspect ratio; on a real plan that produced hundreds
    of unmeasured "elements" from text glyphs and room fills.
    """
    client = TestClient(app)
    image = np.full((700, 900, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (60, 60), (420, 90), (0, 0, 0), thickness=10)
    cv2.rectangle(image, (80, 180), (160, 420), (0, 0, 0), thickness=10)
    cv2.rectangle(image, (520, 120), (610, 300), (0, 0, 0), thickness=10)
    cv2.rectangle(image, (650, 200), (790, 260), (0, 0, 0), thickness=10)
    _, encoded = cv2.imencode(".png", image)

    response = client.post(
        "/extractions",
        files={"file": ("sample_floorplan.png", encoded.tobytes(), "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["elements"] == []
    assert body["status"] == "no_measurements"
    assert body["warnings"]


def test_extraction_endpoint_rejects_unsupported_files() -> None:
    response = TestClient(app).post(
        "/extractions",
        files={"file": ("notes.txt", b"not a drawing", "text/plain")},
    )

    assert response.status_code == 415