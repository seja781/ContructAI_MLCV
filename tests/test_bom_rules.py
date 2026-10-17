"""Tests for the data-driven BOM fixes: thickness resolution, opening deductions,
volume allowances, slab-by-area, scope completeness, conflicts and rule-file loading."""

import json
import shutil
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app.backend import main
from constructai.agents.bom_rules import DATA
from constructai.agents.mapping_agent import MappingAgent
from constructai.boq.schemas import BOQ, BOQItem, BOQStatus
from constructai.extraction.schemas import Dimensions, ExtractedElement, ExtractionResult

AGENT = MappingAgent()


def item(n, desc, qty, unit, element_type="wall", **kw):
    return BOQItem(item_number=n, description=desc, element_type=element_type, quantity=D(str(qty)), unit=unit,
                   calculation="test", confidence=1, **kw)


def run(items, manual=None, agent=AGENT):
    return agent.run(BOQ(source_name="t", status=BOQStatus.APPROVED, items=items), manual)


def line(bom, material_prefix):
    return next(l for l in bom.lines if l["Material"].startswith(material_prefix))


# ------------------------------------------------------------------ thickness
def test_plaster_uses_keyword_thickness_not_wall_thickness():
    bom = run([item(1, "Internal wall plaster on 230 mm wall, 12 mm thick", 100, "m2")])
    assert bom.validation["status"] == "ready"
    assert bom.mapping[0]["Thickness (mm)"] == 12
    # 100 m2 x 0.012 m x (1 + 20% allowance) x 6.24 bags/m3
    assert line(bom, "Cement")["Net qty"] == pytest.approx(100 * 0.012 * 1.2 * 6.24, abs=0.01)


def test_plaster_never_uses_wall_width_as_thickness():
    bom = run([item(1, "Internal wall plaster", 100, "m2",
                    dimensions=Dimensions(length=D("10"), width=D("0.23"), height=D("10")), dimensions_source="human")])
    assert bom.validation["status"] == "blocked" and not bom.lines
    assert "verified thickness" in bom.validation["errors"][0]


def test_ambiguous_thickness_blocks_with_candidates():
    bom = run([item(1, "Internal plaster 12 mm and 15 mm", 100, "m2")])
    assert bom.validation["status"] == "blocked"
    assert "12 mm, 15 mm" in bom.validation["errors"][0]


def test_out_of_range_thickness_blocks():
    bom = run([item(1, "Brick masonry 30 mm thick external wall", 10, "m2")])
    assert bom.validation["status"] == "blocked"
    assert "outside the 75-350 mm range" in bom.validation["errors"][0]


def test_masonry_uses_reviewed_wall_width():
    bom = run([item(1, "Brick masonry external wall", 30, "m2",
                    dimensions=Dimensions(length=D("10"), width=D("0.23"), height=D("3")), dimensions_source="human")])
    assert bom.validation["status"] == "ready"
    assert bom.mapping[0]["Thickness source"] == "human wall width"
    assert line(bom, "Burnt clay bricks")["Order qty"] == 3623  # 30 x 0.23 x 500 x 1.05, rounded up


def test_unreviewed_width_is_not_trusted():
    bom = run([item(1, "Brick masonry external wall", 30, "m2",
                    dimensions=Dimensions(length=D("10"), width=D("0.23"), height=D("3")), dimensions_source="vision")])
    assert bom.validation["status"] == "blocked"


# ------------------------------------------------------------------ slab by area
def test_slab_measured_in_area_uses_stated_thickness():
    bom = run([item(1, "RCC slab 150 mm thick", 100, "m2", element_type="slab")])
    assert bom.validation["status"] == "ready"
    assert line(bom, "Cement")["Net qty"] == pytest.approx(100 * 0.15 * 8.0)
    assert line(bom, "Cement")["Order qty"] == 124


# ------------------------------------------------------------------ deductions
def _wall_and_openings(door_host="W1", window_host="W1"):
    return [
        item(1, "Brick masonry 230 mm thick external wall", 30, "m2", element_id="W1"),
        item(2, "Door", 2, "nos", element_type="door", host_element_id=door_host,
             dimensions=Dimensions(length=D("0.9"), height=D("2.1"))),
        item(3, "Window", 1, "nos", element_type="window", host_element_id=window_host,
             dimensions=Dimensions(length=D("0.3"), height=D("0.3"))),  # 0.09 m2: below the 0.1 m2 threshold
    ]


