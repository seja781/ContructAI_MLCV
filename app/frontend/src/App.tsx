import { ChangeEvent, useEffect, useRef, useState } from "react";

type Screen = "upload" | "extraction" | "boq" | "approval" | "bom" | "procurement";
type BOQStatus = "DRAFT" | "REVIEW" | "APPROVED";
type ElementReviewStatus = "pending" | "approved" | "rejected";

type DimensionSet = {
  length?: string;
  width?: string;
  height?: string;
};

type ExtractedElement = {
  element_type: string;
  element_id?: string;
  description: string;
  location?: string;
  dimensions?: DimensionSet;
  quantity?: number;
  unit?: string;
  specification?: string;
  source_page?: number;
  confidence: number;
  dimensions_source?: string;
  review_status: ElementReviewStatus;
  review_reasons: string[];
  raw_dimension_text?: string | null;
  original_unit?: string | null;
  measurement_source?: string | null;
};

type ScaleCalibration = {
  page: number;
  status: "verified" | "single_reference" | "conflicting" | "unavailable";
  pixels_per_metre?: number | null;
  references: { raw_text: string; agrees_with_consensus: boolean }[];
  reason: string;
};

type ExtractionResult = {
  source_name: string;
  elements: ExtractedElement[];
  extraction_id: string;
  status?: "complete" | "partial" | "no_measurements";
  extraction_method?: string | null;
  warnings?: string[];
  unresolved?: { text: string; reason: string; source_page?: number | null }[];
  scale_calibration?: ScaleCalibration[];
};

type BOQItem = {
  item_number: number;
  description: string;
  element_type: string;
  quantity: number;
  unit: string;
  calculation: string;
  source_page?: number;
  confidence: number;
  dimensions?: DimensionSet;
  dimensions_source?: string;
  element_id?: string;
  item_code?: string;
};

type BOQ = {
  source_name: string;
  status: BOQStatus;
  items: BOQItem[];
  validation_issues: { severity: "error" | "warning"; field: string; message: string }[];
  boq_id: string;
  extraction_id: string;
};

type BOMResult = {
  lines: {
    Material: string;
    Category: string;
    Unit: string;
    "Net qty": number;
    "Wastage %": number;
    "Order qty": number;
    "Rate (Rs)": number;
    "Cost (Rs)": number;
    "Used in BOQ items": string;
  }[];
  mapping: {
    "BOQ item": string;
    Description: string;
    "Matched work item": string;
    Confidence: number;
    Status: "OK" | "REVIEW" | "NO MATCH" | "MANUAL";
  }[];
  warnings: string[];
  validation: {
    status: "ready" | "blocked";
    errors: string[];
    warnings: string[];
  };
  total_cost: number;
  by_category: { Category: string; "Cost (Rs)": number }[];
  deductions?: { "BOQ item": string; "Opening BOQ item": string; Opening: string; "Opening area each (m2)": number; Count: number; "Deducted (m2)": number; Rule: string }[];
  suggestions?: { "For BOQ item": string; "Missing work": string; "Suggested quantity": number | null; Unit: string; Basis: string }[];
};

type BOMApprovalRecord = {
  boq_id: string;
  approved_by: string;
  comment: string | null;
  warnings_acknowledged: string[];
  material_count: number;
  total_cost: number;
  procurement: { project_id: number; project_name: string; bom_id: number; list_id: number; item_count: number; bom_name: string };
};

type BOMApprovalForm = { approved_by: string; comment: string; acknowledge_warnings: boolean; project_name: string; project_code: string; procurement_project_id: number | null };
type ProcurementProject = { id: number; name: string; code: string | null };

type MappingRecipe = {
  work_key: string;
  description: string;
  unit: string;
};

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api";
const unitOptions = ["m", "lm", "m2", "m3", "pcs", "nos", "unit", "kg", "ton", "l", "litre"];
const steps: { id: Screen; label: string; number: string }[] = [
  { id: "upload", label: "Upload drawing", number: "01" },
  { id: "extraction", label: "Extraction results", number: "02" },
  { id: "boq", label: "BOQ review", number: "03" },
  { id: "approval", label: "Approval", number: "04" },
  { id: "bom", label: "BOM & mapping", number: "05" },
  { id: "procurement", label: "Procurement", number: "06" },
];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `Request failed with status ${response.status}`);
  }
  return response.json();
}

