import logging
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from constructai.boq.generator import BOQGenerator
from constructai.boq.review import BOQItemEdit, BOQReviewService
from constructai.boq.schemas import BOQ, BOQStatus, ValidationSeverity
from constructai.boq.validation import BOQValidator
from constructai.extraction.schemas import (
    Dimensions,
    ElementReviewStatus,
    ExtractedElement,
    ExtractionResult,
)
from constructai.extraction.agent import ExtractionAgent
from constructai.extraction.text_providers import (
    PyMuPDFTextProvider,
    SvgTextProvider,
)
from constructai.documents.renderer import SUPPORTED_DRAWING_EXTENSIONS
from constructai.extraction.local_provider import LocalExtractionProvider
from constructai.extraction.ocr import OCRUnavailableError, tesseract_status
from constructai.agents.mapping_agent import MappingAgent
from constructai.procurement.app import create_procurement_app
from constructai.procurement.handoff import handoff_approved_bom


try:  # optional .env support (TESSERACT_CMD, log level, OCR settings)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=os.getenv("CONSTRUCTAI_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("constructai.api")

app = FastAPI(
    title="ConstructAI_MLCV API",
    version="0.1.0",
)

# Step 06 — procurement & vendor contracts (API: /procurement/api/..., UI: /procurement/ui/)
app.mount("/procurement", create_procurement_app())

# Uploaded drawings go through the verified extraction pipeline: SVG/PDF text
# or Tesseract OCR -> dimension parsing -> label association -> verified scale
# calibration. Nothing is estimated or defaulted.
extraction_agent = ExtractionAgent(
    [PyMuPDFTextProvider(), SvgTextProvider()],
    extraction_provider=LocalExtractionProvider(),
)

boq_generator = BOQGenerator()
boq_review_service = BOQReviewService()
boq_validator = BOQValidator()
mapping_agent = MappingAgent()
extraction_sessions: dict[str, ExtractionResult] = {}
extraction_revisions: dict[str, int] = {}
boq_sessions: dict[str, BOQ] = {}
boq_extraction_revisions: dict[str, int] = {}
boq_recipe_overrides: dict[str, dict[int, str]] = {}
bom_approvals: dict[str, dict] = {}  # boq_id -> human BOM approval + procurement handoff record


class ExtractedElementEdit(BaseModel):
    element_type: str | None = None
    host_element_id: str | None = None
    description: str | None = None
    location: str | None = None
    dimensions: Dimensions | None = None
    quantity: Decimal | None = Field(default=None, ge=0)
    unit: str | None = None
    specification: str | None = None
    source_page: int | None = Field(default=None, ge=1)


class ElementReviewDecision(BaseModel):
    decision: Literal["approved", "rejected"]


class BOMApproval(BaseModel):
    approved_by: str = Field(min_length=1, max_length=100)
    comment: str | None = None
    acknowledge_warnings: bool = False
    project_name: str | None = None
    project_code: str | None = None
    procurement_project_id: int | None = None  # add to an existing procurement project instead of a new one


class BOQRecipeMapping(BaseModel):
    item_number: int = Field(ge=1)
    work_key: str


def _get_extraction(extraction_id: str) -> ExtractionResult:
    extraction = extraction_sessions.get(extraction_id)
    if extraction is None:
        raise HTTPException(status_code=404, detail="Extraction review session was not found")
    return extraction


def _approved_only(extraction: ExtractionResult) -> ExtractionResult:
    """Rejected elements never enter the BOQ; only explicitly approved elements do."""
    return extraction.model_copy(update={"elements": [
        element for element in extraction.elements if element.review_status is ElementReviewStatus.APPROVED
    ]})


