"""Tesseract OCR for construction drawings.

``pytesseract`` is only a Python wrapper: it shells out to the Tesseract
executable. This module therefore (1) locates and verifies the executable,
(2) preprocesses the drawing for OCR and (3) returns positioned text lines
so dimension strings can be associated with nearby labels.

Nothing here produces measurements; it only reports what text was read and
where it was found.
"""

from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

logger = logging.getLogger("constructai.extraction.ocr")

# Where the UB-Mannheim Windows installer puts tesseract.exe by default.
_WINDOWS_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
)

# Image-processing parameters (algorithmic constants, not drawing values).
_TESSERACT_CONFIG = "--oem 1 --psm 11 -c preserve_interword_spaces=1"
_MAX_OCR_SIDE = 6000  # px; keeps upscaled passes within Tesseract's comfort zone
_DESKEW_RANGE_DEG = 5.0
_DESKEW_STEP_DEG = 0.25
_DESKEW_MIN_APPLY_DEG = 0.5
_LOW_CONTRAST_STDDEV = 40.0
_SMALL_IMAGE_SIDE = 2000  # px; below this an extra upscaled OCR pass is added
_SMALL_TEXT_TARGET_SIDE = 4500


class OCRUnavailableError(RuntimeError):
    """Raised when the Tesseract engine cannot be used. Never silently ignored."""


@dataclass
class TesseractStatus:
    available: bool
    command: str | None = None
    version: str | None = None
    languages: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "command": self.command,
            "version": self.version,
            "languages": self.languages,
            "error": self.error,
        }


@dataclass
class TextLine:
    """One line of text read from a drawing page.

    ``bbox`` is in the upright *reading frame* of the line (the page rotated
    by ``frame`` degrees counter-clockwise), so "below" and "above" have a
    meaning for vertical text too. ``page_bbox`` is in page coordinates
    (pixels for raster images, PDF points or SVG user units otherwise).
    """

    text: str
    bbox: tuple[float, float, float, float]
    page_bbox: tuple[float, float, float, float]
    frame: int
    confidence: float  # 0..1
    page: int
    source: str  # "tesseract_ocr" | "pdf_text_layer" | "svg_text"
    pass_name: str
    geometry_reliable: bool = True

    @property
    def height(self) -> float:
        return max(1.0, self.bbox[3] - self.bbox[1])


@lru_cache(maxsize=1)
def tesseract_status() -> TesseractStatus:
    """Locate and verify the Tesseract executable (cached per process)."""

    try:
        import pytesseract
    except ImportError:
        return TesseractStatus(
            available=False,
            error="Python package 'pytesseract' is not installed (pip install pytesseract).",
        )

    candidates: list[str] = []
    configured = os.getenv("TESSERACT_CMD")
    if configured:
        candidates.append(configured)
    found_on_path = shutil.which("tesseract")
    if found_on_path:
        candidates.append(found_on_path)
    if os.name == "nt":
        candidates.extend(_WINDOWS_CANDIDATES)

    tried: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        if not Path(candidate).exists() and shutil.which(candidate) is None:
            continue
        pytesseract.pytesseract.tesseract_cmd = candidate
        try:
            version = str(pytesseract.get_tesseract_version())
            languages = pytesseract.get_languages(config="")
        except Exception as error:  # executable present but broken
            logger.warning("Tesseract at %s could not run: %s", candidate, error)
            continue
        logger.info("Tesseract found: %s (version %s, languages %s)", candidate, version, languages)
        return TesseractStatus(True, candidate, version, list(languages))

    message = (
        "Tesseract OCR executable not found. pytesseract is only a wrapper and needs the "
        "Tesseract program installed. Install it (Windows: UB-Mannheim installer) and either add "
        "its folder to PATH or set TESSERACT_CMD to the full path of tesseract.exe. "
        f"Locations checked: {tried or ['(none)']}"
    )
    logger.error(message)
    return TesseractStatus(False, error=message)


def require_tesseract() -> TesseractStatus:
    status = tesseract_status()
    if not status.available:
        raise OCRUnavailableError(status.error or "Tesseract OCR is unavailable")
    return status


# ---------------------------------------------------------------- preprocessing


def flatten_to_grayscale(image: Image.Image) -> Image.Image:
    """Apply EXIF orientation, composite transparency onto white, convert to L."""

    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, rgba)
    return image.convert("L")


