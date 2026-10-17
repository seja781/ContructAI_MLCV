"""ORM models.

Integration notes (ConstructAI):
- `Bom` / `BomItem` mirror an approved BOM/BOQ snapshot. Imported BOMs are read-only;
  the procurement list copies items so the source BOM is never modified.
- `external_ref` fields hold IDs from the upstream ConstructAI system.
"""
from datetime import datetime, date, timezone

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, JSON)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # stored as naive UTC


# Document workflow statuses
DRAFT, PENDING, RETURNED, APPROVED, REJECTED, FINALIZED = (
    "draft", "pending_review", "returned", "approved", "rejected", "finalized")
STATUSES = [DRAFT, PENDING, RETURNED, APPROVED, REJECTED, FINALIZED]
EDITABLE_STATUSES = {DRAFT, RETURNED}


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str | None] = mapped_column(String(60))
    client: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    # Buying party (the organisation issuing POs / contracts for this project)
    buyer_name: Mapped[str | None] = mapped_column(String(200))
    buyer_address: Mapped[str | None] = mapped_column(Text)
    buyer_tax_id: Mapped[str | None] = mapped_column(String(50))
    buyer_contact: Mapped[str | None] = mapped_column(String(200))
    default_delivery_address: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    boms: Mapped[list["Bom"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Bom(Base):
    __tablename__ = "boms"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20))  # "imported" | "manual_test"
    source_format: Mapped[str | None] = mapped_column(String(30))  # constructai_bom | generic_json | csv
    source_file: Mapped[str | None] = mapped_column(String(300))
    external_ref: Mapped[str | None] = mapped_column(String(100))
    source_total: Mapped[float | None] = mapped_column(Float)  # total_cost reported by the source system
    approved_by: Mapped[str | None] = mapped_column(String(100))  # BOM approver in ConstructAI (handoff)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    approval_comment: Mapped[str | None] = mapped_column(Text)
    import_warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    project: Mapped[Project] = relationship(back_populates="boms")
    items: Mapped[list["BomItem"]] = relationship(back_populates="bom", cascade="all, delete-orphan",
                                                   order_by="BomItem.id")


class BomItem(Base):
    __tablename__ = "bom_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    bom_id: Mapped[int] = mapped_column(ForeignKey("boms.id", ondelete="CASCADE"))
    item_code: Mapped[str | None] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(300))
    specification: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30))
    est_rate: Mapped[float | None] = mapped_column(Float)
    # ConstructAI BOM traceability (Net qty, Wastage %, Used in BOQ items)
    net_quantity: Mapped[float | None] = mapped_column(Float)
    wastage_pct: Mapped[float | None] = mapped_column(Float)
    source_boq_items: Mapped[str | None] = mapped_column(String(300))
    external_ref: Mapped[str | None] = mapped_column(String(100))
    bom: Mapped[Bom] = relationship(back_populates="items")


class ProcurementList(Base):
    __tablename__ = "procurement_lists"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    bom_id: Mapped[int | None] = mapped_column(ForeignKey("boms.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    items: Mapped[list["ProcurementItem"]] = relationship(back_populates="plist", cascade="all, delete-orphan",
                                                           order_by="ProcurementItem.id")


class ProcurementItem(Base):
    __tablename__ = "procurement_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("procurement_lists.id", ondelete="CASCADE"))
    bom_item_id: Mapped[int | None] = mapped_column(ForeignKey("bom_items.id", ondelete="SET NULL"))
    item_code: Mapped[str | None] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(300))
    specification: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30))
    est_rate: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    # vendor chosen for this material on the Materials list (used by "Place orders")
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    agreed_rate: Mapped[float | None] = mapped_column(Float)
    quotation_id: Mapped[int | None] = mapped_column(Integer)
    plist: Mapped[ProcurementList] = relationship(back_populates="items")


class Vendor(Base):
    """A party: material supplier, contractor, or both."""
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="supplier")  # supplier | contractor | both
    contact_person: Mapped[str | None] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(150))
    address: Mapped[str | None] = mapped_column(Text)
    tax_id: Mapped[str | None] = mapped_column(String(50))  # e.g. GSTIN
    materials_supplied: Mapped[str | None] = mapped_column(Text)
    trades: Mapped[str | None] = mapped_column(Text)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    quotations: Mapped[list["Quotation"]] = relationship(back_populates="vendor", cascade="all, delete-orphan")


