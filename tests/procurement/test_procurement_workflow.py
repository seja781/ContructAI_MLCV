"""Covers CRUD, calculations, allocation guard, vendor assignment, document generation and approvals."""
import io
import json
from pathlib import Path

SAMPLES = Path(__file__).resolve().parent
ACT = {"actor": "Reviewer One"}


def act(client, path, action, **kw):
    return client.post(f"{path}/action", json={"action": action, **ACT, **kw})


# ------------------------------------------------------------------ projects & BOM
def test_project_crud(client):
    r = client.post("/api/projects", json={"name": ""})
    assert r.status_code == 422
    p = client.post("/api/projects", json={"name": "P1", "code": "p-1"}).json()
    assert client.put(f"/api/projects/{p['id']}", json={"name": "P1 renamed"}).json()["name"] == "P1 renamed"
    assert len(client.get("/api/projects").json()) == 1
    assert client.delete(f"/api/projects/{p['id']}").status_code == 204
    assert client.get(f"/api/projects/{p['id']}").status_code == 404


def test_import_csv_and_json(client):
    p = client.post("/api/projects", json={"name": "P"}).json()
    with open(SAMPLES / "generic_bom.csv", "rb") as f:
        r = client.post(f"/api/projects/{p['id']}/boms/import", files={"file": ("generic_bom.csv", f, "text/csv")})
    assert r.status_code == 201, r.text
    bom = r.json()
    assert bom["source"] == "imported" and bom["read_only"] and bom["item_count"] == 4
    assert bom["items"][1]["quantity"] == 85.5 and bom["items"][0]["est_rate"] is None
    with open(SAMPLES / "generic_bom.json", "rb") as f:
        r = client.post(f"/api/projects/{p['id']}/boms/import", files={"file": ("generic_bom.json", f, "application/json")})
    assert r.status_code == 201 and r.json()["external_ref"] == "CAI-BOM-EXAMPLE"
    # imported BOM is read-only
    r = client.post(f"/api/boms/{bom['id']}/items", json={"name": "X", "quantity": 1, "unit": "nos"})
    assert r.status_code == 409


def test_import_amount_derives_rate_and_rejects_bad_rows(client):
    p = client.post("/api/projects", json={"name": "P"}).json()
    good = b"Material,Qty,UOM,Amount\nSand,10,cum,500\n"
    r = client.post(f"/api/projects/{p['id']}/boms/import", files={"file": ("b.csv", io.BytesIO(good), "text/csv")})
    assert r.json()["items"][0]["est_rate"] == 50
    bad = b"material,quantity,unit\nSand,-1,cum\n,5,nos\nGravel,abc,cum\n"
    r = client.post(f"/api/projects/{p['id']}/boms/import", files={"file": ("b.csv", io.BytesIO(bad), "text/csv")})
    assert r.status_code == 422
    assert len(r.json()["detail"]["row_errors"]) == 3
    assert len(client.get(f"/api/projects/{p['id']}/boms").json()) == 1  # nothing partial imported


def test_import_constructai_bom_result(client):
    """The exact JSON returned by ConstructAI's POST /boqs/{boq_id}/bom (BOMResult)."""
    raw = json.loads((SAMPLES / "constructai_bom_response.json").read_text())
    p = client.post("/api/projects", json={"name": "P"}).json()
    r = client.post(f"/api/projects/{p['id']}/boms/import",
                    files={"file": ("bom.json", io.BytesIO(json.dumps(raw).encode()), "application/json")})
    assert r.status_code == 201, r.text
    bom = r.json()
    assert bom["source_format"] == "constructai_bom" and bom["item_count"] == len(raw["lines"])
    assert bom["source_total"] == raw["total_cost"]
    first, src = bom["items"][0], raw["lines"][0]
    assert first["name"] == src["Material"] and first["unit"] == src["Unit"] and first["category"] == src["Category"]
    assert first["quantity"] == src["Order qty"]            # procure order qty (incl. wastage)
    assert first["net_quantity"] == src["Net qty"] and first["wastage_pct"] == src["Wastage %"]
    assert first["est_rate"] == src["Rate (Rs)"] and first["source_boq_items"] == src["Used in BOQ items"]
    # traceability carried into the procurement list
    pl = client.post(f"/api/projects/{p['id']}/procurement-lists", json={"name": "L", "bom_id": bom["id"]}).json()
    assert "BOQ items" in pl["items"][0]["notes"] and "wastage" in pl["items"][0]["notes"]
    # wrapped form {"boq_id", "bom"} also accepted
    r = client.post(f"/api/projects/{p['id']}/boms/import",
                    files={"file": ("w.json", io.BytesIO(json.dumps({"boq_id": "abc", "bom": raw}).encode()), "application/json")})
    assert r.json()["external_ref"] == "abc"