def _require_approved_extraction(extraction: ExtractionResult) -> None:
    if not extraction.elements:
        raise HTTPException(status_code=409, detail="An empty extraction cannot generate a BOQ")
    if any(element.review_status is ElementReviewStatus.PENDING for element in extraction.elements):
        raise HTTPException(
            status_code=409,
            detail="Every extracted element must be explicitly approved or rejected; rejected elements do not enter the BOQ",
        )
    if not any(element.review_status is ElementReviewStatus.APPROVED for element in extraction.elements):
        raise HTTPException(status_code=409, detail="Approve at least one element to generate a BOQ")
    errors = [
        issue for issue in boq_validator.validate_extraction(_approved_only(extraction))
        if issue.severity is ValidationSeverity.ERROR
    ]
    if errors:
        details = "; ".join(f"{issue.field}: {issue.message}" for issue in errors)
        raise HTTPException(
            status_code=422,
            detail=f"Resolve extraction validation errors before generating the BOQ: {details}",
        )


def _require_current_boq(boq: BOQ) -> ExtractionResult:
    if not boq.boq_id or not boq.extraction_id:
        raise HTTPException(status_code=409, detail="BOQ is not linked to a reviewed extraction")
    extraction = _get_extraction(boq.extraction_id)
    if boq_extraction_revisions.get(boq.boq_id) != extraction_revisions.get(boq.extraction_id):
        raise HTTPException(status_code=409, detail="The source extraction changed; regenerate the BOQ")
    _require_approved_extraction(extraction)
    return extraction


def _create_boq(extraction: ExtractionResult) -> BOQ:
    _require_approved_extraction(extraction)
    try:
        boq = boq_generator.generate(_approved_only(extraction))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    boq_id = str(uuid4())
    boq = boq.model_copy(update={"boq_id": boq_id, "extraction_id": extraction.extraction_id})
    boq_sessions[boq_id] = boq
    if extraction.extraction_id:
        boq_extraction_revisions[boq_id] = extraction_revisions[extraction.extraction_id]
    return boq


def _json_safe(value):
    """Replace NaN/infinity (invalid JSON) with null instead of crashing."""

    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _model_json(model: BaseModel) -> dict:
    return _json_safe(jsonable_encoder(model.model_dump(mode="python"), custom_encoder={Decimal: float}))


@app.exception_handler(Exception)
async def _unhandled_error(request, error):  # last resort: always a readable message
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"Server error: {error.__class__.__name__}: {error}"})


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ocr", tags=["system"])
def ocr_health() -> JSONResponse:
    """Report whether the Tesseract OCR engine (not just pytesseract) is usable."""

    status = tesseract_status()
    return JSONResponse(status_code=200 if status.available else 503, content=status.as_dict())


@app.post("/extractions", tags=["extraction"])
async def extract_drawing(
    file: UploadFile = File(...)
) -> JSONResponse:
    """Extract structured construction elements from one PDF, SVG or image drawing."""

    original_name = Path(file.filename or "drawing").name
    suffix = Path(original_name).suffix.lower()

    if suffix not in SUPPORTED_DRAWING_EXTENSIONS:
        return JSONResponse(
            status_code=415,
            content={
                "detail": "Only PDF, SVG, and image-based floor-plan drawings are supported"
            },
        )

    content = await file.read()
    logger.info("Upload received: %s (%s bytes)", original_name, len(content))
    if not content:
        return JSONResponse(status_code=422, content={"detail": "Uploaded file is empty"})

    # A fresh temporary directory per request: no previous result is reused.
    with TemporaryDirectory() as temporary_directory:
        source = Path(temporary_directory) / f"drawing{suffix}"
        source.write_bytes(content)

        try:
            result = extraction_agent.extract(source)
        except OCRUnavailableError as error:
            logger.error("OCR unavailable: %s", error)
            return JSONResponse(status_code=503, content={"detail": str(error)})
        except ValueError as error:
            logger.warning("Extraction rejected: %s", error)
            return JSONResponse(
                status_code=422,
                content={"detail": str(error)},
            )
        except Exception as error:
            logger.exception("Extraction failed")
            return JSONResponse(
                status_code=502,
                content={"detail": f"Extraction failed: {error.__class__.__name__}: {error}"},
            )

    extraction_id = str(uuid4())
    result = result.model_copy(update={"extraction_id": extraction_id, "source_name": original_name})
    logger.info(
        "Extraction %s for %s: status=%s, %s element(s), %s annotation(s), %s unresolved",
        extraction_id, original_name, result.status, len(result.elements),
        len(result.dimension_annotations), len(result.unresolved),
    )
    extraction_sessions[extraction_id] = result
    extraction_revisions[extraction_id] = 0
    return JSONResponse(content=_model_json(result))


