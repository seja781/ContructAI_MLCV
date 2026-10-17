"""Step 06: human BOM approval gate and handoff into procurement."""
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app.backend import main
from constructai.extraction.schemas import ExtractedElement, ExtractionResult

PLASTER = {"element_type": "wall", "element_id": "P1", "description": "Internal wall plaster 12 mm thick",
           "quantity": D("50"), "unit": "m2"}
PAINT = {"element_type": "wall", "element_id": "P2", "description": "Interior emulsion paint coats",
         "quantity": D("50"), "unit": "m2"}
MASONRY = {"element_type": "wall", "element_id": "W1", "description": "Brick masonry 230 mm thick external wall",
           "quantity": D("30"), "unit": "m2"}
UNKNOWN = {"element_type": "candidate", "element_id": "X1", "description": "Uncertain object",
           "quantity": D("2"), "unit": "pcs"}


@pytest.fixture
def flow(monkeypatch):
    for store in (main.extraction_sessions, main.extraction_revisions, main.boq_sessions,
                  main.boq_extraction_revisions, main.boq_recipe_overrides, main.bom_approvals):
        store.clear()
    elements: list[dict] = []

    def fake_extract(source):
        return ExtractionResult(source_name="Block-A.pdf",
                                elements=[ExtractedElement(confidence=0.9, **e) for e in elements])

    monkeypatch.setattr(main.extraction_agent, "extract", fake_extract)

    def approved_boq(client, *els):
        elements[:] = list(els)
        ext = client.post("/extractions", files={"file": ("Block-A.svg", b"<svg />", "image/svg+xml")}).json()
        for i in range(len(els)):
            assert client.post(f"/extractions/{ext['extraction_id']}/elements/{i}/review",
                               json={"decision": "approved"}).status_code == 200
        boq = client.post(f"/extractions/{ext['extraction_id']}/boq").json()
        client.post(f"/boqs/{boq['boq_id']}/submit")
        client.post(f"/boqs/{boq['boq_id']}/approve")
        return ext["extraction_id"], boq["boq_id"]

    with TestClient(main.app) as client:
        yield client, approved_boq


def approve(client, boq_id, **body):
    return client.post(f"/boqs/{boq_id}/bom/approve", json={"approved_by": "Site Engineer", **body})


def test_approval_hands_bom_to_procurement(flow):
    client, approved_boq = flow
    _, boq_id = approved_boq(client, PLASTER, PAINT)
    bom = client.post(f"/boqs/{boq_id}/bom").json()
    assert bom["validation"]["status"] == "ready" and bom["validation"]["warnings"] == []

    assert client.post(f"/boqs/{boq_id}/bom/approve", json={"approved_by": "   "}).status_code == 422
    r = approve(client, boq_id, comment="Quantities checked", project_code="BLKA")
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["approved_by"] == "Site Engineer" and rec["material_count"] == len(bom["lines"])

    # the procurement list exists, copied from the approved BOM using Order qty
    plist = client.get(f"/procurement/api/procurement-lists/{rec['procurement']['list_id']}").json()
    by_name = {i["name"]: i for i in plist["items"]}
    for line in bom["lines"]:
        assert by_name[line["Material"]]["quantity"] == line["Order qty"]
    pbom = client.get(f"/procurement/api/boms/{rec['procurement']['bom_id']}").json()
    assert pbom["approved_by"] == "Site Engineer" and pbom["approval_comment"] == "Quantities checked"
    assert pbom["read_only"] is True
    project = client.get(f"/procurement/api/projects/{rec['procurement']['project_id']}").json()
    assert project["name"] == "Block-A.svg" and project["code"] == "BLKA"  # real upload name, not "drawing.svg"

    assert approve(client, boq_id).status_code == 409  # no silent re-approval
    assert client.get(f"/boqs/{boq_id}/bom/approval").json()["approved_by"] == "Site Engineer"


