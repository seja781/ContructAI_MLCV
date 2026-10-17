"""Purchase orders: one PO per vendor, allocation-guarded, workflow + revisions."""
from collections import defaultdict

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..db import get_db
from ..pdf import po_pdf
from ..schemas import GeneratePOsIn, POUpdate, ActionIn, RevisionIn, ser_po
from ..services import (err, get_or_404, check_allocation, next_number, ensure_editable, transition,
                        create_revision, log_event)

router = APIRouter(prefix="/api", tags=["purchase-orders"])


@router.get("/projects/{pid}/purchase-orders")
def list_pos(pid: int, include_superseded: bool = False, db: Session = Depends(get_db)):
    stmt = select(m.PurchaseOrder).where(m.PurchaseOrder.project_id == pid).order_by(
        m.PurchaseOrder.po_number, m.PurchaseOrder.revision)
    if not include_superseded:
        stmt = stmt.where(m.PurchaseOrder.superseded_by_id.is_(None))
    return [ser_po(p) for p in db.scalars(stmt)]


def _supplier(db, vid) -> m.Vendor:
    v = get_or_404(db, m.Vendor, vid, "Vendor")
    if not v.active:
        err(422, f"Vendor '{v.name}' is deactivated and cannot receive new purchase orders.")
    if v.kind not in ("supplier", "both"):
        err(422, f"'{v.name}' is registered as a contractor only, not a material supplier.")
    return v


def _line_from(db, item: m.ProcurementItem, qty, rate, quotation_id, vendor_id, description=None, spec=None, unit=None):
    if quotation_id:
        q = get_or_404(db, m.Quotation, quotation_id, "Quotation")
        if q.vendor_id != vendor_id:
            err(422, f"Quotation {quotation_id} belongs to a different vendor.")
        if rate is None:
            rate = q.rate
    if rate is None:
        err(422, f"No rate given for '{item.name}'. Enter the agreed rate or select a quotation.")
    return m.POLine(procurement_item_id=item.id, quotation_id=quotation_id, description=description or item.name,
                    specification=spec if spec is not None else item.specification, quantity=qty,
                    unit=unit or item.unit, rate=rate)


@router.post("/purchase-orders/generate", status_code=201)
def generate_pos(body: GeneratePOsIn, db: Session = Depends(get_db)):
    pl = get_or_404(db, m.ProcurementList, body.list_id, "Procurement list")
    project = db.get(m.Project, pl.project_id)
    check_allocation(db, pl.id, [a.model_dump() for a in body.assignments])
    by_vendor: dict[int, list] = defaultdict(list)
    for a in body.assignments:
        by_vendor[a.vendor_id].append(a)
    created = []
    for vid, assigns in by_vendor.items():
        vendor = _supplier(db, vid)
        po = m.PurchaseOrder(project_id=project.id, list_id=pl.id, vendor_id=vid,
                             po_number=next_number(db, m.PurchaseOrder, m.PurchaseOrder.po_number, project, "PO"),
                             delivery_address=body.delivery_address or project.default_delivery_address,
                             delivery_date=body.delivery_date, tax_pct=body.tax_pct)
        quote_terms = set()
        for a in assigns:
            item = db.get(m.ProcurementItem, a.procurement_item_id)
            po.lines.append(_line_from(db, item, a.quantity, a.rate, a.quotation_id, vid))
            if a.quotation_id:
                qt = db.get(m.Quotation, a.quotation_id).payment_terms
                if qt:
                    quote_terms.add(qt)
        # Payment terms only from saved records: a single quoted term, else vendor default, else blank.
        po.payment_terms = quote_terms.pop() if len(quote_terms) == 1 else vendor.payment_terms
        db.add(po)
        db.flush()
        log_event(db, "po", po, "created", None, m.DRAFT, body.actor or None, f"Generated with {len(po.lines)} line(s)")
        created.append(po)
    db.commit()
    return [ser_po(p) for p in created]


@router.get("/purchase-orders/{poid}")
def get_po(poid: int, db: Session = Depends(get_db)):
    return ser_po(get_or_404(db, m.PurchaseOrder, poid, "Purchase order"))


@router.put("/purchase-orders/{poid}")
def update_po(poid: int, body: POUpdate, db: Session = Depends(get_db)):
    po = get_or_404(db, m.PurchaseOrder, poid, "Purchase order")
    ensure_editable(po)
    if not body.lines:
        err(422, "A purchase order needs at least one line. Delete the draft instead.")
    check_allocation(db, po.list_id, [l.model_dump() for l in body.lines], exclude_po=po.id)
    new_lines = []
    for l in body.lines:
        item = get_or_404(db, m.ProcurementItem, l.procurement_item_id, "Procurement item")
        new_lines.append(_line_from(db, item, l.quantity, l.rate, l.quotation_id, po.vendor_id,
                                    l.description, l.specification, l.unit))
    po.lines.clear()
    db.flush()
    po.lines.extend(new_lines)
    for k in ("delivery_address", "delivery_date", "payment_terms", "tax_pct", "notes"):
        setattr(po, k, getattr(body, k))
    log_event(db, "po", po, "edited", po.status, po.status, body.actor or None)
    db.commit()
    return ser_po(po)


@router.delete("/purchase-orders/{poid}", status_code=204)
def delete_po(poid: int, db: Session = Depends(get_db)):
    po = get_or_404(db, m.PurchaseOrder, poid, "Purchase order")
    if po.status != m.DRAFT or po.superseded_by_id or po.revision > 0:
        err(409, "Only original drafts can be deleted. Reject or revise other documents.")
    if db.scalar(select(m.Contract.id).where(m.Contract.po_id == poid).limit(1)):
        err(409, "A contract references this PO. Delete or reject the contract first.")
    log_event(db, "po", po, "deleted", po.status, None, None)
    db.delete(po)
    db.commit()


@router.post("/purchase-orders/{poid}/action")
def po_action(poid: int, body: ActionIn, db: Session = Depends(get_db)):
    po = get_or_404(db, m.PurchaseOrder, poid, "Purchase order")
    return ser_po(transition(db, "po", po, body.action, body.actor, body.comment, body.confirm))


@router.post("/purchase-orders/{poid}/revision", status_code=201)
def po_revision(poid: int, body: RevisionIn, db: Session = Depends(get_db)):
    po = get_or_404(db, m.PurchaseOrder, poid, "Purchase order")
    return ser_po(create_revision(db, "po", po, body.actor, body.comment))


@router.get("/purchase-orders/{poid}/pdf")
def po_export(poid: int, db: Session = Depends(get_db)):
    po = get_or_404(db, m.PurchaseOrder, poid, "Purchase order")
    data = po_pdf(po, db.get(m.Project, po.project_id))
    name = f"{po.po_number}{'-R' + str(po.revision) if po.revision else ''}.pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})
