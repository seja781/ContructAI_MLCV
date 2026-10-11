from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.backend import main
from constructai.extraction.schemas import ExtractedElement, ExtractionResult


@pytest.fixture
def review_session(monkeypatch):
    main.extraction_sessions.clear()
    main.extraction_revisions.clear()
    main.boq_sessions.clear()
    main.boq_extraction_revisions.clear()
    main.boq_recipe_overrides.clear()

    def fake_extract(source):
        return ExtractionResult(
            source_name=source.name,
            elements=[
                ExtractedElement(
                    element_type="candidate",
                    element_id="E-001",
                    description="Uncertain object",
                    quantity=Decimal("2"),
                    unit="pcs",
                    confidence=0.99,
                    review_reasons=["prototype_classification"],
                )
            ],
        )

    monkeypatch.setattr(main.extraction_agent, "extract", fake_extract)
    with TestClient(main.app) as client:
        response = client.post(
            "/extractions",
            files={"file": ("drawing.svg", b"<svg />", "image/svg+xml")},
        )
        assert response.status_code == 200
        yield client, response.json()["extraction_id"]


def approve_element(client: TestClient, extraction_id: str, fields: dict | None = None) -> dict:
    if fields is not None:
        response = client.patch(
            f"/extractions/{extraction_id}/elements/0",
            json=fields,
        )
        assert response.status_code == 200
    response = client.post(
        f"/extractions/{extraction_id}/elements/0/review",
        json={"decision": "approved"},
    )
    assert response.status_code == 200
    return response.json()


