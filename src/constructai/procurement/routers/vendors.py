"""Vendor directory, quotations and quotation comparison."""
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .. import models as m
from ..db import get_db
from ..schemas import VendorIn, QuotationIn, ser_vendor, ser_quote
from ..services import err, get_or_404, r2, item_allocation

router = APIRouter(prefix="/api", tags=["vendors"])


@router.get("/vendors")
def list_vendors(q: str | None = None, kind: str | None = None, include_inactive: bool = True,
                 db: Session = Depends(get_db)):
    stmt = select(m.Vendor).order_by(m.Vendor.name)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(m.Vendor.name.ilike(like), m.Vendor.materials_supplied.ilike(like),
                              m.Vendor.trades.ilike(like), m.Vendor.contact_person.ilike(like),
                              m.Vendor.email.ilike(like), m.Vendor.phone.ilike(like)))
    if kind == "supplier":
        stmt = stmt.where(m.Vendor.kind.in_(["supplier", "both"]))
    elif kind == "contractor":
        stmt = stmt.where(m.Vendor.kind.in_(["contractor", "both"]))
    if not include_inactive:
        stmt = stmt.where(m.Vendor.active.is_(True))
    return [ser_vendor(v) for v in db.scalars(stmt)]


@router.post("/vendors", status_code=201)
def create_vendor(body: VendorIn, db: Session = Depends(get_db)):
    if db.scalar(select(m.Vendor.id).where(m.Vendor.name.ilike(body.name))):
        err(409, f"A vendor named '{body.name}' already exists.")
    v = m.Vendor(**body.model_dump())
    db.add(v)
    db.commit()
    return ser_vendor(v)


@router.get("/vendors/{vid}")
def get_vendor(vid: int, db: Session = Depends(get_db)):
    v = get_or_404(db, m.Vendor, vid, "Vendor")
    return ser_vendor(v) | {"quotations": [ser_quote(q) for q in v.quotations]}


@router.put("/vendors/{vid}")
def update_vendor(vid: int, body: VendorIn, db: Session = Depends(get_db)):
    v = get_or_404(db, m.Vendor, vid, "Vendor")
    dup = db.scalar(select(m.Vendor.id).where(m.Vendor.name.ilike(body.name), m.Vendor.id != vid))
    if dup:
        err(409, f"Another vendor named '{body.name}' already exists.")
    for k, val in body.model_dump().items():
        setattr(v, k, val)
    db.commit()
    return ser_vendor(v)


@router.post("/vendors/{vid}/deactivate")
def deactivate_vendor(vid: int, db: Session = Depends(get_db)):
    v = get_or_404(db, m.Vendor, vid, "Vendor")
    v.active = False
    db.commit()
    return ser_vendor(v)


@router.post("/vendors/{vid}/activate")
def activate_vendor(vid: int, db: Session = Depends(get_db)):
    v = get_or_404(db, m.Vendor, vid, "Vendor")
    v.active = True
    db.commit()
    return ser_vendor(v)


# ------------------------------------------------------------------ quotations
@router.get("/quotations")
def list_quotes(vendor_id: int | None = None, procurement_item_id: int | None = None, list_id: int | None = None,
                db: Session = Depends(get_db)):
    stmt = select(m.Quotation).order_by(m.Quotation.material_name, m.Quotation.rate)
    if vendor_id:
        stmt = stmt.where(m.Quotation.vendor_id == vendor_id)
    if procurement_item_id:
        stmt = stmt.where(m.Quotation.procurement_item_id == procurement_item_id)
    if list_id:
        stmt = stmt.join(m.ProcurementItem, m.Quotation.procurement_item_id == m.ProcurementItem.id).where(
            m.ProcurementItem.list_id == list_id)
    return [ser_quote(q) for q in db.scalars(stmt)]


def _check_quote(db, body: QuotationIn):
    get_or_404(db, m.Vendor, body.vendor_id, "Vendor")
    if body.procurement_item_id:
        it = get_or_404(db, m.ProcurementItem, body.procurement_item_id, "Procurement item")
        if body.unit and body.unit.strip().lower() != it.unit.strip().lower():
            err(422, f"Quotation unit '{body.unit}' does not match material unit '{it.unit}'. "
                     f"Convert the rate to {it.unit} before saving.")


@router.post("/quotations", status_code=201)
def create_quote(body: QuotationIn, db: Session = Depends(get_db)):
    _check_quote(db, body)
    q = m.Quotation(**body.model_dump())
    db.add(q)
    db.commit()
    db.refresh(q)
    return ser_quote(q)


@router.put("/quotations/{qid}")
def update_quote(qid: int, body: QuotationIn, db: Session = Depends(get_db)):
    q = get_or_404(db, m.Quotation, qid, "Quotation")
    _check_quote(db, body)
    for k, v in body.model_dump().items():
        setattr(q, k, v)
    db.commit()
    db.refresh(q)
    return ser_quote(q)


@router.delete("/quotations/{qid}", status_code=204)
def delete_quote(qid: int, db: Session = Depends(get_db)):
    q = get_or_404(db, m.Quotation, qid, "Quotation")
    db.query(m.POLine).filter(m.POLine.quotation_id == qid).update({"quotation_id": None})
    db.delete(q)
    db.commit()


@router.get("/procurement-lists/{lid}/comparison")
def comparison(lid: int, db: Session = Depends(get_db)):
    """Per material: all linked quotations, lowest valid rate, variance vs estimate."""
    pl = get_or_404(db, m.ProcurementList, lid, "Procurement list")
    out = []
    for it in pl.items:
        quotes = db.scalars(select(m.Quotation).where(m.Quotation.procurement_item_id == it.id)
                            .order_by(m.Quotation.rate)).all()
        valid = [q for q in quotes if q.vendor.active and not (q.valid_until and q.valid_until < date.today())]
        best = valid[0] if valid else None
        rows = []
        for q in quotes:
            sq = ser_quote(q)
            sq["amount_for_required_qty"] = r2(q.rate * it.quantity)
            sq["variance_vs_estimate_pct"] = (r2((q.rate - it.est_rate) / it.est_rate * 100)
                                              if it.est_rate else None)
            sq["covers_required_qty"] = None if q.available_qty is None else q.available_qty >= it.quantity
            sq["is_lowest"] = best is not None and q.id == best.id
            rows.append(sq)
        out.append({"item": {"id": it.id, "name": it.name, "specification": it.specification, "unit": it.unit,
                             "quantity": it.quantity, "est_rate": it.est_rate, **item_allocation(db, it)},
                    "quotations": rows, "lowest_quotation_id": best.id if best else None,
                    "quote_count": len(quotes)})
    return out