def test_blocked_constructai_bom_rejected(client):
    p = client.post("/api/projects", json={"name": "P"}).json()
    blocked = {"lines": [], "mapping": [], "warnings": ["x"], "total_cost": 0.0, "by_category": [],
               "validation": {"status": "blocked", "errors": ["BOQ 2 needs thickness"], "warnings": []}}
    r = client.post(f"/api/projects/{p['id']}/boms/import",
                    files={"file": ("b.json", io.BytesIO(json.dumps(blocked).encode()), "application/json")})
    assert r.status_code == 422 and "blocked" in r.json()["detail"]["message"]


def test_manual_bom_marked_as_test_data(setup, client):
    assert setup["bom"]["source"] == "manual_test" and not setup["bom"]["read_only"]


# ------------------------------------------------------------------ procurement list
def test_procurement_list_edits_do_not_touch_bom(setup, client):
    lid, c = setup["list"]["id"], setup["cement"]
    assert setup["list"]["est_total"] == 1500  # 100*10 + 5*100
    r = client.put(f"/api/procurement-lists/{lid}/items/{c['id']}",
                   json={"name": "Cement", "quantity": 150, "unit": "bag", "est_rate": 12})
    assert r.json()["est_amount"] == 1800
    client.post(f"/api/procurement-lists/{lid}/items", json={"name": "Sand", "quantity": 2, "unit": "cum", "est_rate": 5})
    pl = client.get(f"/api/procurement-lists/{lid}").json()
    assert pl["est_total"] == 1800 + 500 + 10 and pl["item_count"] == 3
    bom = client.get(f"/api/boms/{setup['bom']['id']}").json()
    assert bom["items"][0]["quantity"] == 100 and bom["est_total"] == 1500
    bad = client.post(f"/api/procurement-lists/{lid}/items", json={"name": "X", "quantity": 0, "unit": "nos"})
    assert bad.status_code == 422
    sand = [i for i in pl["items"] if i["name"] == "Sand"][0]
    assert client.delete(f"/api/procurement-lists/{lid}/items/{sand['id']}").status_code == 204


# ------------------------------------------------------------------ vendors & quotes
def test_vendor_crud_search_deactivate(setup, client):
    v = setup["v1"]
    assert client.post("/api/vendors", json={"name": "supplier a"}).status_code == 409
    assert client.post("/api/vendors", json={"name": "Bad", "email": "nope"}).status_code == 422
    upd = client.put(f"/api/vendors/{v['id']}", json={"name": "Supplier A", "materials_supplied": "cement, sand"}).json()
    assert upd["materials_supplied"] == "cement, sand"
    assert [x["name"] for x in client.get("/api/vendors?q=sand").json()] == ["Supplier A"]
    assert {x["name"] for x in client.get("/api/vendors?kind=contractor").json()} == {"Supplier B", "Contractor C"}
    client.post(f"/api/vendors/{v['id']}/deactivate")
    assert all(x["id"] != v["id"] for x in client.get("/api/vendors?include_inactive=false").json())