def _otsu_threshold(gray: Image.Image) -> int:
    histogram = gray.histogram()
    total = sum(histogram)
    sum_all = sum(i * h for i, h in enumerate(histogram))
    sum_bg = weight_bg = 0
    best_variance, threshold = -1.0, 128
    for level in range(256):
        weight_bg += histogram[level]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += level * histogram[level]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if variance > best_variance:
            best_variance, threshold = variance, level
    return threshold


def _pixels(image: Image.Image) -> list[int]:
    # Pillow >= 12.1 renamed getdata() to get_flattened_data(); support both.
    getter = getattr(image, "get_flattened_data", None) or image.getdata
    return list(getter())


def _profile_score(binary: Image.Image) -> float:
    rows = _pixels(binary.resize((1, binary.height), Image.BOX))
    cols = _pixels(binary.resize((binary.width, 1), Image.BOX))

    def variance(values: list[int]) -> float:
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values) / len(values)

    return variance(rows) + variance(cols)


def estimate_skew(gray: Image.Image) -> float:
    """Estimate small scan skew (degrees, PIL CCW convention) by projection profiles."""

    small = gray.copy()
    small.thumbnail((1000, 1000))
    threshold = _otsu_threshold(small)
    ink = small.point(lambda p: 255 if p < threshold else 0)  # ink = white for profiles
    best_angle, best_score = 0.0, _profile_score(ink)
    zero_score = best_score
    steps = int(_DESKEW_RANGE_DEG / _DESKEW_STEP_DEG)
    for step in range(-steps, steps + 1):
        angle = step * _DESKEW_STEP_DEG
        if angle == 0:
            continue
        score = _profile_score(ink.rotate(angle, resample=Image.BILINEAR, fillcolor=0))
        if score > best_score:
            best_angle, best_score = angle, score
    if abs(best_angle) < _DESKEW_MIN_APPLY_DEG or best_score < zero_score * 1.05:
        return 0.0
    return best_angle


@dataclass
class PreparedPage:
    base: Image.Image  # deskewed grayscale page used as the OCR reference frame
    width: int
    height: int
    deskew_angle: float
    steps: list[str]
    low_contrast: bool


def prepare_page(image: Image.Image) -> PreparedPage:
    steps = ["exif-orientation", "alpha-flatten-on-white", "grayscale"]
    gray = flatten_to_grayscale(image)
    angle = estimate_skew(gray)
    if angle:
        gray = gray.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
        steps.append(f"deskew {angle:+.2f} deg")
    else:
        steps.append("deskew: none needed")
    stddev = ImageStat.Stat(gray).stddev[0]
    low_contrast = stddev < _LOW_CONTRAST_STDDEV
    if low_contrast:
        steps.append(f"low contrast detected (stddev {stddev:.1f})")
    return PreparedPage(gray, gray.width, gray.height, angle, steps, low_contrast)


def _variants(page: PreparedPage) -> list[tuple[str, Image.Image, float]]:
    long_side = max(page.width, page.height)
    scale = min(max(3000 / long_side, 1.5), 3.0, _MAX_OCR_SIDE / long_side)
    scale = max(scale, 1.0)
    enhanced = ImageOps.autocontrast(page.base, cutoff=1)
    if page.low_contrast:
        enhanced = enhanced.filter(ImageFilter.MedianFilter(3))
        threshold = _otsu_threshold(enhanced)
        enhanced = enhanced.point(lambda p: 255 if p > threshold else 0)
    # Small images: read the plain pass upscaled too (tiny text at 1x mostly yields noise).
    gray_scale = max(1.0, min(4.0, 2500 / long_side)) if long_side < _SMALL_IMAGE_SIDE else 1.0
    gray = page.base if gray_scale == 1.0 else page.base.resize(
        (round(page.width * gray_scale), round(page.height * gray_scale)), Image.LANCZOS
    )
    variants = [(f"gray-x{gray_scale:.2f}", gray, gray_scale)]
    if scale != 1.0:
        variants.append((
            f"enhanced-x{scale:.2f}" + ("-otsu" if page.low_contrast else ""),
            enhanced.resize((round(page.width * scale), round(page.height * scale)), Image.LANCZOS),
            scale,
        ))
    else:
        variants.append(("enhanced-x1.00" + ("-otsu" if page.low_contrast else ""), enhanced, 1.0))
    # Low-resolution images: an extra, strongly upscaled pass so very small
    # dimension text becomes readable. Added, never replacing the passes above.
    if long_side < _SMALL_IMAGE_SIDE:
        big = min(4.0, _SMALL_TEXT_TARGET_SIDE / long_side, _MAX_OCR_SIDE / long_side)
        if big > scale * 1.2:
            variants.append((
                f"smalltext-x{big:.2f}",
                ImageOps.autocontrast(page.base, cutoff=1).resize(
                    (round(page.width * big), round(page.height * big)), Image.LANCZOS
                ),
                big,
            ))
    return variants