def test_linked_openings_are_deducted_by_rule_threshold():
    bom = run(_wall_and_openings())
    assert bom.validation["status"] == "ready"
    row = bom.mapping[0]
    assert row["Gross quantity"] == 30 and row["Deducted openings"] == pytest.approx(3.78)
    assert row["Net quantity"] == pytest.approx(26.22)
    assert len(bom.deductions) == 1  # small window not deducted
    assert line(bom, "Burnt clay bricks")["Net qty"] == pytest.approx(26.22 * 0.23 * 500, abs=0.01)


def test_unlinked_openings_warn_and_are_not_deducted():
    bom = run(_wall_and_openings(door_host=None, window_host=None))
    assert "Gross quantity" not in bom.mapping[0]
    assert any("not linked to a host wall" in w for w in bom.validation["warnings"])


def test_plaster_deducts_half_for_mid_size_openings():
    items = [item(1, "Internal wall plaster 12 mm thick", 40, "m2", element_id="P1"),
             item(2, "Window", 1, "nos", element_type="window", host_element_id="P1",
                  dimensions=Dimensions(length=D("1.2"), height=D("1.2")))]
    bom = run(items)
    assert bom.mapping[0]["Deducted openings"] == pytest.approx(1.44 * 0.5)


def test_openings_larger_than_wall_block():
    items = [item(1, "Brick masonry 230 mm thick external wall", 2, "m2", element_id="W1"),
             item(2, "Door", 2, "nos", element_type="door", host_element_id="W1",
                  dimensions=Dimensions(length=D("0.9"), height=D("2.1")))]
    bom = run(items)
    assert bom.validation["status"] == "blocked"
    assert "not smaller than the gross quantity" in bom.validation["errors"][0]


# ------------------------------------------------------------------ scope & conflicts
def test_missing_companion_scope_is_suggested_not_added():
    bom = run([item(1, "Brick masonry 230 mm thick external wall", 30, "m2")])
    assert bom.validation["status"] == "ready"
    missing = {s["Missing work"]: s for s in bom.suggestions}
    plaster = next(s for k, s in missing.items() if "plaster" in k)
    assert plaster["Suggested quantity"] == 60 and plaster["Unit"] == "m2"
    assert not any("plaster" in l["Material"].lower() for l in bom.lines)


def test_present_companion_clears_suggestion():
    bom = run([item(1, "Internal wall plaster 12 mm thick", 50, "m2"),
               item(2, "Interior emulsion paint coats", 50, "m2")])
    assert bom.suggestions == []


def test_slab_without_steel_reports_missing_reinforcement():
    bom = run([item(1, "RCC slab 150 mm thick", 100, "m2", element_type="slab")])
    steel = next(s for s in bom.suggestions if "reinforcement" in s["Missing work"])
    assert steel["Suggested quantity"] is None  # never estimated: must come from drawings / BBS


def test_flooring_and_mortar_bed_conflict_warns():
    bom = run([item(1, "Vitrified tile flooring dry areas", 50, "m2", element_type="floor"),
               item(2, "Cement mortar bed under flooring 40 mm thick", 50, "m2", element_type="floor")])
    assert any("counted twice" in w for w in bom.validation["warnings"])


def test_block_masonry_is_not_auto_matched_to_brick_recipe():
    bom = run([item(1, "AAC block masonry 200 mm thick", 20, "m2")])
    assert bom.validation["status"] == "blocked" and bom.mapping[0]["Status"] != "OK"


# ------------------------------------------------------------------ rules are data, not code
@pytest.fixture
def data_copy(tmp_path):
    d = tmp_path / "data"
    shutil.copytree(DATA, d, ignore=shutil.ignore_patterns("sample"))
    return d


def test_changing_data_changes_behaviour_without_code(data_copy):
    (data_copy / "work_rules.csv").write_text(
        (data_copy / "work_rules.csv").read_text().replace("plaster_int,6,30,false,20", "plaster_int,6,30,false,0"))
    agent = MappingAgent(rules_dir=data_copy)
    bom = run([item(1, "Internal wall plaster 12 mm thick", 100, "m2")], agent=agent)
    assert line(bom, "Cement")["Net qty"] == pytest.approx(100 * 0.012 * 6.24, abs=0.01)


def test_threshold_comes_from_config(data_copy):
    cfg = json.loads((data_copy / "mapping_config.json").read_text())
    cfg["ok_score"] = 0.99
    (data_copy / "mapping_config.json").write_text(json.dumps(cfg))
    bom = run([item(1, "Brick masonry 230 mm thick external wall", 30, "m2")], agent=MappingAgent(rules_dir=data_copy))
    assert bom.mapping[0]["Status"] == "REVIEW"