@app.patch("/extractions/{extraction_id}/elements/{element_index}", tags=["extraction"])
def edit_extracted_element(
    extraction_id: str,
    element_index: int,
    edit: ExtractedElementEdit,
) -> JSONResponse:
    extraction = _get_extraction(extraction_id)
    if element_index < 0 or element_index >= len(extraction.elements):
        raise HTTPException(status_code=404, detail="Extracted element was not found")

    current = extraction.elements[element_index]
    updates = edit.model_dump(exclude_unset=True)
    if "dimensions" in updates:
        updates["dimensions_source"] = "human" if updates["dimensions"] is not None else None
    updates["review_status"] = ElementReviewStatus.PENDING
    updates["review_reasons"] = list(dict.fromkeys([*current.review_reasons, "human_edit"]))
    updated = ExtractedElement.model_validate(current.model_dump() | updates)
    elements = list(extraction.elements)
    elements[element_index] = updated
    extraction = extraction.model_copy(update={"elements": elements})
    extraction_sessions[extraction_id] = extraction
    extraction_revisions[extraction_id] += 1
    return JSONResponse(content=_model_json(extraction))


@app.post("/extractions/{extraction_id}/elements/{element_index}/review", tags=["extraction"])
def review_extracted_element(
    extraction_id: str,
    element_index: int,
    decision: ElementReviewDecision,
) -> JSONResponse:
    extraction = _get_extraction(extraction_id)
    if element_index < 0 or element_index >= len(extraction.elements):
        raise HTTPException(status_code=404, detail="Extracted element was not found")

    elements = list(extraction.elements)
    element = elements[element_index]
    if decision.decision == "approved":
        missing_fields = [
            name
            for name, value in (
                ("element_type", element.element_type),
                ("description", element.description),
                ("unit", element.unit),
            )
            if not value or not value.strip()
        ]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"Enter a value for: {', '.join(missing_fields)}",
            )
    next_status = ElementReviewStatus(decision.decision)
    if element.review_status is not next_status:
        elements[element_index] = element.model_copy(update={"review_status": next_status})
        extraction = extraction.model_copy(update={"elements": elements})
        extraction_sessions[extraction_id] = extraction
        extraction_revisions[extraction_id] += 1
    return JSONResponse(content=_model_json(extraction))


@app.post("/extractions/{extraction_id}/boq", tags=["boq"])
def generate_reviewed_boq(extraction_id: str) -> JSONResponse:
    boq = _create_boq(_get_extraction(extraction_id))
    return JSONResponse(content=_model_json(boq))


@app.post("/boq", tags=["boq"])
def generate_boq(
    extraction: ExtractionResult
) -> JSONResponse:
    """Generate a BOQ from the authoritative, reviewed server-side extraction."""

    if not extraction.extraction_id:
        raise HTTPException(status_code=409, detail="Extraction must be reviewed in a server session before BOQ generation")
    reviewed_extraction = _get_extraction(extraction.extraction_id)
    boq = _create_boq(reviewed_extraction)
    return JSONResponse(content=_model_json(boq))


@app.patch("/boqs/{boq_id}/items/{item_number}", tags=["boq"])
def edit_boq_item(boq_id: str, item_number: int, edit: BOQItemEdit) -> JSONResponse:
    boq = boq_sessions.get(boq_id)
    if boq is None:
        raise HTTPException(status_code=404, detail="BOQ review session was not found")
    try:
        boq = boq_review_service.edit_item(boq, item_number, edit)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    boq_sessions[boq_id] = boq
    if boq_id in boq_recipe_overrides:
        boq_recipe_overrides[boq_id].pop(item_number, None)
    return JSONResponse(content=_model_json(boq))


