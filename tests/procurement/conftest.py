import pytest
from fastapi.testclient import TestClient

from constructai.procurement.app import create_procurement_app, init_db
from constructai.procurement.db import Base, engine

app = create_procurement_app()


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def setup(client):
    """Project + manual BOM + procurement list + 2 suppliers + 1 contractor. All values are test inputs."""
    p = client.post("/api/projects", json={"name": "Test Tower", "code": "TT1", "buyer_name": "Test Buyer Pvt Ltd",
                                            "default_delivery_address": "Site Gate 1"}).json()
    bom = client.post(f"/api/projects/{p['id']}/boms/manual", json={"name": "Test BOM", "items": [
        {"name": "Cement", "quantity": 100, "unit": "bag", "est_rate": 10},
        {"name": "Steel", "specification": "Fe500D", "quantity": 5, "unit": "MT", "est_rate": 100},
    ]}).json()
    pl = client.post(f"/api/projects/{p['id']}/procurement-lists", json={"name": "Draft 1", "bom_id": bom["id"]}).json()
    v1 = client.post("/api/vendors", json={"name": "Supplier A", "kind": "supplier", "payment_terms": "30 days"}).json()
    v2 = client.post("/api/vendors", json={"name": "Supplier B", "kind": "both"}).json()
    c1 = client.post("/api/vendors", json={"name": "Contractor C", "kind": "contractor"}).json()
    items = {i["name"]: i for i in pl["items"]}
    return {"project": p, "bom": bom, "list": pl, "v1": v1, "v2": v2, "c1": c1,
            "cement": items["Cement"], "steel": items["Steel"]}