def test_quotation_comparison(setup, client):
    c = setup["cement"]
    q1 = client.post("/api/quotations", json={"vendor_id": setup["v1"]["id"], "procurement_item_id": c["id"],
                                              "material_name": "Cement", "unit": "bag", "rate": 11,
                                              "available_qty": 50}).json()
    q2 = client.post("/api/quotations", json={"vendor_id": setup["v2"]["id"], "procurement_item_id": c["id"],
                                              "material_name": "Cement", "unit": "bag", "rate": 9.5}).json()
    assert client.post("/api/quotations", json={"vendor_id": setup["v1"]["id"], "procurement_item_id": c["id"],
                                                "material_name": "Cement", "unit": "kg", "rate": 1}).status_code == 422
    cmp = client.get(f"/api/procurement-lists/{setup['list']['id']}/comparison").json()
    row = [r for r in cmp if r["item"]["id"] == c["id"]][0]
    assert row["lowest_quotation_id"] == q2["id"] and row["quote_count"] == 2
    by = {q["id"]: q for q in row["quotations"]}
    assert by[q1["id"]]["covers_required_qty"] is False
    assert by[q2["id"]]["variance_vs_estimate_pct"] == -5.0
    assert by[q2["id"]]["amount_for_required_qty"] == 950
    # deactivated vendor no longer counts as lowest
    client.post(f"/api/vendors/{setup['v2']['id']}/deactivate")
    row = [r for r in client.get(f"/api/procurement-lists/{setup['list']['id']}/comparison").json()
           if r["item"]["id"] == c["id"]][0]
    assert row["lowest_quotation_id"] == q1["id"]


# ------------------------------------------------------------------ POs
def _gen(client, setup, assignments, **kw):
    return client.post("/api/purchase-orders/generate", json={"list_id": setup["list"]["id"],
                                                               "assignments": assignments, **kw})


def test_po_per_vendor_totals_and_terms(setup, client):
    c, s = setup["cement"], setup["steel"]
    q = client.post("/api/quotations", json={"vendor_id": setup["v2"]["id"], "procurement_item_id": s["id"],
                                             "material_name": "Steel", "rate": 95, "payment_terms": "Advance 20%"}).json()
    r = _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 60, "rate": 10.5},
                             {"procurement_item_id": s["id"], "vendor_id": setup["v2"]["id"], "quantity": 5, "quotation_id": q["id"]}],
             tax_pct=18)
    assert r.status_code == 201, r.text
    pos = {p["vendor"]["name"]: p for p in r.json()}
    assert len(pos) == 2
    a, b = pos["Supplier A"], pos["Supplier B"]
    assert a["subtotal"] == 630 and a["tax"] == 113.4 and a["total"] == 743.4
    assert a["payment_terms"] == "30 days" and a["delivery_address"] == "Site Gate 1"
    assert b["lines"][0]["rate"] == 95 and b["payment_terms"] == "Advance 20%"
    assert a["po_number"] == "PO-TT1-001" and b["po_number"] == "PO-TT1-002"
    assert a["status"] == "draft" and a["editable"]


def test_po_requires_rate_and_active_supplier(setup, client):
    c = setup["cement"]
    assert _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 1}]).status_code == 422
    assert _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["c1"]["id"], "quantity": 1, "rate": 1}]).status_code == 422
    client.post(f"/api/vendors/{setup['v1']['id']}/deactivate")
    assert _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 1, "rate": 1}]).status_code == 422