function App() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [screen, setScreen] = useState<Screen>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [extraction, setExtraction] = useState<ExtractionResult | null>(null);
  const [boq, setBOQ] = useState<BOQ | null>(null);
  const [bom, setBOM] = useState<BOMResult | null>(null);
  const [bomApproval, setBOMApproval] = useState<BOMApprovalRecord | null>(null);
  const [loading, setLoading] = useState<"extract" | "boq" | "review" | "bom" | null>(null);
  const [error, setError] = useState("");

  const extract = async (selectedFile: File) => {
    setFile(selectedFile);
    setError("");
    setBOQ(null);
    setBOM(null);
    setBOMApproval(null);
    setLoading("extract");
    const form = new FormData();
    form.append("file", selectedFile);
    try {
      const result = await request<ExtractionResult>("/extractions", { method: "POST", body: form });
      setExtraction(result);
      setScreen("extraction");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Extraction failed");
      setExtraction(null);
    } finally {
      setLoading(null);
    }
  };

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFile = event.target.files?.[0];
    if (selectedFile) void extract(selectedFile);
  };

  const updateExtractionElement = (index: number, field: string, value: string) => {
    setExtraction((current) => {
      if (!current) return current;
      const next = [...current.elements];
      const item = { ...next[index], review_status: "pending" as const, review_reasons: [...new Set([...next[index].review_reasons, "human_edit"])] };

      if (field.startsWith("dimensions.")) {
        const dimensionField = field.replace("dimensions.", "") as "length" | "width" | "height";
        const nextDimensions = { ...(item.dimensions ?? {}) };
        const trimmed = value.trim();
        nextDimensions[dimensionField] = trimmed === "" ? undefined : trimmed;
        item.dimensions = Object.values(nextDimensions).some((dimension) => dimension !== undefined)
          ? nextDimensions
          : undefined;
      } else if (field === "quantity") {
        const trimmed = value.trim();
        item.quantity = trimmed === "" ? undefined : Number(value);
      } else if (field === "source_page") {
        const trimmed = value.trim();
        item.source_page = trimmed === "" ? undefined : Number(value);
      } else if (field === "confidence") {
        item.confidence = Number(value);
      } else if (field === "element_type" || field === "element_id" || field === "description" || field === "location" || field === "unit" || field === "specification") {
        item[field as keyof typeof item] = value as never;
      }

      next[index] = item;
      return { ...current, elements: next };
    });
    setBOM(null);
    setBOMApproval(null);
    setBOQ(null);
  };

  const reviewElement = async (index: number, decision: "approved" | "rejected") => {
    if (!extraction) return;
    setLoading("review");
    setError("");
    try {
      const element = extraction.elements[index];
      await request<ExtractionResult>(`/extractions/${extraction.extraction_id}/elements/${index}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...element,
          dimensions: element.dimensions ?? null,
          quantity: element.quantity ?? null,
          location: element.location ?? null,
          unit: element.unit ?? null,
          specification: element.specification ?? null,
          source_page: element.source_page ?? null,
        }),
      });
      const result = await request<ExtractionResult>(`/extractions/${extraction.extraction_id}/elements/${index}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      setExtraction(result);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Element review failed");
    } finally {
      setLoading(null);
    }
  };

  const generateBOQ = async () => {
    if (!extraction) {
      setError("Upload a drawing and review the extracted data before generating a BOQ.");
      return;
    }
    if (extraction.elements.length === 0) {
      setError("The extraction result is empty. Correct or re-upload the drawing.");
      return;
    }
    if (extraction.elements.some((element) => element.review_status !== "approved")) {
      setError("Approve every extracted element before generating the BOQ.");
      return;
    }

    setError("");
    setLoading("boq");
    try {
      const result = await request<BOQ>(`/extractions/${extraction.extraction_id}/boq`, {
        method: "POST",
      });
      setBOQ(result);
      setScreen("boq");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "BOQ generation failed");
    } finally {
      setLoading(null);
    }
  };

  const updateItem = (itemNumber: number, field: "description" | "element_type" | "quantity" | "unit", value: string) => {
    setBOQ((current) => current && {
      ...current,
      items: current.items.map((item) => item.item_number === itemNumber
        ? { ...item, [field]: field === "quantity" ? Number(value) : value }
        : item),
    });
  };

  const saveBOQItem = async (itemNumber: number, field: "description" | "element_type" | "quantity" | "unit", value: string) => {
    if (!boq) return;
    try {
      const result = await request<BOQ>(`/boqs/${boq.boq_id}/items/${itemNumber}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: field === "quantity" ? Number(value) : value }),
      });
      setBOQ(result);
      setError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "BOQ edit failed");
    }
  };

  const submitForReview = async () => {
    if (!boq) return;
    try {
      const result = await request<BOQ>(`/boqs/${boq.boq_id}/submit`, { method: "POST" });
      setBOQ(result);
      setError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "BOQ submission failed");
    }
  };

  const approve = async () => {
    if (!boq) return;
    try {
      const result = await request<BOQ>(`/boqs/${boq.boq_id}/approve`, { method: "POST" });
      setBOQ(result);
      setScreen("approval");
      setError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "BOQ approval failed");
    }
  };

  const generateBOM = async () => {
    if (!boq) return;
    setError("");
    setLoading("bom");
    try {
      const result = await request<BOMResult>(`/boqs/${boq.boq_id}/bom`, { method: "POST" });
      setBOM(result);
      setScreen("bom");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "BOM generation failed");
    } finally {
      setLoading(null);
    }
  };

  const reopenBOQ = async () => {
    if (!boq) return;
    try {
      const result = await request<BOQ>(`/boqs/${boq.boq_id}/reopen`, { method: "POST" });
      setBOQ(result);
      setBOM(null);
      setBOMApproval(null);
      setScreen("boq");
      setError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not reopen approved BOQ");
    }
  };

  const mapBOQItem = async (itemNumber: number, workKey: string) => {
    if (!boq || !workKey) return;
    setError("");
    setLoading("bom");
    try {
      await request(`/boqs/${boq.boq_id}/mapping`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_number: itemNumber, work_key: workKey }),
      });
      setBOMApproval(null); // server invalidates an earlier approval when mapping changes
      const result = await request<BOMResult>(`/boqs/${boq.boq_id}/bom`, { method: "POST" });
      setBOM(result);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Manual BOQ mapping failed");
    } finally {
      setLoading(null);
    }
  };

  const approveBOM = async (form: BOMApprovalForm) => {
    if (!boq) return;
    setError("");
    setLoading("bom");
    try {
      const record = await request<BOMApprovalRecord>(`/boqs/${boq.boq_id}/bom/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, comment: form.comment || null, project_code: form.project_code || null, procurement_project_id: form.procurement_project_id }),
      });
      setBOMApproval(record);
      setScreen("procurement");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "BOM approval failed");
    } finally {
      setLoading(null);
    }
  };

  const activeStep = steps.findIndex((step) => step.id === screen);
  const canNavigate = (step: Screen) => step === "upload" || (step === "extraction" && !!extraction) || (step === "boq" && !!boq) || (step === "approval" && boq?.status === "APPROVED") || (step === "bom" && !!bom) || (step === "procurement" && !!bomApproval);

  return (
    <main className="app-shell">
      <header className="topbar">
        <button className="brand" type="button" onClick={() => setScreen("upload")} aria-label="Go to upload">
          <span className="brand-mark">CF</span>
          <span>ConstructAI_<em>MLCV</em></span>
        </button>
        <div className="topbar-meta"><span>Project Atlas</span><span className="live-dot" /> <span>Milestone 1</span></div>
      </header>

      <div className="layout">
        <aside className="sidebar">
          <div className="sidebar-heading"><span>ESTIMATOR WORKSPACE</span><strong>Drawing to BOM</strong></div>
          <nav aria-label="Workflow steps">
            {steps.map((step, index) => (
              <button key={step.id} type="button" className={`nav-step ${screen === step.id ? "active" : ""} ${index < activeStep ? "complete" : ""}`} onClick={() => canNavigate(step.id) && setScreen(step.id)} disabled={!canNavigate(step.id)}>
                <span className="step-number">{index < activeStep ? "✓" : step.number}</span><span>{step.label}</span>
              </button>
            ))}
          </nav>
          <div className="sidebar-footer"><span className="status-ring" /> <span>API connected</span><small>Local workspace</small></div>
        </aside>

        <section className="content">
          <div className="content-header">
            <div><p className="eyebrow">Technical intelligence / {steps[activeStep].number}</p><h1>{screen === "upload" ? "Start with the source drawing." : screen === "extraction" ? "Review extracted elements." : screen === "boq" ? "Shape the draft BOQ." : screen === "approval" ? "BOQ is ready for approval." : screen === "procurement" ? "Procure from the approved BOM." : "Map quantities to materials."}</h1></div>
            {boq && <span className={`status-badge ${boq.status.toLowerCase()}`}><span /> {boq.status}</span>}
          </div>

          {error && <div className="error-banner" role="alert"><strong>Something needs attention</strong><span>{error}</span><button type="button" onClick={() => setError("")} aria-label="Dismiss error">×</button></div>}

          {screen === "upload" && <UploadScreen inputRef={inputRef} file={file} loading={loading === "extract"} onFileChange={onFileChange} />}
          {screen === "extraction" && extraction && <HumanReviewExtractionScreen extraction={extraction} loading={loading === "boq" || loading === "review"} onEdit={updateExtractionElement} onReview={reviewElement} onGenerate={generateBOQ} />}
          {screen === "boq" && boq && <PersistentBOQScreen boq={boq} onEdit={updateItem} onSave={saveBOQItem} onSubmit={submitForReview} onApprove={approve} />}
          {screen === "approval" && boq && <ApprovalScreen boq={boq} loading={loading === "bom"} onBack={() => setScreen("boq")} onGenerateBOM={generateBOM} />}
          {screen === "bom" && boq && bom && <BOMScreen boq={boq} bom={bom} loading={loading === "bom"} onBack={() => setScreen("approval")} onReviewBOQ={reopenBOQ} onMapItem={mapBOQItem} onError={setError} approval={bomApproval} onApproveBOM={approveBOM} onOpenProcurement={() => setScreen("procurement")} />}
          {screen === "procurement" && bomApproval && <ProcurementScreen approval={bomApproval} onBack={() => setScreen("bom")} />}
        </section>
      </div>
    </main>
  );
}