@app.post("/boqs/{boq_id}/submit", tags=["boq"])
def submit_boq_for_review(boq_id: str) -> JSONResponse:
    boq = boq_sessions.get(boq_id)
    if boq is None:
        raise HTTPException(status_code=404, detail="BOQ review session was not found")
    _require_current_boq(boq)
    try:
        boq = boq_review_service.submit_for_review(boq)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    boq_sessions[boq_id] = boq
    return JSONResponse(content=_model_json(boq))


@app.post("/boqs/{boq_id}/approve", tags=["boq"])
def approve_boq(boq_id: str) -> JSONResponse:
    boq = boq_sessions.get(boq_id)
    if boq is None:
        raise HTTPException(status_code=404, detail="BOQ review session was not found")
    _require_current_boq(boq)
    try:
        boq = boq_review_service.approve(boq)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    boq_sessions[boq_id] = boq
    return JSONResponse(content=jsonable_encoder(boq, custom_encoder={Decimal: float}))


@app.post("/boqs/{boq_id}/reopen", tags=["boq"])
def reopen_approved_boq(boq_id: str) -> JSONResponse:
    boq = boq_sessions.get(boq_id)
    if boq is None:
        raise HTTPException(status_code=404, detail="BOQ review session was not found")
    _require_current_boq(boq)
    if boq.status.value != "APPROVED":
        raise HTTPException(status_code=409, detail="Only an approved BOQ can be reopened")

    reopened = boq.model_copy(update={"status": BOQStatus.DRAFT})
    boq_sessions[boq_id] = reopened
    boq_recipe_overrides.pop(boq_id, None)
    bom_approvals.pop(boq_id, None)
    return JSONResponse(content=jsonable_encoder(reopened, custom_encoder={Decimal: float}))


@app.post("/boqs/{boq_id}/bom", tags=["mapping"])
def generate_reviewed_bom(boq_id: str) -> JSONResponse:
    boq = boq_sessions.get(boq_id)
    if boq is None:
        raise HTTPException(status_code=404, detail="BOQ review session was not found")
    _require_current_boq(boq)
    if boq.status.value != "APPROVED":
        raise HTTPException(status_code=409, detail="The BOQ must be approved before generating a BOM")

    try:
        bom = mapping_agent.run(boq, boq_recipe_overrides.get(boq_id, {}))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return JSONResponse(content=jsonable_encoder(bom))


@app.get("/boqs/{boq_id}/mapping-options/{item_number}", tags=["mapping"])
def get_boq_mapping_options(boq_id: str, item_number: int) -> JSONResponse:
    boq = boq_sessions.get(boq_id)
    if boq is None:
        raise HTTPException(status_code=404, detail="BOQ review session was not found")
    _require_current_boq(boq)
    if boq.status.value != "APPROVED":
        raise HTTPException(status_code=409, detail="The BOQ must be approved before mapping recipes")
    item = next((item for item in boq.items if item.item_number == item_number), None)
    if item is None:
        raise HTTPException(status_code=404, detail="BOQ item was not found")

    return JSONResponse(
        content={
            "unit": item.unit,
            "recipes": mapping_agent.recipe_options(item.unit),
        }
    )


@app.put("/boqs/{boq_id}/mapping", tags=["mapping"])
def set_boq_recipe_mapping(boq_id: str, mapping: BOQRecipeMapping) -> JSONResponse:
    boq = boq_sessions.get(boq_id)
    if boq is None:
        raise HTTPException(status_code=404, detail="BOQ review session was not found")
    _require_current_boq(boq)
    if boq.status.value != "APPROVED":
        raise HTTPException(status_code=409, detail="The BOQ must be approved before mapping recipes")

    item = next(
        (item for item in boq.items if item.item_number == mapping.item_number),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="BOQ item was not found")
    work = mapping_agent.works.get(mapping.work_key)
    if work is None or work["unit"] != item.unit:
        raise HTTPException(
            status_code=422,
            detail=f"Selected recipe is not available for BOQ unit '{item.unit}'",
        )

    boq_recipe_overrides.setdefault(boq_id, {})[mapping.item_number] = mapping.work_key
    bom_approvals.pop(boq_id, None)  # a mapping change invalidates an earlier BOM approval
    return JSONResponse(
        content={
            "item_number": mapping.item_number,
            "work_key": mapping.work_key,
            "matched_work_item": work["text"],
        }
    )