def test_warnings_must_be_acknowledged(flow):
    client, approved_boq = flow
    _, boq_id = approved_boq(client, MASONRY)
    warnings = client.post(f"/boqs/{boq_id}/bom").json()["validation"]["warnings"]
    assert warnings  # missing plaster / paint scope
    r = approve(client, boq_id)
    assert r.status_code == 409 and "acknowledge" in r.json()["detail"]
    r = approve(client, boq_id, acknowledge_warnings=True)
    assert r.status_code == 201 and r.json()["warnings_acknowledged"] == warnings
    pbom = client.get(f"/procurement/api/boms/{r.json()['procurement']['bom_id']}").json()
    assert pbom["import_warnings"] == warnings


def test_blocked_bom_cannot_be_approved(flow):
    client, approved_boq = flow
    _, boq_id = approved_boq(client, UNKNOWN)
    r = approve(client, boq_id, acknowledge_warnings=True)
    assert r.status_code == 409 and "blocking" in r.json()["detail"]


def test_bom_approval_requires_approved_boq(flow):
    client, approved_boq = flow
    _, boq_id = approved_boq(client, PLASTER, PAINT)
    client.post(f"/boqs/{boq_id}/reopen")
    assert approve(client, boq_id).status_code == 409


def test_reopen_invalidates_approval_and_reapproval_reuses_project(flow):
    client, approved_boq = flow
    ext_id, boq_id = approved_boq(client, PLASTER, PAINT)
    first = approve(client, boq_id).json()
    client.post(f"/boqs/{boq_id}/reopen")
    assert client.get(f"/boqs/{boq_id}/bom/approval").status_code == 404
    client.post(f"/boqs/{boq_id}/submit")
    client.post(f"/boqs/{boq_id}/approve")
    second = approve(client, boq_id).json()
    assert second["procurement"]["project_id"] == first["procurement"]["project_id"]
    assert second["procurement"]["bom_id"] != first["procurement"]["bom_id"]
    assert "(approval 2)" in second["procurement"]["bom_name"]


def test_mapping_change_invalidates_approval(flow):
    client, approved_boq = flow
    _, boq_id = approved_boq(client, PLASTER, PAINT)
    approve(client, boq_id)
    client.put(f"/boqs/{boq_id}/mapping", json={"item_number": 1, "work_key": "plaster_int"})
    assert client.get(f"/boqs/{boq_id}/bom/approval").status_code == 404


def test_procurement_ui_is_served(flow):
    client, _ = flow
    page = client.get("/procurement/ui/")
    assert page.status_code == 200 and "static/app.js" in page.text
    assert client.get("/procurement/ui/static/app.js").status_code == 200
    assert client.get("/procurement/api/health").json() == {"status": "ok"}


def test_rejected_elements_are_left_out_of_the_boq(flow):
    """False detections can be rejected without blocking the BOQ; pending elements still block."""
    client, _ = flow
    import constructai.extraction.schemas as s  # noqa: F401
    main.extraction_agent.extract = lambda source: ExtractionResult(source_name="plan.png", elements=[
        ExtractedElement(confidence=0.9, **PLASTER), ExtractedElement(confidence=0.9, **PAINT),
        ExtractedElement(confidence=0.4, **UNKNOWN)])
    ext = client.post("/extractions", files={"file": ("d.svg", b"<svg />", "image/svg+xml")}).json()["extraction_id"]
    client.post(f"/extractions/{ext}/elements/0/review", json={"decision": "approved"})
    client.post(f"/extractions/{ext}/elements/1/review", json={"decision": "approved"})
    assert client.post(f"/extractions/{ext}/boq").status_code == 409  # element 2 still pending
    client.post(f"/extractions/{ext}/elements/2/review", json={"decision": "rejected"})
    boq = client.post(f"/extractions/{ext}/boq")
    assert boq.status_code == 200
    assert [i["description"] for i in boq.json()["items"]] == [PLASTER["description"], PAINT["description"]]