# ---------------------------------------------------------------- geometry


def frame_to_page(bbox, rotation: int, width: float, height: float):
    """Map a bbox from a frame rotated ``rotation`` deg CCW back to page coords."""

    x0, y0, x1, y1 = bbox
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    if rotation == 90:
        points = [(width - v, u) for u, v in corners]
    elif rotation == 270:
        points = [(v, height - u) for u, v in corners]
    else:
        points = corners
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def page_to_frame(bbox, rotation: int, width: float, height: float):
    """Inverse of :func:`frame_to_page`."""

    x0, y0, x1, y1 = bbox
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    if rotation == 90:
        points = [(y, width - x) for x, y in corners]
    elif rotation == 270:
        points = [(height - y, x) for x, y in corners]
    else:
        points = corners
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------- OCR passes


def _rotations() -> list[int]:
    raw = os.getenv("CONSTRUCTAI_OCR_ROTATIONS", "0,90,270")
    values = []
    for part in raw.split(","):
        part = part.strip()
        if part in {"0", "90", "270"}:
            values.append(int(part))
    return values or [0]


def _run_pass(image: Image.Image, rotation: int, scale: float, pass_name: str, page: PreparedPage, page_number: int, language: str):
    import pytesseract

    rotated = image.rotate(rotation, expand=True, fillcolor=255) if rotation else image
    data = pytesseract.image_to_data(
        rotated, lang=language, config=_TESSERACT_CONFIG, output_type=pytesseract.Output.DICT
    )
    groups: dict[tuple[int, int, int], list[int]] = {}
    for index, word in enumerate(data["text"]):
        if not str(word).strip():
            continue
        try:
            conf = float(data["conf"][index])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        groups.setdefault(key, []).append(index)

    frame_w = page.height if rotation in (90, 270) else page.width
    frame_h = page.width if rotation in (90, 270) else page.height
    lines: list[TextLine] = []
    for indices in groups.values():
        indices.sort(key=lambda i: data["left"][i])
        text = " ".join(str(data["text"][i]).strip() for i in indices)
        x0 = min(data["left"][i] for i in indices) / scale
        y0 = min(data["top"][i] for i in indices) / scale
        x1 = max(data["left"][i] + data["width"][i] for i in indices) / scale
        y1 = max(data["top"][i] + data["height"][i] for i in indices) / scale
        frame_bbox = (x0, y0, min(x1, frame_w), min(y1, frame_h))
        confidence = sum(float(data["conf"][i]) for i in indices) / len(indices) / 100.0
        lines.append(
            TextLine(
                text=text,
                bbox=frame_bbox,
                page_bbox=frame_to_page(frame_bbox, rotation, page.width, page.height),
                frame=rotation,
                confidence=max(0.0, min(1.0, confidence)),
                page=page_number,
                source="tesseract_ocr",
                pass_name=f"{pass_name}/rot{rotation}",
            )
        )
    return lines


# (extra upscale, autocontrast, tesseract page-segmentation mode). The base
# upscale is chosen per line so that its text is roughly _REREAD_TEXT_PX tall.
_REREAD_VARIANTS = (
    (0, False, 7),
    (1, True, 7),
    (0, False, 6),
    (1, True, 6),
)
_REREAD_TEXT_PX = 36


