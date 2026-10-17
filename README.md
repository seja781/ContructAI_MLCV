# ConstructAI_MLCV

Milestone 1 foundation for the drawing-to-BOQ and material workflow.

## Backend

```powershell
python -m pip install -e ".[dev]"
uvicorn app.backend.main:app --reload
```

The API exposes `GET /health`, drawing extraction, and server-enforced extraction and BOQ review gates.

## OpenCV floor-plan extraction

The project now includes a reusable vision module at `src/constructai/extraction/vision.py`.

### What it does

- loads a floor-plan image with OpenCV
- converts to grayscale
- applies thresholding and Canny edge detection
- detects contours and classifies likely construction objects such as walls, doors, windows, columns, and beams
- returns structured `ExtractedElement` values compatible with the existing extraction contract
- preserves `source_page` and `source_reference` metadata
- keeps uncertain detections low-confidence and ready for human review
- does not invent missing dimensions or quantities

### Important limitation

A fully trained YOLO object detector or a real labeled construction-drawing dataset is not bundled in this repository. The system includes a YOLO-ready integration class, but it will only work after you provide:

- a trained model weights file (`.weights` or a PyTorch YOLO export)
- a matching config file
- a names file with class labels
- a labeled floor-plan dataset for training or fine-tuning

This is intentional: the code does not pretend that the vision model is already fully trained or accurate for production use. OpenCV contour candidates are not a complete plan-recognition model and cannot guarantee that all rooms, walls, doors, or windows are detected; extracted lists require comparison against the source drawing.

### Optional OCR support

If `pytesseract` is installed, the detector can try to read labels and dimension text from detected regions. If OCR is not available, the code leaves those fields empty and relies on human review instead of guessing.

```powershell
python -m pip install -e ".[dev,vision]"
```

## Run the extraction endpoint locally

```powershell
$env:PYTHONPATH = "src"
uvicorn app.backend.main:app --reload
```

Then upload a PDF, SVG, or image floor-plan drawing to `POST /extractions` using the `file` multipart field. The sample fixture is `data/sample/sample_drawing.svg`.

Each extraction response includes an `extraction_id`. Correct detections with `PATCH /extractions/{extraction_id}/elements/{index}`, then explicitly approve or reject each element with `POST /extractions/{extraction_id}/elements/{index}/review` and a `{"decision":"approved"}` or `{"decision":"rejected"}` body. Confidence scores do not approve detections, and OCR-derived dimensions remain marked for review.

Generate a BOQ with `POST /extractions/{extraction_id}/boq`. BOQ edits use `PATCH /boqs/{boq_id}/items/{item_number}`; transition through `POST /boqs/{boq_id}/submit` and `POST /boqs/{boq_id}/approve` for final approval. Review sessions are held in process memory for this prototype and are lost when the backend restarts.

After BOQ approval, the frontend can generate a materials BOM. Unmatched and low-confidence BOQ mappings block material generation until a reviewer explicitly selects a same-unit recipe. Recipes requiring thickness also block until a millimetre thickness is entered explicitly in the BOQ description; catalog reference values are never substituted for a missing drawing dimension. Manual selections are identified in the mapping results and require human confirmation. The application does not infer construction material from location-only descriptions. Catalog recipes and material rates remain suggested construction data and must be verified for the project before procurement.

## BOM rules (data-driven)

All BOM engineering rules (thickness ranges, opening deductions, volume allowances, scope checks, conflicts,
matching thresholds, countable units) live in `data/` and are documented in `data/BOM_RULES.md`.
Edit those files to change behaviour — no code changes are needed.

## Step 06 — BOM approval and procurement (human-in-the-loop)

After the BOM is generated (step 05) and has no blocking issues:

1. On the BOM screen, fill **Approve BOM for procurement**: approver name (required), procurement project name/code,
   optional comment. If the BOM has warnings (e.g. missing plaster/steel scope), tick the acknowledgement box.
2. The server regenerates the BOM from the approved BOQ and hands it to procurement: a read-only BOM snapshot
   (with approver and comment) and an editable procurement list are created. Procurement quantity = `Order qty`.
3. Step **06 Procurement** opens inside the app: vendors, quotation comparison, purchase orders (one per vendor),
   material packages / contractor assignment, contract drafts from editable templates, and PDF export.
   Every PO, package and contract has its own Draft → Pending review → Approved → Finalized workflow; approved
   documents change only through revisions. Nothing is signed or sent automatically.

Reopening the BOQ or changing a recipe mapping cancels the BOM approval; approve again to send a new snapshot
(earlier procurement data is kept).

API: `POST /boqs/{boq_id}/bom/approve`, `GET /boqs/{boq_id}/bom/approval`, procurement under `/procurement/api/...`,
procurement UI at `/procurement/ui/` (opened inside step 06; also usable in its own tab).

Procurement data is stored in SQLite at `var/procurement.db` (created automatically). Set `PROC_DB_URL` to use
another database. Delete the file to reset procurement data.

## Frontend

```powershell
cd app/frontend
npm install
npm run dev
```

## Tests

```powershell
pytest
```

## Structure

- `app/backend`: FastAPI application entry point.
- `app/frontend`: React/Vite frontend entry point for the milestone workflow.
- `src/constructai/extraction`: extraction contracts, text providers, and reusable vision pipeline.
- `src/constructai/boq`: validated BOQ data contracts and quantity logic.
- `src/constructai/agents`: BOQ-to-material mapping (rules in `data/`, see `data/BOM_RULES.md`).
- `src/constructai/procurement`: step 06 procurement, vendor management, POs, packages, contracts (API + UI).
- `tests`: focused backend and vision tests.

## Human-in-the-loop workflow

The project is intentionally designed so that a human reviews outputs after each major step:

- upload review
- extraction review
- validation review
- BOQ review
- material mapping review
- BOM approval for procurement
- purchase order / package / contract review and approval

This prevents uncertain detections or missing values from silently propagating into the downstream estimate.