def test_missing_work_rule_fails_loudly(data_copy):
    p = data_copy / "work_rules.csv"
    p.write_text("\n".join(l for l in p.read_text().splitlines() if not l.startswith("bed,")) + "\n")
    with pytest.raises(ValueError, match="work_rules.csv has no row for 'bed'"):
        MappingAgent(rules_dir=data_copy)


def test_unknown_work_key_in_rules_fails_loudly(data_copy):
    with open(data_copy / "scope_rules.csv", "a") as f:
        f.write("not_a_work,plaster_int,1,typo\n")
    with pytest.raises(ValueError, match="unknown work_key"):
        MappingAgent(rules_dir=data_copy)


def test_missing_unit_definition_fails_loudly(data_copy):
    p = data_copy / "units.csv"
    p.write_text("\n".join(l for l in p.read_text().splitlines() if not l.startswith("bag,")) + "\n")
    with pytest.raises(ValueError, match="units.csv has no entry"):
        MappingAgent(rules_dir=data_copy)


# ------------------------------------------------------------------ end-to-end through the API
def test_api_host_link_flows_from_extraction_to_bom_deduction(monkeypatch):
    for store in (main.extraction_sessions, main.extraction_revisions, main.boq_sessions,
                  main.boq_extraction_revisions, main.boq_recipe_overrides):
        store.clear()

    def fake_extract(source):
        return ExtractionResult(source_name=source.name, elements=[
            ExtractedElement(element_type="wall", element_id="W1", description="Brick masonry 230 mm thick external wall",
                             dimensions=Dimensions(length=D("10"), height=D("3")), unit="m2", confidence=0.9),
            ExtractedElement(element_type="door", element_id="D1", description="Door", quantity=D("1"), unit="nos",
                             dimensions=Dimensions(length=D("0.9"), height=D("2.1")), confidence=0.9),
        ])

    monkeypatch.setattr(main.extraction_agent, "extract", fake_extract)
    with TestClient(main.app) as client:
        ext = client.post("/extractions", files={"file": ("d.svg", b"<svg />", "image/svg+xml")}).json()["extraction_id"]
        assert client.patch(f"/extractions/{ext}/elements/1", json={"host_element_id": "W1"}).status_code == 200
        for i in (0, 1):
            assert client.post(f"/extractions/{ext}/elements/{i}/review", json={"decision": "approved"}).status_code == 200
        boq = client.post(f"/extractions/{ext}/boq").json()
        assert boq["items"][1]["host_element_id"] == "W1"
        client.post(f"/boqs/{boq['boq_id']}/submit")
        client.post(f"/boqs/{boq['boq_id']}/approve")
        bom = client.post(f"/boqs/{boq['boq_id']}/bom").json()
    assert bom["validation"]["status"] == "ready", bom["validation"]
    assert bom["mapping"][0]["Deducted openings"] == pytest.approx(1.89)
    assert bom["deductions"][0]["Opening BOQ item"] == "2"


def test_room_width_is_never_used_as_thickness():
    """Extracted rooms carry real widths (e.g. 3.35 m); only walls may lend their width as a thickness."""
    room = item(1, "Brick masonry external wall", 30, "m2", element_type="room",
                dimensions=Dimensions(length=D("10"), width=D("0.23"), height=D("3")), dimensions_source="ocr")
    assert run([room]).validation["status"] == "blocked"
    wall = item(1, "Brick masonry external wall", 30, "m2", element_type="wall",
                dimensions=Dimensions(length=D("10"), width=D("0.23"), height=D("3")), dimensions_source="text_layer")
    bom = run([wall])
    assert bom.validation["status"] == "ready" and bom.mapping[0]["Thickness source"] == "text_layer wall width"


def test_width_element_types_come_from_config(data_copy):
    cfg = json.loads((data_copy / "mapping_config.json").read_text())
    cfg["width_as_thickness_element_types"] = ["wall", "room"]
    (data_copy / "mapping_config.json").write_text(json.dumps(cfg))
    room = item(1, "Brick masonry external wall", 30, "m2", element_type="room",
                dimensions=Dimensions(length=D("10"), width=D("0.23"), height=D("3")), dimensions_source="ocr")
    assert run([room], agent=MappingAgent(rules_dir=data_copy)).validation["status"] == "ready"