def test_allocation_guard_across_pos_and_packages(setup, client):
    c, lid = setup["cement"], setup["list"]["id"]
    assert _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 70, "rate": 10}]).status_code == 201
    r = _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v2"]["id"], "quantity": 40, "rate": 10}])
    assert r.status_code == 409 and r.json()["detail"]["over_allocated"][0]["available"] == 30
    r = client.post("/api/packages", json={"list_id": lid, "name": "Pkg", "lines": [{"procurement_item_id": c["id"], "quantity": 31}]})
    assert r.status_code == 409
    r = client.post("/api/packages", json={"list_id": lid, "name": "Pkg", "lines": [{"procurement_item_id": c["id"], "quantity": 30}]})
    assert r.status_code == 201
    item = [i for i in client.get(f"/api/procurement-lists/{lid}").json()["items"] if i["id"] == c["id"]][0]
    assert item["allocated_po"] == 70 and item["allocated_package"] == 30 and item["remaining"] == 0
    # cannot shrink below allocation, cannot delete allocated item
    assert client.put(f"/api/procurement-lists/{lid}/items/{c['id']}",
                      json={"name": "Cement", "quantity": 90, "unit": "bag"}).status_code == 409
    assert client.delete(f"/api/procurement-lists/{lid}/items/{c['id']}").status_code == 409
    # same assignment split into two lines in one request is summed
    s = setup["steel"]
    r = _gen(client, setup, [{"procurement_item_id": s["id"], "vendor_id": setup["v1"]["id"], "quantity": 3, "rate": 1},
                             {"procurement_item_id": s["id"], "vendor_id": setup["v2"]["id"], "quantity": 3, "rate": 1}])
    assert r.status_code == 409


def test_po_edit_and_rejection_releases_allocation(setup, client):
    c = setup["cement"]
    po = _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 100, "rate": 10}]).json()[0]
    path = f"/api/purchase-orders/{po['id']}"
    upd = client.put(path, json={"tax_pct": 5, "lines": [{"procurement_item_id": c["id"], "quantity": 80, "rate": 12}]})
    assert upd.status_code == 200 and upd.json()["total"] == 1008.0
    assert act(client, path, "submit").json()["status"] == "pending_review"
    assert client.put(path, json={"lines": [{"procurement_item_id": c["id"], "quantity": 1, "rate": 1}]}).status_code == 409
    assert act(client, path, "reject").status_code == 422  # comment required
    assert act(client, path, "reject", comment="Rate too high").json()["status"] == "rejected"
    # rejected PO frees its quantity
    assert _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v2"]["id"], "quantity": 100, "rate": 9}]).status_code == 201


# ------------------------------------------------------------------ workflow
def test_full_approval_and_revision(setup, client):
    c = setup["cement"]
    po = _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 50, "rate": 10}]).json()[0]
    path = f"/api/purchase-orders/{po['id']}"
    assert act(client, path, "approve").status_code == 409  # must be submitted first
    assert client.post(f"{path}/action", json={"action": "submit", "actor": ""}).status_code == 422  # actor required
    act(client, path, "submit")
    assert act(client, path, "return", comment="Fix delivery date").json()["status"] == "returned"
    assert client.put(path, json={"delivery_date": "2026-12-01",
                                  "lines": [{"procurement_item_id": c["id"], "quantity": 50, "rate": 10}]}).status_code == 200
    act(client, path, "submit")
    assert act(client, path, "approve").json()["status"] == "approved"
    assert act(client, path, "finalize").status_code == 422  # explicit confirm required
    assert act(client, path, "finalize", confirm=True).json()["status"] == "finalized"
    # finalized cannot be edited; revision creates a new draft and preserves the old one
    assert client.put(path, json={"lines": [{"procurement_item_id": c["id"], "quantity": 1, "rate": 1}]}).status_code == 409
    rev = client.post(f"{path}/revision", json={"actor": "Reviewer One", "comment": "Qty change"}).json()
    assert rev["revision"] == 1 and rev["status"] == "draft" and rev["po_number"] == po["po_number"]
    old = client.get(path).json()
    assert old["status"] == "finalized" and old["superseded_by_id"] == rev["id"] and not old["editable"]
    assert act(client, path, "submit").status_code == 409
    # allocation counted once (old superseded)
    item = [i for i in client.get(f"/api/procurement-lists/{setup['list']['id']}").json()["items"] if i["id"] == c["id"]][0]
    assert item["allocated_po"] == 50
    hist = client.get(f"/api/projects/{setup['project']['id']}/history?doc_type=po").json()
    actions = [h["action"] for h in hist]
    for a in ("created", "submit", "return", "edited", "approve", "finalize", "superseded", "revision_created"):
        assert a in actions
    assert any(h["comment"] == "Fix delivery date" for h in hist)
    assert len(client.get(f"/api/projects/{setup['project']['id']}/purchase-orders?include_superseded=true").json()) == 2


