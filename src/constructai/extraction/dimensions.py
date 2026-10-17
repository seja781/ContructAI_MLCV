"""Parse dimension annotations read from a drawing and normalise their units.

This module only interprets text that was actually read from the drawing
(OCR, PDF text layer, SVG <text> nodes or a vision model's verbatim
transcription). It never supplies a value that is not in the text.

Supported notations (examples):

* feet / inches   12'-0"   9' 6"   12'6"   10'   6 1/2"   12 ft 6 in   9"
* metric          4.5 m   4.50m   3500 mm   350 cm
* pairs           9'-6" x 11'-0"   4500 x 3600 mm   4.5 x 3.6 m   9"x12"
* areas           664.1 sq ft   61.7 sq m   12 m2   12 m²   150 sft

Rules that protect against fabricated values:

* A bare number without a unit stays ``unit=None`` and is never converted
  unless the drawing itself declares a default unit (e.g. "ALL DIMENSIONS
  IN MM") and the caller passes that declared unit in.
* Feet-and-inch values must carry a foot mark (') or the word ft. OCR
  output such as ``4-3"`` (foot mark lost) is NOT silently read as 4'-3";
  it is returned as unparsed text so the caller can report it.
* Inches >= 12 inside a feet-inch value are rejected as unreadable.

The only numeric constants here are exact unit definitions
(1 ft = 0.3048 m, 1 in = 0.0254 m, 1 mm = 0.001 m, 1 cm = 0.01 m).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Exact, internationally defined conversion factors (not drawing values).
METRES_PER_FOOT = Decimal("0.3048")
METRES_PER_INCH = Decimal("0.0254")
METRES_PER_UNIT = {
    "m": Decimal("1"),
    "cm": Decimal("0.01"),
    "mm": Decimal("0.001"),
    "ft": METRES_PER_FOOT,
    "in": METRES_PER_INCH,
    "ft-in": METRES_PER_FOOT,  # value stored in decimal feet
}
SQ_METRES_PER_UNIT = {
    "m2": Decimal("1"),
    "ft2": METRES_PER_FOOT * METRES_PER_FOOT,
}

_QUOTE_TRANSLATION = str.maketrans(
    {
        "’": "'", "‘": "'", "′": "'", "´": "'", "`": "'",
        "“": '"', "”": '"', "″": '"', "„": '"',
        "–": "-", "—": "-", "−": "-",
        "×": "x",
    }
)


@dataclass(frozen=True)
class ParsedValue:
    """One measurement value read from text."""

    raw: str
    value: Decimal  # in the original unit (decimal feet for ft-in)
    unit: str | None  # "ft-in", "ft", "in", "m", "cm", "mm", or None (unknown)
    unit_source: str = "annotation"  # "annotation" | "pair_suffix" | "drawing_note"
    exact_metres: Decimal | None = None  # set for ft-in so 4" is exactly 0.1016 m

    @property
    def metres(self) -> Decimal | None:
        if self.unit is None:
            return None
        if self.exact_metres is not None:
            return self.exact_metres
        return self.value * METRES_PER_UNIT[self.unit]


@dataclass(frozen=True)
class ParsedArea:
    raw: str
    value: Decimal
    unit: str  # "m2" | "ft2"

    @property
    def square_metres(self) -> Decimal:
        return self.value * SQ_METRES_PER_UNIT[self.unit]

    @property
    def decimals(self) -> int:
        exponent = self.value.as_tuple().exponent
        return -exponent if isinstance(exponent, int) and exponent < 0 else 0


@dataclass
class ParsedLine:
    """Everything recognised in one line of drawing text."""

    text: str
    normalized_text: str
    pairs: list[tuple[ParsedValue, ParsedValue]] = field(default_factory=list)
    singles: list[ParsedValue] = field(default_factory=list)
    areas: list[ParsedArea] = field(default_factory=list)
    prefix: str = ""  # text before the first measurement (an inline label)
    suffix: str = ""  # text after the last measurement (e.g. "(overall width)")
    notes: list[str] = field(default_factory=list)
    suspicious_fragments: list[str] = field(default_factory=list)

    @property
    def has_measurement(self) -> bool:
        return bool(self.pairs or self.singles or self.areas)


_NUM = r"\d+(?:\.\d+)?"
_TOKEN = re.compile(
    rf"""
    (?P<area>(?P<area_num>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|{_NUM})\s*
        (?P<area_unit>sq\.?\s*ft|sq\.?\s*feet|sft|ft2|ft²|
                      sq\.?\s*m(?:tr|eters?|etres?)?\b|sqm|m2|m²))
    |(?P<ftword>(?P<fw_ft>{_NUM})\s*(?:ft|feet)\b\.?
        (?:\s*(?P<fw_in>{_NUM})\s*(?:in|inch|inches)\b\.?)?)
    |(?P<ftin>(?P<ft>\d{{1,4}})\s*'
        (?:\s*-?\s*(?P<in>\d{{1,2}}(?:\.\d+)?)(?:\s+(?P<fn>\d{{1,2}})/(?P<fd>\d{{1,2}}))?\s*"?)?)
    |(?P<inch>(?P<in_only>\d{{1,3}}(?:\.\d+)?)(?:\s+(?P<ifn>\d{{1,2}})/(?P<ifd>\d{{1,2}}))?\s*(?:"|\binch(?:es)?\b|\bin\b(?=\s*(?:x|$|\)))))
    |(?P<metric>(?P<m_num>\d+(?:[.,]\d+)?)\s*(?P<m_unit>mm|cm|m|metres?|meters?)(?![a-z0-9²]))
    |(?P<bare>\d+(?:\.\d+)?)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SEPARATOR = re.compile(r"^\s*[xX*]\s*$")
# A measurement must start at a clean boundary; "g'-3\"" (a 9 misread as g)
# must not yield 3 inches.
_ALLOWED_BEFORE = set(" \t(:=xX*,;|[/~\\")
_ALLOWED_AFTER = set(" \t)xX*,;|]=(/~:")
_MULTI_X = re.compile(r"(?<=[\"'\d)])\s*(?:[xX]{2}|[xX]\s+[xX])\s*(?=\d)")
# A dimension that lost its foot mark during OCR, e.g. 4-3" or 6-0".
_SUSPICIOUS = re.compile(r"(?<!['\d])\d{1,3}\s*-\s*\d{1,2}\s*\"")
# OCR commonly reads the digit zero as the letter O inside numbers.
_DEGREE_AS_FOOT = re.compile(r"(?<=\d)\u00b0(?=\s*-\s*\d)")
_LETTER_O_IN_NUMBER = re.compile(r"(?<=[\d'\-])[Oo](?=[\d\"'])|(?<=\d)[Oo](?=\d)")
_DECLARED_UNIT = re.compile(
    r"(?:all\s+)?dim(?:ension)?s?\.?\s+(?:are\s+)?(?:in|are)\s+(?P<unit>mm|millimet(?:re|er)s?|cm|centimet(?:re|er)s?|m|met(?:re|er)s?|feet|ft|inch(?:es)?)\b",
    re.IGNORECASE,
)


def normalize_text(text: str) -> tuple[str, list[str]]:
    """Normalise typographic quotes/dashes and obvious OCR letter-O zeros."""

    notes: list[str] = []
    cleaned = text.translate(_QUOTE_TRANSLATION)
    cleaned = cleaned.replace("''", '"')
    collapsed = _MULTI_X.sub(" x ", cleaned)
    if collapsed != cleaned:
        notes.append("OCR doubled separator 'Xx' read as 'x'")
        cleaned = collapsed
    fixed = _LETTER_O_IN_NUMBER.sub("0", cleaned)
    if fixed != cleaned:
        notes.append("OCR letter 'O' inside a number was read as digit 0")
    # A foot mark misread as a degree sign: 9°-6" -> 9'-6" (only before "-digit").
    unprimed = _DEGREE_AS_FOOT.sub("'", fixed)
    if unprimed != fixed:
        notes.append("OCR degree sign before '-inches' was read as a foot mark")
        fixed = unprimed
    return fixed, notes


def detect_declared_unit(text: str) -> str | None:
    """Return a unit explicitly declared by a drawing note, if any."""

    match = _DECLARED_UNIT.search(text)
    if not match:
        return None
    word = match.group("unit").lower()
    if word.startswith("mm") or word.startswith("millim"):
        return "mm"
    if word.startswith("cm") or word.startswith("centim"):
        return "cm"
    if word in {"feet", "ft"}:
        return "ft"
    if word.startswith("inch"):
        return "in"
    return "m"


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _metric_unit(word: str) -> str:
    word = word.lower()
    if word == "mm":
        return "mm"
    if word == "cm":
        return "cm"
    return "m"


def _metric_number(text: str) -> Decimal | None:
    # "4,5" -> decimal comma; "2,136" -> thousands separator.
    if "," in text:
        head, tail = text.split(",", 1)
        text = head + tail if len(tail) == 3 else head + "." + tail
    return _dec(text)


def _token_value(match: re.Match[str]) -> ParsedValue | ParsedArea | None:
    raw = match.group(0).strip()
    if match.group("area"):
        number = _dec(match.group("area_num").replace(",", ""))
        unit_word = re.sub(r"[\s.]", "", match.group("area_unit").lower())
        unit = "ft2" if ("ft" in unit_word or "feet" in unit_word or unit_word == "sft") else "m2"
        return ParsedArea(raw, number, unit) if number and number > 0 else None
    if match.group("ftword"):
        feet = _dec(match.group("fw_ft"))
        inches = _dec(match.group("fw_in") or "0")
        if feet is None or inches is None or inches >= 12:
            return None
        if match.group("fw_in"):
            return ParsedValue(raw, feet + inches / 12, "ft-in", exact_metres=feet * METRES_PER_FOOT + inches * METRES_PER_INCH)
        return ParsedValue(raw, feet, "ft")
    if match.group("ftin"):
        feet = _dec(match.group("ft"))
        inches = _dec(match.group("in") or "0")
        if match.group("fn") and match.group("fd") and int(match.group("fd")) != 0:
            inches += Decimal(match.group("fn")) / Decimal(match.group("fd"))
        if feet is None or inches >= 12:
            return None
        return ParsedValue(raw, feet + inches / 12, "ft-in", exact_metres=feet * METRES_PER_FOOT + inches * METRES_PER_INCH)
    if match.group("inch"):
        inches = _dec(match.group("in_only"))
        if inches is None:
            return None
        if match.group("ifn") and match.group("ifd") and int(match.group("ifd")) != 0:
            inches += Decimal(match.group("ifn")) / Decimal(match.group("ifd"))
        return ParsedValue(raw, inches, "in")
    if match.group("metric"):
        number = _metric_number(match.group("m_num"))
        return ParsedValue(raw, number, _metric_unit(match.group("m_unit"))) if number is not None else None
    if match.group("bare"):
        number = _dec(match.group("bare"))
        return ParsedValue(raw, number, None) if number is not None else None
    return None


def parse_line(text: str, declared_unit: str | None = None) -> ParsedLine:
    """Parse one text line into dimension pairs, single values and areas.

    ``declared_unit`` must only be supplied when the same drawing contains an
    explicit note declaring the default unit; it is then applied to bare
    numbers that appear in a dimension pair, and recorded as such.
    """

    normalized, notes = normalize_text(text)
    result = ParsedLine(text=text, normalized_text=normalized, notes=notes)
    suspicious_spans = [(m.start(), m.end()) for m in _SUSPICIOUS.finditer(normalized)]
    result.suspicious_fragments = [normalized[s:e] for s, e in suspicious_spans]

    tokens: list[tuple[int, int, ParsedValue | ParsedArea]] = []
    for match in _TOKEN.finditer(normalized):
        if any(match.start() < e and match.end() > s for s, e in suspicious_spans):
            # Part of a value whose foot mark was lost: never reinterpret it.
            continue
        if match.start() > 0 and normalized[match.start() - 1] not in _ALLOWED_BEFORE:
            if match.group("bare"):
                continue  # digits inside a tag such as "W1" or "C13": not a measurement
            fragment = re.search(r"\S*$", normalized[: match.start()]).group(0) + match.group(0)
            result.suspicious_fragments.append(fragment.strip())
            suspicious_spans.append((match.start() - len(fragment) + len(match.group(0)), match.end()))
            continue
        end_char = normalized[match.end()] if match.end() < len(normalized) else " "
        if not match.group("bare") and end_char not in _ALLOWED_AFTER:
            # e.g. 13'-Q": the inches were unreadable, so 13' alone is not trustworthy.
            fragment = match.group(0) + re.match(r"\S*", normalized[match.end():]).group(0)
            result.suspicious_fragments.append(fragment.strip())
            suspicious_spans.append((match.start(), match.end() + len(fragment) - len(match.group(0))))
            continue
        value = _token_value(match)
        if value is None:
            if match.group(0).strip():
                result.suspicious_fragments.append(match.group(0).strip())
            continue
        tokens.append((match.start(), match.end(), value))

    used: set[int] = set()
    first_start: int | None = None
    last_end: int | None = None
    index = 0
    while index < len(tokens):
        start, end, value = tokens[index]
        if isinstance(value, ParsedArea):
            result.areas.append(value)
            used.add(index)
            first_start = start if first_start is None else first_start
            last_end = end
            index += 1
            continue
        if index + 1 < len(tokens):
            n_start, n_end, n_value = tokens[index + 1]
            between = normalized[end:n_start]
            if isinstance(n_value, ParsedValue) and _SEPARATOR.match(between or " "):
                pair = _resolve_pair(value, n_value, declared_unit)
                if pair is not None:
                    result.pairs.append(pair)
                    used.update({index, index + 1})
                    first_start = start if first_start is None else first_start
                    last_end = n_end
                    index += 2
                    continue
        following = normalized[end:]
        preceding = normalized[:start]
        if re.match(r"^\s*[xX*]\s*\S", following) or re.search(r"\S\s*[xX*]\s*$", preceding):
            # One half of an "A x B" pair whose other half was unreadable.
            result.suspicious_fragments.append(normalized[max(0, start - 12): end + 12].strip())
            index += 1
            continue
        if any(s < e2 for s, e2 in suspicious_spans if abs(s - end) <= 3 or abs(e2 - start) <= 3):
            index += 1
            continue
        if value.unit is not None:
            result.singles.append(value)
            used.add(index)
            first_start = start if first_start is None else first_start
            last_end = end
        index += 1

    if first_start is not None:
        # Garbage glued just before the first value (e.g. 'Yo" 6" x 10'-0"' where
        # OCR mangled "10'-") means the reading was truncated: never trust it.
        before = normalized[:first_start].rstrip().split()
        if before and any(mark in before[-1] for mark in ("'", '"', "\u00b0")):
            result.suspicious_fragments.append(normalized[max(0, first_start - 12): (last_end or first_start) + 1].strip())
            result.pairs, result.singles, result.areas = [], [], []
            result.prefix = normalized.strip()
            result.suspicious_fragments = sorted(set(result.suspicious_fragments))
            return result
        result.prefix = normalized[:first_start].strip(" :=-|~_.,;")
        result.suffix = normalized[last_end:].strip(" :=-|~_.,;")
    else:
        result.prefix = normalized.strip()
    # Bare numbers are not measurements, but a value whose foot mark was lost
    # must be reported rather than discarded silently.
    result.suspicious_fragments = sorted(set(result.suspicious_fragments))
    return result


def _resolve_pair(
    first: ParsedValue, second: ParsedValue, declared_unit: str | None
) -> tuple[ParsedValue, ParsedValue] | None:
    if first.value <= 0 or second.value <= 0:
        return None
    if first.unit is None and second.unit in {"m", "cm", "mm"}:
        # "4500 x 3600 mm" / "4.5 x 3.6 m": the trailing unit applies to both.
        first = ParsedValue(first.raw, first.value, second.unit, "pair_suffix")
    if first.unit is None and second.unit is None:
        if declared_unit is None:
            # Keep the pair but with unknown units; caller marks it unverified.
            return first, second
        first = ParsedValue(first.raw, first.value, declared_unit, "drawing_note")
        second = ParsedValue(second.raw, second.value, declared_unit, "drawing_note")
    if (first.unit is None) != (second.unit is None):
        return None
    if {first.unit, second.unit} & {"in"} and {first.unit, second.unit} & {"ft-in", "ft"}:
        # 6" x 10'-0" mixes inches with feet-inches: typically "10'-6"" truncated by OCR.
        return None
    return first, second


def format_metres(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return f"{value.normalize():f}"
