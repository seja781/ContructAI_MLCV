"""Contract templates and contract drafts (generated from saved PO / package records)."""
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..db import get_db
from ..pdf import contract_pdf
from ..schemas import (TemplateIn, GenerateContractIn, ContractUpdate, ActionIn, RevisionIn,
                       ser_template, ser_contract)
from ..services import (err, get_or_404, next_number, ensure_editable, transition, create_revision,
                        log_event, contract_context, render_template)

router = APIRouter(prefix="/api", tags=["contracts"])


# ------------------------------------------------------------------ templates
@router.get("/contract-templates")
def list_templates(kind: str | None = None, db: Session = Depends(get_db)):
    stmt = select(m.ContractTemplate).order_by(m.ContractTemplate.kind, m.ContractTemplate.name)
    if kind:
        stmt = stmt.where(m.ContractTemplate.kind == kind)
    return [ser_template(t) for t in db.scalars(stmt)]


@router.post("/contract-templates", status_code=201)
def create_template(body: TemplateIn, db: Session = Depends(get_db)):
    _check_syntax(body.body)
    t = m.ContractTemplate(**body.model_dump())
    db.add(t)
    db.commit()
    return ser_template(t)


@router.put("/contract-templates/{tid}")
def update_template(tid: int, body: TemplateIn, db: Session = Depends(get_db)):
    t = get_or_404(db, m.ContractTemplate, tid, "Template")
    _check_syntax(body.body)
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    db.commit()
    return ser_template(t)


@router.delete("/contract-templates/{tid}", status_code=204)
def delete_template(tid: int, db: Session = Depends(get_db)):
    t = get_or_404(db, m.ContractTemplate, tid, "Template")
    db.delete(t)
    db.commit()


def _check_syntax(body: str):
    from jinja2 import TemplateSyntaxError
    from ..services import _env
    try:
        _env.parse(body)
    except TemplateSyntaxError as e:
        err(422, f"Template syntax error on line {e.lineno}: {e.message}")


# ------------------------------------------------------------------ contracts
def _latest(db, model, obj):
    while obj.superseded_by_id:
        obj = db.get(model, obj.superseded_by_id)
    return obj


def _source(db, po_id, package_id):
    if bool(po_id) == bool(package_id):
        err(422, "Select exactly one source: a purchase order or a package.")
    if po_id:
        po = _latest(db, m.PurchaseOrder, get_or_404(db, m.PurchaseOrder, po_id, "Purchase order"))
        return "supply", po, po.vendor_id
    pk = _latest(db, m.Package, get_or_404(db, m.Package, package_id, "Package"))
    return "contractor", pk, pk.contractor_id


def _render(db, kind, src, template: m.ContractTemplate):
    if template.kind != kind:
        err(422, f"Template '{template.name}' is a {template.kind} template; this source needs a {kind} template.")
    project = db.get(m.Project, src.project_id)
    buyer = {"name": project.buyer_name or "", "address": project.buyer_address or "",
             "tax_id": project.buyer_tax_id or "", "contact": project.buyer_contact or ""}
    ctx, schedule = contract_context(db, kind, src, buyer)
    return render_template(template.body, ctx), schedule


@router.get("/projects/{pid}/contracts")
def list_contracts(pid: int, include_superseded: bool = False, db: Session = Depends(get_db)):
    stmt = select(m.Contract).where(m.Contract.project_id == pid).order_by(m.Contract.contract_number, m.Contract.revision)
    if not include_superseded:
        stmt = stmt.where(m.Contract.superseded_by_id.is_(None))
    return [ser_contract(c) for c in db.scalars(stmt)]


