"""Free/local extraction provider (no paid API).

Routing per file type:

* SVG / PDF containing ``ELEMENT|...`` records -> the original deterministic
  test-fixture parser (unchanged behaviour, used by the existing tests).
* SVG with real <text> nodes        -> exact text + positions from the SVG.
* PDF with a text layer             -> exact text + positions from PyMuPDF.
* PDF pages without usable text, PNG/JPG/WEBP, outlined SVG
                                    -> rendered, preprocessed, Tesseract OCR.

All real drawings then go through :mod:`constructai.extraction.annotations`,
which reads dimension annotations, normalises units and associates them with
labels. Drawing coordinates (pixels, PDF points, SVG units) are never
converted into real-world lengths: no scale is assumed.

Previous behaviour (the root cause of empty results): PDF and image files
were read with ``Path.read_text()`` - i.e. the binary bytes were decoded as
UTF-8 and searched for ``ELEMENT|`` - so every real drawing returned an empty
element list with HTTP 200 and no warning. OCR was never invoked.
"""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

from constructai.documents.renderer import SUPPORTED_DRAWING_EXTENSIONS, DrawingDocument
from constructai.extraction.annotations import PageInterpretation, _Ids, interpret_page
from constructai.extraction.dimensions import parse_line
from constructai.extraction.ocr import OCRSession, TextLine, page_to_frame
from constructai.extraction.schemas import Dimensions, ExtractedElement, ExtractionResult

logger = logging.getLogger("constructai.extraction.local")

NO_SCALE_MESSAGE = (
    "not established - real-world values come only from dimension annotations; drawing "
    "geometry (pixels/points/SVG units) is not converted. Calibration is required for any "
    "geometry-based measurement."
)
_ELEMENT_PATTERN = re.compile(r"ELEMENT\|(?P<fields>[^\n<]+)")


