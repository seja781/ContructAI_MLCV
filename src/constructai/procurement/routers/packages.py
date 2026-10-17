"""Material packages (clubbing) with optional contractor assignment."""
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..db import get_db
from ..pdf import package_pdf
from ..schemas import PackageIn, ActionIn, RevisionIn, ser_package
from ..services import (err, get_or_404, check_allocation, next_number, ensure_editable, transition,
                        create_revision, log_event, EPS)

router = APIRouter(prefix="/api", tags=["packages"])


@router.get("/projects/{pid}/packages")
def list_packages(pid: int, include_superseded: bool = False, db: Session = Depends(get_db)):
    stmt = select(m.Package).where(m.Package.project_id == pid).order_by(m.Package.package_code, m.Package.revision)
    if not include_superseded:
        stmt = stmt.where(m.Package.superseded_by_id.is_(None))
    return [ser_package(p) for p in db.scalars(stmt)]


def _validate(db, body: PackageIn, pl: m.ProcurementList):
    if body.contractor_id:
        c = get_or_404(db, m.Vendor, body.contractor_id, "Contractor")
        if not c.active:
            err(422, f"'{c.name}' is deactivated.")
        if body.arrangement == "supply_and_execute" and c.kind not in ("contractor", "both"):
            err(422, f"'{c.name}' is registered as a supplier only and cannot execute work.")
    if body.start_date and body.completion_date and body.completion_date < body.start_date:
        err(422, "Completion date cannot be earlier than the start date.")
    if sum(x.payment_pct for x in body.milestones) > 100 + EPS:
        err(422, "Milestone payment percentages add up to more than 100%.")
    ids = [l.procurement_item_id for l in body.lines]
    if len(ids) != len(set(ids)):
        err(422, "The same material appears twice in this package. Combine the quantities into one line.")


def _apply(db, pk: m.Package, body: PackageIn):
    for k in ("name", "contractor_id", "arrangement", "scope_of_work", "execution_amount", "tax_pct",
              "payment_terms", "start_date", "completion_date", "notes"):
        setattr(pk, k, getattr(body, k))
    pk.lines.clear()
    pk.milestones.clear()
    db.flush()
    for l in body.lines:
        item = get_or_404(db, m.ProcurementItem, l.procurement_item_id, "Procurement item")
        pk.lines.append(m.PackageLine(procurement_item_id=item.id, description=l.description or item.name,
                                      specification=l.specification if l.specification is not None else item.specification,
                                      quantity=l.quantity, unit=l.unit or item.unit, rate=l.rate))
    for x in body.milestones:
        pk.milestones.append(m.Milestone(**x.model_dump()))


@router.post("/packages", status_code=201)
def create_package(body: PackageIn, db: Session = Depends(get_db)):
    pl = get_or_404(db, m.ProcurementList, body.list_id, "Procurement list")
    project = db.get(m.Project, pl.project_id)
    _validate(db, body, pl)
    check_allocation(db, pl.id, [l.model_dump() for l in body.lines])
    pk = m.Package(project_id=project.id, list_id=pl.id,
                   package_code=next_number(db, m.Package, m.Package.package_code, project, "PKG"), name=body.name)
    db.add(pk)
    _apply(db, pk, body)
    db.flush()
    log_event(db, "package", pk, "created", None, m.DRAFT, body.actor or None)
    db.commit()
    return ser_package(pk)


@router.get("/packages/{pkid}")
def get_package(pkid: int, db: Session = Depends(get_db)):
    return ser_package(get_or_404(db, m.Package, pkid, "Package"))


@router.put("/packages/{pkid}")
def update_package(pkid: int, body: PackageIn, db: Session = Depends(get_db)):
    pk = get_or_404(db, m.Package, pkid, "Package")
    ensure_editable(pk)
    if body.list_id != pk.list_id:
        err(422, "A package cannot be moved to a different procurement list.")
    pl = db.get(m.ProcurementList, pk.list_id)
    _validate(db, body, pl)
    check_allocation(db, pl.id, [l.model_dump() for l in body.lines], exclude_pkg=pk.id)
    _apply(db, pk, body)
    log_event(db, "package", pk, "edited", pk.status, pk.status, body.actor or None)
    db.commit()
    return ser_package(pk)


@router.delete("/packages/{pkid}", status_code=204)
def delete_package(pkid: int, db: Session = Depends(get_db)):
    pk = get_or_404(db, m.Package, pkid, "Package")
    if pk.status != m.DRAFT or pk.superseded_by_id or pk.revision > 0:
        err(409, "Only original drafts can be deleted. Reject or revise other documents.")
    if db.scalar(select(m.Contract.id).where(m.Contract.package_id == pkid).limit(1)):
        err(409, "A contract references this package. Delete or reject the contract first.")
    log_event(db, "package", pk, "deleted", pk.status, None, None)
    db.delete(pk)
    db.commit()


@router.post("/packages/{pkid}/action")
def package_action(pkid: int, body: ActionIn, db: Session = Depends(get_db)):
    pk = get_or_404(db, m.Package, pkid, "Package")
    return ser_package(transition(db, "package", pk, body.action, body.actor, body.comment, body.confirm))


@router.post("/packages/{pkid}/revision", status_code=201)
def package_revision(pkid: int, body: RevisionIn, db: Session = Depends(get_db)):
    pk = get_or_404(db, m.Package, pkid, "Package")
    return ser_package(create_revision(db, "package", pk, body.actor, body.comment))


@router.get("/packages/{pkid}/pdf")
def package_export(pkid: int, db: Session = Depends(get_db)):
    pk = get_or_404(db, m.Package, pkid, "Package")
    data = package_pdf(pk, db.get(m.Project, pk.project_id))
    name = f"{pk.package_code}{'-R' + str(pk.revision) if pk.revision else ''}.pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})
