from pathlib import Path

import pytest

from constructai.agents.mapping_agent import MappingAgent
from constructai.agents.resource_planner import ResourcePlanner


def test_every_bom_recipe_has_labour_or_equipment_assumptions() -> None:
    mapping_agent = MappingAgent()
    planner = ResourcePlanner(mapping_agent)

    assert set(mapping_agent.works) <= set(planner.assumptions)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            "unknown_work,labour,Mason,0.8,120,Test",
            "unknown work key",
        ),
        (
            "masonry,labour,Mason,-0.8,120,Test",
            "finite, non-negative",
        ),
        (
            "masonry,vehicle,Truck,0.8,120,Test",
            "invalid resource details",
        ),
    ],
)
def test_invalid_resource_assumptions_fail_explicitly(
    tmp_path: Path,
    row: str,
    message: str,
) -> None:
    assumptions = tmp_path / "assumptions.csv"
    assumptions.write_text(
        "work_key,resource_type,resource,hours_per_unit,rate_inr_per_hour,assumption\n"
        f"{row}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        ResourcePlanner(MappingAgent(), assumptions)
