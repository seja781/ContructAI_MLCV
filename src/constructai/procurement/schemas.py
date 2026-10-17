"""Request validation (Pydantic) and response serializers."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import models as m
from .services import r2, po_totals, package_totals


def _strip(v):
    return v.strip() if isinstance(v, str) else v


class _In(BaseModel):
    model_config = {"str_strip_whitespace": True}


# ------------------------------------------------------------------ projects / BOM
class ProjectIn(_In):
    name: str = Field(min_length=1, max_length=200)
    code: Optional[str] = Field(None, max_length=60)
    client: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    buyer_name: Optional[str] = None
    buyer_address: Optional[str] = None
    buyer_tax_id: Optional[str] = None
    buyer_contact: Optional[str] = None
    default_delivery_address: Optional[str] = None
    external_ref: Optional[str] = None


class ItemIn(_In):
    name: str = Field(min_length=1, max_length=300)
    specification: Optional[str] = None
    category: Optional[str] = None
    item_code: Optional[str] = None
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=30)
    est_rate: Optional[float] = Field(None, ge=0)
    notes: Optional[str] = None


class ManualBomIn(_In):
    name: str = Field(min_length=1)
    items: list[ItemIn] = Field(default_factory=list)


class VendorChoiceIn(_In):
    vendor_id: Optional[int] = None
    agreed_rate: Optional[float] = Field(None, ge=0)
    quotation_id: Optional[int] = None


class PlaceOrdersIn(_In):
    delivery_address: Optional[str] = None
    delivery_date: Optional[date] = None
    tax_pct: float = Field(0, ge=0, le=100)
    actor: Optional[str] = None


class ProcListIn(_In):
    name: str = Field(min_length=1)
    bom_id: Optional[int] = None


# ------------------------------------------------------------------ vendors / quotes
class VendorIn(_In):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["supplier", "contractor", "both"] = "supplier"
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    tax_id: Optional[str] = None
    materials_supplied: Optional[str] = None
    trades: Optional[str] = None
    payment_terms: Optional[str] = None
    lead_time_days: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = None
    active: bool = True

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if v and ("@" not in v or "." not in v.split("@")[-1]):
            raise ValueError("Enter a valid email address")
        return v or None


class QuotationIn(_In):
    vendor_id: int
    procurement_item_id: Optional[int] = None
    quote_ref: Optional[str] = None
    material_name: str = Field(min_length=1)
    specification: Optional[str] = None
    unit: Optional[str] = None
    rate: float = Field(gt=0)
    available_qty: Optional[float] = Field(None, ge=0)
    lead_time_days: Optional[int] = Field(None, ge=0)
    payment_terms: Optional[str] = None
    valid_until: Optional[date] = None
    notes: Optional[str] = None


# ------------------------------------------------------------------ POs
class POLineIn(_In):
    procurement_item_id: int
    quantity: float = Field(gt=0)
    rate: float = Field(ge=0)
    quotation_id: Optional[int] = None
    description: Optional[str] = None
    specification: Optional[str] = None
    unit: Optional[str] = None


class Assignment(_In):
    procurement_item_id: int
    vendor_id: int
    quantity: float = Field(gt=0)
    rate: Optional[float] = Field(None, ge=0)
    quotation_id: Optional[int] = None


class GeneratePOsIn(_In):
    list_id: int
    assignments: list[Assignment] = Field(min_length=1)
    delivery_address: Optional[str] = None
    delivery_date: Optional[date] = None
    tax_pct: float = Field(0, ge=0, le=100)
    actor: Optional[str] = None


class POUpdate(_In):
    delivery_address: Optional[str] = None
    delivery_date: Optional[date] = None
    payment_terms: Optional[str] = None
    tax_pct: float = Field(0, ge=0, le=100)
    notes: Optional[str] = None
    lines: list[POLineIn]
    actor: Optional[str] = None


# ------------------------------------------------------------------ packages
class PkgLineIn(_In):
    procurement_item_id: int
    quantity: float = Field(gt=0)
    rate: float = Field(0, ge=0)
    description: Optional[str] = None
    specification: Optional[str] = None
    unit: Optional[str] = None


class MilestoneIn(_In):
    name: str = Field(min_length=1)
    due_date: Optional[date] = None
    payment_pct: float = Field(0, ge=0, le=100)


class PackageIn(_In):
    list_id: int
    name: str = Field(min_length=1)
    contractor_id: Optional[int] = None
    arrangement: Literal["supply_only", "supply_and_execute"] = "supply_only"
    scope_of_work: Optional[str] = None
    execution_amount: float = Field(0, ge=0)
    tax_pct: float = Field(0, ge=0, le=100)
    payment_terms: Optional[str] = None
    start_date: Optional[date] = None
    completion_date: Optional[date] = None
    notes: Optional[str] = None
    lines: list[PkgLineIn] = Field(default_factory=list)
    milestones: list[MilestoneIn] = Field(default_factory=list)
    actor: Optional[str] = None


# ------------------------------------------------------------------ contracts / workflow
class TemplateIn(_In):
    name: str = Field(min_length=1)
    kind: Literal["supply", "contractor"]
    body: str = Field(min_length=1)
    active: bool = True


class GenerateContractIn(_In):
    template_id: int
    po_id: Optional[int] = None
    package_id: Optional[int] = None
    title: Optional[str] = None
    actor: Optional[str] = None


class ContractUpdate(_In):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    actor: Optional[str] = None


class ActionIn(_In):
    action: Literal["submit", "approve", "return", "reject", "finalize"]
    actor: str = ""
    comment: Optional[str] = None
    confirm: bool = False


class RevisionIn(_In):
    actor: str = ""
    comment: Optional[str] = None


# ================================================================== serializers
def d(x):
    return x.isoformat() if x else None


def ser_project(p: m.Project) -> dict:
    return {k: getattr(p, k) for k in ("id", "name", "code", "client", "location", "description", "buyer_name",
                                         "buyer_address", "buyer_tax_id", "buyer_contact",
                                         "default_delivery_address", "external_ref")} | {"created_at": d(p.created_at)}


def ser_bom_item(i: m.BomItem) -> dict:
    return {"id": i.id, "item_code": i.item_code, "name": i.name, "specification": i.specification,
            "category": i.category, "quantity": i.quantity, "unit": i.unit, "est_rate": i.est_rate,
            "est_amount": r2(i.quantity * i.est_rate) if i.est_rate is not None else None, "external_ref": i.external_ref,
            "net_quantity": i.net_quantity, "wastage_pct": i.wastage_pct, "source_boq_items": i.source_boq_items}


def ser_bom(b: m.Bom, items=True) -> dict:
    out = {"id": b.id, "project_id": b.project_id, "name": b.name, "source": b.source, "source_file": b.source_file,
           "source_format": b.source_format, "source_total": b.source_total, "import_warnings": b.import_warnings or [],
           "approved_by": b.approved_by, "approved_at": d(b.approved_at), "approval_comment": b.approval_comment,
           "external_ref": b.external_ref, "created_at": d(b.created_at), "item_count": len(b.items),
           "est_total": r2(sum(i.quantity * (i.est_rate or 0) for i in b.items)),
           "read_only": b.source == "imported"}
    if items:
        out["items"] = [ser_bom_item(i) for i in b.items]
    return out


def ser_proc_item(i: m.ProcurementItem, alloc: dict | None = None) -> dict:
    out = {"id": i.id, "list_id": i.list_id, "bom_item_id": i.bom_item_id, "item_code": i.item_code, "name": i.name,
           "specification": i.specification, "category": i.category, "quantity": i.quantity, "unit": i.unit,
           "est_rate": i.est_rate, "est_amount": r2(i.quantity * i.est_rate) if i.est_rate is not None else None,
           "notes": i.notes, "vendor_id": i.vendor_id, "agreed_rate": i.agreed_rate, "quotation_id": i.quotation_id,
           "agreed_amount": r2(i.quantity * i.agreed_rate) if i.agreed_rate is not None else None}
    if alloc:
        out.update(alloc)
    return out


def ser_vendor(v: m.Vendor) -> dict:
    return {k: getattr(v, k) for k in ("id", "name", "kind", "contact_person", "phone", "email", "address", "tax_id",
                                         "materials_supplied", "trades", "payment_terms", "lead_time_days", "notes",
                                         "active")} | {"quotation_count": len(v.quotations)}


def ser_quote(q: m.Quotation) -> dict:
    return {"id": q.id, "vendor_id": q.vendor_id, "vendor_name": q.vendor.name, "vendor_active": q.vendor.active,
            "procurement_item_id": q.procurement_item_id, "quote_ref": q.quote_ref, "material_name": q.material_name,
            "specification": q.specification, "unit": q.unit, "rate": q.rate, "available_qty": q.available_qty,
            "lead_time_days": q.lead_time_days, "payment_terms": q.payment_terms, "valid_until": d(q.valid_until),
            "expired": bool(q.valid_until and q.valid_until < date.today()), "notes": q.notes,
            "created_at": d(q.created_at)}


def _doc_common(x) -> dict:
    return {"status": x.status, "revision": x.revision, "superseded_by_id": x.superseded_by_id,
            "editable": x.status in m.EDITABLE_STATUSES and not x.superseded_by_id,
            "created_at": d(x.created_at), "updated_at": d(x.updated_at)}


def ser_po(po: m.PurchaseOrder) -> dict:
    return {"id": po.id, "project_id": po.project_id, "list_id": po.list_id, "vendor_id": po.vendor_id,
            "vendor": ser_vendor(po.vendor), "po_number": po.po_number, "delivery_address": po.delivery_address,
            "delivery_date": d(po.delivery_date), "payment_terms": po.payment_terms, "tax_pct": po.tax_pct,
            "notes": po.notes,
            "lines": [{"id": l.id, "procurement_item_id": l.procurement_item_id, "quotation_id": l.quotation_id,
                       "description": l.description, "specification": l.specification, "quantity": l.quantity,
                       "unit": l.unit, "rate": l.rate, "amount": r2(l.quantity * l.rate)} for l in po.lines],
            **po_totals(po), **_doc_common(po)}


def ser_package(p: m.Package) -> dict:
    return {"id": p.id, "project_id": p.project_id, "list_id": p.list_id, "package_code": p.package_code,
            "name": p.name, "contractor_id": p.contractor_id,
            "contractor": ser_vendor(p.contractor) if p.contractor else None, "arrangement": p.arrangement,
            "scope_of_work": p.scope_of_work, "execution_amount": p.execution_amount, "tax_pct": p.tax_pct,
            "payment_terms": p.payment_terms, "start_date": d(p.start_date), "completion_date": d(p.completion_date),
            "notes": p.notes,
            "lines": [{"id": l.id, "procurement_item_id": l.procurement_item_id, "description": l.description,
                       "specification": l.specification, "quantity": l.quantity, "unit": l.unit, "rate": l.rate,
                       "amount": r2(l.quantity * l.rate)} for l in p.lines],
            "milestones": [{"id": x.id, "name": x.name, "due_date": d(x.due_date), "payment_pct": x.payment_pct}
                           for x in p.milestones],
            **package_totals(p), **_doc_common(p)}


def ser_template(t: m.ContractTemplate) -> dict:
    return {"id": t.id, "name": t.name, "kind": t.kind, "body": t.body, "active": t.active,
            "updated_at": d(t.updated_at)}


def ser_contract(c: m.Contract) -> dict:
    return {"id": c.id, "project_id": c.project_id, "kind": c.kind, "contract_number": c.contract_number,
            "title": c.title, "po_id": c.po_id, "package_id": c.package_id, "template_id": c.template_id,
            "party_id": c.party_id, "party_name": c.party.name if c.party else None, "body": c.body,
            "schedule": c.schedule or [], **_doc_common(c)}


def ser_event(e: m.DocumentEvent) -> dict:
    return {"id": e.id, "project_id": e.project_id, "doc_type": e.doc_type, "doc_id": e.doc_id,
            "doc_label": e.doc_label, "action": e.action, "from_status": e.from_status, "to_status": e.to_status,
            "actor": e.actor, "comment": e.comment, "created_at": d(e.created_at)}