def test_pending_and_rejected_elements_cannot_generate_boq(review_session) -> None:
    client, extraction_id = review_session

    assert client.post(f"/extractions/{extraction_id}/boq").status_code == 409
    rejected = client.post(
        f"/extractions/{extraction_id}/elements/0/review",
        json={"decision": "rejected"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["elements"][0]["review_status"] == "rejected"
    assert client.post(f"/extractions/{extraction_id}/boq").status_code == 409


def test_corrected_extraction_values_feed_boq(review_session) -> None:
    client, extraction_id = review_session
    approve_element(
        client,
        extraction_id,
        {
            "element_type": "fixture",
            "description": "Corrected light fitting",
            "quantity": 14,
            "unit": "pcs",
            "specification": "LED",
        },
    )

    response = client.post(f"/extractions/{extraction_id}/boq")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["element_type"] == "fixture"
    assert item["description"] == "Corrected light fitting"
    assert item["quantity"] == 14
    assert item["unit"] == "pcs"


def test_corrected_dimensions_are_used_for_boq_quantity(review_session) -> None:
    client, extraction_id = review_session
    approve_element(
        client,
        extraction_id,
        {
            "element_type": "wall",
            "description": "Measured wall",
            "dimensions": {"length": 5, "height": 3},
            "quantity": None,
            "unit": "m2",
        },
    )

    response = client.post(f"/extractions/{extraction_id}/boq")

    assert response.status_code == 200
    assert response.json()["items"][0]["quantity"] == 15


def test_missing_required_values_block_element_approval(review_session) -> None:
    client, extraction_id = review_session
    client.patch(f"/extractions/{extraction_id}/elements/0", json={"unit": ""})
    response = client.post(
        f"/extractions/{extraction_id}/elements/0/review",
        json={"decision": "approved"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Enter a value for: unit"


def test_boq_generation_reports_extraction_validation_details(review_session) -> None:
    client, extraction_id = review_session
    approve_element(client, extraction_id, {"unit": "bucket"})

    response = client.post(f"/extractions/{extraction_id}/boq")

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Resolve extraction validation errors before generating the BOQ: "
        "elements[0].unit: Unsupported unit 'bucket'"
    )


def test_nos_is_a_supported_count_unit(review_session) -> None:
    client, extraction_id = review_session
    approve_element(client, extraction_id, {"unit": "nos"})

    response = client.post(f"/extractions/{extraction_id}/boq")

    assert response.status_code == 200
    assert response.json()["items"][0]["unit"] == "nos"


def test_bom_requires_approved_boq_and_maps_materials(review_session) -> None:
    client, extraction_id = review_session
    approve_element(
        client,
        extraction_id,
        {
            "element_type": "masonry",
            "description": "External masonry wall",
            "quantity": 10,
            "unit": "m2",
        },
    )
    boq_response = client.post(f"/extractions/{extraction_id}/boq")
    boq_id = boq_response.json()["boq_id"]

    assert client.post(f"/boqs/{boq_id}/bom").status_code == 409
    assert client.post(f"/boqs/{boq_id}/submit").status_code == 200
    assert client.post(f"/boqs/{boq_id}/approve").status_code == 200

    response = client.post(f"/boqs/{boq_id}/bom")

    assert response.status_code == 200
    bom = response.json()
    assert bom["mapping"][0]["Status"] in {"OK", "REVIEW"}
    assert bom["validation"]["status"] == "ready"
    assert bom["validation"]["errors"] == []
    assert bom["lines"]
    assert bom["total_cost"] > 0


def test_bom_mapping_errors_are_reported_for_review(review_session) -> None:
    client, extraction_id = review_session
    approve_element(client, extraction_id)
    boq_response = client.post(f"/extractions/{extraction_id}/boq")
    boq_id = boq_response.json()["boq_id"]
    client.post(f"/boqs/{boq_id}/submit")
    client.post(f"/boqs/{boq_id}/approve")

    bom_response = client.post(f"/boqs/{boq_id}/bom")
    assert bom_response.status_code == 200
    bom = bom_response.json()
    assert bom["validation"]["status"] == "blocked"
    assert bom["validation"]["errors"]
    assert bom["mapping"][0]["Status"] == "NO MATCH"


def test_unmatched_boq_item_can_be_manually_mapped_to_valid_unit_recipe(review_session) -> None:
    client, extraction_id = review_session
    approve_element(
        client,
        extraction_id,
        {
            "description": "Exterior wall, south elevation",
            "quantity": 12,
            "unit": "m2",
        },
    )
    boq_response = client.post(f"/extractions/{extraction_id}/boq")
    boq_id = boq_response.json()["boq_id"]
    client.post(f"/boqs/{boq_id}/submit")
    client.post(f"/boqs/{boq_id}/approve")

    options = client.get(f"/boqs/{boq_id}/mapping-options/1")
    assert options.status_code == 200
    assert options.json()["unit"] == "m2"
    recipe = next(
        recipe for recipe in options.json()["recipes"]
        if recipe["work_key"] == "masonry"
    )
    assert recipe["unit"] == "m2"

    mapped = client.put(
        f"/boqs/{boq_id}/mapping",
        json={"item_number": 1, "work_key": recipe["work_key"]},
    )
    assert mapped.status_code == 200

    response = client.post(f"/boqs/{boq_id}/bom")

    assert response.status_code == 200
    bom = response.json()
    assert bom["validation"]["status"] == "ready"
    assert bom["mapping"][0]["Status"] == "MANUAL"
    assert "manual selection" in bom["mapping"][0]["Matched work item"]
    assert bom["lines"]


def test_manual_mapping_rejects_recipe_with_wrong_unit(review_session) -> None:
    client, extraction_id = review_session
    approve_element(client, extraction_id)
    boq_response = client.post(f"/extractions/{extraction_id}/boq")
    boq_id = boq_response.json()["boq_id"]
    client.post(f"/boqs/{boq_id}/submit")
    client.post(f"/boqs/{boq_id}/approve")

    response = client.put(
        f"/boqs/{boq_id}/mapping",
        json={"item_number": 1, "work_key": "masonry"},
    )

    assert response.status_code == 422
    assert "not available for BOQ unit 'pcs'" in response.json()["detail"]


def test_labour_equipment_requires_validated_bom_and_returns_estimates(review_session) -> None:
    client, extraction_id = review_session
    approve_element(
        client,
        extraction_id,
        {
            "element_type": "wall",
            "description": "External masonry wall",
            "quantity": 10,
            "unit": "m2",
        },
    )
    boq_response = client.post(f"/extractions/{extraction_id}/boq")
    boq_id = boq_response.json()["boq_id"]

    assert client.post(f"/boqs/{boq_id}/resources").status_code == 409
    client.post(f"/boqs/{boq_id}/submit")
    client.post(f"/boqs/{boq_id}/approve")
    mapped = client.put(
        f"/boqs/{boq_id}/mapping",
        json={"item_number": 1, "work_key": "masonry"},
    )
    assert mapped.status_code == 200

    response = client.post(f"/boqs/{boq_id}/resources")

    assert response.status_code == 200
    plan = response.json()
    assert "Illustrative prototype assumptions only" in plan["assumption_notice"]
    assert plan["mappings"][0]["Status"] == "MANUAL"
    assert plan["labour"]
    assert plan["equipment"]
    assert all(line["BOQ quantity"] == 10 for line in plan["labour"] + plan["equipment"])
    assert all(line["BOQ unit"] == "m2" for line in plan["labour"] + plan["equipment"])
    assert plan["labour_cost"] > 0
    assert plan["equipment_cost"] > 0


def test_labour_equipment_is_blocked_when_bom_mapping_has_errors(review_session) -> None:
    client, extraction_id = review_session
    approve_element(client, extraction_id)
    boq_response = client.post(f"/extractions/{extraction_id}/boq")
    boq_id = boq_response.json()["boq_id"]
    client.post(f"/boqs/{boq_id}/submit")
    client.post(f"/boqs/{boq_id}/approve")

    response = client.post(f"/boqs/{boq_id}/resources")

    assert response.status_code == 422
    assert "Resolve BOM mapping errors" in response.json()["detail"]


def test_sample_room_flooring_description_maps_with_assumption_notice(review_session) -> None:
    client, extraction_id = review_session
    approve_element(
        client,
        extraction_id,
        {
            "element_type": "floor",
            "description": "Living room floor area vitrified tile flooring sample assumption",
            "quantity": 16.5,
            "unit": "m2",
        },
    )
    boq_response = client.post(f"/extractions/{extraction_id}/boq")
    boq_id = boq_response.json()["boq_id"]
    client.post(f"/boqs/{boq_id}/submit")
    client.post(f"/boqs/{boq_id}/approve")

    response = client.post(f"/boqs/{boq_id}/bom")

    assert response.status_code == 200
    bom = response.json()
    assert bom["validation"]["status"] == "ready"
    assert bom["mapping"][0]["Matched work item"].startswith("sample assumption")
    assert {line["Material"] for line in bom["lines"]} == {
        "Vitrified tiles 600x600 mm",
        "Tile adhesive (cement based)",
        "Tile grout",
    }

    resources = client.post(f"/boqs/{boq_id}/resources")

    assert resources.status_code == 200
    plan = resources.json()
    assert plan["labour"]
    assert plan["equipment"]
    assert all(line["Work key"] == "sample_room_floor" for line in plan["labour"] + plan["equipment"])


def test_sample_external_wall_description_maps_with_visible_thickness_assumption(review_session) -> None:
    client, extraction_id = review_session
    approve_element(
        client,
        extraction_id,
        {
            "element_type": "wall",
            "description": "External masonry wall sample assumption 3 m height",
            "dimensions": {"length": 10, "width": 0.23, "height": 3},
            "quantity": None,
            "unit": "m2",
        },
    )
    boq_response = client.post(f"/extractions/{extraction_id}/boq")
    boq_id = boq_response.json()["boq_id"]
    client.post(f"/boqs/{boq_id}/submit")
    client.post(f"/boqs/{boq_id}/approve")

    response = client.post(f"/boqs/{boq_id}/bom")

    assert response.status_code == 200
    bom = response.json()
    assert bom["validation"]["status"] == "ready"
    assert bom["mapping"][0]["Matched work item"].startswith("sample assumption")
    assert any("assumes 230 mm" in warning for warning in bom["validation"]["warnings"])


def test_clearing_dimensions_does_not_retain_previous_ocr_values(review_session) -> None:
    client, extraction_id = review_session
    added = client.patch(
        f"/extractions/{extraction_id}/elements/0",
        json={"dimensions": {"length": 4, "height": 3}},
    )
    assert added.status_code == 200
    cleared = client.patch(
        f"/extractions/{extraction_id}/elements/0",
        json={"dimensions": None},
    )

    assert cleared.status_code == 200
    assert cleared.json()["elements"][0]["dimensions"] is None
    assert cleared.json()["elements"][0]["dimensions_source"] is None


def test_final_approval_requires_review_and_validations(review_session) -> None:
    client, extraction_id = review_session
    approve_element(client, extraction_id)
    generated = client.post(f"/extractions/{extraction_id}/boq").json()
    boq_id = generated["boq_id"]

    assert client.post(f"/boqs/{boq_id}/approve").status_code == 409
    assert client.post(f"/boqs/{boq_id}/submit").status_code == 200
    invalid = client.patch(f"/boqs/{boq_id}/items/1", json={"unit": "bucket"})
    assert invalid.status_code == 200
    assert any(issue["severity"] == "error" for issue in invalid.json()["validation_issues"])
    assert client.post(f"/boqs/{boq_id}/submit").status_code == 200
    assert client.post(f"/boqs/{boq_id}/approve").status_code == 409


def test_editing_approved_element_invalidates_old_boq(review_session) -> None:
    client, extraction_id = review_session
    approve_element(client, extraction_id)
    boq = client.post(f"/extractions/{extraction_id}/boq").json()
    assert client.post(f"/boqs/{boq['boq_id']}/submit").status_code == 200

    edited = client.patch(
        f"/extractions/{extraction_id}/elements/0",
        json={"description": "Corrected after approval"},
    )

    assert edited.status_code == 200
    assert edited.json()["elements"][0]["review_status"] == "pending"
    assert client.post(f"/boqs/{boq['boq_id']}/approve").status_code == 409
    assert client.post(f"/extractions/{extraction_id}/boq").status_code == 409