def test_approval_can_add_to_existing_project_and_history_spans_projects(flow):
    client, approved_boq = flow
    _, boq1 = approved_boq(client, PLASTER, PAINT)
    first = approve(client, boq1).json()["procurement"]
    pid, lid = first["project_id"], first["list_id"]
    # order cement-like material in the first project and approve the PO
    item = client.get(f"/procurement/api/procurement-lists/{lid}").json()["items"][0]
    v = client.post("/procurement/api/vendors", json={"name": f"History Supplier {boq1[:6]}"}).json()
    client.put(f"/procurement/api/procurement-lists/{lid}/items/{item['id']}/vendor",
               json={"vendor_id": v["id"], "agreed_rate": 410})
    po = client.post(f"/procurement/api/procurement-lists/{lid}/place-orders", json={}).json()[0]
    for action in ("submit", "approve"):
        client.post(f"/procurement/api/purchase-orders/{po['id']}/action", json={"action": action, "actor": "QA"})

    # a second drawing approved into the SAME project
    _, boq2 = approved_boq(client, PLASTER, PAINT)
    assert approve(client, boq2, procurement_project_id=999999).status_code == 422
    second = approve(client, boq2, procurement_project_id=pid).json()["procurement"]
    assert second["project_id"] == pid and second["list_id"] != lid

    # history shows the earlier order; last-rates offers it for the new list
    hist = client.get(f"/procurement/api/purchase-history?vendor_id={v['id']}").json()
    assert hist["po_count"] == 1 and hist["rows"][0]["rate"] == 410 and hist["rows"][0]["vendor_name"] == v["name"]
    assert any(r["po_id"] == po["id"] for r in client.get("/procurement/api/purchase-history").json()["rows"])
    assert client.get(f"/procurement/api/purchase-history?project_id={pid}").json()["po_count"] == 1
    assert client.get("/procurement/api/purchase-history?q=nomatch").json()["rows"] == []
    rates = client.get(f"/procurement/api/purchase-history/last-rates?list_id={second['list_id']}").json()
    new_item = next(i for i in client.get(f"/procurement/api/procurement-lists/{second['list_id']}").json()["items"]
                    if i["name"] == item["name"])
    assert rates[str(new_item["id"])]["rate"] == 410


def test_sample_house_plan_deducts_openings_end_to_end():
    """ELEMENT records keep host_element_id through the extraction agent, so the BOM deducts openings."""
    from pathlib import Path
    for store in (main.extraction_sessions, main.extraction_revisions, main.boq_sessions,
                  main.boq_extraction_revisions, main.boq_recipe_overrides, main.bom_approvals):
        store.clear()
    plan = Path(__file__).resolve().parents[1] / "data" / "sample" / "sample_house_plan.svg"
    with TestClient(main.app) as client:
        ext = client.post("/extractions", files={"file": (plan.name, plan.read_bytes(), "image/svg+xml")}).json()
        assert ext["source_name"] == "sample_house_plan.svg"
        doors = [e for e in ext["elements"] if e["element_type"] == "door"]
        assert doors and all(d["host_element_id"] for d in doors)
        for i in range(len(ext["elements"])):
            client.post(f"/extractions/{ext['extraction_id']}/elements/{i}/review", json={"decision": "approved"})
        boq = client.post(f"/extractions/{ext['extraction_id']}/boq").json()
        client.post(f"/boqs/{boq['boq_id']}/submit")
        client.post(f"/boqs/{boq['boq_id']}/approve")
        bom = client.post(f"/boqs/{boq['boq_id']}/bom").json()
    assert bom["validation"] == {"status": "ready", "errors": [], "warnings": []}
    assert len(bom["deductions"]) == 6
    assert round(sum(d["Deducted (m2)"] for d in bom["deductions"]), 2) == 9.75
