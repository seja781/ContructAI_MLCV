"""Data-driven BOM rules.

Every engineering rule used by the mapping agent is loaded from editable files in
``data/`` — nothing project- or trade-specific is written in Python:

- ``mapping_config.json``   matching thresholds, stop-words, thickness keywords, trusted dimension sources
- ``units.csv``             which purchase units are countable (rounded up to whole numbers)
- ``work_rules.csv``        per work item: valid thickness range, whether wall width may be used, volume allowance
- ``recipe_aliases.csv``    extra phrases that identify a work item (improves matching)
- ``scope_rules.csv``       companion work expected alongside a work item (missing-scope detection)
- ``conflict_rules.csv``    work items that should not both be present (double counting)
- ``deduction_rules.csv``   which openings are deducted from which host work, with area thresholds

Loading is strict: a malformed row raises ``ValueError`` naming the file and row, and
work items that need a rule but have none are rejected instead of silently defaulted.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path


DATA = Path(__file__).resolve().parents[3] / "data"


@dataclass(frozen=True)
class WorkRule:
    work_key: str
    thickness_min_mm: float
    thickness_max_mm: float
    thickness_from_dimension_width: bool
    volume_allowance_pct: float
    note: str


@dataclass(frozen=True)
class ScopeRule:
    trigger_work_key: str
    expected_work_keys: tuple[str, ...]
    quantity_factor: float | None
    note: str


@dataclass(frozen=True)
class ConflictRule:
    work_key_a: str
    work_key_b: str
    message: str


@dataclass(frozen=True)
class DeductionRule:
    host_work_key: str
    opening_element_types: tuple[str, ...]
    min_opening_area_m2: float
    max_opening_area_m2: float | None
    deduct_fraction: float
    note: str


@dataclass
class BOMRules:
    ok_score: float
    review_score: float
    stopwords: frozenset[str]
    thickness_keywords: tuple[str, ...]
    thickness_keyword_window: int
    trusted_dimension_sources: frozenset[str]
    width_as_thickness_element_types: frozenset[str]
    countable_units: frozenset[str]
    known_units: frozenset[str]
    work_rules: dict[str, WorkRule] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    scope_rules: list[ScopeRule] = field(default_factory=list)
    conflict_rules: list[ConflictRule] = field(default_factory=list)
    deduction_rules: list[DeductionRule] = field(default_factory=list)

    @property
    def opening_element_types(self) -> frozenset[str]:
        return frozenset(t for rule in self.deduction_rules for t in rule.opening_element_types)


# --------------------------------------------------------------------------- helpers
def _rows(path: Path, required: set[str]):
    if not path.exists():
        raise ValueError(f"BOM rule file is missing: {path.name}")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name} is missing column(s): {', '.join(sorted(missing))}")
        for row_number, row in enumerate(reader, start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            yield row_number, {k: (v or "").strip() for k, v in row.items()}


def _float(value: str, path: Path, row: int, column: str, *, optional: bool = False) -> float | None:
    if value == "":
        if optional:
            return None
        raise ValueError(f"{path.name} row {row}: '{column}' is required")
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{path.name} row {row}: '{column}' must be a number, got '{value}'") from None


def _bool(value: str, path: Path, row: int, column: str) -> bool:
    v = value.lower()
    if v not in {"true", "false"}:
        raise ValueError(f"{path.name} row {row}: '{column}' must be true or false, got '{value}'")
    return v == "true"


def _keys(value: str) -> tuple[str, ...]:
    return tuple(k.strip() for k in value.split("|") if k.strip())


# --------------------------------------------------------------------------- loader
def load_rules(data_dir: Path | None = None) -> BOMRules:
    d = Path(data_dir) if data_dir else DATA

    cfg_path = d / "mapping_config.json"
    if not cfg_path.exists():
        raise ValueError("BOM rule file is missing: mapping_config.json")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for key in ("ok_score", "review_score", "stopwords", "thickness_keywords",
                "thickness_keyword_window", "trusted_dimension_sources", "width_as_thickness_element_types"):
        if key not in cfg:
            raise ValueError(f"mapping_config.json is missing '{key}'")
    if not 0 <= cfg["review_score"] <= cfg["ok_score"] <= 1:
        raise ValueError("mapping_config.json: require 0 <= review_score <= ok_score <= 1")

    units_path = d / "units.csv"
    countable, known = set(), set()
    for row, r in _rows(units_path, {"unit", "countable"}):
        known.add(r["unit"])
        if _bool(r["countable"], units_path, row, "countable"):
            countable.add(r["unit"])

    rules = BOMRules(
        ok_score=float(cfg["ok_score"]), review_score=float(cfg["review_score"]),
        stopwords=frozenset(cfg["stopwords"]), thickness_keywords=tuple(cfg["thickness_keywords"]),
        thickness_keyword_window=int(cfg["thickness_keyword_window"]),
        trusted_dimension_sources=frozenset(cfg["trusted_dimension_sources"]),
        width_as_thickness_element_types=frozenset(t.strip().lower() for t in cfg["width_as_thickness_element_types"]),
        countable_units=frozenset(countable), known_units=frozenset(known),
    )

    p = d / "work_rules.csv"
    for row, r in _rows(p, {"work_key", "thickness_min_mm", "thickness_max_mm",
                            "thickness_from_dimension_width", "volume_allowance_pct", "note"}):
        lo = _float(r["thickness_min_mm"], p, row, "thickness_min_mm")
        hi = _float(r["thickness_max_mm"], p, row, "thickness_max_mm")
        if lo <= 0 or hi < lo:
            raise ValueError(f"{p.name} row {row}: thickness range must satisfy 0 < min <= max")
        if r["work_key"] in rules.work_rules:
            raise ValueError(f"{p.name} row {row}: duplicate work_key '{r['work_key']}'")
        rules.work_rules[r["work_key"]] = WorkRule(
            r["work_key"], lo, hi, _bool(r["thickness_from_dimension_width"], p, row, "thickness_from_dimension_width"),
            _float(r["volume_allowance_pct"], p, row, "volume_allowance_pct"), r["note"])

    p = d / "recipe_aliases.csv"
    for _, r in _rows(p, {"work_key", "match_text"}):
        rules.aliases.setdefault(r["work_key"], []).append(r["match_text"])

    p = d / "scope_rules.csv"
    for row, r in _rows(p, {"trigger_work_key", "expected_work_keys", "quantity_factor", "note"}):
        expected = _keys(r["expected_work_keys"])
        if not expected:
            raise ValueError(f"{p.name} row {row}: expected_work_keys is empty")
        rules.scope_rules.append(ScopeRule(r["trigger_work_key"], expected,
                                           _float(r["quantity_factor"], p, row, "quantity_factor", optional=True), r["note"]))

    p = d / "conflict_rules.csv"
    for _, r in _rows(p, {"work_key_a", "work_key_b", "message"}):
        rules.conflict_rules.append(ConflictRule(r["work_key_a"], r["work_key_b"], r["message"]))

    p = d / "deduction_rules.csv"
    for row, r in _rows(p, {"host_work_key", "opening_element_types", "min_opening_area_m2",
                            "max_opening_area_m2", "deduct_fraction", "note"}):
        frac = _float(r["deduct_fraction"], p, row, "deduct_fraction")
        if not 0 < frac <= 1:
            raise ValueError(f"{p.name} row {row}: deduct_fraction must be in (0, 1]")
        rules.deduction_rules.append(DeductionRule(
            r["host_work_key"], tuple(t.lower() for t in _keys(r["opening_element_types"])),
            _float(r["min_opening_area_m2"], p, row, "min_opening_area_m2"),
            _float(r["max_opening_area_m2"], p, row, "max_opening_area_m2", optional=True), frac, r["note"]))
    return rules


def validate_against_recipes(rules: BOMRules, works: dict, catalog: dict) -> None:
    """Cross-check rule files against recipes and catalog so typos fail loudly at start-up."""
    for key, work in works.items():
        if work["requires_thickness"] and key not in rules.work_rules:
            raise ValueError(f"work_rules.csv has no row for '{key}', which requires a thickness in recipes.csv")
    referenced = (set(rules.work_rules) | set(rules.aliases)
                  | {s.trigger_work_key for s in rules.scope_rules}
                  | {k for s in rules.scope_rules for k in s.expected_work_keys}
                  | {c.work_key_a for c in rules.conflict_rules} | {c.work_key_b for c in rules.conflict_rules}
                  | {x.host_work_key for x in rules.deduction_rules})
    unknown = sorted(referenced - set(works))
    if unknown:
        raise ValueError(f"BOM rule files reference unknown work_key(s): {', '.join(unknown)}")
    bad_units = sorted({m["unit"] for m in catalog.values()} - rules.known_units)
    if bad_units:
        raise ValueError(f"units.csv has no entry for catalog unit(s): {', '.join(bad_units)}")