function UploadScreen({ inputRef, file, loading, onFileChange }: { inputRef: React.RefObject<HTMLInputElement | null>; file: File | null; loading: boolean; onFileChange: (event: ChangeEvent<HTMLInputElement>) => void }) {
  return <div className="screen upload-screen"><div className="section-intro"><span className="section-index">01</span><div><h2>Upload a project drawing</h2><p>Bring in a PDF, SVG, or floor-plan image to identify measurable construction elements.</p></div></div><button className="drop-zone" type="button" onClick={() => inputRef.current?.click()}><span className="upload-icon">↑</span><strong>{loading ? "Reading drawing..." : file ? file.name : "Choose a drawing"}</strong><span>{loading ? "The extraction agent is processing your source" : "PDF, SVG, JPG, PNG, TIFF · one sheet at a time"}</span><span className="browse-label">Browse files</span></button><input ref={inputRef} className="visually-hidden" type="file" accept=".pdf,.svg,.png,.jpg,.jpeg,.bmp,.tif,.tiff,application/pdf,image/svg+xml,image/png,image/jpeg,image/bmp" onChange={onFileChange} /><div className="workflow-note"><span>Next</span><strong>Extraction results</strong><span>Detected elements stay linked to their source page and can be corrected by the reviewer.</span></div></div>;
}