class LocalExtractionProvider:
    supported_extensions = tuple(sorted(SUPPORTED_DRAWING_EXTENSIONS))

    def extract(self, drawing: Path) -> ExtractionResult:
        drawing = Path(drawing)
        if not drawing.exists():
            raise FileNotFoundError(f"Drawing not found: {drawing}")
        extension = drawing.suffix.lower()
        if extension not in self.supported_extensions:
            supported = ", ".join(self.supported_extensions)
            raise ValueError(f"Unsupported drawing format '{extension}'. Supported formats: {supported}")

        logger.info("Local extraction started: %s (%s bytes)", drawing.name, drawing.stat().st_size)

        # 1) Original deterministic fixture records (kept for the existing tests).
        fixture_text = self._fixture_text(drawing, extension)
        if fixture_text and _ELEMENT_PATTERN.search(fixture_text):
            elements = self._extract_elements(fixture_text)
            logger.info("ELEMENT| fixture records found: %s element(s)", len(elements))
            return ExtractionResult(
                source_name=drawing.name,
                elements=elements,
                extraction_method="text_fixture",
                real_world_scale="not applicable (values supplied by ELEMENT| fixture records)",
            )

        # 2) Real drawing interpretation.
        ids = _Ids()
        pages: list[PageInterpretation] = []
        page_diagnostics: list[dict] = []
        methods: set[str] = set()
        if extension == ".svg":
            self._extract_svg(drawing, ids, pages, page_diagnostics, methods)
        elif extension == ".pdf":
            self._extract_pdf(drawing, ids, pages, page_diagnostics, methods)
        else:
            for page in DrawingDocument(drawing).render_pages():
                self._ocr_page(page.image, page.page_number, ids, pages, page_diagnostics, methods)

        return self._assemble(drawing.name, pages, page_diagnostics, methods)

    # ------------------------------------------------------------------ routing

    @staticmethod
    def _fixture_text(drawing: Path, extension: str) -> str:
        if extension == ".svg":
            # Raw text: fixture records may live in <text> nodes or XML comments.
            return drawing.read_text(encoding="utf-8", errors="ignore")
        if extension == ".pdf":
            import pymupdf

            with pymupdf.open(drawing) as document:
                return "\n".join(page.get_text() for page in document)
        return ""  # raster images have no text layer

    def _ocr_page(self, image, page_number, ids, pages, page_diagnostics, methods) -> None:
        session = OCRSession(image, page_number)
        lines, diagnostics = session.read()
        interpretation = interpret_page(lines, page_number, ids, session=session)
        _apply_calibration(interpretation, session, page_number)
        diagnostics.update(interpretation.diagnostics)
        page_diagnostics.append(diagnostics)
        pages.append(interpretation)
        methods.add("tesseract_ocr")

    def _extract_pdf(self, drawing, ids, pages, page_diagnostics, methods) -> None:
        import pymupdf

        rendered = None
        with pymupdf.open(drawing) as document:
            for index, page in enumerate(document, start=1):
                lines = _pdf_text_lines(page, index)
                has_dimension_text = any(parse_line(line.text).has_measurement for line in lines)
                if lines and has_dimension_text:
                    logger.info("PDF page %s: using text layer (%s lines)", index, len(lines))
                    interpretation = interpret_page(lines, index, ids)
                    page_diagnostics.append(
                        {
                            "engine": "pdf_text_layer",
                            "page": index,
                            "page_size_pt": [page.rect.width, page.rect.height],
                            "raw_text": {"pdf_text_layer": [line.text for line in lines]},
                            **interpretation.diagnostics,
                        }
                    )
                    pages.append(interpretation)
                    methods.add("pdf_text_layer")
                    continue
                logger.info("PDF page %s: no dimension text in text layer; rendering for OCR", index)
                if rendered is None:
                    rendered = {p.page_number: p for p in DrawingDocument(drawing).render_pages()}
                self._ocr_page(rendered[index].image, index, ids, pages, page_diagnostics, methods)

    def _extract_svg(self, drawing, ids, pages, page_diagnostics, methods) -> None:
        lines, width, height = _svg_text_lines(drawing)
        if lines:
            logger.info("SVG: %s <text> node(s) read directly", len(lines))
            interpretation = interpret_page(lines, 1, ids)
            page_diagnostics.append(
                {
                    "engine": "svg_text",
                    "page": 1,
                    "svg_size_user_units": [width, height],
                    "geometry_reliable": all(line.geometry_reliable for line in lines),
                    "raw_text": {"svg_text": [line.text for line in lines]},
                    **interpretation.diagnostics,
                }
            )
            pages.append(interpretation)
            methods.add("svg_text")
            return
        logger.info("SVG has no <text> nodes (text may be outlined); rendering for OCR")
        for page in DrawingDocument(drawing).render_pages():
            self._ocr_page(page.image, page.page_number, ids, pages, page_diagnostics, methods)

    # ------------------------------------------------------------------ result

    @staticmethod
    def _assemble(source_name, pages, page_diagnostics, methods) -> ExtractionResult:
        calibrations = [p.scale for p in pages if p.scale is not None]
        elements = [e for p in pages for e in p.elements]
        annotations = [a for p in pages for a in p.annotations]
        unresolved = [u for p in pages for u in p.unresolved]
        warnings = [w for p in pages for w in p.warnings]

        if not annotations:
            status = "no_measurements"
            warnings.append(
                "No dimension annotations could be read from this drawing. No measurements were "
                "returned; nothing was estimated or defaulted."
            )
        elif unresolved or any(a.status != "extracted" for a in annotations):
            status = "partial"
        else:
            status = "complete"
        if annotations and not elements:
            warnings.append(
                "Dimension text was read, but none could be confidently tied to a room/element. "
                "See dimension_annotations and unresolved for the raw readings."
            )
        unverified = sum(1 for a in annotations if a.status != "extracted")
        if unverified:
            warnings.append(f"{unverified} dimension reading(s) are unverified/unreadable and were not used for quantities.")
        unassociated = sum(1 for a in annotations if a.kind in {"linear", "pair"} and a.association_status != "associated")
        if unassociated:
            warnings.append(
                f"{unassociated} dimension annotation(s) are not linked to an element (e.g. wall/chain "
                "dimensions); they are listed for review but not used for quantities."
            )

        logger.info(
            "Extraction finished: status=%s elements=%s annotations=%s unresolved=%s",
            status, len(elements), len(annotations), len(unresolved),
        )
        return ExtractionResult(
            source_name=source_name,
            elements=elements,
            status=status,
            extraction_method="+".join(sorted(methods)) or None,
            real_world_scale=_scale_message(calibrations),
            scale_calibration=calibrations,
            warnings=warnings,
            dimension_annotations=annotations,
            unresolved=unresolved,
            diagnostics={"pages": page_diagnostics},
        )

    # ------------------------------------------------------------------ fixture parser (unchanged)

    def _extract_elements(self, text: str) -> list[ExtractedElement]:
        """Extract ELEMENT records from drawing text."""

        elements: list[ExtractedElement] = []
        for match in _ELEMENT_PATTERN.finditer(text):
            fields = self._parse_fields(match.group("fields"))
            if not fields.get("element_type"):
                continue
            elements.append(
                ExtractedElement(
                    element_type=fields.get("element_type", "unknown"),
                    element_id=fields.get("element_id", "unknown"),
                    host_element_id=fields.get("host_element_id") or None,  # door/window -> host wall (BOM deductions)
                    description=fields.get("description", ""),
                    location=fields.get("location", ""),
                    dimensions=self._parse_dimensions(fields.get("dimensions")),
                    quantity=self._decimal(fields.get("quantity")),
                    unit=fields.get("unit", ""),
                    specification=fields.get("specification", ""),
                    source_page=1,
                    source_reference=None,
                    confidence=self._float(fields.get("confidence"), default=0.0),
                    measurement_source="text_fixture",
                )
            )
        return elements

    def _parse_fields(self, field_text: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for part in field_text.split("|"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
        return fields

    def _parse_dimensions(self, value: str | None) -> Dimensions | None:
        if not value:
            return None
        dimensions: dict[str, float] = {}
        for part in value.split(","):
            if ":" not in part:
                continue
            key, raw_value = part.split(":", 1)
            parsed = self._float(raw_value.strip())
            if parsed is not None:
                dimensions[key.strip()] = parsed
        return Dimensions(**dimensions) if dimensions else None

    @staticmethod
    def _decimal(value: str | None):
        if value is None or value == "":
            return None
        try:
            return Decimal(value)
        except Exception:
            return None

    @staticmethod
    def _float(value: str | None, default: float | None = None) -> float | None:
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


# ---------------------------------------------------------------------- scale calibration


def _apply_calibration(interpretation: PageInterpretation, session: OCRSession, page_number: int) -> None:
    """Calibrate the page scale from its own dimensions and cross-check them."""

    import numpy as np

    from constructai.extraction.calibration import calibrate

    frames = {ref.frame for ref in interpretation.linear_references}
    images = {frame: np.asarray(session.frame_image(frame)) for frame in frames}
    scale = calibrate(page_number, images, interpretation.linear_references)
    interpretation.scale = scale
    logger.info("Page %s scale calibration: %s (%s)", page_number, scale.status, scale.reason)

    if scale.status != "verified":
        return
    by_id = {a.annotation_id: a for a in interpretation.annotations}
    for reference in scale.references:
        annotation = by_id.get(reference.annotation_id)
        if annotation is None:
            continue
        if reference.agrees_with_consensus:
            annotation.notes.append(
                f"drawn length ({reference.pixel_length:.0f} px) agrees with the verified drawing scale"
            )
        else:
            expected = Decimal(str(reference.pixel_length / scale.pixels_per_metre)).quantize(Decimal("0.001"))
            message = (
                f"'{reference.raw_text}' is drawn {expected} m long at the verified scale, but annotated "
                f"{reference.value_m.normalize()} m - possible OCR misread or a dimension not drawn to scale"
            )
            annotation.notes.append(message)
            annotation.status = "unverified"
            interpretation.warnings.append(f"Page {page_number}: {message}")


def _scale_message(calibrations) -> str:
    verified = [c for c in calibrations if c.status == "verified"]
    if verified:
        parts = "; ".join(
            f"page {c.page}: {c.pixels_per_metre:g} px per metre ({c.reason})" for c in verified
        )
        return (
            f"verified from the drawing's own dimensions - {parts}. Used to cross-check annotations; "
            "no element dimension was derived from geometry."
        )
    if calibrations:
        return "not established - " + "; ".join(f"page {c.page}: {c.reason}" for c in calibrations)
    return NO_SCALE_MESSAGE


# ---------------------------------------------------------------------- PDF text layer


def _pdf_text_lines(page, page_number: int) -> list[TextLine]:
    width, height = page.rect.width, page.rect.height
    lines: list[TextLine] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not text:
                continue
            dx, dy = line.get("dir", (1, 0))
            if abs(dx) >= abs(dy):
                frame, reliable = (0, dx > 0)
            else:
                frame, reliable = (270 if dy < 0 else 90), True
            page_bbox = tuple(float(v) for v in line["bbox"])
            lines.append(
                TextLine(
                    text=text,
                    bbox=page_to_frame(page_bbox, frame, width, height),
                    page_bbox=page_bbox,
                    frame=frame,
                    confidence=1.0,
                    page=page_number,
                    source="pdf_text_layer",
                    pass_name="pdf_text_layer",
                    geometry_reliable=reliable,
                )
            )
    return lines


# ---------------------------------------------------------------------- SVG text nodes

_SVG_NS = "{http://www.w3.org/2000/svg}"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", re.IGNORECASE)
_TRANSFORM = re.compile(r"(\w+)\s*\(([^)]*)\)")
_SVG_DEFAULT_FONT_SIZE = 16.0  # CSS 'medium'; used only to size text boxes, never as a measurement


def _first_number(value: str | None) -> float | None:
    if not value:
        return None
    match = _NUMBER.search(value)
    if not match:
        return None
    number = float(match.group(0))
    return number if math.isfinite(number) else None


def _style(element, name: str) -> str | None:
    if element.get(name):
        return element.get(name)
    match = re.search(rf"{name}\s*:\s*([^;]+)", element.get("style", ""))
    return match.group(1).strip() if match else None


def _svg_size(root) -> tuple[float, float]:
    view_box = root.get("viewBox")
    if view_box:
        parts = [float(v) for v in _NUMBER.findall(view_box)]
        if len(parts) == 4:
            return parts[2], parts[3]
    return _first_number(root.get("width")) or 0.0, _first_number(root.get("height")) or 0.0


Matrix = tuple[float, float, float, float, float, float]  # SVG (a, b, c, d, e, f)
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _multiply(m: Matrix, n: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    )


def _parse_transform(text: str) -> Matrix | None:
    """SVG transform attribute -> matrix. None if it cannot be interpreted."""

    matrix = _IDENTITY
    for name, args in _TRANSFORM.findall(text or ""):
        values = [float(v) for v in _NUMBER.findall(args)]
        if not all(math.isfinite(v) for v in values):
            return None
        if name == "translate" and values:
            step = (1.0, 0.0, 0.0, 1.0, values[0], values[1] if len(values) > 1 else 0.0)
        elif name == "scale" and values:
            sx = values[0]
            sy = values[1] if len(values) > 1 else sx
            step = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate" and values:
            angle = math.radians(values[0])
            cos, sin = math.cos(angle), math.sin(angle)
            step = (cos, sin, -sin, cos, 0.0, 0.0)
            if len(values) == 3:
                cx, cy = values[1], values[2]
                step = _multiply((1, 0, 0, 1, cx, cy), _multiply(step, (1, 0, 0, 1, -cx, -cy)))
        elif name == "matrix" and len(values) == 6:
            step = tuple(values)  # type: ignore[assignment]
        elif name in {"skewX", "skewY"}:
            return None
        else:
            continue
        matrix = _multiply(matrix, step)
    return matrix


def _apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _svg_text_lines(drawing: Path) -> tuple[list[TextLine], float, float]:
    """Read <text> nodes with their positions. Positions are SVG user units and
    are used only to decide which label a dimension sits next to - they are
    never converted into real-world lengths."""

    root = ET.parse(drawing).getroot()
    width, height = _svg_size(root)
    raw: list[tuple[str, tuple[float, float, float, float], int, bool]] = []

    def walk(element, matrix: Matrix, reliable: bool, font_size, anchor):
        local = _parse_transform(element.get("transform", ""))
        if local is None:
            reliable = False
        else:
            matrix = _multiply(matrix, local)
        size = _first_number(_style(element, "font-size"))
        font_size = size if size and size > 0 else font_size
        anchor = _style(element, "text-anchor") or anchor

        if element.tag == f"{_SVG_NS}text":
            text = " ".join("".join(element.itertext()).split())
            if not text:
                return
            x = _first_number(element.get("x")) or 0.0
            y = _first_number(element.get("y")) or 0.0
            fs = font_size or _SVG_DEFAULT_FONT_SIZE
            w = 0.55 * fs * len(text)
            x0 = x - w / 2 if anchor == "middle" else x - w if anchor == "end" else x
            corners = [_apply(matrix, cx, cy) for cx, cy in ((x0, y - 0.8 * fs), (x0 + w, y - 0.8 * fs), (x0, y + 0.2 * fs), (x0 + w, y + 0.2 * fs))]
            xs, ys = [p[0] for p in corners], [p[1] for p in corners]
            box = (min(xs), min(ys), max(xs), max(ys))
            angle = math.degrees(math.atan2(matrix[1], matrix[0])) % 360
            if math.isclose(angle, 0.0, abs_tol=1) or math.isclose(angle, 360.0, abs_tol=1):
                frame = 0
            elif math.isclose(angle, 270.0, abs_tol=1):
                frame = 270  # reads bottom-to-top
            elif math.isclose(angle, 90.0, abs_tol=1):
                frame = 90  # reads top-to-bottom
            else:
                frame, reliable = 0, False
            raw.append((text, box, frame, reliable))
            return
        for child in element:
            walk(child, matrix, reliable, font_size, anchor)

    walk(root, _IDENTITY, True, None, None)
    raw = [item for item in raw if all(math.isfinite(v) for v in item[1])]
    if not width or not height:
        width = max((b[2] for _, b, _, _ in raw), default=0.0)
        height = max((b[3] for _, b, _, _ in raw), default=0.0)
    lines = [
        TextLine(
            text=text,
            bbox=page_to_frame(box, frame, width, height),
            page_bbox=box,
            frame=frame,
            confidence=1.0,
            page=1,
            source="svg_text",
            pass_name="svg_text",
            geometry_reliable=reliable,
        )
        for text, box, frame, reliable in raw
    ]
    return lines, width, height
