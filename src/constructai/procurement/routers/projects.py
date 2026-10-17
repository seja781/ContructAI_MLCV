"""Projects and BOM import / manual BOM creation."""
from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..db import get_db
from ..schemas import ProjectIn, ManualBomIn, ItemIn, ser_project, ser_bom, ser_bom_item
from ..services import err, get_or_404, parse_bom_file

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    return [ser_project(p) for p in db.scalars(select(m.Project).order_by(m.Project.id.desc()))]


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db)):
    p = m.Project(**body.model_dump())
    db.add(p)
    db.commit()
    return ser_project(p)


@router.get("/projects/{pid}")
def get_project(pid: int, db: Session = Depends(get_db)):
    return ser_project(get_or_404(db, m.Project, pid, "Project"))


@router.put("/projects/{pid}")
def update_project(pid: int, body: ProjectIn, db: Session = Depends(get_db)):
    p = get_or_404(db, m.Project, pid, "Project")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return ser_project(p)


@router.delete("/projects/{pid}", status_code=204)
def delete_project(pid: int, db: Session = Depends(get_db)):
    p = get_or_404(db, m.Project, pid, "Project")
    db.query(m.DocumentEvent).filter(m.DocumentEvent.project_id == pid).delete()
    db.delete(p)
    db.commit()


# ------------------------------------------------------------------ BOMs
@router.get("/projects/{pid}/boms")
def list_boms(pid: int, db: Session = Depends(get_db)):
    get_or_404(db, m.Project, pid, "Project")
    return [ser_bom(b, items=False) for b in db.scalars(select(m.Bom).where(m.Bom.project_id == pid).order_by(m.Bom.id))]


@router.post("/projects/{pid}/boms/import", status_code=201)
async def import_bom(pid: int, file: UploadFile = File(...), name: str | None = Form(None),
                     db: Session = Depends(get_db)):
    get_or_404(db, m.Project, pid, "Project")
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        err(413, "File too large (max 5 MB)")
    parsed = parse_bom_file(file.filename or "", content)
    meta = parsed["meta"]
    bom = m.Bom(project_id=pid, name=name or meta.get("bom_name") or f"Imported BOM - {file.filename}",
                source="imported", source_format=meta.get("format"), source_file=file.filename,
                external_ref=meta.get("external_ref"), source_total=meta.get("source_total"),
                import_warnings=meta.get("warnings") or [])
    bom.items = [m.BomItem(**it) for it in parsed["items"]]
    db.add(bom)
    db.commit()
    return ser_bom(bom)


@router.post("/projects/{pid}/boms/manual", status_code=201)
def create_manual_bom(pid: int, body: ManualBomIn, db: Session = Depends(get_db)):
    get_or_404(db, m.Project, pid, "Project")
    bom = m.Bom(project_id=pid, name=body.name, source="manual_test")
    bom.items = [m.BomItem(**it.model_dump(exclude={"notes"})) for it in body.items]
    db.add(bom)
    db.commit()
    return ser_bom(bom)


@router.get("/boms/{bid}")
def get_bom(bid: int, db: Session = Depends(get_db)):
    return ser_bom(get_or_404(db, m.Bom, bid, "BOM"))


@router.delete("/boms/{bid}", status_code=204)
def delete_bom(bid: int, db: Session = Depends(get_db)):
    """Procurement lists created from this BOM keep their copied items."""
    bom = get_or_404(db, m.Bom, bid, "BOM")
    db.delete(bom)
    db.commit()


def _manual_bom(db, bid):
    bom = get_or_404(db, m.Bom, bid, "BOM")
    if bom.source == "imported":
        err(409, "Imported BOMs are read-only snapshots. Edit the procurement list instead.")
    return bom


@router.post("/boms/{bid}/items", status_code=201)
def add_bom_item(bid: int, body: ItemIn, db: Session = Depends(get_db)):
    bom = _manual_bom(db, bid)
    it = m.BomItem(bom_id=bom.id, **body.model_dump(exclude={"notes"}))
    db.add(it)
    db.commit()
    return ser_bom_item(it)


@router.put("/boms/{bid}/items/{iid}")
def update_bom_item(bid: int, iid: int, body: ItemIn, db: Session = Depends(get_db)):
    _manual_bom(db, bid)
    it = get_or_404(db, m.BomItem, iid, "BOM item")
    if it.bom_id != bid:
        err(404, "Item not in this BOM")
    for k, v in body.model_dump(exclude={"notes"}).items():
        setattr(it, k, v)
    db.commit()
    return ser_bom_item(it)


@router.delete("/boms/{bid}/items/{iid}", status_code=204)
def delete_bom_item(bid: int, iid: int, db: Session = Depends(get_db)):
    _manual_bom(db, bid)
    it = get_or_404(db, m.BomItem, iid, "BOM item")
    if it.bom_id != bid:
        err(404, "Item not in this BOM")
    db.delete(it)
    db.commit()
