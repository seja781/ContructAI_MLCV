"""Editable procurement lists (copied from a BOM — the source BOM is never modified)."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..db import get_db
from ..schemas import ProcListIn, ItemIn, VendorChoiceIn, PlaceOrdersIn, Assignment, GeneratePOsIn, ser_proc_item
from .purchase_orders import generate_pos
from ..services import err, get_or_404, item_allocation, allocated_qty, r2, EPS

router = APIRouter(prefix="/api", tags=["procurement"])


def ser_list(db: Session, pl: m.ProcurementList, items=True) -> dict:
    bom = db.get(m.Bom, pl.bom_id) if pl.bom_id else None
    out = {"id": pl.id, "project_id": pl.project_id, "bom_id": pl.bom_id, "name": pl.name,
           "bom_name": bom.name if bom else None, "bom_source": bom.source if bom else None,
           "created_at": pl.created_at.isoformat(), "item_count": len(pl.items),
           "est_total": r2(sum(i.quantity * (i.est_rate or 0) for i in pl.items)),
           "unpriced_items": sum(1 for i in pl.items if i.est_rate is None)}
    if items:
        out["items"] = [ser_proc_item(i, item_allocation(db, i)) for i in pl.items]
    return out


def _trace_note(i: m.BomItem) -> str | None:
    """Carry ConstructAI traceability into the procurement item without inventing anything."""
    parts = []
    if i.net_quantity is not None:
        parts.append(f"Net {i.net_quantity:g} {i.unit}" + (f" + {i.wastage_pct:g}% wastage" if i.wastage_pct is not None else ""))
    if i.source_boq_items:
        parts.append(f"BOQ items {i.source_boq_items}")
    return "; ".join(parts) or None


@router.get("/projects/{pid}/procurement-lists")
def list_lists(pid: int, db: Session = Depends(get_db)):
    get_or_404(db, m.Project, pid, "Project")
    rows = db.scalars(select(m.ProcurementList).where(m.ProcurementList.project_id == pid).order_by(m.ProcurementList.id))
    return [ser_list(db, pl, items=False) for pl in rows]


@router.post("/projects/{pid}/procurement-lists", status_code=201)
def create_list(pid: int, body: ProcListIn, db: Session = Depends(get_db)):
    get_or_404(db, m.Project, pid, "Project")
    pl = m.ProcurementList(project_id=pid, name=body.name, bom_id=body.bom_id)
    if body.bom_id:
        bom = get_or_404(db, m.Bom, body.bom_id, "BOM")
        if bom.project_id != pid:
            err(422, "BOM belongs to a different project")
        pl.items = [m.ProcurementItem(bom_item_id=i.id, item_code=i.item_code, name=i.name,
                                      specification=i.specification, category=i.category, quantity=i.quantity,
                                      unit=i.unit, est_rate=i.est_rate, notes=_trace_note(i)) for i in bom.items]
    db.add(pl)
    db.commit()
    return ser_list(db, pl)


@router.get("/procurement-lists/{lid}")
def get_list(lid: int, db: Session = Depends(get_db)):
    return ser_list(db, get_or_404(db, m.ProcurementList, lid, "Procurement list"))


@router.put("/procurement-lists/{lid}")
def rename_list(lid: int, body: ProcListIn, db: Session = Depends(get_db)):
    pl = get_or_404(db, m.ProcurementList, lid, "Procurement list")
    pl.name = body.name
    db.commit()
    return ser_list(db, pl)


@router.delete("/procurement-lists/{lid}", status_code=204)
def delete_list(lid: int, db: Session = Depends(get_db)):
    pl = get_or_404(db, m.ProcurementList, lid, "Procurement list")
    has_docs = db.scalar(select(m.PurchaseOrder.id).where(m.PurchaseOrder.list_id == lid).limit(1)) or \
        db.scalar(select(m.Package.id).where(m.Package.list_id == lid).limit(1))
    if has_docs:
        err(409, "This list has purchase orders or packages. Delete those drafts first.")
    db.delete(pl)
    db.commit()


@router.post("/procurement-lists/{lid}/items", status_code=201)
def add_item(lid: int, body: ItemIn, db: Session = Depends(get_db)):
    get_or_404(db, m.ProcurementList, lid, "Procurement list")
    it = m.ProcurementItem(list_id=lid, **body.model_dump())
    db.add(it)
    db.commit()
    return ser_proc_item(it, item_allocation(db, it))


@router.put("/procurement-lists/{lid}/items/{iid}")
def update_item(lid: int, iid: int, body: ItemIn, db: Session = Depends(get_db)):
    it = get_or_404(db, m.ProcurementItem, iid, "Procurement item")
    if it.list_id != lid:
        err(404, "Item not in this list")
    already = allocated_qty(db, iid)["total"]
    if body.quantity + EPS < already:
        err(409, f"Quantity cannot be reduced below the {r2(already)} {it.unit} already allocated to "
                 f"purchase orders/packages. Revise those documents first.")
    for k, v in body.model_dump().items():
        setattr(it, k, v)
    db.commit()
    return ser_proc_item(it, item_allocation(db, it))


@router.delete("/procurement-lists/{lid}/items/{iid}", status_code=204)
def delete_item(lid: int, iid: int, db: Session = Depends(get_db)):
    it = get_or_404(db, m.ProcurementItem, iid, "Procurement item")
    if it.list_id != lid:
        err(404, "Item not in this list")
    used = db.scalar(select(m.POLine.id).where(m.POLine.procurement_item_id == iid).limit(1)) or \
        db.scalar(select(m.PackageLine.id).where(m.PackageLine.procurement_item_id == iid).limit(1))
    if used:
        err(409, "This material is used in a purchase order or package and cannot be removed.")
    db.query(m.Quotation).filter(m.Quotation.procurement_item_id == iid).update({"procurement_item_id": None})
    db.delete(it)
    db.commit()


# ------------------------------------------------------------------ vendor choice per material + place orders
@router.put("/procurement-lists/{lid}/items/{iid}/vendor")
def set_item_vendor(lid: int, iid: int, body: VendorChoiceIn, db: Session = Depends(get_db)):
    """Choose the vendor and agreed rate for one material. Clearing the vendor clears the rate."""
    it = get_or_404(db, m.ProcurementItem, iid, "Procurement item")
    if it.list_id != lid:
        err(404, "Item not in this list")
    vendor_id, rate, quote_id = body.vendor_id, body.agreed_rate, body.quotation_id
    if quote_id:
        q = get_or_404(db, m.Quotation, quote_id, "Quotation")
        if q.procurement_item_id not in (None, iid):
            err(422, "This quotation is for a different material.")
        if vendor_id and vendor_id != q.vendor_id:
            err(422, "This quotation belongs to a different vendor.")
        vendor_id = q.vendor_id
        if rate is None:
            rate = q.rate
    if vendor_id:
        v = get_or_404(db, m.Vendor, vendor_id, "Vendor")
        if not v.active:
            err(422, f"Vendor '{v.name}' is deactivated.")
        if v.kind not in ("supplier", "both"):
            err(422, f"'{v.name}' is registered as a contractor only, not a material supplier.")
    else:
        rate, quote_id = None, None
    it.vendor_id, it.agreed_rate, it.quotation_id = vendor_id, rate, quote_id
    db.commit()
    return ser_proc_item(it, item_allocation(db, it))


@router.post("/procurement-lists/{lid}/place-orders", status_code=201)
def place_orders(lid: int, body: PlaceOrdersIn, db: Session = Depends(get_db)):
    """Create one draft purchase order per vendor for every material that has a vendor and agreed rate,
    for the quantity not yet ordered. Materials without a vendor are left for later."""
    pl = get_or_404(db, m.ProcurementList, lid, "Procurement list")
    assignments, missing_rate = [], []
    for it in pl.items:
        if not it.vendor_id:
            continue
        remaining = it.quantity - allocated_qty(db, it.id)["total"]
        if remaining <= EPS:
            continue
        if it.agreed_rate is None:
            missing_rate.append(it.name)
            continue
        assignments.append(Assignment(procurement_item_id=it.id, vendor_id=it.vendor_id, quantity=remaining,
                                      rate=it.agreed_rate, quotation_id=it.quotation_id))
    if missing_rate:
        err(422, f"Enter the agreed rate for: {', '.join(missing_rate)}")
    if not assignments:
        err(422, "No materials are ready to order. Choose a vendor and agreed rate for at least one material "
                 "that still has quantity to order.")
    return generate_pos(GeneratePOsIn(list_id=lid, assignments=assignments, delivery_address=body.delivery_address,
                                      delivery_date=body.delivery_date, tax_pct=body.tax_pct, actor=body.actor), db)