# ------------------------------------------------------------------ packages
def test_package_supply_and_execute(setup, client):
    lid, c, s = setup["list"]["id"], setup["cement"], setup["steel"]
    body = {"list_id": lid, "name": "RCC Works", "arrangement": "supply_and_execute", "execution_amount": 1000,
            "tax_pct": 10, "scope_of_work": "Supply and lay RCC",
            "lines": [{"procurement_item_id": c["id"], "quantity": 40, "rate": 10},
                      {"procurement_item_id": s["id"], "quantity": 2, "rate": 100}],
            "milestones": [{"name": "Foundation", "payment_pct": 40}, {"name": "Completion", "payment_pct": 60}]}
    pk = client.post("/api/packages", json=body).json()
    assert pk["material_value"] == 600 and pk["subtotal"] == 1600 and pk["total"] == 1760
    assert pk["milestone_pct_total"] == 100 and pk["package_code"] == "PKG-TT1-001"
    path = f"/api/packages/{pk['id']}"
    assert act(client, path, "submit").status_code == 422  # supply_and_execute needs contractor
    # supplier-only party cannot execute
    assert client.put(path, json={**body, "contractor_id": setup["v1"]["id"]}).status_code == 422
    assert client.put(path, json={**body, "milestones": [{"name": "x", "payment_pct": 101}]}).status_code == 422
    dup = {**body, "lines": [{"procurement_item_id": c["id"], "quantity": 1}, {"procurement_item_id": c["id"], "quantity": 1}]}
    assert client.put(path, json=dup).status_code == 422
    assert client.put(path, json={**body, "start_date": "2026-12-10", "completion_date": "2026-12-01"}).status_code == 422
    upd = client.put(path, json={**body, "contractor_id": setup["c1"]["id"]}).json()
    assert upd["contractor"]["name"] == "Contractor C"
    assert act(client, path, "submit").json()["status"] == "pending_review"


# ------------------------------------------------------------------ contracts & PDF
def _templates(client, kind):
    return client.get(f"/api/contract-templates?kind={kind}").json()


def test_supply_contract_from_po_and_pdf(setup, client):
    c = setup["cement"]
    po = _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 50, "rate": 10}],
              tax_pct=18).json()[0]
    tpl = _templates(client, "supply")[0]
    # wrong template kind rejected
    ctpl = _templates(client, "contractor")[0]
    assert client.post("/api/contracts/generate", json={"template_id": ctpl["id"], "po_id": po["id"]}).status_code == 422
    ct = client.post("/api/contracts/generate", json={"template_id": tpl["id"], "po_id": po["id"]}).json()
    assert ct["kind"] == "supply" and ct["party_name"] == "Supplier A"
    assert "Test Buyer Pvt Ltd" in ct["body"] and "Supplier A" in ct["body"] and "590.00" in ct["body"]
    assert "[[MATERIAL_SCHEDULE]]" in ct["body"] and ct["schedule"][0]["amount"] == 500
    assert client.post("/api/contracts/generate", json={"template_id": tpl["id"], "po_id": po["id"]}).status_code == 409
    path = f"/api/contracts/{ct['id']}"
    edited = client.put(path, json={"title": "Edited title", "body": ct["body"] + "\n# Extra\nCustom clause."}).json()
    assert edited["title"] == "Edited title" and "Custom clause." in edited["body"]
    act(client, path, "submit"); act(client, path, "approve")
    assert act(client, path, "finalize", confirm=True).status_code == 409  # source PO not approved yet
    ppath = f"/api/purchase-orders/{po['id']}"
    act(client, ppath, "submit"); act(client, ppath, "approve")
    assert act(client, path, "finalize", confirm=True).json()["status"] == "finalized"
    for url in (f"{path}/pdf", f"{ppath}/pdf"):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:4] == b"%PDF"


