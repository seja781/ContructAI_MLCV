"""Estimate labour and equipment requirements from approved BOQ quantities."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from constructai.agents.mapping_agent import MappingAgent, OK_SCORE, REVIEW_SCORE
from constructai.boq.schemas import BOQ


DATA = Path(__file__).resolve().parents[3] / "data"
ASSUMPTION_NOTICE = (
    "Illustrative prototype assumptions only. Productivity and hourly rates "
    "are sample values, not company rates; validate before project or procurement use."
)


@dataclass(frozen=True)
class ResourceAssumption:
    work_key: str
    resource_type: str
    resource: str
    hours_per_unit: float
    rate_inr_per_hour: float
    assumption: str


class ResourcePlanner:
    def __init__(
        self,
        mapping_agent: MappingAgent,
        assumptions_path: Path | None = None,
    ) -> None:
        self.mapping_agent = mapping_agent
        path = assumptions_path or DATA / "labour_equipment_assumptions.csv"
        self.assumptions: dict[str, list[ResourceAssumption]] = {}

        with path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            required_columns = {
                "work_key",
                "resource_type",
                "resource",
                "hours_per_unit",
                "rate_inr_per_hour",
                "assumption",
            }
            if not required_columns.issubset(reader.fieldnames or set()):
                missing = sorted(required_columns - set(reader.fieldnames or []))
                raise ValueError(
                    f"Resource assumptions file is missing columns: {', '.join(missing)}"
                )
            for row_number, row in enumerate(reader, start=2):
                values = {
                    column: (row.get(column) or "").strip()
                    for column in required_columns
                }
                missing_values = sorted(
                    column for column, value in values.items() if not value
                )
                if missing_values:
                    raise ValueError(
                        f"Resource assumptions row {row_number} is missing values: "
                        f"{', '.join(missing_values)}"
                    )
                work_key = values["work_key"]
                resource_type = values["resource_type"]
                resource = values["resource"]
                try:
                    hours_per_unit = float(values["hours_per_unit"])
                    rate_inr_per_hour = float(values["rate_inr_per_hour"])
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"Resource assumptions row {row_number} has invalid productivity or rate"
                    ) from error
                if work_key not in mapping_agent.works:
                    raise ValueError(
                        f"Resource assumptions row {row_number} has unknown work key '{work_key}'"
                    )
                if resource_type not in {"labour", "equipment"} or not resource:
                    raise ValueError(
                        f"Resource assumptions row {row_number} has invalid resource details"
                    )
                if (
                    not math.isfinite(hours_per_unit)
                    or not math.isfinite(rate_inr_per_hour)
                    or hours_per_unit < 0
                    or rate_inr_per_hour < 0
                ):
                    raise ValueError(
                        f"Resource assumptions row {row_number} requires finite, non-negative productivity and rate"
                    )
                assumption = ResourceAssumption(
                    work_key=work_key,
                    resource_type=resource_type,
                    resource=resource,
                    hours_per_unit=hours_per_unit,
                    rate_inr_per_hour=rate_inr_per_hour,
                    assumption=values["assumption"],
                )
                self.assumptions.setdefault(work_key, []).append(assumption)

    def generate(
        self,
        boq: BOQ,
        manual_mappings: dict[int, str] | None = None,
    ) -> dict[str, Any]:
        mappings: list[dict[str, Any]] = []
        labour: list[dict[str, Any]] = []
        equipment: list[dict[str, Any]] = []
        warnings: list[str] = []
        manual_mappings = manual_mappings or {}

        for item in boq.items:
            work_key, score = self.mapping_agent.match(item.description, item.unit)
            manually_mapped = item.item_number in manual_mappings
            if manually_mapped:
                work_key = manual_mappings[item.item_number]
                work = self.mapping_agent.works.get(work_key)
                if work is None or work["unit"] != item.unit:
                    work_key, score = None, 0.0
                    manually_mapped = False
                else:
                    score = 1.0

            status = "MANUAL" if manually_mapped else (
                "OK"
                if score >= OK_SCORE
                else "REVIEW"
                if score >= REVIEW_SCORE
                else "NO MATCH"
            )
            work_description = (
                f"{self.mapping_agent.works[work_key]['text']} (manual selection)"
                if work_key and status == "MANUAL"
                else self.mapping_agent.works[work_key]["text"]
                if work_key and status != "NO MATCH"
                else "-"
            )
            mappings.append(
                {
                    "BOQ item": str(item.item_number),
                    "Description": item.description,
                    "Matched work item": work_description,
                    "Confidence": round(score, 2),
                    "Status": status,
                }
            )

            if status == "NO MATCH" or work_key is None:
                warnings.append(
                    f"BOQ {item.item_number} '{item.description}': no resource "
                    "productivity match; add an assumption for this work item."
                )
                continue

            assumptions = self.assumptions.get(work_key, [])
            if not assumptions:
                warnings.append(
                    f"BOQ {item.item_number}: no labour or equipment assumptions "
                    f"are configured for '{work_description}'."
                )
                continue

            for assumption in assumptions:
                hours = float(item.quantity) * assumption.hours_per_unit
                resource = {
                    "BOQ item": str(item.item_number),
                    "BOQ quantity": float(item.quantity),
                    "BOQ unit": item.unit,
                    "Work key": work_key,
                    "Work item": work_description,
                    "Resource": assumption.resource,
                    "Hours": round(hours, 2),
                    "Hours per BOQ unit": assumption.hours_per_unit,
                    "Rate (Rs/hour)": assumption.rate_inr_per_hour,
                    "Estimated cost (Rs)": round(
                        hours * assumption.rate_inr_per_hour, 2
                    ),
                    "Assumption": assumption.assumption,
                }
                destination = (
                    labour
                    if assumption.resource_type == "labour"
                    else equipment
                )
                destination.append(resource)

            if status == "REVIEW":
                warnings.append(
                    f"BOQ {item.item_number} matched '{work_description}' "
                    f"with low confidence ({score:.2f}); verify this resource estimate."
                )
            elif status == "MANUAL":
                warnings.append(
                    f"BOQ {item.item_number} uses manually selected work item "
                    f"'{work_description}'; confirm the resource estimate."
                )

        labour_cost = round(
            sum(line["Estimated cost (Rs)"] for line in labour), 2
        )
        equipment_cost = round(
            sum(line["Estimated cost (Rs)"] for line in equipment), 2
        )
        return {
            "assumption_notice": ASSUMPTION_NOTICE,
            "mappings": mappings,
            "labour": labour,
            "equipment": equipment,
            "warnings": warnings,
            "labour_cost": labour_cost,
            "equipment_cost": equipment_cost,
            "total_cost": round(labour_cost + equipment_cost, 2),
        }
