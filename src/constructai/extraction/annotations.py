"""Turn positioned drawing text into measured elements with provenance.

Input: :class:`TextLine` objects read from one page (OCR passes, a PDF text
layer, or SVG <text> nodes). Output: elements, dimension annotations and
unresolved items.

Principles
----------
* Every real-world value comes from a dimension annotation that was read.
  Pixel / point / SVG-unit geometry is used only to decide *which label a
  dimension sits under*; it is never converted into metres.
* OCR readings are cross-checked: the same line is read by several passes
  and targeted re-reads. A value is ``extracted`` only when independent
  readings agree; otherwise it is ``unverified`` and does not become an
  element (so it cannot reach quantities/BOQ unnoticed).
* A dimension is attached to a label only when the geometry is unambiguous;
  otherwise the association is ``uncertain`` and no element is created.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from constructai.extraction.dimensions import (
    SQ_METRES_PER_UNIT,
    ParsedLine,
    ParsedValue,
    detect_declared_unit,
    parse_line,
)
from constructai.extraction.ocr import OCRSession, TextLine
from constructai.extraction.schemas import (
    DimensionAnnotation,
    Dimensions,
    ExtractedElement,
    MeasurementValue,
    UnresolvedItem,
)

logger = logging.getLogger("constructai.extraction.annotations")

EXACT_TEXT_SOURCES = {"pdf_text_layer", "svg_text"}

# Vocabulary used only to *classify* a label that was read from the drawing.
# It never supplies a dimension.
_ROOM_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"\bmaster\b|\bbed\b|bedroom|\bbr\b", "bedroom"),
    (r"kitchen|\bkit\b|pantry", "kitchen"),
    (r"toilet|\bbath|\bwc\b|washroom|powder|w\.c", "toilet"),
    (r"living|drawing\s*room|lounge|\bhall\b|family", "living"),
    (r"dining", "dining"),
    (r"balcony|verandah|veranda|terrace|deck|sit[\s-]?out", "balcony"),
    (r"foyer|entry|entrance|lobby|vestibule", "foyer"),
    (r"pooja|puja|prayer", "pooja"),
    (r"passage|corridor|lobby", "circulation"),
    (r"store|storage|utility|laundry|wash\s*area", "utility"),
    (r"study|office|library", "study"),
    (r"garage|parking|porch", "parking"),
    (r"stair|staircase", "stair"),
    (r"\broom\b|guest|dress|closet|wardrobe", "room"),
)
_NON_INSTANCE_KEYWORDS = re.compile(
    r"\bcolumn\b|\bbeam\b|\bfooting\b|\blintel\b|\bdoor\b|\bwindow\b|\bwall\b|\bsill\b|"
    r"\bvent(ilator)?\b|\blegend\b|\bschedule\b|\btypical\b|\btyp\b",
    re.IGNORECASE,
)
_SLAB = re.compile(r"\bslab\b|\broof\b", re.IGNORECASE)
# Location evidence printed on the drawing itself.
_SHEET_TITLE = re.compile(r"\bPLAN\b", re.IGNORECASE)
_COMPASS_ZONE = re.compile(r"\(\s*(N|S|E|W|NE|NW|SE|SW)\s*\)", re.IGNORECASE)


def sheet_title(labels: list["Cluster"]) -> str | None:
    """The largest printed text containing 'PLAN' (e.g. 'SECOND FLOOR PLAN')."""

    candidates = []
    for cluster in labels:
        text, confidence = label_text(cluster)
        if text and confidence >= 0.5 and _SHEET_TITLE.search(text):
            candidates.append((cluster.height, text))
    return max(candidates)[1] if candidates else None


def location_from_drawing(label: str, title: str | None) -> str | None:
    """Location built only from text printed on the drawing; None if there is none."""

    parts = []
    if title:
        parts.append(title)
    zone = _COMPASS_ZONE.search(label or "")
    if zone:
        parts.append(f"{zone.group(1).upper()} zone")
    return ", ".join(parts) or None


_DIRECTION_WORD = re.compile(r"\b(NORTH|SOUTH|EAST|WEST)\b", re.IGNORECASE)
_ZONE_THRESHOLD = 0.08  # fraction of the rooms' spread treated as "central" on an axis


def direction_labels(labels: list["Cluster"]) -> dict[str, tuple[float, float]]:
    """Page positions of the printed NORTH/SOUTH/EAST/WEST labels (best reading each)."""

    found: dict[str, tuple[float, tuple[float, float]]] = {}
    for cluster in labels:
        text, confidence = label_text(cluster)
        words = {w.upper() for w in _DIRECTION_WORD.findall(text or "")}
        if confidence < 0.5 or len(words) != 1:
            continue
        word = words.pop()
        box = cluster.page_bbox
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        if word not in found or confidence > found[word][0]:
            found[word] = (confidence, centre)
    return {word: centre for word, (_, centre) in found.items()}


def _unit(vx: float, vy: float) -> tuple[float, float] | None:
    length = (vx * vx + vy * vy) ** 0.5
    return (vx / length, vy / length) if length > 0 else None


def _axis_vector(positive: str, negative: str, directions, centre) -> tuple[float, float] | None:
    """Unit vector pointing towards ``positive`` (e.g. NORTH), from printed labels."""

    if positive in directions and negative in directions:
        (px, py), (nx, ny) = directions[positive], directions[negative]
        return _unit(px - nx, py - ny)
    if positive in directions:
        return _unit(directions[positive][0] - centre[0], directions[positive][1] - centre[1])
    if negative in directions:
        vector = _unit(directions[negative][0] - centre[0], directions[negative][1] - centre[1])
        return (-vector[0], -vector[1]) if vector else None
    return None


def apply_position_zones(out: "PageInterpretation", labels: list["Cluster"]) -> None:
    """Add a compass zone to rooms whose label/dimension does not print one.

    Orientation comes from the drawing's printed NORTH/SOUTH/EAST/WEST labels;
    the reference point is the middle of the measured rooms. Zones are only
    added when every room that *prints* its zone agrees with its position.
    """

    directions = direction_labels(labels)
    by_id = {a.annotation_id: a for a in out.annotations}
    rooms = []
    for element in out.elements:
        if element.element_type != "room" or not element.annotation_ids:
            continue
        annotation = by_id.get(element.annotation_ids[0])
        if annotation is None or not annotation.bbox:
            continue
        b = annotation.bbox
        printed = re.search(r"\b(N|S|E|W|NE|NW|SE|SW) zone$", element.location or "")
        rooms.append((element, ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), printed.group(1) if printed else None))
    if len(rooms) < 3 or not directions:
        return
    xs, ys = [c[0] for _, c, _ in rooms], [c[1] for _, c, _ in rooms]
    centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    north = _axis_vector("NORTH", "SOUTH", directions, centre)
    east = _axis_vector("EAST", "WEST", directions, centre)
    if north is None or east is None:
        return

    def project(point, axis):
        return (point[0] - centre[0]) * axis[0] + (point[1] - centre[1]) * axis[1]

    spans = {
        "ns": max(project(c, north) for _, c, _ in rooms) - min(project(c, north) for _, c, _ in rooms),
        "ew": max(project(c, east) for _, c, _ in rooms) - min(project(c, east) for _, c, _ in rooms),
    }
    if spans["ns"] <= 0 or spans["ew"] <= 0:
        return

    def zone(point) -> str:
        t_ns, t_ew = project(point, north) / spans["ns"], project(point, east) / spans["ew"]
        ns = "N" if t_ns > _ZONE_THRESHOLD else "S" if t_ns < -_ZONE_THRESHOLD else ""
        ew = "E" if t_ew > _ZONE_THRESHOLD else "W" if t_ew < -_ZONE_THRESHOLD else ""
        return (ns + ew) or "central"

    def agrees(printed: str, computed: str) -> bool:
        # every compass letter printed must be in the computed zone (N matches NE/NW)
        return computed != "central" and all(letter in computed for letter in printed)

    for element, point, printed in rooms:
        if printed and not agrees(printed, zone(point)):
            out.warnings.append(
                f"Position-based zones not used: '{element.description}' prints {printed} "
                f"but its position on the plan gives {zone(point)}"
            )
            return
    for element, point, printed in rooms:
        if printed:
            continue
        computed = zone(point)
        suffix = "central (by position)" if computed == "central" else f"{computed} zone (by position)"
        element.location = f"{element.location}, {suffix}" if element.location else suffix
    out.diagnostics["direction_labels"] = sorted(directions)


def classify_label(label: str) -> tuple[str, str | None]:
    """Return (element_type, room_type) for a label read from the drawing."""

    if _SLAB.search(label):
        return "slab", None
    if _NON_INSTANCE_KEYWORDS.search(label):
        return "reference", None
    lowered = label.lower()
    for pattern, room_type in _ROOM_KEYWORDS:
        if re.search(pattern, lowered):
            return "room", room_type
    return "room", None


def clean_label(text: str) -> str:
    text = re.sub(r"^[^A-Za-z0-9(]+", "", text.strip())
    text = re.sub(r"[^A-Za-z0-9)]+$", "", text)
    return re.sub(r"\s+", " ", text)


def _is_label_text(text: str) -> bool:
    cleaned = clean_label(text)
    letters = sum(ch.isalpha() for ch in cleaned)
    visible = sum(not ch.isspace() for ch in cleaned)
    if letters < 3 or visible == 0:
        return False
    longest_word = max((len(re.sub(r"[^A-Za-z]", "", w)) for w in cleaned.split()), default=0)
    return letters / visible >= 0.6 and longest_word >= 3


# ---------------------------------------------------------------- clustering


def _overlap(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)


def _same_line(a: TextLine, b: TextLine) -> bool:
    if a.frame != b.frame or a.page != b.page:
        return False
    if _overlap(a.bbox, b.bbox) < 0.5:
        return False
    ca, cb = (a.bbox[1] + a.bbox[3]) / 2, (b.bbox[1] + b.bbox[3]) / 2
    return abs(ca - cb) <= 0.6 * min(a.height, b.height)


@dataclass
class Reading:
    line: TextLine
    parsed: ParsedLine

    @property
    def signature(self) -> tuple | None:
        p = self.parsed
        if not p.has_measurement:
            return None
        key: list = []
        for first, second in p.pairs:
            key.append(("pair", _sig(first), _sig(second)))
        for single in p.singles:
            key.append(("single", _sig(single)))
        for area in p.areas:
            key.append(("area", str(area.value.normalize()), area.unit))
        return tuple(key)


def _sig(value: ParsedValue) -> tuple:
    return (str(value.value.normalize()), value.unit)


@dataclass
class Cluster:
    members: list[TextLine]
    readings: list[Reading] = field(default_factory=list)
    frozen_kind: bool | None = None  # set before targeted re-reads are added

    @property
    def frame(self) -> int:
        return self.members[0].frame

    @property
    def page(self) -> int:
        return self.members[0].page

    @property
    def source(self) -> str:
        return self.members[0].source

    @property
    def _geometry_members(self) -> list[TextLine]:
        # Geometry comes from full-page passes; crop re-reads only add text votes.
        base = [m for m in self.members if not m.pass_name.startswith("reread")]
        return base or self.members

    @property
    def bbox(self):
        return _median_box([m.bbox for m in self._geometry_members])

    @property
    def page_bbox(self):
        return _median_box([m.page_bbox for m in self._geometry_members])

    @property
    def height(self) -> float:
        b = self.bbox
        return max(1.0, b[3] - b[1])

    @property
    def is_dimension(self) -> bool:
        if self.frozen_kind is not None:
            return self.frozen_kind
        return any(r.parsed.has_measurement or r.parsed.suspicious_fragments for r in self.readings)


def _median_box(boxes):
    def median(values):
        values = sorted(values)
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2

    return tuple(median([b[i] for b in boxes]) for i in range(4))


def cluster_lines(lines: list[TextLine], declared_unit: str | None) -> list[Cluster]:
    clusters: list[Cluster] = []
    for line in lines:
        target = next((c for c in clusters if any(_same_line(line, m) for m in c.members)), None)
        if target is None:
            target = Cluster(members=[])
            clusters.append(target)
        target.members.append(line)
        target.readings.append(Reading(line, parse_line(line.text, declared_unit)))
    return clusters


# ---------------------------------------------------------------- consensus


@dataclass
class DimensionConsensus:
    cluster: Cluster
    chosen: Reading | None
    agreeing: list[Reading]
    status: str  # extracted | unverified
    confidence: float
    alternatives: list[str]
    notes: list[str]


def consensus(cluster: Cluster) -> DimensionConsensus:
    readings = cluster.readings
    votes: dict[tuple, list[Reading]] = defaultdict(list)
    unreadable: list[Reading] = []
    for reading in readings:
        signature = reading.signature
        if signature is None:
            if reading.parsed.suspicious_fragments:
                unreadable.append(reading)
            continue
        votes[signature].append(reading)

    alternatives = sorted({r.line.text for r in readings})
    if not votes:
        return DimensionConsensus(cluster, None, [], "unverified", 0.0, alternatives, ["no reading could be parsed"])

    ranked = sorted(
        votes.items(),
        key=lambda item: (len(item[1]), sum(r.line.confidence for r in item[1])),
        reverse=True,
    )
    best_signature, agreeing = ranked[0]
    runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
    exact_source = cluster.source in EXACT_TEXT_SOURCES
    notes: list[str] = []

    if exact_source:
        status = "extracted"
    elif len(agreeing) >= 2 and len(agreeing) >= 2 * runner_up:
        status = "extracted"
    else:
        status = "unverified"
        if len(agreeing) < 2:
            notes.append("only one OCR reading produced this value; not independently confirmed")
        if runner_up:
            notes.append("OCR readings disagree on the value")

    total_considered = len(agreeing) + sum(len(v) for s, v in ranked[1:]) + len(unreadable)
    ratio = len(agreeing) / max(1, total_considered)
    best_conf = max(r.line.confidence for r in agreeing)
    # Best agreeing reading's OCR confidence, scaled by the share of readings that agree.
    confidence = best_conf if exact_source else best_conf * (ratio ** 0.5)
    chosen = max(agreeing, key=lambda r: r.line.confidence)
    for reading in agreeing:
        for note in reading.parsed.notes:
            if note not in notes:
                notes.append(note)
    return DimensionConsensus(
        cluster, chosen, agreeing, status, round(min(1.0, confidence), 3), alternatives, notes
    )


def label_text(cluster: Cluster) -> tuple[str, float]:
    """Vote on the label text across readings, weighted by OCR confidence."""

    weights: Counter[str] = Counter()
    spelled: dict[str, str] = {}
    for member in cluster.members:
        cleaned = clean_label(member.text)
        if not _is_label_text(cleaned):
            continue
        key = re.sub(r"[^A-Z0-9]", "", cleaned.upper())
        weights[key] += member.confidence
        if key not in spelled or len(cleaned) > len(spelled[key]):
            spelled[key] = cleaned
    if not weights or sum(weights.values()) <= 0:
        return "", 0.0
    key, weight = weights.most_common(1)[0]
    supporters = [m for m in cluster.members if re.sub(r"[^A-Z0-9]", "", clean_label(m.text).upper()) == key]
    confidence = sum(m.confidence for m in supporters) / len(supporters)
    share = weight / sum(weights.values())
    return spelled[key], round(confidence * share ** 0.5, 3)


# ---------------------------------------------------------------- association


@dataclass
class Association:
    labels: list[Cluster]
    status: str  # associated | uncertain | unassociated
    reason: str


def _horizontal_match(a, b, height: float) -> bool:
    overlap = min(a[2], b[2]) - max(a[0], b[0])
    if overlap > 0:
        return True
    center_gap = abs((a[0] + a[2]) / 2 - (b[0] + b[2]) / 2)
    return center_gap <= max(a[2] - a[0], b[2] - b[0]) / 2 + height


def associate(dimension: Cluster, labels: list[Cluster]) -> Association:
    """Find the label printed directly above a dimension string (same frame)."""

    d = dimension.bbox
    h = dimension.height
    candidates = []
    for label in labels:
        if label.page != dimension.page or label.frame != dimension.frame:
            continue
        lb = label.bbox
        gap = d[1] - lb[3]
        reach = 5.0 * max(h, label.height)
        if -0.5 * h <= gap <= reach and lb[1] < d[1] and _horizontal_match(lb, d, h):
            candidates.append((gap, label))
    if not candidates:
        return Association([], "unassociated", "no label found directly above this dimension")
    candidates.sort(key=lambda item: item[0])
    best_gap, best = candidates[0]

    stack = [best]
    top = best.bbox
    for label in sorted(labels, key=lambda c: -c.bbox[3]):
        if label in stack or label.page != best.page or label.frame != best.frame:
            continue
        lb = label.bbox
        if -0.5 * label.height <= top[1] - lb[3] <= 1.2 * label.height and lb[1] < top[1] and _horizontal_match(lb, top, label.height):
            stack.insert(0, label)
            top = lb

    if not (dimension.members[0].geometry_reliable and all(c.members[0].geometry_reliable for c in stack)):
        return Association(stack, "uncertain", "text positions could not be resolved reliably (unsupported transform)")
    if len(candidates) > 1:
        second_gap, second = candidates[1]
        if second not in stack and second_gap - best_gap < 0.3 * h:
            return Association(
                stack,
                "uncertain",
                f"two labels are equally close above the dimension ('{label_text(best)[0]}', '{label_text(second)[0]}')",
            )
    return Association(stack, "associated", "label printed directly above the dimension string")


# ---------------------------------------------------------------- page interpretation


@dataclass
class PageInterpretation:
    elements: list[ExtractedElement] = field(default_factory=list)
    annotations: list[DimensionAnnotation] = field(default_factory=list)
    unresolved: list[UnresolvedItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    # Confirmed linear dimensions with their text position, for scale calibration.
    linear_references: list = field(default_factory=list)
    scale: object | None = None  # ScaleCalibration, set by the provider


class _Ids:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()

    def next(self, prefix: str) -> str:
        self.counters[prefix] += 1
        return f"{prefix}-{self.counters[prefix]:03d}"


def _values(parsed: ParsedLine) -> list[MeasurementValue]:
    out = []
    for first, second in parsed.pairs:
        out.extend([_value(first), _value(second)])
    for single in parsed.singles:
        out.append(_value(single))
    return out


def _value(value: ParsedValue) -> MeasurementValue:
    return MeasurementValue(
        raw_text=value.raw,
        value=value.value.quantize(Decimal("0.000001")).normalize(),
        original_unit=value.unit,
        unit_source=value.unit_source if value.unit else None,
        value_m=value.metres.normalize() if value.metres is not None else None,
    )


def interpret_page(
    lines: list[TextLine],
    page_number: int,
    ids: _Ids | None = None,
    session: OCRSession | None = None,
) -> PageInterpretation:
    ids = ids or _Ids()
    out = PageInterpretation()
    lines = [
        line for line in lines
        if all(math.isfinite(v) for v in (*line.bbox, *line.page_bbox, line.confidence))
    ]
    all_text = "\n".join(line.text for line in lines)
    declared_unit = detect_declared_unit(all_text)
    if declared_unit:
        out.diagnostics["declared_unit"] = declared_unit
        logger.info("Page %s declares default unit: %s", page_number, declared_unit)

    clusters = cluster_lines(lines, declared_unit)
    for cluster in clusters:
        cluster.frozen_kind = cluster.is_dimension
    dimension_clusters = [c for c in clusters if c.is_dimension]
    label_clusters = [c for c in clusters if not c.is_dimension and label_text(c)[0]]

    if session is not None:
        _reread(session, dimension_clusters, label_clusters, declared_unit)

    title = sheet_title([c for c in label_clusters if c.frame == 0])
    if title:
        out.diagnostics["sheet_title"] = title
    consensuses = [consensus(c) for c in dimension_clusters]
    readable = [c for c in consensuses if c.chosen is not None]
    # Text read upside-down / sideways in a wrong rotation pass duplicates a good
    # reading made in the correct frame: drop those unreadable duplicates.
    consensuses = [
        c for c in consensuses
        if c.chosen is not None
        or not any(
            r.status == "extracted"
            and r.cluster.frame != c.cluster.frame and _overlap(r.cluster.page_bbox, c.cluster.page_bbox) >= 0.5
            for r in readable
        )
    ]
    used_labels: dict[int, str] = {}

    for item in sorted(consensuses, key=lambda c: (c.cluster.bbox[1], c.cluster.bbox[0])):
        _interpret_dimension(item, label_clusters, ids, out, used_labels, page_number)

    # Room-like labels that never received a dimension: report, never fill.
    for label in label_clusters:
        if id(label) in used_labels:
            continue
        text, conf = label_text(label)
        element_type, room_type = classify_label(text)
        if room_type and conf >= 0.5:
            out.unresolved.append(
                UnresolvedItem(
                    text=text,
                    reason="label read, but no dimension annotation was found below it; dimensions left unknown",
                    source_page=page_number,
                    bbox=[round(v, 1) for v in label.page_bbox],
                    source=label.source,
                )
            )

    apply_position_zones(out, label_clusters)
    out.diagnostics.update(
        {
            "text_lines_read": len(lines),
            "line_clusters": len(clusters),
            "dimension_clusters": len(dimension_clusters),
            "label_clusters": len(label_clusters),
            "ocr_rereads": session.reread_count if session else 0,
        }
    )
    return out


def _reread(session: OCRSession, dims: list[Cluster], labels: list[Cluster], declared_unit: str | None) -> None:
    """Add targeted crop re-reads to dimension lines and the labels above them."""

    targets = list(dims)
    for dim in dims:
        association = associate(dim, labels)
        targets.extend(c for c in association.labels if c not in targets)
    results = session.reread_many([(c.frame, c.bbox) for c in targets])
    for cluster, lines in zip(targets, results):
        box = cluster.bbox
        h = cluster.height
        for line in lines:
            cx = (line.bbox[0] + line.bbox[2]) / 2
            cy = (line.bbox[1] + line.bbox[3]) / 2
            if box[0] - h <= cx <= box[2] + h and box[1] - 0.3 * h <= cy <= box[3] + 0.3 * h:
                cluster.members.append(line)
                cluster.readings.append(Reading(line, parse_line(line.text, declared_unit)))


def _interpret_dimension(
    item: DimensionConsensus,
    labels: list[Cluster],
    ids: _Ids,
    out: PageInterpretation,
    used_labels: dict[int, str],
    page_number: int,
) -> None:
    cluster = item.cluster
    bbox = [round(v, 1) for v in cluster.page_bbox]
    base = dict(
        source=cluster.source,
        source_page=page_number,
        bbox=bbox,
        readings_agreeing=len(item.agreeing),
        readings_total=len(cluster.readings),
        alternative_readings=item.alternatives[:8],
    )

    if item.chosen is None:
        annotation = DimensionAnnotation(
            annotation_id=ids.next("A"),
            kind="unreadable",
            raw_text=" | ".join(item.alternatives[:4]),
            status="unverified",
            confidence=0.0,
            association_reason="text looks like a dimension but no reading could be parsed reliably",
            notes=item.notes,
            **base,
        )
        out.annotations.append(annotation)
        association = associate(cluster, labels)
        near = " ".join(label_text(c)[0] for c in association.labels)
        for label_cluster in association.labels:
            used_labels.setdefault(id(label_cluster), annotation.annotation_id)
        if near:
            annotation.associated_label = near
            annotation.association_status = "uncertain"
        out.unresolved.append(
            UnresolvedItem(
                text=annotation.raw_text,
                reason="unreadable dimension text" + (f" near label '{near}'" if near else ""),
                source_page=page_number,
                bbox=bbox,
                source=cluster.source,
            )
        )
        return

    parsed = item.chosen.parsed
    prefix = clean_label(_vote_prefix(item))
    suffix = parsed.suffix if re.search(r"[A-Za-z0-9]", parsed.suffix) else ""
    context = " ".join(part for part in (prefix, suffix) if part) or None

    # Areas: always report; they are annotations, not elements on their own.
    for area in parsed.areas:
        out.annotations.append(
            DimensionAnnotation(
                annotation_id=ids.next("A"),
                kind="area",
                raw_text=parsed.normalized_text,
                area_value=area.value,
                area_unit=area.unit,
                area_m2=area.square_metres.quantize(Decimal("0.0001")).normalize(),
                context_text=context,
                status=item.status,
                confidence=item.confidence,
                association_reason="area annotation recorded as read; not linked to a single element",
                notes=item.notes,
                **base,
            )
        )

    for single in parsed.singles:
        annotation_id = ids.next("A")
        if item.status == "extracted" and single.metres is not None and len(parsed.singles) == 1 and not parsed.pairs:
            from constructai.extraction.calibration import LinearReference

            out.linear_references.append(
                LinearReference(annotation_id, single.raw, single.metres, cluster.frame, tuple(cluster.bbox))
            )
        out.annotations.append(
            DimensionAnnotation(
                annotation_id=annotation_id,
                kind="linear",
                raw_text=single.raw,
                values=[_value(single)],
                context_text=context,
                status=item.status,
                confidence=item.confidence,
                association_status="unassociated",
                association_reason=(
                    "linear dimension; linking it to a specific wall/opening requires dimension-line "
                    "geometry, which is not inferred"
                ),
                notes=item.notes,
                **base,
            )
        )

    for first, second in parsed.pairs:
        _interpret_pair(item, first, second, prefix, context, labels, ids, out, used_labels, page_number, base)


def _vote_prefix(item: DimensionConsensus) -> str:
    counter: Counter[str] = Counter()
    for reading in item.agreeing:
        cleaned = clean_label(reading.parsed.prefix)
        if _is_label_text(cleaned):
            counter[cleaned] += reading.line.confidence
    return counter.most_common(1)[0][0] if counter else ""


def _interpret_pair(item, first, second, prefix, context, labels, ids, out, used_labels, page_number, base) -> None:
    parsed = item.chosen.parsed
    raw_pair = f"{first.raw} x {second.raw}"
    annotation = DimensionAnnotation(
        annotation_id=ids.next("A"),
        kind="pair",
        raw_text=raw_pair,
        values=[_value(first), _value(second)],
        context_text=context,
        status=item.status,
        confidence=item.confidence,
        notes=list(item.notes),
        **base,
    )
    out.annotations.append(annotation)

    # Which label does this pair describe?
    if prefix:
        label, label_conf = prefix, item.confidence
        association = Association([], "associated", "label printed on the same line as the dimension")
    else:
        association = associate(item.cluster, labels)
        label = " ".join(label_text(c)[0] for c in association.labels).strip()
        label_conf = min((label_text(c)[1] for c in association.labels), default=0.0)
    annotation.associated_label = label or None
    annotation.association_status = association.status
    annotation.association_reason = association.reason
    for label_cluster in association.labels:
        used_labels.setdefault(id(label_cluster), annotation.annotation_id)

    # Cross-check against an area written on the same line (e.g. "= 664.1 sq ft").
    if first.metres is not None and second.metres is not None:
        for area in parsed.areas:
            computed = first.metres * second.metres / SQ_METRES_PER_UNIT[area.unit]
            tolerance = Decimal(5) * Decimal(10) ** (-(area.decimals + 1))
            if abs(computed - area.value) <= tolerance:
                annotation.notes.append(f"stated area {area.raw} matches {raw_pair}")
            else:
                message = f"stated area {area.raw} does not match {raw_pair} (computed {computed:.2f})"
                annotation.notes.append(message)
                out.warnings.append(f"Page {page_number}: {message}")

    def unresolved(reason: str) -> None:
        out.unresolved.append(
            UnresolvedItem(
                text=f"{label + ': ' if label else ''}{raw_pair}",
                reason=reason,
                source_page=page_number,
                bbox=base["bbox"],
                source=base["source"],
            )
        )

    if not label:
        annotation.association_reason = "no label found next to this dimension pair"
        unresolved("dimension pair read, but no room/element label could be associated with it")
        return
    element_type, room_type = classify_label(label)
    if association.status != "associated":
        unresolved(f"association uncertain: {association.reason}")
        return
    if item.status != "extracted":
        unresolved("dimension reading not confirmed by independent OCR readings: " + "; ".join(item.notes))
        return
    if first.metres is None or second.metres is None:
        unresolved("dimension pair has no unit and the drawing declares no default unit; not converted")
        annotation.notes.append("unit unknown")
        return
    if element_type == "reference":
        annotation.association_reason = (
            f"dimension describes '{label}' (a type/section/legend entry); no element instance or count is inferred"
        )
        return

    element_id = ids.next("S" if element_type == "slab" else "R")
    confidence = round(min(item.confidence, label_conf) if label_conf else item.confidence, 3)
    element = ExtractedElement(
        element_type=element_type,
        element_id=element_id,
        description=label,
        location=location_from_drawing(f"{label} {parsed.normalized_text}", out.diagnostics.get("sheet_title")),
        dimensions=Dimensions(length=first.metres.normalize(), width=second.metres.normalize()),
        unit="m2",
        source_page=page_number,
        # where the dimension text sits on the page (pixels / points / SVG units)
        source_reference=(
            f"page-{page_number}@{(base['bbox'][0] + base['bbox'][2]) / 2:.0f},"
            f"{(base['bbox'][1] + base['bbox'][3]) / 2:.0f}"
        ),
        confidence=confidence,
        evidence=(
            f"{base['source']}: '{label}' with dimension '{raw_pair}' "
            f"({len(item.agreeing)}/{len(item.cluster.readings)} readings agree); "
            "length/width as written, converted with exact unit factors"
        ),
        room_type=room_type,
        dimensions_source="ocr" if base["source"] == "tesseract_ocr" else "text_layer",
        review_reasons=[
            "dimension_annotation",
            *(["ocr_dimensions"] if base["source"] == "tesseract_ocr" else []),
            *(["low_confidence"] if confidence < 0.75 else []),
        ],
        measurement_source="dimension_annotation",
        measurement_status="extracted",
        raw_dimension_text=raw_pair,
        original_unit=first.unit if first.unit == second.unit else f"{first.unit}/{second.unit}",
        annotation_ids=[annotation.annotation_id],
        association_status=association.status,
    )
    annotation.associated_element_id = element_id
    out.elements.append(element)
    logger.info(
        "Element %s '%s': %s -> %s m x %s m (source=%s, confidence=%.2f)",
        element_id, label, raw_pair, element.dimensions.length, element.dimensions.width,
        base["source"], confidence,
    )