class OCRSession:
    """OCR state for one rendered page: full-page passes plus targeted re-reads."""

    def __init__(self, image: Image.Image, page_number: int):
        self.status = require_tesseract()
        self.language = os.getenv("CONSTRUCTAI_OCR_LANG", "eng")
        self.page_number = page_number
        self.page = prepare_page(image)
        self._frames: dict[int, Image.Image] = {0: self.page.base}
        self.reread_count = 0
        logger.info("Page %s preprocessing: %s", page_number, "; ".join(self.page.steps))

    def frame_image(self, rotation: int) -> Image.Image:
        if rotation not in self._frames:
            self._frames[rotation] = self.page.base.rotate(rotation, expand=True, fillcolor=255)
        return self._frames[rotation]

    def read(self) -> tuple[list[TextLine], dict]:
        jobs = [
            (variant_image, rotation, scale, name)
            for name, variant_image, scale in _variants(self.page)
            for rotation in _rotations()
        ]
        workers = max(1, min(len(jobs), os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_run_pass, img, rot, scale, name, self.page, self.page_number, self.language)
                for img, rot, scale, name in jobs
            ]
            results = [future.result() for future in futures]

        lines = [line for result in results for line in result]
        raw_text: dict[str, list[str]] = {}
        for line in lines:
            raw_text.setdefault(line.pass_name, []).append(line.text)
        for pass_name, texts in raw_text.items():
            logger.info("Raw OCR text page %s [%s]: %s", self.page_number, pass_name, " | ".join(texts))

        diagnostics = {
            "engine": "tesseract",
            "tesseract": self.status.as_dict(),
            "tesseract_config": _TESSERACT_CONFIG,
            "language": self.language,
            "page": self.page_number,
            "image_size_px": [self.page.width, self.page.height],
            "preprocessing": self.page.steps,
            "deskew_angle_deg": self.page.deskew_angle,
            "passes": sorted(raw_text),
            "raw_text": raw_text,
        }
        return lines, diagnostics

    def reread_many(self, requests: list[tuple[int, tuple[float, float, float, float]]]) -> list[list[TextLine]]:
        """Re-OCR specific line regions (frame, frame-bbox) as single-line crops."""

        if not requests:
            return []
        for rotation in {frame for frame, _ in requests}:
            self.frame_image(rotation)
        workers = max(1, min(len(requests), os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda req: self._reread(*req), requests))

    def _reread(self, frame: int, bbox: tuple[float, float, float, float]) -> list[TextLine]:
        import pytesseract

        image = self._frames[frame]
        x0, y0, x1, y1 = bbox
        pad = 0.5 * max(1.0, y1 - y0) + 4
        left, top = max(0, int(x0 - pad)), max(0, int(y0 - pad))
        right, bottom = min(image.width, int(x1 + pad) + 1), min(image.height, int(y1 + pad) + 1)
        if right - left < 4 or bottom - top < 4:
            return []
        crop = image.crop((left, top, right, bottom))
        base_scale = int(min(6, max(2, round(_REREAD_TEXT_PX / max(1.0, y1 - y0)))))
        lines: list[TextLine] = []
        for extra, contrast, psm in _REREAD_VARIANTS:
            scale = base_scale + extra
            variant = ImageOps.autocontrast(crop, cutoff=1) if contrast else crop
            variant = variant.resize((variant.width * scale, variant.height * scale), Image.LANCZOS)
            data = pytesseract.image_to_data(
                variant,
                lang=self.language,
                config=f"--oem 1 --psm {psm} -c preserve_interword_spaces=1",
                output_type=pytesseract.Output.DICT,
            )
            self.reread_count += 1
            groups: dict[tuple[int, int, int], list[int]] = {}
            for index, word in enumerate(data["text"]):
                try:
                    conf = float(data["conf"][index])
                except (TypeError, ValueError):
                    conf = -1.0
                if str(word).strip() and conf >= 0:
                    key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
                    groups.setdefault(key, []).append(index)
            for indices in groups.values():
                indices.sort(key=lambda i: data["left"][i])
                fx0 = left + min(data["left"][i] for i in indices) / scale
                fy0 = top + min(data["top"][i] for i in indices) / scale
                fx1 = left + max(data["left"][i] + data["width"][i] for i in indices) / scale
                fy1 = top + max(data["top"][i] + data["height"][i] for i in indices) / scale
                frame_bbox = (fx0, fy0, fx1, fy1)
                lines.append(
                    TextLine(
                        text=" ".join(str(data["text"][i]).strip() for i in indices),
                        bbox=frame_bbox,
                        page_bbox=frame_to_page(frame_bbox, frame, self.page.width, self.page.height),
                        frame=frame,
                        confidence=sum(float(data["conf"][i]) for i in indices) / len(indices) / 100.0,
                        page=self.page_number,
                        source="tesseract_ocr",
                        pass_name=f"reread-x{scale}{'-contrast' if contrast else ''}-psm{psm}/rot{frame}",
                    )
                )
        return lines
