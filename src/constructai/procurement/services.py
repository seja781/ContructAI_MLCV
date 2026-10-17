"""Business logic: allocation guard, workflow, revisions, numbering, import, contract rendering."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date

from fastapi import HTTPException
from jinja2.sandbox import SandboxedEnvironment
from jinja2 import TemplateSyntaxError, UndefinedError, StrictUndefined
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from . import models as m

EPS = 1e-6


def r2(x: float | None) -> float:
    return round(float(x or 0), 2)


def err(status: int, msg: str, **extra):
    raise HTTPException(status_code=status, detail={"message": msg, **extra})


def get_or_404(db: Session, model, obj_id: int, label: str | None = None):
    obj = db.get(model, obj_id)
    if not obj:
        err(404, f"{label or model.__name__} {obj_id} not found")
    return obj


# --------------------------------------------------------------------------- totals
def po_totals(po: m.PurchaseOrder) -> dict:
    sub = sum(l.quantity * l.rate for l in po.lines)
    tax = sub * (po.tax_pct or 0) / 100
    return {"subtotal": r2(sub), "tax": r2(tax), "total": r2(sub + tax)}


def package_totals(p: m.Package) -> dict:
    mat = sum(l.quantity * l.rate for l in p.lines)
    exe = p.execution_amount or 0
    sub = mat + exe
    tax = sub * (p.tax_pct or 0) / 100
    return {"material_value": r2(mat), "execution_value": r2(exe), "subtotal": r2(sub),
            "tax": r2(tax), "total": r2(sub + tax),
            "milestone_pct_total": r2(sum(ms.payment_pct for ms in p.milestones))}


# --------------------------------------------------------------------------- allocation
def _active(model):
    return (model.superseded_by_id.is_(None)) & (model.status != m.REJECTED)


def allocated_qty(db: Session, item_id: int, exclude_po: int | None = None,
                  exclude_pkg: int | None = None) -> dict:
    q_po = select(func.coalesce(func.sum(m.POLine.quantity), 0)).join(m.PurchaseOrder).where(
        m.POLine.procurement_item_id == item_id, _active(m.PurchaseOrder))
    if exclude_po:
        q_po = q_po.where(m.PurchaseOrder.id != exclude_po)
    q_pk = select(func.coalesce(func.sum(m.PackageLine.quantity), 0)).join(m.Package).where(
        m.PackageLine.procurement_item_id == item_id, _active(m.Package))
    if exclude_pkg:
        q_pk = q_pk.where(m.Package.id != exclude_pkg)
    po_q, pk_q = float(db.scalar(q_po)), float(db.scalar(q_pk))
    return {"po": po_q, "package": pk_q, "total": po_q + pk_q}


def item_allocation(db: Session, item: m.ProcurementItem) -> dict:
    a = allocated_qty(db, item.id)
    return {"allocated_po": r2(a["po"]), "allocated_package": r2(a["package"]),
            "allocated_total": r2(a["total"]), "remaining": r2(item.quantity - a["total"])}


def check_allocation(db: Session, list_id: int, lines: list[dict], exclude_po=None, exclude_pkg=None):
    """lines: [{procurement_item_id, quantity}]. Raises 409 if any item would be over-allocated."""
    wanted: dict[int, float] = {}
    for ln in lines:
        wanted[ln["procurement_item_id"]] = wanted.get(ln["procurement_item_id"], 0) + ln["quantity"]
    problems = []
    for item_id, qty in wanted.items():
        item = db.get(m.ProcurementItem, item_id)
        if not item or item.list_id != list_id:
            err(422, f"Procurement item {item_id} does not belong to this procurement list")
        already = allocated_qty(db, item_id, exclude_po, exclude_pkg)["total"]
        if already + qty > item.quantity + EPS:
            problems.append({"procurement_item_id": item_id, "name": item.name, "unit": item.unit,
                             "required": item.quantity, "already_allocated": r2(already),
                             "requested": r2(qty), "available": r2(item.quantity - already)})
    if problems:
        names = ", ".join(f"{p['name']} (available {p['available']} {p['unit']}, requested {p['requested']})"
                          for p in problems)
        err(409, f"Over-allocation blocked: {names}", over_allocated=problems)


# --------------------------------------------------------------------------- numbering
def project_prefix(project: m.Project) -> str:
    code = (project.code or f"P{project.id}").strip().upper()
    return re.sub(r"[^A-Z0-9-]", "", code) or f"P{project.id}"


def next_number(db: Session, model, number_col, project: m.Project, kind: str) -> str:
    n = db.scalar(select(func.count()).select_from(model).where(
        model.project_id == project.id, model.revision == 0)) or 0
    while True:
        n += 1
        num = f"{kind}-{project_prefix(project)}-{n:03d}"
        if not db.scalar(select(func.count()).select_from(model).where(number_col == num)):
            return num


# --------------------------------------------------------------------------- workflow
TRANSITIONS = {
    "submit": ({m.DRAFT, m.RETURNED}, m.PENDING),
    "approve": ({m.PENDING}, m.APPROVED),
    "return": ({m.PENDING}, m.RETURNED),
    "reject": ({m.PENDING}, m.REJECTED),
    "finalize": ({m.APPROVED}, m.FINALIZED),
}


def log_event(db: Session, doc_type: str, doc, action: str, frm=None, to=None, actor=None, comment=None):
    label = getattr(doc, "po_number", None) or getattr(doc, "package_code", None) or getattr(doc, "contract_number", None)
    if label and doc.revision:
        label = f"{label} R{doc.revision}"
    db.add(m.DocumentEvent(project_id=doc.project_id, doc_type=doc_type, doc_id=doc.id, doc_label=label,
                           action=action, from_status=frm, to_status=to, actor=actor, comment=comment))


def ensure_editable(doc):
    if doc.superseded_by_id:
        err(409, "This document has been superseded by a newer revision and is read-only.")
    if doc.status not in m.EDITABLE_STATUSES:
        err(409, f"Document is '{doc.status}' and cannot be edited. Create a revision instead.")


def transition(db: Session, doc_type: str, doc, action: str, actor: str | None, comment: str | None,
               confirm: bool = False):
    if action not in TRANSITIONS:
        err(422, f"Unknown action '{action}'")
    if doc.superseded_by_id:
        err(409, "Superseded documents cannot change status.")
    allowed_from, to = TRANSITIONS[action]
    if doc.status not in allowed_from:
        err(409, f"Cannot '{action}' a document in status '{doc.status}'.")
    if not (actor or "").strip():
        err(422, "Reviewer / actor name is required for every workflow action.")
    if action in ("return", "reject") and not (comment or "").strip():
        err(422, f"A comment is required to {action} a document.")
    if action == "finalize" and not confirm:
        err(422, "Finalization requires explicit confirmation (confirm=true).")
    _validate_for_action(db, doc_type, doc, action)
    frm = doc.status
    doc.status = to
    log_event(db, doc_type, doc, action, frm, to, actor.strip(), comment)
    db.commit()
    return doc


def _validate_for_action(db, doc_type, doc, action):
    if action == "submit":
        if doc_type in ("po", "package") and not doc.lines:
            err(422, "Add at least one line item before submitting for review.")
        if doc_type == "po" and any(l.rate <= 0 for l in doc.lines):
            err(422, "All PO lines need an agreed rate greater than zero before review.")
        if doc_type == "package" and doc.arrangement == "supply_and_execute" and not doc.contractor_id:
            err(422, "Supply-and-execute packages need an assigned contractor before review.")
        if doc_type == "contract" and not (doc.body or "").strip():
            err(422, "Contract body is empty.")
    if action == "finalize" and doc_type == "contract":
        src = db.get(m.PurchaseOrder, doc.po_id) if doc.po_id else db.get(m.Package, doc.package_id) if doc.package_id else None
        if src is not None and src.status not in (m.APPROVED, m.FINALIZED):
            err(409, "The source purchase order / package must be approved before the contract can be finalized.")


def create_revision(db: Session, doc_type: str, doc, actor: str | None, comment: str | None):
    if doc.superseded_by_id:
        err(409, "A newer revision already exists.")
    if doc.status not in (m.APPROVED, m.FINALIZED, m.REJECTED):
        err(409, "Revisions are created from approved, finalized or rejected documents. Edit drafts directly.")
    if not (actor or "").strip():
        err(422, "Actor name is required to create a revision.")
    if doc_type == "po":
        new = m.PurchaseOrder(project_id=doc.project_id, list_id=doc.list_id, vendor_id=doc.vendor_id,
                              po_number=doc.po_number, delivery_address=doc.delivery_address,
                              delivery_date=doc.delivery_date, payment_terms=doc.payment_terms,
                              tax_pct=doc.tax_pct, notes=doc.notes)
        new.lines = [m.POLine(procurement_item_id=l.procurement_item_id, quotation_id=l.quotation_id,
                              description=l.description, specification=l.specification, quantity=l.quantity,
                              unit=l.unit, rate=l.rate) for l in doc.lines]
        if doc.status == m.REJECTED:
            check_allocation(db, doc.list_id, [{"procurement_item_id": l.procurement_item_id, "quantity": l.quantity}
                                               for l in doc.lines], exclude_po=doc.id)
    elif doc_type == "package":
        new = m.Package(project_id=doc.project_id, list_id=doc.list_id, package_code=doc.package_code,
                        name=doc.name, contractor_id=doc.contractor_id, arrangement=doc.arrangement,
                        scope_of_work=doc.scope_of_work, execution_amount=doc.execution_amount, tax_pct=doc.tax_pct,
                        payment_terms=doc.payment_terms, start_date=doc.start_date,
                        completion_date=doc.completion_date, notes=doc.notes)
        new.lines = [m.PackageLine(procurement_item_id=l.procurement_item_id, description=l.description,
                                   specification=l.specification, quantity=l.quantity, unit=l.unit, rate=l.rate)
                     for l in doc.lines]
        new.milestones = [m.Milestone(name=x.name, due_date=x.due_date, payment_pct=x.payment_pct)
                          for x in doc.milestones]
        if doc.status == m.REJECTED:
            check_allocation(db, doc.list_id, [{"procurement_item_id": l.procurement_item_id, "quantity": l.quantity}
                                               for l in doc.lines], exclude_pkg=doc.id)
    else:
        new = m.Contract(project_id=doc.project_id, kind=doc.kind, contract_number=doc.contract_number,
                         title=doc.title, po_id=doc.po_id, package_id=doc.package_id, template_id=doc.template_id,
                         party_id=doc.party_id, body=doc.body, schedule=list(doc.schedule or []))
    new.revision = doc.revision + 1
    new.status = m.DRAFT
    db.add(new)
    db.flush()
    doc.superseded_by_id = new.id
    log_event(db, doc_type, doc, "superseded", doc.status, doc.status, actor.strip(),
              f"Superseded by revision R{new.revision}" + (f": {comment}" if comment else ""))
    log_event(db, doc_type, new, "revision_created", None, m.DRAFT, actor.strip(), comment)
    db.commit()
    return new


# --------------------------------------------------------------------------- BOM import
# Column aliases (after normalisation). ConstructAI BOMResult line keys normalise to:
# material, category, unit, net_qty, wastage, order_qty, rate_rs, cost_rs, used_in_boq_items.
# Procurement quantity = "Order qty" (net + wastage, rounded by ConstructAI).
ALIASES = {
    "name": ["name", "material", "material_name", "item", "item_name", "description"],
    "specification": ["specification", "spec", "specs", "specifications", "details"],
    "quantity": ["quantity", "qty", "order_qty", "order_quantity", "total_quantity", "net_qty", "net_quantity"],
    "unit": ["unit", "uom", "units"],
    "est_rate": ["est_rate", "rate", "rate_rs", "rate_inr", "unit_rate", "estimated_rate", "unit_cost"],
    "est_amount": ["amount", "est_amount", "cost_rs", "cost", "estimated_amount", "total_cost", "estimated_cost"],
    "item_code": ["item_code", "code", "material_id", "sl_no", "item_no", "boq_code"],
    "category": ["category", "trade", "group", "section", "discipline"],
    "external_ref": ["id", "external_ref", "item_id", "uid"],
    "net_quantity": ["net_qty", "net_quantity"],
    "wastage_pct": ["wastage", "wastage_pct", "wastage_percent"],
    "source_boq_items": ["used_in_boq_items", "boq_items", "source_boq_items"],
}


def _is_constructai_bom(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("lines"), list) and (
        "validation" in data or "mapping" in data or "total_cost" in data)


def _norm(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")


def _pick(row: dict, field: str):
    for a in ALIASES[field]:
        if a in row and row[a] not in (None, ""):
            return row[a]
    return None


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace(",", "").strip())


def parse_bom_file(filename: str, content: bytes) -> dict:
    """Returns {"meta": {...}, "items": [normalized items]} or raises 422 with row errors."""
    text = content.decode("utf-8-sig")
    meta: dict = {}
    if filename.lower().endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            err(422, f"Invalid JSON: {e}")
        # Optional wrapper: {"boq_id": "...", "source_name": "...", "bom": <BOMResult>}
        if isinstance(data, dict) and _is_constructai_bom(data.get("bom")):
            data = {**data["bom"], "boq_id": data.get("boq_id"), "source_name": data.get("source_name")}
        if _is_constructai_bom(data):
            validation = data.get("validation") or {}
            if validation.get("status") == "blocked" or validation.get("errors"):
                err(422, "This ConstructAI BOM is blocked (unresolved mapping/validation errors). Resolve them in "
                         "ConstructAI and regenerate the BOM before procurement.",
                    row_errors=[{"row": 0, "error": e} for e in validation.get("errors", [])])
            meta = {"format": "constructai_bom", "external_ref": data.get("boq_id"),
                    "bom_name": f"ConstructAI BOM - {data['source_name']}" if data.get("source_name") else None,
                    "source_total": data.get("total_cost"),
                    "warnings": list(validation.get("warnings") or data.get("warnings") or [])}
            rows = data["lines"]
        elif isinstance(data, dict):
            meta = {"format": "generic_json", "project": data.get("project") or {},
                    "bom_name": (data.get("bom") or {}).get("name") or data.get("name"),
                    "external_ref": (data.get("bom") or {}).get("id") or data.get("bom_id")}
            rows = data.get("items") or (data.get("bom") or {}).get("items")
        else:
            meta = {"format": "generic_json"}
            rows = data
        if not isinstance(rows, list):
            err(422, "JSON must be a list of items or an object with an 'items' array.")
    elif filename.lower().endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(text)))
        meta = {"format": "csv"}
    else:
        err(422, "Unsupported file type. Upload a .json or .csv file.")
    if not rows:
        err(422, "The file contains no BOM items.")

    items, errors = [], []
    for idx, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            errors.append({"row": idx, "error": "Row is not an object"})
            continue
        row = {_norm(k): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
        name, unit = _pick(row, "name"), _pick(row, "unit")
        row_err = []
        try:
            qty = _num(_pick(row, "quantity"))
            rate = _num(_pick(row, "est_rate"))
            amount = _num(_pick(row, "est_amount"))
            net_q = _num(_pick(row, "net_quantity"))
            wastage = _num(_pick(row, "wastage_pct"))
        except ValueError:
            errors.append({"row": idx, "error": "Quantity, rate or amount is not a valid number"})
            continue
        if not name:
            row_err.append("missing material name")
        if not unit:
            row_err.append("missing unit")
        if qty is None or qty <= 0:
            row_err.append("quantity must be greater than 0")
        if rate is not None and rate < 0:
            row_err.append("rate cannot be negative")
        if row_err:
            errors.append({"row": idx, "error": "; ".join(row_err)})
            continue
        if rate is None and amount is not None and qty:
            rate = amount / qty
        items.append({"name": str(name), "specification": _pick(row, "specification"), "quantity": qty,
                      "unit": str(unit), "est_rate": r2(rate) if rate is not None else None,
                      "item_code": (str(_pick(row, "item_code")) if _pick(row, "item_code") is not None else None),
                      "category": _pick(row, "category"),
                      "external_ref": (str(_pick(row, "external_ref")) if _pick(row, "external_ref") is not None else None),
                      "net_quantity": net_q, "wastage_pct": wastage,
                      "source_boq_items": (str(_pick(row, "source_boq_items")) if _pick(row, "source_boq_items") is not None else None)})
    if errors:
        err(422, f"Import rejected: {len(errors)} row(s) have errors. Nothing was imported.", row_errors=errors)
    return {"meta": meta, "items": items}


# --------------------------------------------------------------------------- contracts
SCHEDULE_MARKER = "[[MATERIAL_SCHEDULE]]"
_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)


def _fmt_money(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_date(v):
    return v.strftime("%d %b %Y") if isinstance(v, date) else (v or "To be agreed")


_env.filters["money"] = _fmt_money
_env.filters["date"] = _fmt_date
_env.filters["or_tba"] = lambda v: v if v not in (None, "") else "To be agreed"


def _party_ctx(v: m.Vendor | None) -> dict:
    if not v:
        return {"name": "To be assigned", "contact_person": "", "phone": "", "email": "", "address": "", "tax_id": ""}
    return {"name": v.name, "contact_person": v.contact_person or "", "phone": v.phone or "",
            "email": v.email or "", "address": v.address or "", "tax_id": v.tax_id or ""}


def contract_context(db: Session, kind: str, source, buyer: dict) -> tuple[dict, list]:
    project = db.get(m.Project, source.project_id)
    proj = {"name": project.name, "code": project.code or "", "client": project.client or "",
            "location": project.location or "", "description": project.description or ""}
    if kind == "supply":
        po: m.PurchaseOrder = source
        t = po_totals(po)
        schedule = [{"description": l.description, "specification": l.specification or "", "quantity": l.quantity,
                     "unit": l.unit, "rate": l.rate, "amount": r2(l.quantity * l.rate)} for l in po.lines]
        ctx = {"project": proj, "buyer": buyer, "party": _party_ctx(po.vendor), "vendor": _party_ctx(po.vendor),
               "po": {"number": po.po_number, "revision": po.revision, "delivery_address": po.delivery_address or "",
                      "delivery_date": po.delivery_date, "payment_terms": po.payment_terms or "",
                      "tax_pct": po.tax_pct, "notes": po.notes or ""},
               "items": schedule, "totals": t, "today": date.today(), "schedule": SCHEDULE_MARKER}
    else:
        pk: m.Package = source
        t = package_totals(pk)
        schedule = [{"description": l.description, "specification": l.specification or "", "quantity": l.quantity,
                     "unit": l.unit, "rate": l.rate, "amount": r2(l.quantity * l.rate)} for l in pk.lines]
        ctx = {"project": proj, "buyer": buyer, "party": _party_ctx(pk.contractor), "contractor": _party_ctx(pk.contractor),
               "package": {"code": pk.package_code, "name": pk.name, "arrangement": pk.arrangement,
                           "arrangement_label": "Supply and Execute" if pk.arrangement == "supply_and_execute" else "Supply Only",
                           "scope_of_work": pk.scope_of_work or "", "payment_terms": pk.payment_terms or "",
                           "start_date": pk.start_date, "completion_date": pk.completion_date,
                           "tax_pct": pk.tax_pct, "notes": pk.notes or ""},
               "milestones": [{"name": x.name, "due_date": x.due_date, "payment_pct": x.payment_pct} for x in pk.milestones],
               "items": schedule, "totals": t, "today": date.today(), "schedule": SCHEDULE_MARKER}
    return ctx, schedule


def render_template(body: str, ctx: dict) -> str:
    try:
        return _env.from_string(body).render(**ctx)
    except TemplateSyntaxError as e:
        err(422, f"Template syntax error on line {e.lineno}: {e.message}")
    except UndefinedError as e:
        err(422, f"Template references an unknown field: {e.message}")