class Quotation(Base):
    __tablename__ = "quotations"
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"))
    procurement_item_id: Mapped[int | None] = mapped_column(ForeignKey("procurement_items.id", ondelete="SET NULL"))
    quote_ref: Mapped[str | None] = mapped_column(String(100))
    material_name: Mapped[str] = mapped_column(String(300))
    specification: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(30))
    rate: Mapped[float] = mapped_column(Float)
    available_qty: Mapped[float | None] = mapped_column(Float)
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    vendor: Mapped[Vendor] = relationship(back_populates="quotations")


class _DocMixin:
    status: Mapped[str] = mapped_column(String(20), default=DRAFT)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    superseded_by_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class PurchaseOrder(_DocMixin, Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    list_id: Mapped[int] = mapped_column(ForeignKey("procurement_lists.id", ondelete="CASCADE"))
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    po_number: Mapped[str] = mapped_column(String(60))
    delivery_address: Mapped[str | None] = mapped_column(Text)
    delivery_date: Mapped[date | None] = mapped_column(Date)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    tax_pct: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text)
    vendor: Mapped[Vendor] = relationship()
    lines: Mapped[list["POLine"]] = relationship(back_populates="po", cascade="all, delete-orphan", order_by="POLine.id")


class POLine(Base):
    __tablename__ = "po_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"))
    procurement_item_id: Mapped[int] = mapped_column(ForeignKey("procurement_items.id"))
    quotation_id: Mapped[int | None] = mapped_column(ForeignKey("quotations.id", ondelete="SET NULL"))
    description: Mapped[str] = mapped_column(String(300))
    specification: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30))
    rate: Mapped[float] = mapped_column(Float)
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class Package(_DocMixin, Base):
    """Material clubbing package, optionally assigned to a contractor."""
    __tablename__ = "packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    list_id: Mapped[int] = mapped_column(ForeignKey("procurement_lists.id", ondelete="CASCADE"))
    package_code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    contractor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    arrangement: Mapped[str] = mapped_column(String(30), default="supply_only")  # supply_only | supply_and_execute
    scope_of_work: Mapped[str | None] = mapped_column(Text)
    execution_amount: Mapped[float] = mapped_column(Float, default=0.0)  # labour / execution value
    tax_pct: Mapped[float] = mapped_column(Float, default=0.0)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    completion_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    contractor: Mapped[Vendor | None] = relationship()
    lines: Mapped[list["PackageLine"]] = relationship(back_populates="package", cascade="all, delete-orphan",
                                                      order_by="PackageLine.id")
    milestones: Mapped[list["Milestone"]] = relationship(back_populates="package", cascade="all, delete-orphan",
                                                         order_by="Milestone.id")


class PackageLine(Base):
    __tablename__ = "package_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))
    procurement_item_id: Mapped[int] = mapped_column(ForeignKey("procurement_items.id"))
    description: Mapped[str] = mapped_column(String(300))
    specification: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30))
    rate: Mapped[float] = mapped_column(Float, default=0.0)
    package: Mapped[Package] = relationship(back_populates="lines")


class Milestone(Base):
    __tablename__ = "milestones"
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    due_date: Mapped[date | None] = mapped_column(Date)
    payment_pct: Mapped[float] = mapped_column(Float, default=0.0)
    package: Mapped[Package] = relationship(back_populates="milestones")


class ContractTemplate(Base):
    __tablename__ = "contract_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # supply | contractor
    body: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Contract(_DocMixin, Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # supply | contractor
    contract_number: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(300))
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id", ondelete="SET NULL"))
    package_id: Mapped[int | None] = mapped_column(ForeignKey("packages.id", ondelete="SET NULL"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("contract_templates.id", ondelete="SET NULL"))
    party_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    body: Mapped[str] = mapped_column(Text)
    schedule: Mapped[list] = mapped_column(JSON, default=list)  # material schedule snapshot
    party: Mapped[Vendor | None] = relationship()


class DocumentEvent(Base):
    """Audit trail for every status change, edit and revision."""
    __tablename__ = "document_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(Integer)
    doc_type: Mapped[str] = mapped_column(String(20))  # po | package | contract
    doc_id: Mapped[int] = mapped_column(Integer)
    doc_label: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    actor: Mapped[str | None] = mapped_column(String(100))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