@router.post("/contracts/generate", status_code=201)
def generate_contract(body: GenerateContractIn, db: Session = Depends(get_db)):
    kind, src, party_id = _source(db, body.po_id, body.package_id)
    if src.status == m.REJECTED:
        err(409, "Cannot generate a contract from a rejected document.")
    if kind == "contractor" and not party_id:
        err(422, "Assign a contractor to the package before generating a contractor agreement.")
    col = m.Contract.po_id if kind == "supply" else m.Contract.package_id
    dup = db.scalar(select(m.Contract).where(col == src.id, m.Contract.superseded_by_id.is_(None),
                                             m.Contract.status != m.REJECTED))
    if dup:
        err(409, f"Contract {dup.contract_number} already exists for this source. Edit or revise it instead.")
    template = get_or_404(db, m.ContractTemplate, body.template_id, "Template")
    text, schedule = _render(db, kind, src, template)
    project = db.get(m.Project, src.project_id)
    party = db.get(m.Vendor, party_id) if party_id else None
    default_title = (f"Supply Agreement - {party.name if party else ''} - {src.po_number}" if kind == "supply"
                     else f"Contractor Agreement - {src.name} - {party.name if party else ''}")
    c = m.Contract(project_id=project.id, kind=kind, title=body.title or default_title,
                   contract_number=next_number(db, m.Contract, m.Contract.contract_number, project, "CT"),
                   po_id=src.id if kind == "supply" else None, package_id=src.id if kind == "contractor" else None,
                   template_id=template.id, party_id=party_id, body=text, schedule=schedule)
    db.add(c)
    db.flush()
    log_event(db, "contract", c, "created", None, m.DRAFT, body.actor or None, f"From template '{template.name}'")
    db.commit()
    return ser_contract(c)


@router.get("/contracts/{cid}")
def get_contract(cid: int, db: Session = Depends(get_db)):
    return ser_contract(get_or_404(db, m.Contract, cid, "Contract"))


@router.put("/contracts/{cid}")
def update_contract(cid: int, body: ContractUpdate, db: Session = Depends(get_db)):
    c = get_or_404(db, m.Contract, cid, "Contract")
    ensure_editable(c)
    c.title, c.body = body.title, body.body
    log_event(db, "contract", c, "edited", c.status, c.status, body.actor or None)
    db.commit()
    return ser_contract(c)


@router.post("/contracts/{cid}/regenerate")
def regenerate_contract(cid: int, body: RevisionIn, db: Session = Depends(get_db)):
    """Re-render from the template using the latest source revision. Overwrites manual edits in this draft."""
    c = get_or_404(db, m.Contract, cid, "Contract")
    ensure_editable(c)
    if not c.template_id:
        err(409, "The template used for this contract has been deleted.")
    kind, src, party_id = _source(db, c.po_id, c.package_id)
    text, schedule = _render(db, kind, src, db.get(m.ContractTemplate, c.template_id))
    c.body, c.schedule, c.party_id = text, schedule, party_id
    if kind == "supply":
        c.po_id = src.id
    else:
        c.package_id = src.id
    log_event(db, "contract", c, "regenerated", c.status, c.status, body.actor or None, body.comment)
    db.commit()
    return ser_contract(c)


@router.delete("/contracts/{cid}", status_code=204)
def delete_contract(cid: int, db: Session = Depends(get_db)):
    c = get_or_404(db, m.Contract, cid, "Contract")
    if c.status != m.DRAFT or c.superseded_by_id or c.revision > 0:
        err(409, "Only original drafts can be deleted.")
    log_event(db, "contract", c, "deleted", c.status, None, None)
    db.delete(c)
    db.commit()


@router.post("/contracts/{cid}/action")
def contract_action(cid: int, body: ActionIn, db: Session = Depends(get_db)):
    c = get_or_404(db, m.Contract, cid, "Contract")
    return ser_contract(transition(db, "contract", c, body.action, body.actor, body.comment, body.confirm))


@router.post("/contracts/{cid}/revision", status_code=201)
def contract_revision(cid: int, body: RevisionIn, db: Session = Depends(get_db)):
    c = get_or_404(db, m.Contract, cid, "Contract")
    return ser_contract(create_revision(db, "contract", c, body.actor, body.comment))


@router.get("/contracts/{cid}/pdf")
def contract_export(cid: int, db: Session = Depends(get_db)):
    c = get_or_404(db, m.Contract, cid, "Contract")
    data = contract_pdf(c, db.get(m.Project, c.project_id))
    name = f"{c.contract_number}{'-R' + str(c.revision) if c.revision else ''}.pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})