function HumanReviewExtractionScreen({ extraction, loading, onEdit, onReview, onGenerate }: { extraction: ExtractionResult; loading: boolean; onEdit: (index: number, field: string, value: string) => void; onReview: (index: number, decision: "approved" | "rejected") => void; onGenerate: () => void }) {
  const approvedCount = extraction.elements.filter((element) => element.review_status === "approved").length;
  const rejectedCount = extraction.elements.filter((element) => element.review_status === "rejected").length;
  const pendingCount = extraction.elements.length - approvedCount - rejectedCount;
  const allApproved = approvedCount > 0 && pendingCount === 0; // rejected elements are left out of the BOQ
  const scale = extraction.scale_calibration?.[0];
  const scaleText = !scale ? "Not assessed" : scale.status === "verified"
    ? `Verified · ${scale.pixels_per_metre?.toFixed(1)} px/m (${scale.references.filter((r) => r.agrees_with_consensus).length} refs)`
    : { single_reference: "Not established (1 reference)", conflicting: "Not established (references disagree)", unavailable: "Not established", verified: "" }[scale.status];
  const notes = [...(extraction.warnings ?? []), ...(extraction.unresolved ?? []).map((item) => `${item.text} — ${item.reason}`)];

  return (
    <div className="screen">
      <div className="data-strip">
        <div><span>Source document</span><strong>{extraction.source_name}</strong></div>
        <div><span>Elements found</span><strong>{extraction.elements.length}</strong></div>
        <div><span>Human decisions</span><strong className={allApproved ? "good-text" : "warning-text"}>{approvedCount} approved · {rejectedCount} rejected · {extraction.elements.length - approvedCount - rejectedCount} pending</strong></div>
        <div><span>Drawing scale</span><strong className={scale?.status === "verified" ? "good-text" : "warning-text"} title={scale?.reason}>{scaleText}</strong></div>
      </div>
      <div className="section-intro compact"><span className="section-index">02</span><div><h2>Detected construction elements</h2><p>Elements are created only where a label and its dimension annotation were read from the drawing and confirmed by independent readings; nothing is estimated or filled in. Compare every row with the source drawing, add anything missing by hand, and approve or reject each element. The BOQ shows its calculation for review.</p></div></div>
      {notes.length > 0 && <div className="issues"><strong>Extraction notes — nothing below was estimated or filled in</strong>{notes.map((note, index) => <span className="warning" key={`${index}-${note}`}>{note}</span>)}</div>}
      {extraction.elements.length === 0 && <div className="issues"><strong>No measured elements were returned.</strong><span>{extraction.status === "no_measurements" ? "No dimension annotations could be read from this drawing. Nothing was guessed; check the file quality or enter elements manually." : "Readings were found but none could be tied to a room or element; see the notes above."}</span></div>}
      <div className="table-wrap"><table><thead><tr><th>#</th><th>Type</th><th>Description</th><th>Location</th><th>Length</th><th>Width</th><th>Height</th><th>Qty</th><th>Unit</th><th>Page</th><th>Review</th></tr></thead>
        <tbody>{extraction.elements.map((element, index) => <tr key={`${element.element_id ?? element.description}-${index}`}>
          <td className="number-cell">{index + 1}</td>
          <td><input value={element.element_type} onChange={(event) => onEdit(index, "element_type", event.target.value)} aria-label={`Element type ${index + 1}`} /></td>
          <td><input value={element.description} onChange={(event) => onEdit(index, "description", event.target.value)} aria-label={`Description ${index + 1}`} /></td>
          <td><input className="location-input" title={element.location ?? ""} value={element.location ?? ""} onChange={(event) => onEdit(index, "location", event.target.value)} aria-label={`Location ${index + 1}`} /></td>
          <td><input className="dimension-input" placeholder="Not detected" value={element.dimensions?.length ?? ""} onChange={(event) => onEdit(index, "dimensions.length", event.target.value)} aria-label={`Length ${index + 1}`} /></td>
          <td><input className="dimension-input" placeholder="Not detected" value={element.dimensions?.width ?? ""} onChange={(event) => onEdit(index, "dimensions.width", event.target.value)} aria-label={`Width ${index + 1}`} /></td>
          <td><input className="dimension-input" placeholder="Not detected" value={element.dimensions?.height ?? ""} onChange={(event) => onEdit(index, "dimensions.height", event.target.value)} aria-label={`Height ${index + 1}`} /></td>
          <td><input className="quantity-input" type="number" min="0" placeholder="Enter qty" value={element.quantity ?? ""} onChange={(event) => onEdit(index, "quantity", event.target.value)} aria-label={`Quantity ${index + 1}`} /></td>
          <td><select className="unit-select" value={element.unit ?? ""} onChange={(event) => onEdit(index, "unit", event.target.value)} aria-label={`Unit ${index + 1}`}>
            <option value="">Select unit</option>
            {element.unit && !unitOptions.includes(element.unit) && <option value={element.unit}>{element.unit} (unsupported)</option>}
            {unitOptions.map((unit) => <option key={unit} value={unit}>{unit}</option>)}
          </select></td>
          <td><input type="number" min="1" value={element.source_page ?? ""} onChange={(event) => onEdit(index, "source_page", event.target.value)} aria-label={`Source page ${index + 1}`} /></td>
          <td><strong className={element.review_status === "approved" ? "good-text" : "warning-text"}>{element.review_status}</strong>{element.dimensions && <small>Measurement source: {element.dimensions_source ?? "not recorded"}{element.dimensions_source === "ocr" ? " — verify against drawing" : ""}</small>}{element.raw_dimension_text && <small>Read on drawing: {element.raw_dimension_text}{element.original_unit ? ` (${element.original_unit})` : ""} → metres</small>}<div className="row-actions"><button type="button" className="secondary-button" disabled={loading} onClick={() => onReview(index, "approved")}>Approve</button><button type="button" className="secondary-button" disabled={loading} onClick={() => onReview(index, "rejected")}>Reject</button></div></td>
        </tr>)}</tbody>
      </table></div>
      <div className="action-row"><button className="primary-button" type="button" onClick={onGenerate} disabled={loading || !allApproved}>{loading ? "Generating BOQ..." : allApproved ? "Generate BOQ →" : approvedCount === 0 && pendingCount === 0 ? "Approve at least one element" : "Approve or reject every element first"}</button></div>
    </div>
  );
}

