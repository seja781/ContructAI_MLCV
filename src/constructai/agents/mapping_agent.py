"""MAPPING AGENT (BOQ -> BOM).

1. MATCH each BOQ line to a work item in data/recipes.csv (plus data/recipe_aliases.csv)
   using TF-IDF cosine similarity on the description (same unit only).
2. RESOLVE measurement basis: verified thickness (range-checked per work item), opening
   deductions and volume allowances — all taken from data/work_rules.csv and
   data/deduction_rules.csv.
3. CONVERT work quantity -> material quantity using recipe consumption rates.
4. PRICE using data/material_catalog.csv.
5. CHECK scope completeness and conflicts using data/scope_rules.csv and
   data/conflict_rules.csv (reported for review; never auto-added to the BOM).

No engineering values live in this file: see constructai.agents.bom_rules for the rule files.
"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from constructai.boq.schemas import BOQ
from constructai.agents.bom_rules import BOMRules, load_rules, validate_against_recipes


DATA = Path(__file__).resolve().parents[3] / "data"
MM_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*mm\b", re.IGNORECASE)


@dataclass
class BOMResult:
    lines: list = field(default_factory=list)
    mapping: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    validation: dict = field(default_factory=dict)
    total_cost: float = 0.0
    by_category: list = field(default_factory=list)
    deductions: list = field(default_factory=list)   # openings deducted from host work
    suggestions: list = field(default_factory=list)  # missing companion scope, for review (not in lines)


class MappingAgent:

    def __init__(self, catalog_path=None, recipes_path=None, rules_dir=None):
        data_dir = Path(rules_dir) if rules_dir else DATA
        catalog_path = catalog_path or data_dir / "material_catalog.csv"
        recipes_path = recipes_path or data_dir / "recipes.csv"
        self.rules: BOMRules = load_rules(data_dir)

        self.catalog = {}
        with open(catalog_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self.catalog[row["material_id"]] = {
                    **row,
                    "rate_inr": float(row["rate_inr"]),
                    "wastage_pct": float(row["wastage_pct"]),
                }

        self.works = {}
        with open(recipes_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "requires_thickness" not in (reader.fieldnames or []):
                raise ValueError("Recipe catalog is missing the requires_thickness column")
            for row_number, row in enumerate(reader, start=2):
                thickness_flag = row["requires_thickness"].strip().lower()
                if thickness_flag not in {"", "true", "false"}:
                    raise ValueError(f"Recipe catalog row {row_number} has invalid requires_thickness value")
                material_id = row["material_id"]
                if material_id != "NONE" and material_id not in self.catalog:
                    raise ValueError(f"Recipe catalog row {row_number} references unknown material '{material_id}'")
                work = self.works.setdefault(
                    row["work_key"],
                    {"text": row["match_text"], "unit": row["unit"],
                     "requires_thickness": thickness_flag == "true", "rows": []},
                )
                work["rows"].append((material_id, float(row["qty_per_unit"]), row["note"]))

        validate_against_recipes(self.rules, self.works, self.catalog)
        for key, work in self.works.items():
            work["texts"] = [work["text"], *self.rules.aliases.get(key, [])]

        n = len(self.works)
        df = Counter(token for work in self.works.values()
                     for token in {t for text in work["texts"] for t in self._tokens(text)})
        self.idf = {token: math.log((1 + n) / (1 + count)) + 1 for token, count in df.items()}

    # ------------------------------------------------------------------ matching
    def _tokens(self, text: str):
        text = text.lower()
        text = re.sub(r"\d+(?:\.\d+)?\s*x\s*\d+(?:\.\d+)?\s*m\b", " ", text)
        text = re.sub(r"(\d+)\s*mm", r"\1mm", text)
        return [w for w in re.findall(r"\d+mm|[a-z]+", text) if w not in self.rules.stopwords and len(w) > 1]

    def _vec(self, text: str):
        counter = Counter(self._tokens(text))
        vector = {t: (1 + math.log(c)) * self.idf.get(t, 1.0) for t, c in counter.items()}
        norm = math.sqrt(sum(v * v for v in vector.values())) or 1.0
        return vector, norm

    def _score(self, a: str, b: str):
        (va, na), (vb, nb) = self._vec(a), self._vec(b)
        return sum(v * vb.get(t, 0) for t, v in va.items()) / (na * nb)

    def match(self, description: str, unit: str):
        best = max(
            ((max(self._score(description, t) for t in work["texts"]), key)
             for key, work in self.works.items() if work["unit"] == unit),
            default=(0.0, None),
        )
        return best[1], best[0]

    def recipe_options(self, unit: str) -> list[dict[str, str]]:
        return [{"work_key": key, "description": work["text"], "unit": work["unit"]}
                for key, work in self.works.items() if work["unit"] == unit]

    # ------------------------------------------------------------------ measurement basis
    def _resolve_thickness(self, item, work_key: str) -> tuple[float | None, str]:
        """Return (thickness_mm, source) or (None, error message). Range comes from work_rules.csv."""
        rule = self.rules.work_rules[work_key]
        lo, hi = rule.thickness_min_mm, rule.thickness_max_mm
        description = item.description
        found = [(float(m.group(1)), m.start(), m.end()) for m in MM_PATTERN.finditer(description)]
        lowered = description.lower()
        window = self.rules.thickness_keyword_window

        def near_keyword(start, end):
            return any(kw in lowered[max(0, start - window): end + window] for kw in self.rules.thickness_keywords)

        keyed = [v for v, s, e in found if near_keyword(s, e) and lo <= v <= hi]
        in_range = sorted({v for v, _, _ in found if lo <= v <= hi})
        candidates = sorted(set(keyed)) or in_range
        if len(candidates) == 1:
            return candidates[0], "description"
        if len(candidates) > 1:
            listed = ", ".join(f"{v:g} mm" for v in candidates)
            return None, (f"several possible thicknesses ({listed}). State the verified thickness explicitly, "
                          f"e.g. '{candidates[0]:g} mm thick'")

        dims = item.dimensions
        if (rule.thickness_from_dimension_width and dims is not None and dims.width is not None
                and item.element_type.strip().lower() in self.rules.width_as_thickness_element_types
                and item.dimensions_source in self.rules.trusted_dimension_sources):
            width_mm = float(dims.width) * 1000
            if lo <= width_mm <= hi:
                return width_mm, f"{item.dimensions_source} wall width"
            return None, (f"reviewed width {width_mm:g} mm is outside the {lo:g}-{hi:g} mm range for this work item. "
                          f"Add the verified thickness explicitly")

        if found:
            listed = ", ".join(f"{v:g} mm" for v, _, _ in found)
            return None, (f"{listed} in the description is outside the {lo:g}-{hi:g} mm range for this work item. "
                          f"Add the verified thickness explicitly")
        return None, ("a verified thickness in millimetres is required. Add the thickness explicitly from the "
                      "reviewed drawing dimensions or BOQ description")

    def _opening_deductions(self, boq: BOQ, keys: dict[int, str]):
        """Area to deduct per host BOQ item, from openings linked by host_element_id."""
        rules = self.rules.deduction_rules
        opening_types = self.rules.opening_element_types
        by_element = {it.element_id: it for it in boq.items if it.element_id}
        deduct: dict[int, float] = {}
        records, warnings, unlinked = [], [], []

        for op in boq.items:
            if op.element_type.lower() not in opening_types:
                continue
            if not op.host_element_id:
                unlinked.append(op)
                continue
            host = by_element.get(op.host_element_id)
            if host is None:
                warnings.append(f"BOQ {op.item_number} '{op.description}': host element "
                                f"'{op.host_element_id}' is not in this BOQ; no deduction made.")
                continue
            host_key = keys.get(host.item_number)
            dims = op.dimensions
            if dims is None or dims.length is None or dims.height is None:
                if any(r.host_work_key == host_key for r in rules):
                    warnings.append(f"BOQ {op.item_number} '{op.description}': opening size (length x height) "
                                    f"is missing, so it was not deducted from BOQ {host.item_number}.")
                continue
            each = float(dims.length) * float(dims.height)
            count = float(op.quantity) if op.unit.lower() in self.rules.countable_units else 1.0
            for r in rules:
                if (r.host_work_key == host_key and op.element_type.lower() in r.opening_element_types
                        and each > r.min_opening_area_m2
                        and (r.max_opening_area_m2 is None or each <= r.max_opening_area_m2)):
                    area = each * count * r.deduct_fraction
                    deduct[host.item_number] = deduct.get(host.item_number, 0.0) + area
                    records.append({"BOQ item": str(host.item_number), "Opening BOQ item": str(op.item_number),
                                    "Opening": op.description, "Opening area each (m2)": round(each, 3),
                                    "Count": count, "Deducted (m2)": round(area, 3), "Rule": r.note})
        hosts_with_rules = {r.host_work_key for r in rules}
        if unlinked and any(keys.get(it.item_number) in hosts_with_rules for it in boq.items):
            ids = ", ".join(str(op.item_number) for op in unlinked)
            warnings.append(f"Openings (BOQ {ids}) are not linked to a host wall (host_element_id), so they were "
                            f"not deducted from masonry/plaster. Link them to get net quantities.")
        return deduct, records, warnings

    # ------------------------------------------------------------------ main
    def run(self, boq: BOQ, manual_mappings: dict[int, str] | None = None) -> BOMResult:
        out, acc = BOMResult(), {}
        validation_errors, validation_warnings = [], []
        manual_mappings = manual_mappings or {}
        resolved: dict[int, tuple[str, str]] = {}  # item_number -> (work_key, status)

        # pass 1: match every line
        for it in boq.items:
            description, unit, item_number = it.description, it.unit, it.item_number
            key, score = self.match(description, unit)
            manually_mapped = item_number in manual_mappings
            if manually_mapped:
                selected_key = manual_mappings[item_number]
                selected_work = self.works.get(selected_key)
                if selected_work is None or selected_work["unit"] != unit:
                    validation_errors.append(f"BOQ {item_number}: selected recipe is invalid for unit '{unit}'.")
                    key, score, manually_mapped = None, 0.0, False
                else:
                    key, score = selected_key, 1.0
            status = "MANUAL" if manually_mapped else (
                "OK" if score >= self.rules.ok_score else "REVIEW" if score >= self.rules.review_score else "NO MATCH")
            out.mapping.append({
                "BOQ item": str(item_number),
                "Description": description,
                "Matched work item": (f"{self.works[key]['text']} (manual selection)" if key and status == "MANUAL"
                                      else self.works[key]["text"] if key and status != "NO MATCH" else "-"),
                "Confidence": round(score, 2),
                "Status": status,
            })
            if status == "NO MATCH":
                validation_errors.append(
                    f"BOQ {item_number} '{description}': no work-item recipe match. Correct the BOQ description/unit "
                    "or select a verified recipe in the BOM mapping table.")
                continue
            if status == "REVIEW":
                validation_errors.append(
                    f"BOQ {item_number} matched '{self.works[key]['text']}' with low confidence "
                    f"({score:.2f}); select and confirm the correct recipe before continuing.")
                continue
            resolved[item_number] = (key, status)

        keys = {n: k for n, (k, _) in resolved.items()}
        deduct, out.deductions, ded_warnings = self._opening_deductions(boq, keys)
        validation_warnings.extend(ded_warnings)
        net_qty: dict[int, float] = {}

        # pass 2: measurement basis + material take-off
        for it, row in zip(boq.items, out.mapping):
            if it.item_number not in resolved:
                continue
            key, status = resolved[it.item_number]
            work = self.works[key]
            quantity = float(it.quantity)
            if it.item_number in deduct:
                d = deduct[it.item_number]
                if d >= quantity:
                    validation_errors.append(
                        f"BOQ {it.item_number} '{it.description}': linked openings ({d:.2f} m2) are not smaller than "
                        f"the gross quantity ({quantity:g} {it.unit}). Check the wall and opening dimensions.")
                    continue
                quantity -= d
                row["Gross quantity"] = float(it.quantity)
                row["Deducted openings"] = round(d, 3)
            row["Net quantity"] = round(quantity, 3)
            net_qty[it.item_number] = quantity

            mult = 1.0
            if work["requires_thickness"]:
                thickness_mm, info = self._resolve_thickness(it, key)
                if thickness_mm is None:
                    validation_errors.append(
                        f"BOQ {it.item_number} '{it.description}': the selected recipe requires a verified "
                        f"thickness — {info} before generating the BOM.")
                    continue
                rule = self.rules.work_rules[key]
                mult = thickness_mm / 1000 * (1 + rule.volume_allowance_pct / 100)
                row["Thickness (mm)"] = thickness_mm
                row["Thickness source"] = info
                if rule.volume_allowance_pct:
                    row["Volume allowance %"] = rule.volume_allowance_pct

            for material_id, per, _note in work["rows"]:
                if material_id == "NONE":
                    continue
                material = acc.setdefault(material_id, {"net": 0.0, "used": []})
                material["net"] += quantity * mult * per
                if str(it.item_number) not in material["used"]:
                    material["used"].append(str(it.item_number))

            if status == "MANUAL":
                validation_warnings.append(
                    f"BOQ {it.item_number} was manually mapped to '{work['text']}'; "
                    "confirm the recipe matches the actual construction.")

        # scope completeness and conflicts (reported, never auto-added)
        present = set(resolved_key for resolved_key, _ in resolved.values())
        for n, (key, _) in resolved.items():
            for rule in self.rules.scope_rules:
                if rule.trigger_work_key != key or present & set(rule.expected_work_keys):
                    continue
                target = self.works[rule.expected_work_keys[0]]
                item = next(i for i in boq.items if i.item_number == n)
                qty = None
                if rule.quantity_factor is not None and n in net_qty and target["unit"] == item.unit:
                    qty = round(net_qty[n] * rule.quantity_factor, 3)
                out.suggestions.append({
                    "For BOQ item": str(n), "Missing work": " / ".join(self.works[k]["text"] for k in rule.expected_work_keys),
                    "Suggested quantity": qty, "Unit": target["unit"], "Basis": rule.note,
                })
                validation_warnings.append(
                    f"BOQ {n} '{item.description}' has no matching '{self.works[rule.expected_work_keys[0]]['text']}' "
                    f"line — {rule.note}. Add it to the BOQ if it is in scope"
                    + (f" (about {qty:g} {target['unit']})." if qty is not None else "."))
        for c in self.rules.conflict_rules:
            if c.work_key_a in present and c.work_key_b in present:
                validation_warnings.append(c.message)

        # pricing
        categories = {}
        for material_id, material_data in sorted(acc.items(), key=lambda kv: (self.catalog[kv[0]]["category"], kv[0])):
            material = self.catalog[material_id]
            gross = material_data["net"] * (1 + material["wastage_pct"] / 100)
            order = (math.ceil(round(gross, 6)) if material["unit"] in self.rules.countable_units else round(gross, 2))
            cost = order * material["rate_inr"]
            out.lines.append({
                "Material": material["name"], "Category": material["category"], "Unit": material["unit"],
                "Net qty": round(material_data["net"], 2), "Wastage %": material["wastage_pct"],
                "Order qty": order, "Rate (Rs)": material["rate_inr"], "Cost (Rs)": round(cost),
                "Used in BOQ items": ", ".join(material_data["used"]),
            })
            categories[material["category"]] = categories.get(material["category"], 0) + cost

        out.total_cost = round(sum(categories.values()))
        out.by_category = [{"Category": c, "Cost (Rs)": round(v)}
                           for c, v in sorted(categories.items(), key=lambda kv: -kv[1])]
        out.validation = {"status": "blocked" if validation_errors else "ready",
                          "errors": validation_errors, "warnings": validation_warnings}
        out.warnings = [*validation_errors, *validation_warnings]
        if validation_errors:
            out.lines.clear()
            out.total_cost = 0.0
            out.by_category.clear()
        return out