def test_contractor_agreement_template_edit_and_regenerate(setup, client):
    lid, c = setup["list"]["id"], setup["cement"]
    pk = client.post("/api/packages", json={"list_id": lid, "name": "Masonry", "contractor_id": setup["c1"]["id"],
                                            "arrangement": "supply_and_execute", "execution_amount": 500,
                                            "lines": [{"procurement_item_id": c["id"], "quantity": 10, "rate": 10}],
                                            "milestones": [{"name": "Half way", "payment_pct": 50}]}).json()
    tpl = _templates(client, "contractor")[0]
    # templates are configurable
    new_body = tpl["body"] + "\n# Custom\nPackage {{ package.code }} for {{ contractor.name }}."
    assert client.put(f"/api/contract-templates/{tpl['id']}", json={**tpl, "body": new_body}).status_code == 200
    assert client.put(f"/api/contract-templates/{tpl['id']}", json={**tpl, "body": "{% if %}"}).status_code == 422
    ct = client.post("/api/contracts/generate", json={"template_id": tpl["id"], "package_id": pk["id"]}).json()
    assert "Package PKG-TT1-001 for Contractor C." in ct["body"] and "Half way" in ct["body"]
    # bad field in template surfaces a clear error
    bad = client.post("/api/contract-templates", json={"name": "Bad", "kind": "contractor", "body": "{{ nope.x }}"}).json()
    client.post(f"/api/contracts/{ct['id']}/action", json={"action": "submit", **ACT})
    client.post(f"/api/contracts/{ct['id']}/action", json={"action": "reject", "comment": "redo", **ACT})
    assert client.post("/api/contracts/generate", json={"template_id": bad["id"], "package_id": pk["id"]}).status_code == 422
    # package pdf
    r = client.get(f"/api/packages/{pk['id']}/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


def test_regenerate_follows_latest_source_revision(setup, client):
    c = setup["cement"]
    po = _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 10, "rate": 10}]).json()[0]
    ppath = f"/api/purchase-orders/{po['id']}"
    act(client, ppath, "submit"); act(client, ppath, "approve")
    tpl = _templates(client, "supply")[0]
    ct = client.post("/api/contracts/generate", json={"template_id": tpl["id"], "po_id": po["id"]}).json()
    rev = client.post(f"{ppath}/revision", json=ACT).json()
    client.put(f"/api/purchase-orders/{rev['id']}", json={"lines": [{"procurement_item_id": c["id"], "quantity": 20, "rate": 10}]})
    regen = client.post(f"/api/contracts/{ct['id']}/regenerate", json=ACT).json()
    assert regen["po_id"] == rev["id"] and regen["schedule"][0]["quantity"] == 20