function ExtractionScreen({ extraction, approvedExtraction, loading, onEdit, onApprove, onGenerate }: { extraction: ExtractionResult; approvedExtraction: ExtractionResult | null; loading: boolean; onEdit: (index: number, field: string, value: string) => void; onApprove: () => void; onGenerate: () => void }) {
  return <div className="screen"><div className="data-strip"><div><span>Source document</span><strong>{extraction.source_name}</strong></div><div><span>Elements found</span><strong>{extraction.elements.length}</strong></div><div><span>Extraction state</span><strong className={approvedExtraction ? "good-text" : "warning-text"}>{approvedExtraction ? "Approved" : "Awaiting review"}</strong></div></div><div className="section-intro compact"><span className="section-index">02</span><div><h2>Detected construction elements</h2><p>Review, correct, and approve each extracted element before generating the BOQ.</p></div></div>{extraction.elements.length === 0 && <div className="issues"><strong>No elements were returned.</strong><span>The uploaded drawing did not produce usable extraction results. Check the file and try again.</span></div>}<div className="table-wrap"><table><thead><tr><th>#</th><th>Type</th><th>Description</th><th>Location</th><th>Length</th><th>Width</th><th>Height</th><th>Qty</th><th>Unit</th><th>Spec</th><th>Page</th><th>Confidence</th></tr></thead><tbody>{extraction.elements.map((element, index) => <tr key={`${element.element_id ?? element.description}-${index}`}><td className="number-cell">{index + 1}</td><td><input value={element.element_type} onChange={(event) => onEdit(index, "element_type", event.target.value)} aria-label={`Element type ${index + 1}`} /></td><td><input value={element.description} onChange={(event) => onEdit(index, "description", event.target.value)} aria-label={`Description ${index + 1}`} /></td><td><input value={element.location ?? ""} onChange={(event) => onEdit(index, "location", event.target.value)} aria-label={`Location ${index + 1}`} /></td><td><input value={element.dimensions?.length ?? ""} onChange={(event) => onEdit(index, "dimensions.length", event.target.value)} aria-label={`Length ${index + 1}`} /></td><td><input value={element.dimensions?.width ?? ""} onChange={(event) => onEdit(index, "dimensions.width", event.target.value)} aria-label={`Width ${index + 1}`} /></td><td><input value={element.dimensions?.height ?? ""} onChange={(event) => onEdit(index, "dimensions.height", event.target.value)} aria-label={`Height ${index + 1}`} /></td><td><input type="number" min="0" value={element.quantity ?? ""} onChange={(event) => onEdit(index, "quantity", event.target.value)} aria-label={`Quantity ${index + 1}`} /></td><td><input value={element.unit ?? ""} onChange={(event) => onEdit(index, "unit", event.target.value)} aria-label={`Unit ${index + 1}`} /></td><td><input value={element.specification ?? ""} onChange={(event) => onEdit(index, "specification", event.target.value)} aria-label={`Specification ${index + 1}`} /></td><td><input type="number" min="1" value={element.source_page ?? ""} onChange={(event) => onEdit(index, "source_page", event.target.value)} aria-label={`Source page ${index + 1}`} /></td><td><span className="confidence-pill">{Math.round(element.confidence * 100)}%</span></td></tr>)}</tbody></table></div><div className="action-row"><button className="secondary-button" type="button" onClick={onApprove}>{approvedExtraction ? "Approved" : "Approve extracted data"}</button><button className="primary-button" type="button" onClick={onGenerate} disabled={loading || !approvedExtraction}>{loading ? "Generating BOQ..." : approvedExtraction ? "Generate BOQ →" : "Approve extraction first"}</button></div></div>;
}

function PersistentBOQScreen({ boq, onEdit, onSave, onSubmit, onApprove }: { boq: BOQ; onEdit: (itemNumber: number, field: "description" | "element_type" | "quantity" | "unit", value: string) => void; onSave: (itemNumber: number, field: "description" | "element_type" | "quantity" | "unit", value: string) => void; onSubmit: () => void; onApprove: () => void }) {
  const errors = boq.validation_issues.filter((issue) => issue.severity === "error");
  return (
    <div className="screen">
      <div className="data-strip"><div><span>BOQ source</span><strong>{boq.source_name}</strong></div><div><span>Line items</span><strong>{boq.items.length}</strong></div><div><span>Validation</span><strong className={errors.length ? "warning-text" : "good-text"}>{errors.length ? `${errors.length} error${errors.length > 1 ? "s" : ""}` : "Ready"}</strong></div></div>
      <div className="section-intro compact"><span className="section-index">03</span><div><h2>Bill of quantities</h2><p>BOQ edits are saved to the review session and require resubmission.</p></div></div>
      {boq.validation_issues.length > 0 && <div className="issues"><strong>Validation notes</strong>{boq.validation_issues.map((issue) => <span className={issue.severity} key={`${issue.field}-${issue.message}`}>{issue.severity === "error" ? "!" : "i"} {issue.message}</span>)}</div>}
      <div className="table-wrap"><table><thead><tr><th>#</th><th>Description</th><th>Element</th><th>Quantity</th><th>Unit</th><th>Calculation</th><th>Source measurement</th><th>Source</th><th>Score</th></tr></thead><tbody>
        {boq.items.map((item) => <tr key={item.item_number}>
          <td className="number-cell">{String(item.item_number).padStart(2, "0")}</td>
          <td><input value={item.description} onChange={(event) => onEdit(item.item_number, "description", event.target.value)} onBlur={(event) => onSave(item.item_number, "description", event.currentTarget.value)} aria-label={`Description for item ${item.item_number}`} /></td>
          <td><input value={item.element_type} onChange={(event) => onEdit(item.item_number, "element_type", event.target.value)} onBlur={(event) => onSave(item.item_number, "element_type", event.currentTarget.value)} aria-label={`Element type for item ${item.item_number}`} /><small>{item.element_id ?? "—"}</small></td>
          <td><input className="quantity-input" type="number" min="0" value={item.quantity} onChange={(event) => onEdit(item.item_number, "quantity", event.target.value)} onBlur={(event) => onSave(item.item_number, "quantity", event.currentTarget.value)} aria-label={`Quantity for item ${item.item_number}`} /></td>
          <td><input className="unit-input" value={item.unit} onChange={(event) => onEdit(item.item_number, "unit", event.target.value)} onBlur={(event) => onSave(item.item_number, "unit", event.currentTarget.value)} aria-label={`Unit for item ${item.item_number}`} /></td>
          <td className="calculation">{item.calculation}</td><td>{item.dimensions ? `${item.dimensions.length ?? "—"} × ${item.dimensions.width ?? "—"} × ${item.dimensions.height ?? "—"} m` : "No dimensions"}<small>{item.dimensions_source ?? "Source not recorded"}</small></td><td><span className="source-page">p. {item.source_page ?? "—"}</span></td><td><span className="confidence-value">{Math.round(item.confidence * 100)}%</span></td>
        </tr>)}
      </tbody></table></div>
      <div className="action-row">{boq.status === "DRAFT" ? <button className="primary-button" type="button" onClick={onSubmit} disabled={errors.length > 0}>Submit for review →</button> : <button className="primary-button" type="button" onClick={onApprove}>Approve BOQ →</button>}</div>
    </div>
  );
}

