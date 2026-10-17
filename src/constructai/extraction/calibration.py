"""Verified drawing-scale calibration from dimension annotations.

OpenCV cannot discover a real-world scale from pixels alone. This module
derives one only from the drawing's own evidence:

1. Take each *linear* dimension annotation that was read and confirmed
   (e.g. ``3'-9"``, ``23'-0"``) together with its position.
2. In the annotation's upright reading frame, find the thin dimension line
   printed beside the text and the tick marks / extension lines that bound the
   segment the text sits between. Its pixel length is the reference length.
3. ``pixels_per_metre = pixel length / annotated length`` for that reference.
4. Accept a scale only when at least two independent references agree within
   a tolerance, and those agreeing references are a clear majority. One
   reference is reported as ``single_reference``; disagreement is reported as
   ``conflicting``; nothing is forced.

The resulting scale is used to cross-check annotations (a dimension whose
drawn length disagrees with the verified scale is flagged). It is never used
to invent a measurement that has no supporting evidence.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

import numpy as np

from constructai.extraction.schemas import ScaleCalibration, ScaleReference

logger = logging.getLogger("constructai.extraction.calibration")

# Algorithm parameters (configurable; not drawing values).
DEFAULT_TOLERANCE_PCT = 2.0
_SEARCH_TEXT_HEIGHTS = 2.5  # how far from the text to look for its dimension line
_MAX_LINE_THICKNESS_RATIO = 0.4  # a dimension line is thin relative to its text
_GAP_PX = 3  # tolerate tiny breaks in a scanned line


@dataclass
class LinearReference:
    """A confirmed linear dimension and where its text sits (frame coordinates)."""

    annotation_id: str | None
    raw_text: str
    value_m: Decimal
    frame: int  # rotation (deg CCW) that makes the text upright
    bbox: tuple[float, float, float, float]


def _otsu(values: np.ndarray) -> int:
    histogram = np.bincount(values.ravel(), minlength=256).astype(float)
    total = histogram.sum()
    levels = np.arange(256)
    sum_all = (levels * histogram).sum()
    weight_bg = np.cumsum(histogram)
    sum_bg = np.cumsum(levels * histogram)
    weight_fg = total - weight_bg
    valid = (weight_bg > 0) & (weight_fg > 0)
    mean_bg = np.where(valid, sum_bg / np.maximum(weight_bg, 1), 0)
    mean_fg = np.where(valid, (sum_all - sum_bg) / np.maximum(weight_fg, 1), 0)
    variance = np.where(valid, weight_bg * weight_fg * (mean_bg - mean_fg) ** 2, -1)
    return int(np.argmax(variance))


def _vertical_thickness(ink: np.ndarray, row: int, column: int) -> int:
    top = row
    while top > 0 and ink[top - 1, column]:
        top -= 1
    bottom = row
    while bottom < ink.shape[0] - 1 and ink[bottom + 1, column]:
        bottom += 1
    return bottom - top + 1


def _run(ink: np.ndarray, row: int, start: int, step: int) -> int:
    position, gap = start, 0
    limit = ink.shape[1] - 1
    while 0 < position < limit:
        nxt = position + step
        if ink[row, nxt]:
            gap = 0
        else:
            gap += 1
            if gap > _GAP_PX:
                break
        position = nxt
    return position - step * gap


def measure_reference(ink: np.ndarray, reference: LinearReference) -> tuple[float, str] | None:
    """Pixel length of the dimension line belonging to one annotation."""

    height_px, width_px = ink.shape
    x0, y0, x1, y1 = (int(round(v)) for v in reference.bbox)
    x0, x1 = max(1, x0), min(width_px - 2, x1)
    text_h = max(4, y1 - y0)
    cx = (x0 + x1) // 2
    reach = int(_SEARCH_TEXT_HEIGHTS * text_h)
    rows = [*range(min(height_px - 1, y1), min(height_px - 1, y1 + reach)), *range(max(0, y0 - reach), max(0, y0))]

    best: tuple[int, int, int, int] | None = None  # (run length, row, left, right)
    for row in rows:
        through_text = bool(ink[row, max(0, cx - 1): cx + 2].any())
        beside_text = bool(ink[row, max(0, x0 - 4): x0].any() and ink[row, x1 + 1: x1 + 5].any())
        if not (through_text or beside_text):
            continue
        probe = cx if through_text else x0 - 2
        if _vertical_thickness(ink, row, probe) > _MAX_LINE_THICKNESS_RATIO * text_h:
            continue  # a wall or filled area, not a thin dimension line
        left = _run(ink, row, cx if through_text else x0 - 2, -1)
        right = _run(ink, row, cx if through_text else x1 + 2, +1)
        if right - left < (x1 - x0):
            continue
        if best is None or right - left > best[0]:
            best = (right - left, row, left, right)
    if best is None:
        return None

    _, row, left, right = best
    # Line edges at a plain (non-tick) column next to the text.
    plain = x0 - 2 if ink[row, max(0, x0 - 2)] else cx
    top = row
    while top > 0 and ink[top - 1, plain]:
        top -= 1
    bottom = row
    while bottom < height_px - 1 and ink[bottom + 1, plain]:
        bottom += 1
    min_tick = max(3, int(0.25 * text_h))  # tick / extension line must stand out of the line
    tick_columns = []
    for column in range(left, right + 1):
        if x0 <= column <= x1:
            continue  # strokes of the text itself are not ticks
        up = 0
        while top - 1 - up >= 0 and ink[top - 1 - up, column]:
            up += 1
        down = 0
        while bottom + 1 + down < height_px and ink[bottom + 1 + down, column]:
            down += 1
        if up >= min_tick or down >= min_tick:
            tick_columns.append(column)
    groups: list[list[int]] = []
    for column in tick_columns:
        if groups and column - groups[-1][-1] <= 2:
            groups[-1].append(column)
        else:
            groups.append([column])
    centres = [(g[0] + g[-1]) / 2 for g in groups]
    left_ticks = [c for c in centres if c < cx]
    right_ticks = [c for c in centres if c > cx]
    start = max(left_ticks) if left_ticks else float(left)
    end = min(right_ticks) if right_ticks else float(right)
    how = "ticks" if left_ticks and right_ticks else "line ends"
    length = end - start
    return (length, how) if length > 0 else None


def calibrate(page: int, frame_images: dict[int, np.ndarray], references: list[LinearReference]) -> ScaleCalibration:
    tolerance = float(os.getenv("CONSTRUCTAI_SCALE_TOLERANCE_PCT", DEFAULT_TOLERANCE_PCT))
    measured: list[ScaleReference] = []
    for reference in references:
        image = frame_images.get(reference.frame)
        if image is None or reference.value_m <= 0:
            continue
        ink = image <= min(_otsu(image), 200)
        result = measure_reference(ink, reference)
        if result is None:
            continue
        pixels, how = result
        measured.append(
            ScaleReference(
                annotation_id=reference.annotation_id,
                raw_text=reference.raw_text,
                value_m=reference.value_m,
                pixel_length=round(pixels, 1),
                pixels_per_metre=round(pixels / float(reference.value_m), 3),
                orientation="horizontal" if reference.frame == 0 else "vertical",
            )
        )
        logger.info("Scale reference %s: %.1f px (%s) -> %.2f px/m", reference.raw_text, pixels, how, pixels / float(reference.value_m))

    if not measured:
        return ScaleCalibration(
            page=page,
            status="unavailable",
            tolerance_pct=tolerance,
            reason="no confirmed linear dimension could be matched to a dimension line; real-world calibration unavailable",
        )
    if len(measured) == 1:
        return ScaleCalibration(
            page=page,
            status="single_reference",
            tolerance_pct=tolerance,
            references=measured,
            reason="only one dimension reference was measured; a scale needs at least two agreeing references",
        )

    # Largest group of references that agree with each other within tolerance.
    best_group: list[ScaleReference] = []
    for candidate in measured:
        group = [
            other for other in measured
            if abs(other.pixels_per_metre - candidate.pixels_per_metre) / candidate.pixels_per_metre * 100 <= tolerance
        ]
        if len(group) > len(best_group):
            best_group = group
    consensus = median(r.pixels_per_metre for r in best_group)
    for reference in measured:
        reference.agrees_with_consensus = (
            abs(reference.pixels_per_metre - consensus) / consensus * 100 <= tolerance
        )
    agreeing = [r for r in measured if r.agrees_with_consensus]
    spread = (max(r.pixels_per_metre for r in agreeing) - min(r.pixels_per_metre for r in agreeing)) / consensus * 100

    if len(agreeing) >= 2 and len(agreeing) > len(measured) / 2:
        outliers = len(measured) - len(agreeing)
        return ScaleCalibration(
            page=page,
            status="verified",
            pixels_per_metre=round(consensus, 3),
            tolerance_pct=tolerance,
            spread_pct=round(spread, 3),
            references=measured,
            reason=(
                f"{len(agreeing)} of {len(measured)} independent dimension references agree within "
                f"{tolerance:g}% (spread {spread:.2f}%)"
                + (f"; {outliers} disagreeing reference(s) flagged" if outliers else "")
            ),
        )
    return ScaleCalibration(
        page=page,
        status="conflicting",
        tolerance_pct=tolerance,
        references=measured,
        reason=(
            f"dimension references disagree: the largest agreeing group has {len(agreeing)} of "
            f"{len(measured)}; no scale is used (the drawing may not be to scale)"
        ),
    )
