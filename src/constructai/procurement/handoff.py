"""Handoff from ConstructAI BOM approval to procurement.

Called only after a reviewer has explicitly approved a ready BOM. Creates (or reuses) the
procurement project for the drawing, stores the approved BOM as a read-only imported snapshot
with the approver's name, and opens an editable procurement list copied from it.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from . import models as m
from .db import SessionLocal
from .routers.procurement import _trace_note
from .services import parse_bom_file


def handoff_approved_bom(bom_payload: dict, *, boq_id: str, extraction_id: str | None, source_name: str,
                         approved_by: str, comment: str | None = None, project_name: str | None = None,
                         project_code: str | None = None, procurement_project_id: int | None = None) -> dict:
    payload = json.dumps({"boq_id": boq_id, "source_name": source_name, "bom": bom_payload}).encode()
    parsed = parse_bom_file("constructai_bom.json", payload)  # same validation as a file import
    meta = parsed["meta"]
    with SessionLocal() as db:
        project = None
        if procurement_project_id:
            project = db.get(m.Project, procurement_project_id)
            if project is None:
                raise LookupError(f"Procurement project {procurement_project_id} was not found")
        elif extraction_id:
            project = db.scalar(select(m.Project).where(m.Project.external_ref == extraction_id))
        if project is None:
            project = m.Project(name=project_name or source_name, code=project_code or None,
                                external_ref=extraction_id)
            db.add(project)
            db.flush()
        revision = 1 + len(db.scalars(select(m.Bom.id).where(m.Bom.project_id == project.id,
                                                              m.Bom.external_ref == boq_id)).all())
        bom = m.Bom(project_id=project.id, name=f"{meta.get('bom_name') or 'ConstructAI BOM'} (approval {revision})",
                    source="imported", source_format="constructai_bom", source_file=f"ConstructAI BOQ {boq_id}",
                    external_ref=boq_id, source_total=meta.get("source_total"),
                    import_warnings=meta.get("warnings") or [], approved_by=approved_by,
                    approved_at=m.now(), approval_comment=comment)
        bom.items = [m.BomItem(**it) for it in parsed["items"]]
        db.add(bom)
        db.flush()
        plist = m.ProcurementList(project_id=project.id, bom_id=bom.id, name=f"Procurement - {bom.name}")
        plist.items = [m.ProcurementItem(bom_item_id=i.id, item_code=i.item_code, name=i.name,
                                         specification=i.specification, category=i.category, quantity=i.quantity,
                                         unit=i.unit, est_rate=i.est_rate, notes=_trace_note(i)) for i in bom.items]
        db.add(plist)
        db.commit()
        return {"project_id": project.id, "project_name": project.name, "bom_id": bom.id, "list_id": plist.id,
                "item_count": len(bom.items), "bom_name": bom.name}