function BOQScreen({ boq, onEdit, onSubmit, onApprove }: { boq: BOQ; onEdit: (itemNumber: number, field: "description" | "quantity" | "unit", value: string) => void; onSubmit: () => void; onApprove: () => void }) {
  const errors = boq.validation_issues.filter((issue) => issue.severity === "error");
  return <div className="screen"><div className="data-strip"><div><span>BOQ source</span><strong>{boq.source_name}</strong></div><div><span>Line items</span><strong>{boq.items.length}</strong></div><div><span>Validation</span><strong className={errors.length ? "warning-text" : "good-text"}>{errors.length ? `${errors.length} error${errors.length > 1 ? "s" : ""}` : "Ready"}</strong></div></div><div className="section-intro compact"><span className="section-index">03</span><div><h2>Bill of quantities</h2><p>Edit description, quantity, or unit while the BOQ is still a draft.</p></div></div>{boq.validation_issues.length > 0 && <div className="issues"><strong>Validation notes</strong>{boq.validation_issues.map((issue) => <span className={issue.severity} key={`${issue.field}-${issue.message}`}>{issue.severity === "error" ? "!" : "i"} {issue.message}</span>)}</div>}<div className="table-wrap"><table><thead><tr><th>#</th><th>Description</th><th>Element</th><th>Quantity</th><th>Unit</th><th>Calculation</th><th>Source</th><th>Confidence</th></tr></thead><tbody>{boq.items.map((item) => <tr key={item.item_number}><td className="number-cell">{String(item.item_number).padStart(2, "0")}</td><td><input value={item.description} onChange={(event) => onEdit(item.item_number, "description", event.target.value)} aria-label={`Description for item ${item.item_number}`} /></td><td><span className="type-tag">{item.element_type}</span><small>{item.element_id ?? "—"}</small></td><td><input className="quantity-input" type="number" min="0" value={item.quantity} onChange={(event) => onEdit(item.item_number, "quantity", event.target.value)} aria-label={`Quantity for item ${item.item_number}`} /></td><td><input className="unit-input" value={item.unit} onChange={(event) => onEdit(item.item_number, "unit", event.target.value)} aria-label={`Unit for item ${item.item_number}`} /></td><td className="calculation">{item.calculation}</td><td><span className="source-page">p. {item.source_page ?? "—"}</span></td><td><span className="confidence-value">{Math.round(item.confidence * 100)}%</span></td></tr>)}</tbody></table></div><div className="action-row"><span className="muted">Edits remain local until the approval workflow is connected to persistence.</span>{boq.status === "DRAFT" ? <button className="primary-button" type="button" onClick={onSubmit} disabled={errors.length > 0}>Submit for review  →</button> : <button className="primary-button" type="button" onClick={onApprove}>Approve BOQ  →</button>}</div></div>;
}

function ApprovalScreen({ boq, loading, onBack, onGenerateBOM }: { boq: BOQ; loading: boolean; onBack: () => void; onGenerateBOM: () => void }) {
  return <div className="screen approval-screen"><div className="approval-mark">✓</div><p className="eyebrow">Final status</p><h2>Approved BOQ</h2><p className="approval-copy">The reviewed quantities for <strong>{boq.source_name}</strong> are ready to move into material mapping.</p><div className="approval-summary"><div><span>Status</span><strong className="approved-label">APPROVED</strong></div><div><span>Approved lines</span><strong>{boq.items.length}</strong></div><div><span>Traceability</span><strong>Complete</strong></div></div><div className="action-row"><button className="secondary-button" type="button" onClick={onBack}>View approved BOQ</button><button className="primary-button" type="button" onClick={onGenerateBOM} disabled={loading}>{loading ? "Mapping materials..." : "Generate BOM →"}</button></div></div>;
}