@app.post("/mapping", tags=["mapping"])
def generate_bom(
    boq: BOQ
) -> JSONResponse:
    """Generate BOM by mapping materials from the BOQ."""

    try:
        bom = mapping_agent.run(boq)
    except ValueError as error:
        return JSONResponse(
            status_code=422,
            content={"detail": str(error)},
        )

    return JSONResponse(
        content=jsonable_encoder(
            bom,
            custom_encoder={Decimal: float},
        )
    )


# --------------------------------------------------------------------------- step 06: BOM approval -> procurement
@app.post("/boqs/{boq_id}/bom/approve", tags=["procurement"], status_code=201)
def approve_bom_for_procurement(boq_id: str, approval: BOMApproval) -> JSONResponse:
    """Human-in-the-loop gate: a named reviewer approves the BOM, which is then handed to procurement.

    The BOM is regenerated on the server from the approved BOQ (client data is never trusted).
    Blocked BOMs cannot be approved; warnings must be explicitly acknowledged.
    """
    boq = boq_sessions.get(boq_id)
    if boq is None:
        raise HTTPException(status_code=404, detail="BOQ review session was not found")
    _require_current_boq(boq)
    if boq.status.value != "APPROVED":
        raise HTTPException(status_code=409, detail="The BOQ must be approved before the BOM can be approved")
    approved_by = approval.approved_by.strip()
    if not approved_by:
        raise HTTPException(status_code=422, detail="Enter the name of the person approving the BOM")
    if boq_id in bom_approvals:
        raise HTTPException(status_code=409, detail="This BOM is already approved and handed to procurement. "
                                                    "Reopen the BOQ to make changes and approve again.")
    try:
        bom = mapping_agent.run(boq, boq_recipe_overrides.get(boq_id, {}))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if bom.validation["status"] != "ready" or not bom.lines:
        raise HTTPException(status_code=409, detail="The BOM has blocking review items. Resolve them before approval.")
    warnings = bom.validation["warnings"]
    if warnings and not approval.acknowledge_warnings:
        raise HTTPException(status_code=409, detail=f"Review and acknowledge {len(warnings)} BOM warning(s) "
                                                    "before approving for procurement.")
    try:
        handoff = handoff_approved_bom(
            jsonable_encoder(bom), boq_id=boq_id, extraction_id=boq.extraction_id, source_name=boq.source_name,
            approved_by=approved_by, comment=approval.comment, project_name=approval.project_name,
            project_code=approval.project_code, procurement_project_id=approval.procurement_project_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    record = {
        "boq_id": boq_id, "approved_by": approved_by, "comment": approval.comment,
        "warnings_acknowledged": warnings, "material_count": len(bom.lines), "total_cost": bom.total_cost,
        "procurement": handoff,
    }
    bom_approvals[boq_id] = record
    return JSONResponse(status_code=201, content=jsonable_encoder(record))


@app.get("/boqs/{boq_id}/bom/approval", tags=["procurement"])
def get_bom_approval(boq_id: str) -> JSONResponse:
    boq = boq_sessions.get(boq_id)
    record = bom_approvals.get(boq_id)
    if boq is None or record is None:
        raise HTTPException(status_code=404, detail="This BOM has not been approved for procurement")
    try:
        _require_current_boq(boq)
    except HTTPException:
        bom_approvals.pop(boq_id, None)  # source changed after approval
        raise HTTPException(status_code=404, detail="The source changed after approval; approve the BOM again")
    return JSONResponse(content=jsonable_encoder(record))
