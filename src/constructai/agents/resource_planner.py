"""Estimate labour and equipment requirements from approved BOQ quantities."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from constructai.agents.mapping_agent import MappingAgent, OK_SCORE, REVIEW_SCORE
from constructai.boq.schemas import BOQ


DATA = Path(__file__).resolve().parents[3] / "data"
ASSUMPTION_NOTICE = (
    "Illustrative prototype assumptions only. Productivity and hourly rates "
    "are sample values, not company rates; validate before project or procurement use."
)


class ResourcePlanner:
    def __init__(
        self,
        mapping_agent: MappingAgent,
        assumptions_path: Path | None = None,
    ) -> None:
        self.mapping_agent = mapping_agent
        path = assumptions_path or DATA / "labour_equipment_assumptions.csv"
        self.assumptions: dict[str, list[dict[str, Any]]] = {}

        with path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                self.assumptions.setdefault(row["work_key"], []).append(
                    {
                        **row,
                        "hours_per_unit": float(row["hours_per_unit"]),
                        "rate_inr_per_hour": float(row["rate_inr_per_hour"]),
                    }
                )

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
                hours = float(item.quantity) * assumption["hours_per_unit"]
                resource = {
                    "BOQ item": str(item.item_number),
                    "Work item": work_description,
                    "Resource": assumption["resource"],
                    "Hours": round(hours, 2),
                    "Hours per BOQ unit": assumption["hours_per_unit"],
                    "Rate (Rs/hour)": assumption["rate_inr_per_hour"],
                    "Estimated cost (Rs)": round(
                        hours * assumption["rate_inr_per_hour"], 2
                    ),
                    "Assumption": assumption["assumption"],
                }
                destination = (
                    labour
                    if assumption["resource_type"] == "labour"
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