function BOMScreen({ boq, bom, loading, onBack, onReviewBOQ, onMapItem, onError, approval, onApproveBOM, onOpenProcurement }: { boq: BOQ; bom: BOMResult; loading: boolean; onBack: () => void; onReviewBOQ: () => void; onMapItem: (itemNumber: number, workKey: string) => void; onError: (error: string) => void; approval: BOMApprovalRecord | null; onApproveBOM: (form: BOMApprovalForm) => void; onOpenProcurement: () => void }) {
  const formatINR = (amount: number) => `₹${amount.toLocaleString("en-IN")}`;
  const [recipeOptions, setRecipeOptions] = useState<Record<number, MappingRecipe[]>>({});
  const unmatchedCount = bom.mapping.filter((item) => item.Status === "NO MATCH").length;
  const reviewCount = bom.mapping.filter((item) => item.Status === "REVIEW").length;

  useEffect(() => {
    let active = true;
    const needsConfirmation = bom.mapping.filter((item) => item.Status === "NO MATCH" || item.Status === "REVIEW");
    void Promise.all(needsConfirmation.map(async (item) => {
      const itemNumber = Number(item["BOQ item"]);
      try {
        const result = await request<{ recipes: MappingRecipe[] }>(`/boqs/${boq.boq_id}/mapping-options/${itemNumber}`);
        if (active) {
          setRecipeOptions((current) => ({ ...current, [itemNumber]: result.recipes }));
        }
      } catch (requestError) {
        if (active) onError(requestError instanceof Error ? requestError.message : "Could not load recipe options");
      }
    }));
    return () => { active = false; };
  }, [bom.mapping, boq.boq_id, onError]);

  const unitForItem = (itemNumber: string) =>
    boq.items.find((boqItem) => boqItem.item_number === Number(itemNumber))?.unit ?? "";

  return (
    <div className="screen">
      <div className="data-strip">
        <div><span>BOQ source</span><strong>{boq.source_name}</strong></div>
        <div><span>Materials</span><strong>{bom.lines.length}</strong></div>
        <div><span>Estimated material cost</span><strong>{formatINR(bom.total_cost)}</strong></div>
      </div>
      <div className="section-intro compact"><span className="section-index">05</span><div><h2>Bill of materials</h2><p>Suggested material requirements are calculated from the approved BOQ using the current recipe catalog. Review every match before procurement.</p></div></div>
      {bom.validation.errors.length > 0 && <div className="issues" role="alert"><strong>BOM needs your review — {unmatchedCount} need a recipe; {reviewCount} need confirmation; {bom.validation.errors.length - unmatchedCount - reviewCount} need measurement details.</strong><span className="warning">No material total is provided until every blocking issue is resolved. Select a recipe only when it matches the actual construction; material and finish cannot be determined from a room or wall name alone.</span><details className="validation-details"><summary>See all {bom.validation.errors.length} review items</summary>{bom.validation.errors.map((error) => <span className="error" key={error}>{error}</span>)}</details></div>}
      {bom.validation.warnings.length > 0 && <div className="issues"><strong>Mapping warnings — review before procurement</strong>{bom.validation.warnings.map((warning) => <span className="warning" key={warning}>{warning}</span>)}</div>}
      <h3>BOQ mapping</h3>
      <div className="table-wrap"><table><thead><tr><th>BOQ item</th><th>Description</th><th>Matched work item</th><th>Confidence</th><th>Status</th><th>Manual recipe selection</th></tr></thead><tbody>
        {bom.mapping.map((item) => {
          const itemNumber = Number(item["BOQ item"]);
          const unit = unitForItem(item["BOQ item"]);
          const needsConfirmation = item.Status === "NO MATCH" || item.Status === "REVIEW";
          return <tr key={item["BOQ item"]}><td>{item["BOQ item"]}</td><td>{item.Description}</td><td>{item["Matched work item"]}</td><td>{Math.round(item.Confidence * 100)}%</td><td><span className={`type-tag ${item.Status === "OK" || item.Status === "MANUAL" ? "good-text" : "warning-text"}`}>{item.Status}</span></td><td>{needsConfirmation ? <select aria-label={`Select and confirm recipe for BOQ item ${itemNumber}`} value="" disabled={loading || !recipeOptions[itemNumber]} onChange={(event) => onMapItem(itemNumber, event.target.value)}><option value="">{recipeOptions[itemNumber] ? "Select verified recipe" : "Loading recipes..."}</option>{(recipeOptions[itemNumber] ?? []).filter((recipe) => recipe.unit === unit).map((recipe) => <option key={recipe.work_key} value={recipe.work_key}>{recipe.description} ({recipe.unit})</option>)}</select> : item.Status === "MANUAL" ? <span>Human-selected; verify the match.</span> : "—"}</td></tr>;
        })}
      </tbody></table></div>
      {(bom.deductions ?? []).length > 0 && <details className="deductions-summary"><summary><strong>Opening deductions:</strong> {(bom.deductions ?? []).length} door/window openings, {(bom.deductions ?? []).reduce((a, d) => a + d["Deducted (m2)"], 0).toFixed(2)} m² removed from wall quantities <span className="muted">(click to check)</span></summary><div className="table-wrap"><table><thead><tr><th>Host BOQ item</th><th>Opening</th><th>Area each (m2)</th><th>Count</th><th>Deducted (m2)</th><th>Rule</th></tr></thead><tbody>
        {(bom.deductions ?? []).map((d) => <tr key={`${d["BOQ item"]}-${d["Opening BOQ item"]}-${d.Rule}`}><td>{d["BOQ item"]}</td><td>{d["Opening BOQ item"]} · {d.Opening}</td><td>{d["Opening area each (m2)"]}</td><td>{d.Count}</td><td>{d["Deducted (m2)"]}</td><td className="calculation">{d.Rule}</td></tr>)}
      </tbody></table></div></details>}
      {(bom.suggestions ?? []).length > 0 && <><h3>Possibly missing scope — not included in the BOM</h3><div className="table-wrap"><table><thead><tr><th>For BOQ item</th><th>Missing work</th><th>Suggested quantity</th><th>Basis</th></tr></thead><tbody>
        {(bom.suggestions ?? []).map((sg) => <tr key={`${sg["For BOQ item"]}-${sg["Missing work"]}`}><td>{sg["For BOQ item"]}</td><td>{sg["Missing work"]}</td><td>{sg["Suggested quantity"] === null ? "From drawings" : `${sg["Suggested quantity"]} ${sg.Unit}`}</td><td className="calculation">{sg.Basis}</td></tr>)}
      </tbody></table></div></>}
      <h3>Material requirements</h3>
      {bom.lines.length > 0 && bom.validation.status === "ready" ? <div className="table-wrap"><table><thead><tr><th>Material</th><th>Category</th><th>Net qty</th><th>Wastage</th><th>Order qty</th><th>Unit</th><th>Rate</th><th>Estimated cost</th><th>BOQ items</th></tr></thead><tbody>
        {bom.lines.map((line) => <tr key={line.Material}><td>{line.Material}</td><td>{line.Category}</td><td>{line["Net qty"]}</td><td>{line["Wastage %"]}%</td><td>{line["Order qty"]}</td><td>{line.Unit}</td><td>{formatINR(line["Rate (Rs)"])}</td><td>{formatINR(line["Cost (Rs)"])}</td><td>{line["Used in BOQ items"]}</td></tr>)}
      </tbody></table></div> : bom.validation.status === "blocked" ? <div className="issues"><strong>Material quantities and costs are withheld.</strong><span>Resolve the mapping and measurement review items above, then regenerate the BOM to calculate a complete estimate.</span></div> : <div className="issues"><strong>No materials were mapped.</strong><span>Check the BOQ mapping warnings and add a matching recipe to the project catalog.</span></div>}
      {bom.validation.status === "ready" && bom.by_category.length > 0 && <><h3>Estimated cost by category</h3><div className="table-wrap"><table><thead><tr><th>Category</th><th>Estimated cost</th></tr></thead><tbody>{bom.by_category.map((category) => <tr key={category.Category}><td>{category.Category}</td><td>{formatINR(category["Cost (Rs)"])}</td></tr>)}</tbody></table></div></>}
      {bom.validation.status === "ready" && bom.lines.length > 0 && <BOMApprovalPanel boq={boq} bom={bom} loading={loading} approval={approval} onApprove={onApproveBOM} onOpen={onOpenProcurement} />}
      <div className="action-row"><span className="muted">Material rates are prototype catalog estimates, not confirmed company rates.</span><div><button className="secondary-button" type="button" onClick={onBack}>Back to approved BOQ</button> {bom.validation.errors.length > 0 && <button className="secondary-button" type="button" onClick={onReviewBOQ}>Reopen BOQ to edit</button>}</div></div>
    </div>
  );
}

