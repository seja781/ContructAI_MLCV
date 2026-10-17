"""Approval / document history and project summary."""
from collections import Counter

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..db import get_db
from ..schemas import ser_event
from ..services import get_or_404, po_totals, package_totals, r2

router = APIRouter(prefix="/api", tags=["history"])


@router.get("/projects/{pid}/history")
def history(pid: int, doc_type: str | None = None, doc_id: int | None = None, limit: int = 500,
            db: Session = Depends(get_db)):
    stmt = select(m.DocumentEvent).where(m.DocumentEvent.project_id == pid).order_by(m.DocumentEvent.id.desc()).limit(limit)
    if doc_type:
        stmt = stmt.where(m.DocumentEvent.doc_type == doc_type)
    if doc_id:
        stmt = stmt.where(m.DocumentEvent.doc_id == doc_id)
    return [ser_event(e) for e in db.scalars(stmt)]


@router.get("/projects/{pid}/summary")
def summary(pid: int, db: Session = Depends(get_db)):
    get_or_404(db, m.Project, pid, "Project")
    pos = db.scalars(select(m.PurchaseOrder).where(m.PurchaseOrder.project_id == pid,
                                                   m.PurchaseOrder.superseded_by_id.is_(None))).all()
    pks = db.scalars(select(m.Package).where(m.Package.project_id == pid, m.Package.superseded_by_id.is_(None))).all()
    cts = db.scalars(select(m.Contract).where(m.Contract.project_id == pid, m.Contract.superseded_by_id.is_(None))).all()
    lists = db.scalars(select(m.ProcurementList).where(m.ProcurementList.project_id == pid)).all()
    boms = db.scalars(select(m.Bom).where(m.Bom.project_id == pid)).all()
    live = lambda d: d.status != m.REJECTED
    return {
        "boms": {"imported": sum(b.source == "imported" for b in boms), "manual_test": sum(b.source != "imported" for b in boms)},
        "procurement_lists": len(lists),
        "estimated_value": r2(sum(i.quantity * (i.est_rate or 0) for pl in lists for i in pl.items)),
        "po": {"count": len(pos), "value": r2(sum(po_totals(p)["total"] for p in pos if live(p))),
               "by_status": dict(Counter(p.status for p in pos))},
        "package": {"count": len(pks), "value": r2(sum(package_totals(p)["total"] for p in pks if live(p))),
                    "by_status": dict(Counter(p.status for p in pks))},
        "contract": {"count": len(cts), "by_status": dict(Counter(c.status for c in cts))},
        "pending_review": sum(d.status == m.PENDING for d in [*pos, *pks, *cts]),
    }


# ------------------------------------------------------------------ purchase history (all projects)
_ORDERED = (m.APPROVED, m.FINALIZED)


@router.get("/purchase-history")
def purchase_history(project_id: int | None = None, vendor_id: int | None = None, q: str | None = None,
                     status: str | None = None, include_rejected: bool = False, limit: int = 2000,
                     db: Session = Depends(get_db)):
    """Every purchase-order line across all projects (latest revision of each PO), newest first."""
    stmt = (select(m.POLine, m.PurchaseOrder, m.Project, m.Vendor)
            .join(m.PurchaseOrder, m.POLine.po_id == m.PurchaseOrder.id)
            .join(m.Project, m.PurchaseOrder.project_id == m.Project.id)
            .join(m.Vendor, m.PurchaseOrder.vendor_id == m.Vendor.id)
            .where(m.PurchaseOrder.superseded_by_id.is_(None))
            .order_by(m.PurchaseOrder.created_at.desc(), m.POLine.id).limit(limit))
    if project_id:
        stmt = stmt.where(m.PurchaseOrder.project_id == project_id)
    if vendor_id:
        stmt = stmt.where(m.PurchaseOrder.vendor_id == vendor_id)
    if q:
        stmt = stmt.where(m.POLine.description.ilike(f"%{q.strip()}%"))
    if status:
        stmt = stmt.where(m.PurchaseOrder.status == status)
    elif not include_rejected:
        stmt = stmt.where(m.PurchaseOrder.status != m.REJECTED)
    rows, by_vendor, total = [], {}, 0.0
    for line, po, project, vendor in db.execute(stmt):
        amount = r2(line.quantity * line.rate)
        rows.append({"po_id": po.id, "po_number": po.po_number, "revision": po.revision, "status": po.status,
                     "date": po.created_at.isoformat(), "project_id": project.id, "project_name": project.name,
                     "project_code": project.code, "vendor_id": vendor.id, "vendor_name": vendor.name,
                     "material": line.description, "specification": line.specification, "quantity": line.quantity,
                     "unit": line.unit, "rate": line.rate, "amount": amount})
        total += amount
        by_vendor[vendor.name] = r2(by_vendor.get(vendor.name, 0) + amount)
    return {"rows": rows, "total": r2(total), "po_count": len({r["po_id"] for r in rows}),
            "by_vendor": [{"vendor": k, "amount": v} for k, v in sorted(by_vendor.items(), key=lambda kv: -kv[1])]}


@router.get("/purchase-history/last-rates")
def last_rates(list_id: int, db: Session = Depends(get_db)):
    """For each material on a procurement list: the most recent rate paid on an approved or finalized PO
    (any project, same material name)."""
    pl = get_or_404(db, m.ProcurementList, list_id, "Procurement list")
    out = {}
    for it in pl.items:
        row = db.execute(
            select(m.POLine, m.PurchaseOrder, m.Vendor, m.Project)
            .join(m.PurchaseOrder, m.POLine.po_id == m.PurchaseOrder.id)
            .join(m.Vendor, m.PurchaseOrder.vendor_id == m.Vendor.id)
            .join(m.Project, m.PurchaseOrder.project_id == m.Project.id)
            .where(func.lower(m.POLine.description) == it.name.lower(), m.POLine.unit == it.unit,
                   m.PurchaseOrder.status.in_(_ORDERED), m.PurchaseOrder.superseded_by_id.is_(None))
            .order_by(m.PurchaseOrder.created_at.desc()).limit(1)).first()
        if row:
            line, po, vendor, project = row
            out[str(it.id)] = {"rate": line.rate, "vendor_id": vendor.id, "vendor_name": vendor.name,
                               "po_number": po.po_number, "date": po.created_at.isoformat(), "project_name": project.name}
    return out
