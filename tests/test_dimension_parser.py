"""Unit tests for dimension parsing and unit normalisation (no OCR needed)."""

from decimal import Decimal

import pytest

from constructai.extraction.dimensions import detect_declared_unit, parse_line


def _pair(text, declared=None):
    parsed = parse_line(text, declared)
    assert len(parsed.pairs) == 1, (text, parsed)
    return parsed.pairs[0]


@pytest.mark.parametrize(
    "text, first_m, second_m",
    [
        ("9'-6\" x 11'-0\"", "2.8956", "3.3528"),
        ("9' 6\" X 11' 0\"", "2.8956", "3.3528"),
        ("4500 x 3600 mm", "4.5", "3.6"),
        ("4.5 x 3.6 m", "4.5", "3.6"),
        ("450cm x 360cm", "4.5", "3.6"),
        ("9\"x12\"", "0.2286", "0.3048"),
        ("9′-3″ × 11′-0″", "2.8194", "3.3528"),  # typographic primes and x
    ],
)
def test_pairs_are_normalised_to_metres_with_exact_factors(text, first_m, second_m):
    first, second = _pair(text)
    assert first.metres == Decimal(first_m)
    assert second.metres == Decimal(second_m)


def test_feet_and_inches_keep_original_unit_and_text():
    first, second = _pair("Roof slab: 19'-3\" x 34'-6\" = 664.1 sq ft")
    assert first.unit == second.unit == "ft-in"
    assert first.raw == "19'-3\""
    assert first.value == Decimal("19.25")


@pytest.mark.parametrize(
    "text, metres",
    [("4.5 m", "4.5"), ("3500 mm", "3.5"), ("12'-0\"", "3.6576"), ("12 ft 6 in", "3.8100"), ("10'", "3.0480")],
)
def test_single_values(text, metres):
    parsed = parse_line(text)
    assert [v.metres for v in parsed.singles] == [Decimal(metres)]


def test_areas_are_parsed_with_units():
    parsed = parse_line("SLAB AREA = 664.1 sq ft (61.7 sq m)")
    assert [(a.value, a.unit) for a in parsed.areas] == [(Decimal("664.1"), "ft2"), (Decimal("61.7"), "m2")]
    assert parsed.prefix == "SLAB AREA"


def test_bare_numbers_keep_unknown_unit_without_a_declared_unit():
    first, second = _pair("KITCHEN 3500 x 4200")
    assert first.unit is None and second.unit is None
    assert first.metres is None and second.metres is None


def test_declared_unit_note_is_required_to_convert_bare_numbers():
    assert detect_declared_unit("NOTE: ALL DIMENSIONS ARE IN MM") == "mm"
    first, second = _pair("KITCHEN 3500 x 4200", declared="mm")
    assert first.metres == Decimal("3.5")
    assert first.unit_source == "drawing_note"


@pytest.mark.parametrize(
    "text",
    [
        "4-3\" x 10'-0\"",  # foot mark lost by OCR: must not become 4'-3" or 3"
        "g'-3\" X 10'-0\"",  # a 9 misread as g: must not yield 3 inches
        "teh x 10'-0\"",  # half of a pair unreadable
        "3'-6\" x 3)",  # second half unreadable
        "13'-Q\"",  # inches unreadable: 13' alone is not trustworthy
        "51-9\"",
        "12'-14\"",  # inches >= 12 is not a valid feet-inch value
    ],
)
def test_corrupted_ocr_text_is_rejected_not_reinterpreted(text):
    parsed = parse_line(text)
    assert not parsed.pairs and not parsed.singles, (text, parsed)
    assert parsed.suspicious_fragments


def test_letter_o_inside_number_is_normalised_and_noted():
    parsed = parse_line("23'-O\" (overall width)")
    assert [v.metres for v in parsed.singles] == [Decimal("7.0104")]
    assert parsed.suffix == "(overall width)"
    assert parsed.notes


def test_tags_and_plain_words_are_not_measurements():
    for text in ("C13", "W1", "BEDROOM 2 (NW)", "SECOND FLOOR PLAN"):
        parsed = parse_line(text)
        assert not parsed.has_measurement
        assert not parsed.suspicious_fragments


@pytest.mark.parametrize(
    "text",
    [
        'Yo" 6" x 10\'-0"',  # "10'-" mangled into junk: the rest must not be trusted
        "'Ne 6\" x 10'-0\"",
        '6" x 10\'-0"',  # inches mixed with feet-inches in one pair: truncated reading
    ],
)
def test_truncated_ocr_readings_are_not_trusted(text):
    parsed = parse_line(text)
    assert not parsed.pairs
    assert parsed.suspicious_fragments


def test_degree_sign_for_foot_mark_and_stray_line_before_value():
    first, second = parse_line('9\u00b0-6" x 11\'-0"').pairs[0]
    assert first.metres == Decimal("2.8956")
    first, _ = parse_line('\\10\'-6" x 10\'-0"').pairs[0]
    assert first.metres == Decimal("3.2004")
    assert not parse_line("13'-9\u00b0 x 7'-0\"").pairs  # degree in the inch position stays ambiguous