function BOMApprovalPanel({ boq, bom, loading, approval, onApprove, onOpen }: { boq: BOQ; bom: BOMResult; loading: boolean; approval: BOMApprovalRecord | null; onApprove: (form: BOMApprovalForm) => void; onOpen: () => void }) {
  const [form, setForm] = useState<BOMApprovalForm>({ approved_by: "", comment: "", acknowledge_warnings: false, project_name: boq.source_name, project_code: "", procurement_project_id: null });
  const [projects, setProjects] = useState<ProcurementProject[]>([]);
  useEffect(() => {
    // existing procurement projects, so a revised drawing can be added to the same project and keep its purchase history
    fetch(`${API_BASE}/procurement/api/projects`).then((r) => (r.ok ? r.json() : [])).then((list: ProcurementProject[]) => {
      setProjects(list);
      const same = list.find((p) => p.name === boq.source_name);
      if (same) setForm((f) => ({ ...f, procurement_project_id: same.id }));
    }).catch(() => setProjects([]));
  }, [boq.source_name]);
  const warnings = bom.validation.warnings;
  const blockedReason = !form.approved_by.trim() ? "Enter the approver name" : warnings.length > 0 && !form.acknowledge_warnings ? "Acknowledge the warnings above" : "";
  if (approval) {
    return <div className="approval-panel approved"><p className="eyebrow">Step 06 · BOM approved</p><h3>Approved by {approval.approved_by}</h3>
      <p className="muted">{approval.material_count} materials handed to procurement project “{approval.procurement.project_name}”. Reopen the BOQ to change quantities and approve again.</p>
      <div className="action-row"><span /><button className="primary-button" type="button" onClick={onOpen}>Open procurement →</button></div></div>;
  }
  return <div className="approval-panel"><p className="eyebrow">Human approval before procurement</p><h3>Approve BOM for procurement</h3>
    <p className="muted">Confirms the material quantities above are reviewed. The BOM becomes a read-only snapshot in procurement; vendors, prices and contracts are handled there with their own approvals.</p>
    <div className="approval-form">
      <label><span>Approved by *</span><input value={form.approved_by} onChange={(e) => setForm({ ...form, approved_by: e.target.value })} placeholder="Reviewer name" aria-label="Approved by" /></label>
      <label><span>Procurement project</span><select value={form.procurement_project_id ?? ""} onChange={(e) => setForm({ ...form, procurement_project_id: e.target.value ? Number(e.target.value) : null })} aria-label="Procurement project">
        <option value="">+ Create new project</option>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.name}{p.code ? ` · ${p.code}` : ""}</option>)}
      </select></label>
      {form.procurement_project_id === null ? <>
        <label><span>New project name</span><input value={form.project_name} onChange={(e) => setForm({ ...form, project_name: e.target.value })} aria-label="Procurement project name" /></label>
        <label><span>Project code</span><input value={form.project_code} onChange={(e) => setForm({ ...form, project_code: e.target.value })} placeholder="Optional, used in PO numbers" aria-label="Project code" /></label>
      </> : <label><span>&nbsp;</span><span className="muted">The BOM is added to this project; its earlier materials and purchase orders stay there.</span></label>}
      <label className="wide"><span>Comment</span><input value={form.comment} onChange={(e) => setForm({ ...form, comment: e.target.value })} placeholder="Optional" aria-label="Approval comment" /></label>
      {warnings.length > 0 && <label className="wide check"><input type="checkbox" checked={form.acknowledge_warnings} onChange={(e) => setForm({ ...form, acknowledge_warnings: e.target.checked })} aria-label="Acknowledge warnings" /> <span>I have reviewed the {warnings.length} warning(s) listed above</span></label>}
    </div>
    <div className="action-row"><span className="muted">{blockedReason}</span><button className="primary-button" type="button" disabled={!!blockedReason || loading} onClick={() => onApprove(form)}>{loading ? "Approving..." : "Approve BOM & start procurement →"}</button></div></div>;
}

function ProcurementScreen({ approval, onBack }: { approval: BOMApprovalRecord; onBack: () => void }) {
  const p = approval.procurement;
  const src = `${API_BASE}/procurement/ui/?embedded=1&project=${p.project_id}&list=${p.list_id}&view=procurement&actor=${encodeURIComponent(approval.approved_by)}`;
  return <div className="screen procurement-screen">
    <div className="data-strip"><div><span>Approved by</span><strong>{approval.approved_by}</strong></div><div><span>Materials handed over</span><strong>{p.item_count}</strong></div><div><span>Procurement project</span><strong>{p.project_name}</strong></div></div>
    <div className="section-intro compact"><span className="section-index">06</span><div><h2>Procurement</h2><p>Edit the procurement list, compare vendor quotations, raise purchase orders, club packages and draft contracts. Each document needs its own review and approval; nothing is signed or sent automatically.</p></div></div>
    <iframe className="procurement-frame" title="Procurement workspace" src={src} />
    <div className="action-row"><button className="secondary-button" type="button" onClick={onBack}>Back to BOM</button><a className="secondary-button" href={src.replace("embedded=1&", "")} target="_blank" rel="noopener">Open in new tab ↗</a></div>
  </div>;
}

export default App;
