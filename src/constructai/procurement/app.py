"""Procurement sub-application, mounted by the ConstructAI backend at /procurement.

API:  /procurement/api/...      UI: /procurement/ui/
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select, text

from . import models as m
from .db import Base, engine, SessionLocal
from .routers import projects, procurement, vendors, purchase_orders, packages, contracts, history

PKG = Path(__file__).resolve().parent
UI = PKG / "ui"
TEMPLATES = PKG / "templates"
STARTER_TEMPLATES = [("Standard Supply Agreement (starter)", "supply", "supply_contract.j2"),
                     ("Standard Contractor Agreement (starter)", "contractor", "contractor_agreement.j2")]


def _add_missing_columns():
    """Additive upgrade: add new nullable columns to tables created by an older version (keeps all data)."""
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing or not col.nullable:
                continue
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'))


def init_db():
    """Create tables, upgrade older databases, and load the editable starter contract templates on first run."""
    Base.metadata.create_all(engine)
    _add_missing_columns()
    with SessionLocal() as db:
        if not db.scalar(select(m.ContractTemplate.id).limit(1)):
            for name, kind, fn in STARTER_TEMPLATES:
                p = TEMPLATES / fn
                if p.exists():
                    db.add(m.ContractTemplate(name=name, kind=kind, body=p.read_text(encoding="utf-8")))
            db.commit()


def create_procurement_app() -> FastAPI:
    init_db()
    app = FastAPI(title="ConstructAI Procurement", version="0.2.0")
    for r in (projects, procurement, vendors, purchase_orders, packages, contracts, history):
        app.include_router(r.router)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta():
        return {"statuses": m.STATUSES, "editable_statuses": sorted(m.EDITABLE_STATUSES)}

    app.mount("/ui/static", StaticFiles(directory=UI), name="procurement-static")

    @app.get("/ui", include_in_schema=False)
    def ui_redirect():
        return RedirectResponse(url="ui/")

    @app.get("/ui/", include_in_schema=False)
    def ui_index():
        return FileResponse(UI / "index.html")

    return app