def test_summary(setup, client):
    c = setup["cement"]
    _gen(client, setup, [{"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 10, "rate": 10}])
    s = client.get(f"/api/projects/{setup['project']['id']}/summary").json()
    assert s["po"]["count"] == 1 and s["po"]["value"] == 100 and s["boms"]["manual_test"] == 1


# ------------------------------------------------------------------ vendor per material + place orders
def test_choose_vendor_on_materials_and_place_orders(setup, client):
    lid, c, s = setup["list"]["id"], setup["cement"], setup["steel"]
    base = f"/api/procurement-lists/{lid}/items"
    # contractor-only party cannot supply; cleared vendor clears rate
    assert client.put(f"{base}/{c['id']}/vendor", json={"vendor_id": setup["c1"]["id"], "agreed_rate": 5}).status_code == 422
    r = client.put(f"{base}/{c['id']}/vendor", json={"vendor_id": setup["v1"]["id"], "agreed_rate": 10.5}).json()
    assert r["vendor_id"] == setup["v1"]["id"] and r["agreed_rate"] == 10.5 and r["agreed_amount"] == 1050
    # choosing a quotation fills vendor and rate from the quote
    q = client.post("/api/quotations", json={"vendor_id": setup["v2"]["id"], "procurement_item_id": s["id"],
                                             "material_name": "Steel", "rate": 95}).json()
    r = client.put(f"{base}/{s['id']}/vendor", json={"quotation_id": q["id"]}).json()
    assert r["vendor_id"] == setup["v2"]["id"] and r["agreed_rate"] == 95 and r["quotation_id"] == q["id"]

    pos = client.post(f"/api/procurement-lists/{lid}/place-orders", json={"tax_pct": 18, "actor": "QA"})
    assert pos.status_code == 201, pos.text
    by_vendor = {p["vendor"]["name"]: p for p in pos.json()}
    assert set(by_vendor) == {"Supplier A", "Supplier B"}
    assert by_vendor["Supplier A"]["lines"][0]["quantity"] == 100 and by_vendor["Supplier A"]["subtotal"] == 1050
    assert by_vendor["Supplier B"]["lines"][0]["quotation_id"] == q["id"]
    # everything ordered: nothing left to place
    assert client.post(f"/api/procurement-lists/{lid}/place-orders", json={}).status_code == 422
    items = {i["name"]: i for i in client.get(f"/api/procurement-lists/{lid}").json()["items"]}
    assert items["Cement"]["remaining"] == 0 and items["Steel"]["remaining"] == 0


def test_place_orders_requires_rate_and_only_orders_remaining(setup, client):
    lid, c = setup["list"]["id"], setup["cement"]
    client.post("/api/purchase-orders/generate", json={"list_id": lid, "assignments": [
        {"procurement_item_id": c["id"], "vendor_id": setup["v1"]["id"], "quantity": 40, "rate": 10}]})
    client.put(f"/api/procurement-lists/{lid}/items/{c['id']}/vendor", json={"vendor_id": setup["v2"]["id"]})
    r = client.post(f"/api/procurement-lists/{lid}/place-orders", json={})
    assert r.status_code == 422 and "agreed rate" in r.json()["detail"]["message"]
    client.put(f"/api/procurement-lists/{lid}/items/{c['id']}/vendor", json={"vendor_id": setup["v2"]["id"], "agreed_rate": 9})
    po = client.post(f"/api/procurement-lists/{lid}/place-orders", json={}).json()[0]
    assert po["lines"][0]["quantity"] == 60  # only the unordered balance


def test_old_database_gets_new_columns(tmp_path):
    """An existing procurement.db from the previous version is upgraded in place, keeping its rows."""
    import sqlite3
    from sqlalchemy import create_engine, inspect
    from constructai.procurement import app as papp
    from constructai.procurement.db import Base
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE procurement_items (id INTEGER PRIMARY KEY, list_id INTEGER, name VARCHAR(300), "
                "quantity FLOAT, unit VARCHAR(30))")
    con.execute("INSERT INTO procurement_items (list_id, name, quantity, unit) VALUES (1, 'Cement', 10, 'bag')")
    con.commit(); con.close()
    eng = create_engine(f"sqlite:///{db}")
    old_engine = papp.engine
    papp.engine = eng
    try:
        papp._add_missing_columns()
    finally:
        papp.engine = old_engine
    cols = {c["name"] for c in inspect(eng).get_columns("procurement_items")}
    assert {"vendor_id", "agreed_rate", "quotation_id", "est_rate"} <= cols
    assert sqlite3.connect(db).execute("SELECT name FROM procurement_items").fetchone()[0] == "Cement"
