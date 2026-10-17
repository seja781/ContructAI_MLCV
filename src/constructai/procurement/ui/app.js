/* ConstructAI Procurement Prototype — vanilla JS SPA talking to the FastAPI backend. */
"use strict";

const BASE = location.pathname.replace(/\/ui(\/.*)?$/, "");  // e.g. /procurement or /api/procurement

// ============================================================ utilities
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => (n === null || n === undefined || n === "" || isNaN(n)) ? "—" :
  Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qty = (n) => (n === null || n === undefined || n === "") ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 3 });
const r2 = (n) => Math.round((Number(n) || 0) * 100) / 100;
const dt = (s) => s ? new Date(s.length <= 10 ? s + "T00:00:00" : s + (s.endsWith("Z") ? "" : "Z")).toLocaleString("en-IN",
  s.length <= 10 ? { day: "2-digit", month: "short", year: "numeric" } : { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
const store = {
  get: (k, d = null) => { try { const v = localStorage.getItem("proc." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem("proc." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
const STATUS_LABEL = { draft: "Draft", pending_review: "Pending review", returned: "Returned for revision", approved: "Approved", rejected: "Rejected", finalized: "Finalized" };
const statusBadge = (s, superseded) => superseded ? `<span class="badge">Superseded</span>` : `<span class="badge ${s}">${STATUS_LABEL[s] || s}</span>`;
const sourceBadge = (src) => src === "imported" ? `<span class="badge imported">Imported project data</span>` : src ? `<span class="badge test">User-created test data</span>` : "";
const revLabel = (num, rev) => esc(num) + (rev ? ` <span class="badge">R${rev}</span>` : "");

class ApiError extends Error { constructor(status, detail) { super(typeof detail === "string" ? detail : detail?.message || "Request failed"); this.status = status; this.detail = detail; } }

async function api(method, path, body, isForm = false) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opt.body = body;
    else { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  }
  const res = await fetch(BASE + path, opt);
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.detail ?? data);
  return data;
}

function errHTML(e) {
  const d = e.detail;
  if (Array.isArray(d)) { // pydantic validation errors
    return `<b>Please fix the following:</b><ul>${d.map((x) => `<li>${esc((x.loc || []).filter((p) => p !== "body").join(" › "))}: ${esc(x.msg)}</li>`).join("")}</ul>`;
  }
  let h = esc(e.message);
  if (d?.row_errors) h += `<ul>${d.row_errors.map((r) => `<li>Row ${r.row}: ${esc(r.error)}</li>`).join("")}</ul>`;
  return h;
}

function toast(msg, isErr = false) {
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " error" : "");
  t.innerHTML = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), isErr ? 7000 : 3500);
}
const fail = (e) => toast(errHTML(e), true);

// ============================================================ modal + forms
function openModal({ title, body, wide = false, submitLabel = "Save", submitClass = "primary", onSubmit, onOpen, cancelLabel = "Cancel" }) {
  const root = $("#modalRoot");
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">
    <div class="modal-h"><h2>${title}</h2><button class="x" data-close aria-label="Close">×</button></div>
    <div class="modal-b"><div class="modal-err"></div>${body}</div>
    <div class="modal-f"><button class="btn" data-close>${cancelLabel}</button>${onSubmit ? `<button class="btn ${submitClass}" data-submit>${submitLabel}</button>` : ""}</div></div>`;
  root.appendChild(bg);
  const close = () => bg.remove();
  $$("[data-close]", bg).forEach((b) => (b.onclick = close));
  const m = $(".modal", bg);
  const showErr = (html) => { $(".modal-err", bg).innerHTML = html ? `<div class="alert error">${html}</div>` : ""; if (html) $(".modal-b", bg).scrollTop = 0; };
  if (onSubmit) {
    $("[data-submit]", bg).onclick = async () => {
      const btn = $("[data-submit]", bg);
      btn.disabled = true; showErr("");
      try { const keep = await onSubmit(m, showErr); if (keep !== false) close(); }
      catch (e) { showErr(errHTML(e)); }
      finally { btn.disabled = false; }
    };
  }
  onOpen && onOpen(m, close, showErr);
  const first = $("input:not([type=hidden]):not([type=checkbox]), select, textarea", m);
  first && setTimeout(() => first.focus(), 30);
  return { el: m, close };
}

function fieldHTML(f, v) {
  const val = v ?? f.default ?? "";
  const req = f.required ? ' <span class="req">*</span>' : "";
  let input;
  if (f.type === "textarea") input = `<textarea name="${f.name}" rows="${f.rows || 3}" ${f.code ? 'class="code"' : ""}>${esc(val)}</textarea>`;
  else if (f.type === "select") input = `<select name="${f.name}">${(f.options || []).map(([ov, ol]) => `<option value="${esc(ov)}" ${String(ov) === String(val ?? "") ? "selected" : ""}>${esc(ol)}</option>`).join("")}</select>`;
  else if (f.type === "checkbox") return `<label class="field check ${f.full ? "full" : ""}"><input type="checkbox" name="${f.name}" ${val ? "checked" : ""}> ${esc(f.label)}</label>`;
  else input = `<input name="${f.name}" type="${f.type || "text"}" value="${esc(val)}" ${f.step ? `step="${f.step}"` : ""} ${f.min !== undefined ? `min="${f.min}"` : ""} ${f.placeholder ? `placeholder="${esc(f.placeholder)}"` : ""}>`;
  return `<label class="field ${f.full ? "full" : ""}" data-f="${f.name}"><span>${esc(f.label)}${req}</span>${input}${f.hint ? `<small class="hint">${esc(f.hint)}</small>` : ""}<small class="ferr"></small></label>`;
}
const formHTML = (fields, vals = {}) => `<div class="form">${fields.map((f) => fieldHTML(f, vals[f.name])).join("")}</div>`;

function readForm(el, fields) {
  const out = {}; let ok = true;
  for (const f of fields) {
    const inp = $(`[name="${f.name}"]`, el);
    if (!inp) continue;
    const wrap = inp.closest(".field");
    let v = f.type === "checkbox" ? inp.checked : inp.value.trim();
    let msg = "";
    if (f.type !== "checkbox") {
      if (f.required && v === "") msg = "Required";
      else if (v === "") v = f.type === "number" && f.zeroDefault ? 0 : null;
      else if (f.type === "number") {
        const n = Number(v);
        if (isNaN(n)) msg = "Must be a number";
        else if (f.min !== undefined && n < f.min) msg = `Must be ≥ ${f.min}`;
        else if (f.gt !== undefined && n <= f.gt) msg = `Must be greater than ${f.gt}`;
        else if (f.max !== undefined && n > f.max) msg = `Must be ≤ ${f.max}`;
        else v = f.int ? parseInt(v, 10) : n;
      } else if (f.type === "email" && !/^\S+@\S+\.\S+$/.test(v)) msg = "Enter a valid email";
      else if (f.type === "select" && f.numeric) v = v === "" ? null : Number(v);
    }
    if (wrap) { wrap.classList.toggle("invalid", !!msg); const fe = $(".ferr", wrap); if (fe) fe.textContent = msg; }
    if (msg) ok = false;
    out[f.name] = v;
  }
  return ok ? out : null;
}

function getActor() { return ($("#actorInput").value || "").trim(); }
function requireActor() {
  const a = getActor();
  if (!a) { toast("Enter your name in <b>Acting as</b> (top right) — it is recorded on every review action.", true); $("#actorInput").focus(); }
  return a;
}

// ============================================================ state
const S = {
  view: store.get("view", "projects"),
  projects: [], project: null, lists: [], list: null, vendors: [],
  sel: { po: null, package: null, contract: null }, showSuperseded: false, contractTab: "drafts",
  vendorTab: "directory", packageTab: "list", embedded: false,
};

const NAV_MAIN = [
  ["procurement", "Materials"], ["vendors", "Vendors & quotes"], ["pos", "Purchase orders"],
  ["purchases", "Purchase history"], ["contracts", "Contracts & approvals"],
];
// Inside ConstructAI the approved BOM arrives automatically, so "Projects & BOM import" is only shown standalone.
const navItems = () => (S.embedded ? NAV_MAIN : [["projects", "Projects & BOM import"], ...NAV_MAIN]);
// Older screen names now live as tabs inside the merged sections.
const VIEW_ALIAS = { quotes: ["vendors", "vendorTab", "quotes"], history: ["contracts", "contractTab", "approvals"] };
function resolveView(view) {
  if (VIEW_ALIAS[view]) { const [v, key, tab] = VIEW_ALIAS[view]; S[key] = tab; view = v; }
  if (S.embedded && view === "projects") view = "procurement";
  if (view === "packages" || view === "contractors") view = "procurement";  // packages are not used in this workflow
  return view;
}

function renderNav(pending = 0) {
  $("#nav").innerHTML = navItems().map(([id, label], i) => `<button data-v="${id}" class="${S.view === id ? "active" : ""}">
    <span class="num">${i + 1}</span>${label}${id === "contracts" && pending ? `<span class="badge accent">${pending}</span>` : ""}</button>`).join("");
  $$("#nav button").forEach((b) => (b.onclick = () => { go(b.dataset.v); $("#sidebar").classList.remove("open"); }));
}

function go(view) { S.view = resolveView(view); store.set("view", S.view); render(); }

async function loadProjects() {
  S.projects = await api("GET", "/api/projects");
  const want = store.get("projectId");
  S.project = S.projects.find((p) => p.id === want) || S.projects[0] || null;
  $("#projectSel").innerHTML = S.projects.length ? S.projects.map((p) => `<option value="${p.id}" ${S.project?.id === p.id ? "selected" : ""}>${esc(p.name)}${p.code ? " · " + esc(p.code) : ""}</option>`).join("")
    : `<option value="">No projects yet</option>`;
  await loadLists();
}

async function loadLists() {
  S.lists = S.project ? await api("GET", `/api/projects/${S.project.id}/procurement-lists`) : [];
  const want = store.get("listId");
  const l = S.lists.find((x) => x.id === want) || S.lists[S.lists.length - 1];
  S.list = l ? await api("GET", `/api/procurement-lists/${l.id}`) : null;
  $("#listSel").innerHTML = S.lists.length ? S.lists.map((x) => `<option value="${x.id}" ${S.list?.id === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")
    : `<option value="">No procurement list</option>`;
}

async function refreshList() { if (S.list) S.list = await api("GET", `/api/procurement-lists/${S.list.id}`); }
async function loadVendors() { S.vendors = await api("GET", "/api/vendors"); }
const suppliers = () => S.vendors.filter((v) => v.active && v.kind !== "contractor");
const contractors = () => S.vendors.filter((v) => v.active && v.kind !== "supplier");

async function render() {
  renderNav();
  const v = $("#view");
  v.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const fn = VIEWS[S.view] || VIEWS.projects;
    await fn(v);
  } catch (e) { v.innerHTML = `<div class="alert error">${errHTML(e)}</div>`; }
  if (S.project) api("GET", `/api/projects/${S.project.id}/summary`).then((s) => renderNav(s.pending_review)).catch(() => {});
}

const needProject = (v) => { if (S.project) return false; v.innerHTML = `<div class="card"><div class="empty"><b>No project selected</b>Create a project first.<br><br><button class="btn primary" id="gp">Go to Projects</button></div></div>`; $("#gp").onclick = () => go("projects"); return true; };
const needList = (v) => { if (needProject(v)) return true; if (S.list) return false; v.innerHTML = `<div class="card"><div class="empty"><b>No procurement list</b>Import or create a BOM, then create a procurement list from it.<br><br><button class="btn primary" id="gp">Go to Projects & BOM</button></div></div>`; $("#gp").onclick = () => go("projects"); return true; };

// ============================================================ editable item table (BOM + procurement)
const ITEM_FIELDS = [
  { name: "item_code", label: "Code" }, { name: "name", label: "Material", required: true },
  { name: "specification", label: "Specification" }, { name: "category", label: "Category" },
  { name: "quantity", label: "Quantity", type: "number", required: true, gt: 0, step: "any" },
  { name: "unit", label: "Unit", required: true }, { name: "est_rate", label: "Est. rate", type: "number", min: 0, step: "any" },
];

function traceLine(it) {
  if (it.notes) return `<div class="spec" title="Traceability">↳ ${esc(it.notes)}</div>`;
  const parts = [];
  if (it.net_quantity != null) parts.push(`Net ${qty(it.net_quantity)} ${esc(it.unit)}${it.wastage_pct != null ? ` + ${it.wastage_pct}% wastage` : ""}`);
  if (it.source_boq_items) parts.push(`BOQ items ${esc(it.source_boq_items)}`);
  return parts.length ? `<div class="spec">↳ ${parts.join("; ")}</div>` : "";
}

function itemTable(container, { items, editable, showAlloc, onSave, onDelete, onAdd, footerLabel = "Estimated total" }) {
  let editing = null;
  const rowInputs = (it = {}) => `
    <td><input data-k="item_code" value="${esc(it.item_code)}" style="width:80px"></td>
    <td><input data-k="name" value="${esc(it.name)}" placeholder="Material name *"><textarea data-k="specification" rows="1" placeholder="Specification">${esc(it.specification)}</textarea></td>
    <td><input data-k="category" value="${esc(it.category)}"></td>
    <td class="num"><input data-k="quantity" type="number" step="any" min="0" value="${it.quantity ?? ""}" style="width:100px"></td>
    <td><input data-k="unit" value="${esc(it.unit)}" style="width:70px" placeholder="Unit *"></td>
    <td class="num"><input data-k="est_rate" type="number" step="any" min="0" value="${it.est_rate ?? ""}" style="width:110px" placeholder="optional"></td>
    <td class="num" data-amt>${it.quantity && it.est_rate != null ? money(it.quantity * it.est_rate) : "—"}</td>`;
  const read = (tr) => {
    const g = (k) => $(`[data-k="${k}"]`, tr).value.trim();
    const errs = [];
    const q = Number(g("quantity")), rate = g("est_rate");
    $$("input,textarea", tr).forEach((i) => i.classList.remove("invalid"));
    const bad = (k, m) => { errs.push(m); $(`[data-k="${k}"]`, tr).classList.add("invalid"); };
    if (!g("name")) bad("name", "Material name is required");
    if (!g("unit")) bad("unit", "Unit is required");
    if (!(q > 0)) bad("quantity", "Quantity must be greater than 0");
    if (rate !== "" && (isNaN(Number(rate)) || Number(rate) < 0)) bad("est_rate", "Rate must be ≥ 0");
    if (errs.length) { toast(errs.join("<br>"), true); return null; }
    return { item_code: g("item_code") || null, name: g("name"), specification: g("specification") || null, category: g("category") || null,
      quantity: q, unit: g("unit"), est_rate: rate === "" ? null : Number(rate) };
  };
  function draw() {
    const cols = 7 + (showAlloc ? 2 : 0) + (editable ? 1 : 0);
    let total = 0, unpriced = 0;
    const rows = items.map((it) => {
      if (editing === it.id) return `<tr data-id="${it.id}" class="selected">${rowInputs(it)}${showAlloc ? `<td></td><td></td>` : ""}
        <td><div class="actions"><button class="btn sm primary" data-save>Save</button><button class="btn sm" data-cancel>Cancel</button></div></td></tr>`;
      const amt = it.est_rate != null ? it.quantity * it.est_rate : null;
      if (amt != null) total += amt; else unpriced++;
      const pct = showAlloc ? Math.min(100, (it.allocated_total / it.quantity) * 100) : 0;
      return `<tr data-id="${it.id}"><td class="spec">${esc(it.item_code || "")}</td>
        <td><b>${esc(it.name)}</b>${it.specification ? `<div class="spec">${esc(it.specification)}</div>` : ""}${traceLine(it)}</td>
        <td>${esc(it.category || "")}</td><td class="num">${qty(it.quantity)}</td><td>${esc(it.unit)}</td>
        <td class="num">${it.est_rate != null ? money(it.est_rate) : `<span class="badge warn">No rate</span>`}</td><td class="num">${money(amt)}</td>
        ${showAlloc ? `<td class="num" title="PO ${qty(it.allocated_po)} · Package ${qty(it.allocated_package)}">${qty(it.allocated_total)}<div class="bar"><i class="${pct >= 99.99 ? "full" : ""}" style="width:${pct}%"></i></div></td>
          <td class="num">${it.remaining > 0 ? qty(it.remaining) : `<span class="badge ok">Fully allocated</span>`}</td>` : ""}
        ${editable ? `<td><div class="actions"><button class="btn sm" data-edit>Edit</button><button class="btn sm danger" data-del>Remove</button></div></td>` : ""}</tr>`;
    }).join("");
    container.innerHTML = `<div class="tbl-wrap"><table><thead><tr><th>Code</th><th>Material / specification</th><th>Category</th><th class="num">Qty</th><th>Unit</th>
      <th class="num">Est. rate</th><th class="num">Est. amount</th>${showAlloc ? `<th class="num">Allocated</th><th class="num">Remaining</th>` : ""}${editable ? "<th></th>" : ""}</tr></thead>
      <tbody>${rows || `<tr><td colspan="${cols}" class="empty">No materials yet.</td></tr>`}
      ${editable && onAdd ? `<tr data-new>${rowInputs()}${showAlloc ? "<td></td><td></td>" : ""}<td><button class="btn sm primary" data-add>+ Add</button></td></tr>` : ""}</tbody>
      <tfoot><tr><td colspan="6">${footerLabel}${unpriced ? ` <span class="badge warn">${unpriced} item(s) without rate excluded</span>` : ""}</td><td class="num" data-total>${money(total)}</td><td colspan="${cols - 7}"></td></tr></tfoot></table></div>`;
    // live recalculation while typing
    $$("tr", container).forEach((tr) => {
      const q = $('[data-k="quantity"]', tr), r = $('[data-k="est_rate"]', tr);
      if (!q) return;
      const upd = () => {
        const a = $("[data-amt]", tr); const qq = Number(q.value), rr = r.value === "" ? null : Number(r.value);
        a.textContent = qq > 0 && rr != null && !isNaN(rr) ? money(qq * rr) : "—";
        if (tr.dataset.id) {
          let t = 0; items.forEach((it) => { const isE = String(it.id) === tr.dataset.id; const rate = isE ? rr : it.est_rate; const qn = isE ? qq : it.quantity; if (rate != null && !isNaN(rate)) t += qn * rate; });
          $("[data-total]", container).textContent = money(t);
        }
      };
      q.oninput = upd; r.oninput = upd;
    });
    $$("[data-edit]", container).forEach((b) => (b.onclick = () => { editing = Number(b.closest("tr").dataset.id); draw(); }));
    $$("[data-cancel]", container).forEach((b) => (b.onclick = () => { editing = null; draw(); }));
    $$("[data-save]", container).forEach((b) => (b.onclick = async () => {
      const tr = b.closest("tr"); const vals = read(tr); if (!vals) return;
      const orig = items.find((x) => x.id === Number(tr.dataset.id));
      if (orig && "notes" in orig) vals.notes = orig.notes ?? null; // keep traceability notes
      try { await onSave(Number(tr.dataset.id), vals); editing = null; } catch (e) { fail(e); }
    }));
    $$("[data-del]", container).forEach((b) => (b.onclick = async () => {
      const it = items.find((x) => x.id === Number(b.closest("tr").dataset.id));
      if (!confirm(`Remove "${it.name}" from this list?`)) return;
      try { await onDelete(it.id); } catch (e) { fail(e); }
    }));
    const add = $("[data-add]", container);
    if (add) add.onclick = async () => { const vals = read(add.closest("tr")); if (!vals) return; try { await onAdd(vals); } catch (e) { fail(e); } };
  }
  draw();
  return { redraw: (newItems) => { items = newItems; draw(); } };
}

// ============================================================ workflow actions (shared)
const DOC = {
  po: { path: (id) => `/api/purchase-orders/${id}`, label: (d) => d.po_number },
  package: { path: (id) => `/api/packages/${id}`, label: (d) => d.package_code },
  contract: { path: (id) => `/api/contracts/${id}`, label: (d) => d.contract_number },
};

function actionButtons(type, d) {
  const b = [];
  if (d.superseded_by_id) return `<span class="badge">Superseded — read-only</span> <a class="btn sm" href="${BASE}${DOC[type].path(d.id)}/pdf" target="_blank">Export PDF</a>`;
  if (d.editable) b.push(`<button class="btn sm primary" data-act="submit">Submit for review</button>`);
  if (d.status === "pending_review") b.push(`<button class="btn sm ok" data-act="approve">Approve</button><button class="btn sm" data-act="return">Return for revision</button><button class="btn sm danger" data-act="reject">Reject</button>`);
  if (d.status === "approved") b.push(`<button class="btn sm ok" data-act="finalize">Finalize…</button>`);
  if (["approved", "finalized", "rejected"].includes(d.status)) b.push(`<button class="btn sm" data-act="revision">Create revision</button>`);
  if (d.status === "draft" && !d.revision) b.push(`<button class="btn sm danger" data-act="delete">Delete draft</button>`);
  b.push(`<a class="btn sm" href="${BASE}${DOC[type].path(d.id)}/pdf" target="_blank" rel="noopener">Export PDF</a>`);
  return b.join("");
}

function bindActions(el, type, d, after) {
  $$("[data-act]", el).forEach((btn) => (btn.onclick = () => runAction(type, d, btn.dataset.act, after)));
}

async function runAction(type, d, act, after) {
  const path = DOC[type].path(d.id), label = DOC[type].label(d) + (d.revision ? ` R${d.revision}` : "");
  if (act === "delete") {
    if (!confirm(`Delete draft ${label}? This cannot be undone.`)) return;
    try { await api("DELETE", path); toast(`${label} deleted`); S.sel[type] = null; after(); } catch (e) { fail(e); }
    return;
  }
  const actor = requireActor(); if (!actor) return;
  const cfg = {
    submit: { t: "Submit for review", btn: "Submit", cls: "primary", comment: "optional" },
    approve: { t: "Approve", btn: "Approve", cls: "ok", comment: "optional", note: "Approval confirms you have reviewed quantities, prices and terms. Approved documents can no longer be edited — changes require a revision." },
    return: { t: "Return for revision", btn: "Return", cls: "primary", comment: "required" },
    reject: { t: "Reject", btn: "Reject", cls: "danger", comment: "required", note: "Rejected documents release their allocated quantities." },
    finalize: { t: "Finalize", btn: "Finalize", cls: "ok", comment: "optional", confirm: true, note: "Finalizing locks this document. It will not be signed or sent automatically — signing and dispatch remain manual." },
    revision: { t: "Create revision", btn: "Create revision", cls: "primary", comment: "optional", note: "The current version is kept unchanged in history; a new editable draft revision is created." },
  }[act];
  openModal({
    title: `${cfg.t} · ${esc(label)}`, submitLabel: cfg.btn, submitClass: cfg.cls,
    body: `${cfg.note ? `<div class="alert info">${cfg.note}</div>` : ""}<p>Acting as <b>${esc(actor)}</b></p>
      <label class="field full"><span>Comment ${cfg.comment === "required" ? '<span class="req">*</span>' : "(optional)"}</span><textarea name="comment" rows="3"></textarea><small class="ferr"></small></label>
      ${cfg.confirm ? `<label class="check" style="margin-top:12px"><input type="checkbox" name="confirm"> I confirm this document has been reviewed and should be locked as final.</label>` : ""}`,
    onSubmit: async (m, showErr) => {
      const comment = $("[name=comment]", m).value.trim();
      if (cfg.comment === "required" && !comment) { showErr("A comment is required so the author knows what to change."); return false; }
      if (cfg.confirm && !$("[name=confirm]", m).checked) { showErr("Tick the confirmation box to finalize."); return false; }
      let res;
      if (act === "revision") res = await api("POST", `${path}/revision`, { actor, comment });
      else res = await api("POST", `${path}/action`, { action: act, actor, comment, confirm: !!cfg.confirm });
      toast(`${label}: ${act === "revision" ? `revision R${res.revision} created` : STATUS_LABEL[res.status]}`);
      S.sel[type] = res.id;
      after();
    },
  });
}

async function docTimeline(type, id) {
  if (!S.project) return "";
  const ev = await api("GET", `/api/projects/${S.project.id}/history?doc_type=${type}&doc_id=${id}`);
  if (!ev.length) return `<p class="spec">No history yet.</p>`;
  return `<ul class="timeline">${ev.map((e) => `<li><b>${esc(e.action.replace(/_/g, " "))}</b>${e.to_status && e.from_status !== e.to_status ? ` → ${statusBadge(e.to_status)}` : ""}
    ${e.actor ? ` by ${esc(e.actor)}` : ""}${e.comment ? `<div>“${esc(e.comment)}”</div>` : ""}<div class="when">${dt(e.created_at)}</div></li>`).join("")}</ul>`;
}

// ============================================================ VIEWS
const VIEWS = {};

// ---------------------------------------------------------------- 1. projects & BOM
const PROJECT_FIELDS = [
  { name: "name", label: "Project name", required: true }, { name: "code", label: "Project code", hint: "Used in PO / contract numbers" },
  { name: "client", label: "Client" }, { name: "location", label: "Location" },
  { name: "description", label: "Description", type: "textarea", full: true },
  { name: "buyer_name", label: "Buyer / employer (issuing organisation)" }, { name: "buyer_tax_id", label: "Buyer tax ID (e.g. GSTIN)" },
  { name: "buyer_address", label: "Buyer address", type: "textarea" }, { name: "buyer_contact", label: "Buyer contact" },
  { name: "default_delivery_address", label: "Default site delivery address", type: "textarea", full: true },
  { name: "external_ref", label: "ConstructAI project ID (optional)", hint: "For future integration" },
];

function projectForm(p) {
  openModal({
    title: p ? "Edit project" : "New project", wide: true, body: formHTML(PROJECT_FIELDS, p || {}),
    onSubmit: async (m) => {
      const vals = readForm(m, PROJECT_FIELDS); if (!vals) return false;
      const res = p ? await api("PUT", `/api/projects/${p.id}`, vals) : await api("POST", "/api/projects", vals);
      store.set("projectId", res.id); await loadProjects(); render(); toast(p ? "Project updated" : "Project created");
    },
  });
}

VIEWS.projects = async (v) => {
  const p = S.project;
  let boms = [], summary = null;
  if (p) [boms, summary] = await Promise.all([api("GET", `/api/projects/${p.id}/boms`), api("GET", `/api/projects/${p.id}/summary`)]);
  v.innerHTML = `<h1>Projects & BOM import</h1><p class="sub">Start from an approved BOM: import it (JSON/CSV) or create test data manually, then create an editable procurement list.</p>
    ${summary ? `<div class="stats">
      <div class="stat"><div class="k">Estimated value (all lists)</div><div class="v">${money(summary.estimated_value)}</div></div>
      <div class="stat"><div class="k">Purchase orders</div><div class="v">${summary.po.count} · ${money(summary.po.value)}</div></div>
      <div class="stat"><div class="k">Packages</div><div class="v">${summary.package.count} · ${money(summary.package.value)}</div></div>
      <div class="stat"><div class="k">Contracts</div><div class="v">${summary.contract.count}</div></div>
      <div class="stat"><div class="k">Pending review</div><div class="v">${summary.pending_review}</div></div></div>` : ""}
    <div class="card"><div class="card-h"><h2>Projects</h2><div class="spacer"></div><button class="btn primary" id="newProj">+ New project</button></div>
      <div class="card-b flush">${S.projects.length ? `<div class="tbl-wrap"><table><thead><tr><th>Project</th><th>Code</th><th>Client</th><th>Location</th><th>Buyer</th><th></th></tr></thead><tbody>
        ${S.projects.map((x) => `<tr class="clickable ${p?.id === x.id ? "selected" : ""}" data-pid="${x.id}"><td><b>${esc(x.name)}</b></td><td>${esc(x.code || "")}</td><td>${esc(x.client || "")}</td><td>${esc(x.location || "")}</td>
          <td>${x.buyer_name ? esc(x.buyer_name) : `<span class="badge warn">Not set</span>`}</td><td><div class="actions"><button class="btn sm" data-editp="${x.id}">Edit</button><button class="btn sm danger" data-delp="${x.id}">Delete</button></div></td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty"><b>No projects yet</b>Create a project to begin.</div>`}</div></div>
    ${p ? `
    <div class="card"><div class="card-h"><h2>BOMs · ${esc(p.name)}</h2><div class="spacer"></div>
      <button class="btn" id="manualBom">+ Create sample BOM manually</button><button class="btn primary" id="importBom">Import BOM (JSON / CSV)</button></div>
      <div class="card-b flush">${boms.length ? `<div class="tbl-wrap"><table><thead><tr><th>BOM</th><th>Source</th><th class="num">Items</th><th class="num">Est. total</th><th>Created</th><th></th></tr></thead><tbody>
        ${boms.map((b) => `<tr><td><b>${esc(b.name)}</b>${b.source_file ? `<div class="spec">${esc(b.source_file)}</div>` : ""}</td><td>${sourceBadge(b.source)}</td><td class="num">${b.item_count}</td><td class="num">${money(b.est_total)}</td><td>${dt(b.created_at)}</td>
          <td><div class="actions"><button class="btn sm" data-viewb="${b.id}">${b.read_only ? "View" : "View / edit"}</button><button class="btn sm primary" data-plb="${b.id}" data-name="${esc(b.name)}">Create procurement list</button><button class="btn sm danger" data-delb="${b.id}">Delete</button></div></td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty"><b>No BOM yet</b>Import the approved BOM exported from ConstructAI, or create a sample BOM manually for testing.</div>`}</div></div>
    <div class="card"><div class="card-h"><h2>Procurement lists</h2><span class="spec">Editable working copies — never modify the source BOM</span><div class="spacer"></div><button class="btn" id="emptyList">+ Empty list</button></div>
      <div class="card-b flush">${S.lists.length ? `<div class="tbl-wrap"><table><thead><tr><th>List</th><th>From BOM</th><th class="num">Items</th><th class="num">Est. total</th><th></th></tr></thead><tbody>
        ${S.lists.map((l) => `<tr><td><b>${esc(l.name)}</b></td><td>${esc(l.bom_name || "—")} ${sourceBadge(l.bom_source)}</td><td class="num">${l.item_count}</td><td class="num">${money(l.est_total)}</td>
          <td><div class="actions"><button class="btn sm primary" data-openl="${l.id}">Open</button><button class="btn sm danger" data-dell="${l.id}">Delete</button></div></td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">No procurement lists yet.</div>`}</div></div>` : ""}`;

  $("#newProj").onclick = () => projectForm(null);
  $$("[data-pid]", v).forEach((tr) => (tr.onclick = async (e) => { if (e.target.closest("button")) return; store.set("projectId", Number(tr.dataset.pid)); await loadProjects(); render(); }));
  $$("[data-editp]", v).forEach((b) => (b.onclick = () => projectForm(S.projects.find((x) => x.id === Number(b.dataset.editp)))));
  $$("[data-delp]", v).forEach((b) => (b.onclick = async () => {
    const x = S.projects.find((y) => y.id === Number(b.dataset.delp));
    if (!confirm(`Delete project "${x.name}" and ALL its BOMs, lists, POs, packages and contracts?`)) return;
    try { await api("DELETE", `/api/projects/${x.id}`); await loadProjects(); render(); toast("Project deleted"); } catch (e) { fail(e); }
  }));
  if (!p) return;
  $("#importBom").onclick = importBomModal;
  $("#manualBom").onclick = manualBomModal;
  $("#emptyList").onclick = () => newListModal(null, "");
  $$("[data-viewb]", v).forEach((b) => (b.onclick = () => bomModal(Number(b.dataset.viewb))));
  $$("[data-plb]", v).forEach((b) => (b.onclick = () => newListModal(Number(b.dataset.plb), b.dataset.name)));
  $$("[data-delb]", v).forEach((b) => (b.onclick = async () => {
    if (!confirm("Delete this BOM? Procurement lists already created from it keep their copied materials.")) return;
    try { await api("DELETE", `/api/boms/${b.dataset.delb}`); render(); } catch (e) { fail(e); }
  }));
  $$("[data-openl]", v).forEach((b) => (b.onclick = async () => { store.set("listId", Number(b.dataset.openl)); await loadLists(); go("procurement"); }));
  $$("[data-dell]", v).forEach((b) => (b.onclick = async () => {
    if (!confirm("Delete this procurement list?")) return;
    try { await api("DELETE", `/api/procurement-lists/${b.dataset.dell}`); await loadLists(); render(); } catch (e) { fail(e); }
  }));
};

function importBomModal() {
  openModal({
    title: "Import BOM", submitLabel: "Import",
    body: `<div class="alert info"><b>ConstructAI BOM:</b> save the JSON returned by <code>POST /boqs/{boq_id}/bom</code> and upload it as-is.
      Procurement quantity = <i>Order qty</i> (net + wastage); <i>Rate (Rs)</i> becomes the estimated rate; net qty, wastage % and source BOQ items are kept for traceability.
      Blocked BOMs (unresolved mapping errors) are refused.<br><br>
      <b>Other formats:</b> <b>.csv</b> with a header row, or <b>.json</b> (array of items, or <code>{"bom": {...}, "items": [...]}</code>). Required per row: material name, quantity &gt; 0, unit.
      Column names are matched flexibly. If any row is invalid, nothing is imported.</div>
      <div class="form"><label class="field full"><span>File <span class="req">*</span></span><input type="file" name="file" accept=".csv,.json"></label>
      <label class="field full"><span>BOM name (optional)</span><input name="name" placeholder="Defaults to the name in the file or the file name"></label></div>`,
    onSubmit: async (m, showErr) => {
      const f = $("[name=file]", m).files[0];
      if (!f) { showErr("Choose a file to import."); return false; }
      const fd = new FormData(); fd.append("file", f);
      const name = $("[name=name]", m).value.trim(); if (name) fd.append("name", name);
      const bom = await api("POST", `/api/projects/${S.project.id}/boms/import`, fd, true);
      toast(`Imported ${bom.item_count} items into “${esc(bom.name)}”`); render();
    },
  });
}

function manualBomModal() {
  const row = () => `<tr>${["item_code", "name", "specification", "category"].map((k) => `<td><input data-k="${k}" ${k === "name" ? 'placeholder="required"' : ""}></td>`).join("")}
    <td><input data-k="quantity" type="number" step="any" min="0" style="width:90px"></td><td><input data-k="unit" style="width:70px"></td>
    <td><input data-k="est_rate" type="number" step="any" min="0" style="width:100px"></td><td><button class="btn sm danger" data-rm>✕</button></td></tr>`;
  openModal({
    title: "Create sample BOM manually", wide: true, submitLabel: "Create BOM",
    body: `<div class="alert warn">Manually created BOMs are tagged <b>User-created test data</b> and kept separate from imported project data.</div>
      <div class="form"><label class="field full"><span>BOM name <span class="req">*</span></span><input name="bomname"></label></div><br>
      <div class="tbl-wrap"><table><thead><tr><th>Code</th><th>Material *</th><th>Specification</th><th>Category</th><th>Qty *</th><th>Unit *</th><th>Est. rate</th><th></th></tr></thead><tbody id="mrows">${row()}${row()}${row()}</tbody></table></div>
      <br><button class="btn" id="addRow">+ Add row</button> <span class="spec">Blank rows are ignored.</span>`,
    onOpen: (m) => {
      const bindRm = () => $$("[data-rm]", m).forEach((b) => (b.onclick = () => b.closest("tr").remove()));
      $("#addRow", m).onclick = () => { $("#mrows", m).insertAdjacentHTML("beforeend", row()); bindRm(); };
      bindRm();
    },
    onSubmit: async (m, showErr) => {
      const name = $("[name=bomname]", m).value.trim();
      const items = [], errs = [];
      $$("#mrows tr", m).forEach((tr, i) => {
        const g = (k) => $(`[data-k="${k}"]`, tr).value.trim();
        if (!["item_code", "name", "specification", "category", "quantity", "unit", "est_rate"].some((k) => g(k))) return;
        const q = Number(g("quantity")), rate = g("est_rate");
        const e = [];
        if (!g("name")) e.push("material name"); if (!(q > 0)) e.push("quantity > 0"); if (!g("unit")) e.push("unit");
        if (rate && (isNaN(Number(rate)) || Number(rate) < 0)) e.push("rate ≥ 0");
        if (e.length) errs.push(`Row ${i + 1}: needs ${e.join(", ")}`);
        else items.push({ item_code: g("item_code") || null, name: g("name"), specification: g("specification") || null, category: g("category") || null, quantity: q, unit: g("unit"), est_rate: rate ? Number(rate) : null });
      });
      if (!name) errs.unshift("BOM name is required");
      if (!items.length && !errs.length) errs.push("Add at least one material");
      if (errs.length) { showErr(`<ul>${errs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`); return false; }
      await api("POST", `/api/projects/${S.project.id}/boms/manual`, { name, items });
      toast("Test BOM created"); render();
    },
  });
}

async function bomModal(bid) {
  const bom = await api("GET", `/api/boms/${bid}`);
  const md = openModal({
    title: `${esc(bom.name)} ${sourceBadge(bom.source)}`, wide: true, cancelLabel: "Close",
    body: `${bom.read_only ? `<div class="alert info">Imported BOMs are read-only snapshots of the approved BOM. Make changes in a procurement list.</div>` : ""}
      ${bom.source_format === "constructai_bom" ? `<p class="spec">ConstructAI BOM${bom.external_ref ? ` · BOQ ${esc(bom.external_ref)}` : ""} · source total ₹ ${money(bom.source_total)} · rates are ConstructAI catalog suggestions — verify with vendor quotations before ordering.</p>` : ""}
      ${bom.approved_by ? `<div class="alert info">Approved in ConstructAI by <b>${esc(bom.approved_by)}</b> on ${dt(bom.approved_at)}${bom.approval_comment ? ` — “${esc(bom.approval_comment)}”` : ""}</div>` : ""}
      ${bom.import_warnings.length ? `<div class="alert warn"><b>Warnings carried from the source BOM</b><ul>${bom.import_warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
      <div id="bomItems"></div>`,
  });
  const reload = async () => { const b = await api("GET", `/api/boms/${bid}`); tbl.redraw(b.items); };
  const tbl = itemTable($("#bomItems", md.el), {
    items: bom.items, editable: !bom.read_only,
    onSave: async (id, vals) => { await api("PUT", `/api/boms/${bid}/items/${id}`, vals); await reload(); },
    onDelete: async (id) => { await api("DELETE", `/api/boms/${bid}/items/${id}`); await reload(); },
    onAdd: async (vals) => { await api("POST", `/api/boms/${bid}/items`, vals); await reload(); },
  });
}

function newListModal(bomId, bomName) {
  const fields = [{ name: "name", label: "Procurement list name", required: true }];
  openModal({
    title: bomId ? "Create procurement list from BOM" : "New empty procurement list", submitLabel: "Create",
    body: `${bomId ? `<p>Materials from <b>${esc(bomName)}</b> will be <i>copied</i> into a new editable list. The BOM itself stays unchanged.</p>` : ""}${formHTML(fields, { name: bomId ? `Procurement - ${bomName}` : "" })}`,
    onSubmit: async (m) => {
      const vals = readForm(m, fields); if (!vals) return false;
      const pl = await api("POST", `/api/projects/${S.project.id}/procurement-lists`, { name: vals.name, bom_id: bomId });
      store.set("listId", pl.id); await loadLists(); go("procurement"); toast("Procurement list created");
    },
  });
}

// ---------------------------------------------------------------- 2. procurement materials
function materialForm(listId, it, after) {
  openModal({
    title: it ? `Edit material · ${esc(it.name)}` : "Add material", wide: true, submitLabel: it ? "Save" : "Add",
    body: `<p class="spec">Changes here never alter the approved BOM.</p>${formHTML(ITEM_FIELDS, it || {})}`,
    onSubmit: async (m) => {
      const vals = readForm(m, ITEM_FIELDS); if (!vals) return false;
      vals.notes = it ? it.notes ?? null : null;
      if (it) await api("PUT", `/api/procurement-lists/${listId}/items/${it.id}`, vals);
      else await api("POST", `/api/procurement-lists/${listId}/items`, vals);
      toast(it ? "Material updated" : "Material added"); after();
    },
  });
}

const readyToOrder = (items) => items.filter((i) => i.vendor_id && i.agreed_rate != null && i.remaining > 1e-9);

function placeOrdersModal(list, vendors) {
  const vname = (id) => vendors.find((x) => x.id === id)?.name || `Vendor ${id}`;
  const ready = readyToOrder(list.items);
  const noRate = list.items.filter((i) => i.vendor_id && i.agreed_rate == null && i.remaining > 1e-9);
  const noVendor = list.items.filter((i) => !i.vendor_id && i.remaining > 1e-9);
  const groups = {};
  ready.forEach((i) => (groups[i.vendor_id] = groups[i.vendor_id] || []).push(i));
  let grand = 0;
  const tables = Object.entries(groups).map(([vid, items]) => {
    const sub = items.reduce((a, i) => a + i.remaining * i.agreed_rate, 0); grand += sub;
    return `<h3 style="margin:14px 0 6px">${esc(vname(Number(vid)))} <span class="spec">· ${items.length} item(s) · ${money(sub)} before tax</span></h3>
      <div class="tbl-wrap"><table><thead><tr><th>Material</th><th class="num">Qty</th><th>Unit</th><th class="num">Agreed rate</th><th class="num">Amount</th></tr></thead><tbody>
      ${items.map((i) => `<tr><td>${esc(i.name)}</td><td class="num">${qty(i.remaining)}</td><td>${esc(i.unit)}</td><td class="num">${money(i.agreed_rate)}</td><td class="num">${money(i.remaining * i.agreed_rate)}</td></tr>`).join("")}</tbody></table></div>`;
  }).join("");
  openModal({
    title: `Place orders · ${Object.keys(groups).length} purchase order(s)`, wide: true, submitLabel: "Create draft purchase orders",
    body: `${noRate.length ? `<div class="alert error">Enter the agreed rate for: ${noRate.map((i) => esc(i.name)).join(", ")}</div>` : ""}
      ${noVendor.length ? `<div class="alert warn">${noVendor.length} material(s) have no vendor yet and will stay on the list for later: ${noVendor.map((i) => esc(i.name)).join(", ")}</div>` : ""}
      <div class="alert info">One draft PO is created per vendor for the quantity not yet ordered. Each PO still needs review and approval before it is final; nothing is sent automatically.</div>
      ${tables}<p style="text-align:right;margin-top:10px"><b>Total before tax: ${money(grand)}</b></p>
      <div class="form">${fieldHTML({ name: "delivery_address", label: "Delivery address", type: "textarea" }, S.project.default_delivery_address)}
        ${fieldHTML({ name: "delivery_date", label: "Required delivery date", type: "date" })}
        ${fieldHTML({ name: "tax_pct", label: "Tax %", type: "number", min: 0, step: "any", hint: "Editable per PO later" }, 0)}</div>`,
    onSubmit: async (m, showErr) => {
      if (noRate.length) { showErr("Enter the missing agreed rates on the Materials list first."); return false; }
      const pos = await api("POST", `/api/procurement-lists/${list.id}/place-orders`, {
        delivery_address: $("[name=delivery_address]", m).value.trim() || null, delivery_date: $("[name=delivery_date]", m).value || null,
        tax_pct: Number($("[name=tax_pct]", m).value || 0), actor: getActor() || null,
      });
      toast(`${pos.length} draft PO(s) created: ${pos.map((x) => x.po_number).join(", ")}`);
      S.sel.po = pos[0].id; go("pos");
    },
  });
}

VIEWS.procurement = async (v) => {
  if (needList(v)) return;
  await Promise.all([refreshList(), loadVendors()]);
  const [quotes, lastPaid] = await Promise.all([api("GET", `/api/quotations?list_id=${S.list.id}`),
    api("GET", `/api/purchase-history/last-rates?list_id=${S.list.id}`)]);
  const l = S.list, sup = suppliers();
  const ready = readyToOrder(l.items);
  const readyVendors = new Set(ready.map((i) => i.vendor_id)).size;
  const quoteFor = (itemId, vendorId) => quotes.filter((q) => q.procurement_item_id === itemId && q.vendor_id === vendorId && !q.expired)
    .sort((a, b) => a.rate - b.rate)[0];
  const lowest = (itemId) => quotes.filter((q) => q.procurement_item_id === itemId && q.vendor_active && !q.expired).sort((a, b) => a.rate - b.rate)[0];
  let estTotal = 0, orderTotal = 0;
  const rows = l.items.map((it) => {
    if (it.est_rate != null) estTotal += it.quantity * it.est_rate;
    const open = it.remaining > 1e-9;
    const value = it.agreed_rate != null && open ? it.remaining * it.agreed_rate : null;
    if (value != null && it.vendor_id) orderTotal += value;
    const pct = Math.min(100, (it.allocated_total / it.quantity) * 100);
    const low = lowest(it.id);
    const vendorCell = open
      ? `<select data-vendor="${it.id}"><option value="">— choose vendor —</option>${sup.map((x) => { const q = quoteFor(it.id, x.id);
          return `<option value="${x.id}" ${x.id === it.vendor_id ? "selected" : ""}>${esc(x.name)}${q ? ` (quote ${money(q.rate)})` : ""}</option>`; }).join("")}</select>`
      : `<span class="badge ok">Ordered</span>`;
    const rateCell = open
      ? `<input data-rate="${it.id}" type="number" step="any" min="0" value="${it.agreed_rate ?? ""}" placeholder="agreed rate" style="width:110px">
         ${low ? `<div class="spec">lowest quote ${money(low.rate)} · ${esc(low.vendor_name)}</div>` : ""}
         ${lastPaid[it.id] ? `<div class="spec" title="${esc(lastPaid[it.id].po_number)} · ${esc(lastPaid[it.id].project_name)}">last paid ${money(lastPaid[it.id].rate)} · ${esc(lastPaid[it.id].vendor_name)} · ${dt(lastPaid[it.id].date.slice(0, 10))}</div>` : ""}`
      : (it.agreed_rate != null ? money(it.agreed_rate) : "—");
    return `<tr data-id="${it.id}"><td><b>${esc(it.name)}</b>${it.item_code ? ` <span class="spec">${esc(it.item_code)}</span>` : ""}${it.specification ? `<div class="spec">${esc(it.specification)}</div>` : ""}${traceLine(it)}</td>
      <td>${esc(it.category || "")}</td><td class="num">${qty(it.quantity)} ${esc(it.unit)}</td>
      <td class="num">${it.est_rate != null ? money(it.est_rate) : `<span class="badge warn">No estimate</span>`}</td>
      <td class="num">${qty(it.allocated_total)} / ${qty(it.quantity)}<div class="bar"><i class="${pct >= 99.99 ? "full" : ""}" style="width:${pct}%"></i></div>${open ? `<div class="spec">${qty(it.remaining)} to order</div>` : ""}</td>
      <td>${vendorCell}</td><td class="num">${rateCell}</td><td class="num">${money(value)}</td>
      <td><div class="actions"><button class="btn sm" data-edit="${it.id}">Edit</button><button class="btn sm danger" data-del="${it.id}">Remove</button></div></td></tr>`;
  }).join("");
  v.innerHTML = `<h1>Materials</h1><p class="sub">Choose a vendor and agreed rate for each material, then place the orders: one purchase order per vendor. Estimated rates come from the BOM catalog and are for reference only. Changes here never alter the approved BOM.</p>
    <div class="card"><div class="card-h"><h2>${esc(l.name)}</h2>${l.bom_name ? sourceBadge(l.bom_source) : `<span class="badge">Manual list</span>`}
      <div class="spacer"></div><button class="btn" id="addMat">+ Add material</button>
      <button class="btn primary" id="placeOrders" ${ready.length ? "" : "disabled"}>Place orders${readyVendors ? ` (${readyVendors} vendor${readyVendors > 1 ? "s" : ""})` : ""}</button></div>
      ${sup.length && l.items.some((i) => !i.vendor_id && i.remaining > 1e-9) ? `<div class="card-b row" style="border-bottom:1px solid var(--line)">
        <span class="spec">Set one vendor for all materials without a vendor:</span>
        <select id="bulkVendor"><option value="">— choose vendor —</option>${sup.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
        <button class="btn sm" id="bulkApply">Apply</button><span class="spec">Rates fill from that vendor's quotation where one exists; otherwise enter them.</span></div>` : ""}
      ${sup.length ? "" : `<div class="card-b"><div class="alert warn">No active suppliers yet. Add vendors in <b>Vendors & quotes</b> first. <button class="btn sm" id="goVendors">Add vendors</button></div></div>`}
      <div class="card-b flush"><div class="tbl-wrap"><table><thead><tr><th>Material</th><th>Category</th><th class="num">Quantity</th><th class="num">Est. rate</th><th class="num">Ordered</th><th>Vendor</th><th class="num">Agreed rate</th><th class="num">Order value</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="9" class="empty">No materials yet.</td></tr>`}</tbody>
      <tfoot><tr><td colspan="3">Estimated total (BOM catalog rates)</td><td class="num">${money(estTotal)}</td><td colspan="3" style="text-align:right">Ready to order (${ready.length} item${ready.length === 1 ? "" : "s"})</td><td class="num">${money(orderTotal)}</td><td></td></tr></tfoot>
      </table></div></div></div>`;

  const reload = () => render();
  const save = async (itemId, body) => {
    try { await api("PUT", `/api/procurement-lists/${l.id}/items/${itemId}/vendor`, body); reload(); } catch (e) { fail(e); reload(); }
  };
  $$("[data-vendor]", v).forEach((sel) => (sel.onchange = () => {
    const itemId = Number(sel.dataset.vendor), vendorId = sel.value ? Number(sel.value) : null;
    const rateVal = $(`[data-rate="${itemId}"]`, v).value;
    if (!vendorId) return save(itemId, { vendor_id: null });
    const q = quoteFor(itemId, vendorId);
    if (q && rateVal === "") return save(itemId, { vendor_id: vendorId, quotation_id: q.id });  // rate from the vendor's own quote
    save(itemId, { vendor_id: vendorId, agreed_rate: rateVal === "" ? null : Number(rateVal) });
  }));
  $$("[data-rate]", v).forEach((inp) => (inp.onchange = () => {
    const itemId = Number(inp.dataset.rate), vendorId = Number($(`[data-vendor="${itemId}"]`, v).value) || null;
    if (inp.value !== "" && (isNaN(Number(inp.value)) || Number(inp.value) < 0)) { inp.classList.add("invalid"); toast("Rate must be 0 or more", true); return; }
    if (!vendorId) { toast("Choose the vendor first, then enter the agreed rate.", true); return; }
    save(itemId, { vendor_id: vendorId, agreed_rate: inp.value === "" ? null : Number(inp.value) });
  }));
  $$("[data-edit]", v).forEach((b) => (b.onclick = () => materialForm(l.id, l.items.find((i) => i.id === Number(b.dataset.edit)), reload)));
  $$("[data-del]", v).forEach((b) => (b.onclick = async () => {
    const it = l.items.find((i) => i.id === Number(b.dataset.del));
    if (!confirm(`Remove "${it.name}" from this list?`)) return;
    try { await api("DELETE", `/api/procurement-lists/${l.id}/items/${it.id}`); toast("Material removed"); reload(); } catch (e) { fail(e); }
  }));
  $("#addMat").onclick = () => materialForm(l.id, null, reload);
  const bulk = $("#bulkApply");
  if (bulk) bulk.onclick = async () => {
    const vendorId = Number($("#bulkVendor").value);
    if (!vendorId) { toast("Choose a vendor first.", true); return; }
    const targets = l.items.filter((i) => !i.vendor_id && i.remaining > 1e-9);
    bulk.disabled = true;
    try {
      for (const it of targets) {
        const q = quoteFor(it.id, vendorId);
        await api("PUT", `/api/procurement-lists/${l.id}/items/${it.id}/vendor`, q ? { vendor_id: vendorId, quotation_id: q.id } : { vendor_id: vendorId });
      }
      toast(`Vendor set for ${targets.length} material(s). Enter the agreed rates, then place orders.`);
    } catch (e) { fail(e); }
    reload();
  };
  $("#placeOrders").onclick = () => placeOrdersModal(S.list, S.vendors);
  const gv = $("#goVendors"); if (gv) gv.onclick = () => { S.vendorTab = "directory"; go("vendors"); };
};

// ---------------------------------------------------------------- 3. vendors
const VENDOR_FIELDS = [
  { name: "name", label: "Company name", required: true },
  { name: "kind", label: "Type", type: "select", options: [["supplier", "Material supplier"], ["contractor", "Contractor"], ["both", "Supplier & contractor"]] },
  { name: "contact_person", label: "Contact person" }, { name: "phone", label: "Phone" }, { name: "email", label: "Email", type: "email" },
  { name: "tax_id", label: "Tax ID (e.g. GSTIN)" }, { name: "address", label: "Address", type: "textarea", full: true },
  { name: "materials_supplied", label: "Materials / specifications supplied", type: "textarea", hint: "Free text, searchable" },
  { name: "trades", label: "Trades / work executed (contractors)", type: "textarea" },
  { name: "payment_terms", label: "Default payment terms", type: "textarea" },
  { name: "lead_time_days", label: "Typical lead time (days)", type: "number", min: 0, int: true },
  { name: "notes", label: "Notes", type: "textarea" }, { name: "active", label: "Active", type: "checkbox", default: true },
];

function vendorForm(vd, preset = {}) {
  openModal({
    title: vd ? `Edit ${esc(vd.name)}` : "New vendor / contractor", wide: true,
    body: `<p class="spec">Only enter details you have received from the vendor — nothing is auto-filled.</p>${formHTML(VENDOR_FIELDS, vd || { active: true, ...preset })}`,
    onSubmit: async (m) => {
      const vals = readForm(m, VENDOR_FIELDS); if (!vals) return false;
      vd ? await api("PUT", `/api/vendors/${vd.id}`, vals) : await api("POST", "/api/vendors", vals);
      await loadVendors(); render(); toast(vd ? "Vendor updated" : "Vendor created");
    },
  });
}

const viewVendorDirectory = async (v) => {
  const q = store.get("vq", ""), kind = store.get("vkind", ""), showInactive = store.get("vinactive", true);
  const list = await api("GET", `/api/vendors?q=${encodeURIComponent(q)}&kind=${kind}&include_inactive=${showInactive}`);
  await loadVendors();
  v.innerHTML = `<h1>Vendor directory</h1><p class="sub">Suppliers and contractors. Deactivated vendors stay on record but cannot receive new POs or packages.</p>
    <div class="card"><div class="card-h"><input id="vq" placeholder="Search name, material, trade, contact…" value="${esc(q)}" style="min-width:260px">
      <select id="vkind"><option value="">All types</option><option value="supplier" ${kind === "supplier" ? "selected" : ""}>Suppliers</option><option value="contractor" ${kind === "contractor" ? "selected" : ""}>Contractors</option></select>
      <label class="check"><input type="checkbox" id="vin" ${showInactive ? "checked" : ""}> Show inactive</label><div class="spacer"></div><button class="btn primary" id="newV">+ New vendor</button></div>
      <div class="card-b flush">${list.length ? `<div class="tbl-wrap"><table><thead><tr><th>Vendor</th><th>Type</th><th>Contact</th><th>Supplies / trades</th><th>Payment terms</th><th class="num">Lead time</th><th class="num">Quotes</th><th>Status</th><th></th></tr></thead><tbody>
      ${list.map((x) => `<tr class="${x.active ? "" : "muted"}"><td><b>${esc(x.name)}</b>${x.tax_id ? `<div class="spec">${esc(x.tax_id)}</div>` : ""}</td>
        <td>${{ supplier: "Supplier", contractor: "Contractor", both: "Supplier & contractor" }[x.kind]}</td>
        <td>${esc(x.contact_person || "")}<div class="spec">${esc([x.phone, x.email].filter(Boolean).join(" · "))}</div></td>
        <td class="spec">${esc([x.materials_supplied, x.trades].filter(Boolean).join(" | ") || "—")}</td><td class="spec">${esc(x.payment_terms || "—")}</td>
        <td class="num">${x.lead_time_days != null ? x.lead_time_days + " d" : "—"}</td><td class="num">${x.quotation_count}</td>
        <td>${x.active ? `<span class="badge ok">Active</span>` : `<span class="badge">Inactive</span>`}</td>
        <td><div class="actions"><button class="btn sm" data-ev="${x.id}">Edit</button><button class="btn sm" data-qv="${x.id}">Quotes</button>
          ${x.active ? `<button class="btn sm danger" data-deact="${x.id}">Deactivate</button>` : `<button class="btn sm" data-act-v="${x.id}">Reactivate</button>`}</div></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty"><b>No vendors found</b>${q || kind ? "Try a different search." : "Add your first supplier or contractor."}</div>`}</div></div>`;
  let t;
  $("#vq").oninput = (e) => { clearTimeout(t); t = setTimeout(() => { store.set("vq", e.target.value); render().then(() => { const i = $("#vq"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 300); };
  $("#vkind").onchange = (e) => { store.set("vkind", e.target.value); render(); };
  $("#vin").onchange = (e) => { store.set("vinactive", e.target.checked); render(); };
  $("#newV").onclick = () => vendorForm(null);
  $$("[data-ev]", v).forEach((b) => (b.onclick = () => vendorForm(list.find((x) => x.id === Number(b.dataset.ev)))));
  $$("[data-deact]", v).forEach((b) => (b.onclick = async () => { try { await api("POST", `/api/vendors/${b.dataset.deact}/deactivate`); render(); toast("Vendor deactivated"); } catch (e) { fail(e); } }));
  $$("[data-act-v]", v).forEach((b) => (b.onclick = async () => { try { await api("POST", `/api/vendors/${b.dataset.actV}/activate`); render(); toast("Vendor reactivated"); } catch (e) { fail(e); } }));
  $$("[data-qv]", v).forEach((b) => (b.onclick = async () => {
    const vd = await api("GET", `/api/vendors/${b.dataset.qv}`);
    openModal({ title: `Quotations · ${esc(vd.name)}`, wide: true, cancelLabel: "Close",
      body: vd.quotations.length ? `<div class="tbl-wrap"><table><thead><tr><th>Ref</th><th>Material</th><th class="num">Rate</th><th>Unit</th><th class="num">Available</th><th class="num">Lead</th><th>Valid until</th><th>Terms</th></tr></thead><tbody>
        ${vd.quotations.map((q) => `<tr><td>${esc(q.quote_ref || "")}</td><td>${esc(q.material_name)}<div class="spec">${esc(q.specification || "")}</div></td><td class="num">${money(q.rate)}</td><td>${esc(q.unit || "")}</td>
          <td class="num">${qty(q.available_qty)}</td><td class="num">${q.lead_time_days ?? "—"}</td><td>${dt(q.valid_until)} ${q.expired ? `<span class="badge bad">Expired</span>` : ""}</td><td class="spec">${esc(q.payment_terms || "")}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">No quotations recorded for this vendor.</div>` });
  }));
};

// ---------------------------------------------------------------- 4. quotation comparison
function quoteFields(items) {
  return [
    { name: "procurement_item_id", label: "Material (procurement list)", type: "select", numeric: true, required: true, options: [["", "— select —"], ...items.map((i) => [i.id, `${i.name} (${qty(i.quantity)} ${i.unit})`])] },
    { name: "vendor_id", label: "Vendor", type: "select", numeric: true, required: true, options: [["", "— select —"], ...suppliers().map((x) => [x.id, x.name])] },
    { name: "quote_ref", label: "Quotation reference" }, { name: "material_name", label: "Material as quoted", required: true },
    { name: "specification", label: "Specification as quoted", type: "textarea" },
    { name: "unit", label: "Unit", hint: "Must match the material unit" }, { name: "rate", label: "Rate per unit", type: "number", required: true, gt: 0, step: "any" },
    { name: "available_qty", label: "Available quantity", type: "number", min: 0, step: "any" },
    { name: "lead_time_days", label: "Delivery time (days)", type: "number", min: 0, int: true },
    { name: "valid_until", label: "Valid until", type: "date" }, { name: "payment_terms", label: "Payment terms", type: "textarea" },
    { name: "notes", label: "Notes", type: "textarea" },
  ];
}

function quoteForm(q, preset = {}) {
  if (!suppliers().length) { toast("Add an active supplier in the Vendor directory first.", true); return; }
  const fields = quoteFields(S.list.items);
  const m = openModal({
    title: q ? "Edit quotation" : "Record quotation", wide: true,
    body: `<p class="spec">Enter the figures exactly as quoted by the vendor.</p>${formHTML(fields, q || preset)}`,
    onOpen: (el) => {
      const sel = $("[name=procurement_item_id]", el);
      sel.onchange = () => {
        const it = S.list.items.find((i) => i.id === Number(sel.value)); if (!it) return;
        const mn = $("[name=material_name]", el), un = $("[name=unit]", el), sp = $("[name=specification]", el);
        if (!mn.value) mn.value = it.name; if (!un.value) un.value = it.unit; if (!sp.value && it.specification) sp.value = it.specification;
      };
    },
    onSubmit: async (el) => {
      const vals = readForm(el, fields); if (!vals) return false;
      q ? await api("PUT", `/api/quotations/${q.id}`, vals) : await api("POST", "/api/quotations", vals);
      toast("Quotation saved"); render();
    },
  });
  return m;
}

const viewQuotes = async (v) => {
  if (needList(v)) return;
  await Promise.all([loadVendors(), refreshList()]);
  const cmp = await api("GET", `/api/procurement-lists/${S.list.id}/comparison`);
  v.innerHTML = `<h1>Quotation comparison</h1><p class="sub">Multiple quotations per material. Lowest = cheapest valid quote from an active vendor (expired quotes excluded). Variance is against the BOM estimate.</p>
    <div class="row" style="margin-bottom:14px"><span class="spec">${cmp.filter((c) => c.quote_count).length} of ${cmp.length} materials have quotations</span><div class="spacer"></div><button class="btn primary" id="addQ">+ Record quotation</button></div>
    ${cmp.length ? cmp.map((c) => `<div class="card"><div class="card-h"><div><h2>${esc(c.item.name)}</h2><div class="spec">${esc(c.item.specification || "")}</div></div><div class="spacer"></div>
      <span class="spec">Required ${qty(c.item.quantity)} ${esc(c.item.unit)} · remaining ${qty(c.item.remaining)} · est. rate ${money(c.item.est_rate)}</span>
      <button class="btn sm" data-addq="${c.item.id}">+ Quote</button></div>
      <div class="card-b flush">${c.quotations.length ? `<div class="tbl-wrap"><table><thead><tr><th>Vendor</th><th>Ref</th><th class="num">Rate</th><th class="num">vs estimate</th><th class="num">Amount for req. qty</th><th class="num">Available</th><th class="num">Delivery</th><th>Payment terms</th><th>Valid until</th><th></th></tr></thead><tbody>
        ${c.quotations.map((q) => `<tr class="${q.vendor_active && !q.expired ? "" : "muted"}"><td><b>${esc(q.vendor_name)}</b> ${q.is_lowest ? `<span class="badge ok">Lowest</span>` : ""}${!q.vendor_active ? ` <span class="badge">Inactive vendor</span>` : ""}</td>
          <td class="spec">${esc(q.quote_ref || "")}</td><td class="num"><b>${money(q.rate)}</b></td>
          <td class="num">${q.variance_vs_estimate_pct == null ? "—" : `<span class="badge ${q.variance_vs_estimate_pct > 0 ? "bad" : "ok"}">${q.variance_vs_estimate_pct > 0 ? "+" : ""}${q.variance_vs_estimate_pct}%</span>`}</td>
          <td class="num">${money(q.amount_for_required_qty)}</td>
          <td class="num">${q.available_qty == null ? "—" : `${qty(q.available_qty)} ${q.covers_required_qty ? "" : `<span class="badge warn">Partial</span>`}`}</td>
          <td class="num">${q.lead_time_days != null ? q.lead_time_days + " d" : "—"}</td><td class="spec">${esc(q.payment_terms || "—")}</td>
          <td>${dt(q.valid_until)} ${q.expired ? `<span class="badge bad">Expired</span>` : ""}</td>
          <td><div class="actions"><button class="btn sm primary" data-usepo="${q.id}" data-item="${c.item.id}" ${q.vendor_active && c.item.remaining > 0 ? "" : "disabled"}>Use this quote</button>
            <button class="btn sm" data-eq="${q.id}">Edit</button><button class="btn sm danger" data-dq="${q.id}">Delete</button></div></td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">No quotations yet for this material.</div>`}</div></div>`).join("") : `<div class="card"><div class="empty">The procurement list is empty.</div></div>`}`;
  const all = cmp.flatMap((c) => c.quotations);
  $("#addQ").onclick = () => quoteForm(null);
  $$("[data-addq]", v).forEach((b) => (b.onclick = () => { const it = S.list.items.find((i) => i.id === Number(b.dataset.addq)); quoteForm(null, { procurement_item_id: it.id, material_name: it.name, unit: it.unit, specification: it.specification }); }));
  $$("[data-eq]", v).forEach((b) => (b.onclick = () => quoteForm(all.find((q) => q.id === Number(b.dataset.eq)))));
  $$("[data-dq]", v).forEach((b) => (b.onclick = async () => { if (!confirm("Delete this quotation?")) return; try { await api("DELETE", `/api/quotations/${b.dataset.dq}`); render(); } catch (e) { fail(e); } }));
  $$("[data-usepo]", v).forEach((b) => (b.onclick = async () => {
    const q = all.find((x) => x.id === Number(b.dataset.usepo));
    try {
      await api("PUT", `/api/procurement-lists/${S.list.id}/items/${b.dataset.item}/vendor`, { quotation_id: q.id });
      toast(`${esc(q.vendor_name)} @ ${money(q.rate)} set on Materials`); render();
    } catch (e) { fail(e); }
  }));
};

// ---------------------------------------------------------------- 5. purchase orders
VIEWS.pos = async (v) => {
  if (needProject(v)) return;
  const pos = await api("GET", `/api/projects/${S.project.id}/purchase-orders?include_superseded=${S.showSuperseded}`);
  v.innerHTML = `<h1>Purchase orders</h1><p class="sub">One PO per vendor. Draft and returned POs are editable; approved POs change only through revisions.</p>
    <div class="card"><div class="card-h"><h2>${pos.length} purchase order(s)</h2><label class="check"><input type="checkbox" id="ss" ${S.showSuperseded ? "checked" : ""}> Show superseded revisions</label>
      <div class="spacer"></div><button class="btn primary" id="genPO" ${S.list ? "" : "disabled"}>+ New orders from Materials</button></div>
      <div class="card-b flush">${pos.length ? `<div class="tbl-wrap"><table><thead><tr><th>PO</th><th>Vendor</th><th class="num">Lines</th><th class="num">Total</th><th>Delivery</th><th>Status</th><th>Updated</th></tr></thead><tbody>
      ${pos.map((p) => `<tr class="clickable ${S.sel.po === p.id ? "selected" : ""} ${p.superseded_by_id ? "muted" : ""}" data-po="${p.id}"><td><b>${revLabel(p.po_number, p.revision)}</b></td><td>${esc(p.vendor.name)}</td>
        <td class="num">${p.lines.length}</td><td class="num">${money(p.total)}</td><td>${dt(p.delivery_date)}</td><td>${statusBadge(p.status, p.superseded_by_id)}</td><td>${dt(p.updated_at)}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty"><b>No purchase orders yet</b>Assign materials to vendors from the procurement list or quotation comparison.</div>`}</div></div>
    <div id="poDetail"></div>`;
  $("#ss").onchange = (e) => { S.showSuperseded = e.target.checked; render(); };
  $("#genPO").onclick = () => go("procurement");
  $$("[data-po]", v).forEach((tr) => (tr.onclick = () => { S.sel.po = Number(tr.dataset.po); render(); }));
  if (S.sel.po && pos.some((p) => p.id === S.sel.po)) await poDetail($("#poDetail", v), S.sel.po);
};

async function poDetail(el, id) {
  const po = await api("GET", `/api/purchase-orders/${id}`);
  const pl = await api("GET", `/api/procurement-lists/${po.list_id}`);
  const itemById = Object.fromEntries(pl.items.map((i) => [i.id, i]));
  const ed = po.editable;
  el.innerHTML = `<div class="card"><div class="card-h"><h2>PO ${revLabel(po.po_number, po.revision)}</h2>${statusBadge(po.status, po.superseded_by_id)}<div class="spacer"></div><div class="actions">${actionButtons("po", po)}</div></div>
    <div class="card-b">
      ${po.status === "returned" ? `<div class="alert warn">Returned for revision — see the reviewer comment in history below, edit and resubmit.</div>` : ""}
      <div class="doc-head">
        <div class="kv"><div class="k">Vendor</div><div class="v"><b>${esc(po.vendor.name)}</b>\n${esc([po.vendor.contact_person, po.vendor.phone, po.vendor.email].filter(Boolean).join(" · "))}\n${esc(po.vendor.address || "")}</div></div>
        <div class="kv"><div class="k">Project</div><div class="v">${esc(S.project.name)}${S.project.code ? " · " + esc(S.project.code) : ""}\nList: ${esc(pl.name)}</div></div>
      </div><br>
      <div class="form">
        ${fieldHTML({ name: "delivery_address", label: "Delivery address", type: "textarea" }, po.delivery_address)}
        ${fieldHTML({ name: "delivery_date", label: "Delivery date", type: "date" }, po.delivery_date)}
        ${fieldHTML({ name: "payment_terms", label: "Payment terms", type: "textarea" }, po.payment_terms)}
        ${fieldHTML({ name: "tax_pct", label: "Tax %", type: "number", min: 0, step: "any" }, po.tax_pct)}
        ${fieldHTML({ name: "notes", label: "Notes / special conditions", type: "textarea", full: true }, po.notes)}
      </div><br>
      <div class="tbl-wrap"><table><thead><tr><th>Description</th><th>Specification</th><th class="num">Qty</th><th>Unit</th><th class="num">Rate</th><th class="num">Amount</th>${ed ? "<th></th>" : ""}</tr></thead><tbody id="poLines"></tbody></table></div>
      ${ed ? `<div class="row" style="margin-top:10px"><select id="addLineSel"><option value="">+ Add material…</option>${pl.items.filter((i) => i.remaining > 0 && !po.lines.some((l) => l.procurement_item_id === i.id)).map((i) => `<option value="${i.id}">${esc(i.name)} (remaining ${qty(i.remaining)} ${esc(i.unit)})</option>`).join("")}</select></div>` : ""}
      <div class="row"><div class="totals" id="poTotals"></div></div>
      ${ed ? `<div class="row" style="justify-content:flex-end;margin-top:10px"><button class="btn primary" id="savePO">Save changes</button></div>` : ""}
      <h2 style="margin-top:18px">Document history</h2><div id="poHist"></div>
    </div></div>`;
  if (!ed) $$(".form input, .form textarea, .form select", el).forEach((i) => (i.disabled = true));
  let lines = po.lines.map((l) => ({ ...l }));
  const maxFor = (l) => { const it = itemById[l.procurement_item_id]; const own = po.lines.filter((x) => x.procurement_item_id === l.procurement_item_id).reduce((a, x) => a + x.quantity, 0); return it ? it.remaining + (po.superseded_by_id || po.status === "rejected" ? 0 : own) : Infinity; };
  const draw = () => {
    $("#poLines", el).innerHTML = lines.map((l, i) => ed ? `<tr data-i="${i}"><td><input data-k="description" value="${esc(l.description)}"></td><td><input data-k="specification" value="${esc(l.specification || "")}"></td>
        <td class="num"><input data-k="quantity" type="number" step="any" min="0" value="${l.quantity}" style="width:100px"><div class="spec">max ${qty(maxFor(l))}</div></td><td>${esc(l.unit)}</td>
        <td class="num"><input data-k="rate" type="number" step="any" min="0" value="${l.rate}" style="width:110px"></td><td class="num" data-amt>${money(l.quantity * l.rate)}</td><td><button class="btn sm danger" data-rm>✕</button></td></tr>`
      : `<tr><td>${esc(l.description)}</td><td class="spec">${esc(l.specification || "")}</td><td class="num">${qty(l.quantity)}</td><td>${esc(l.unit)}</td><td class="num">${money(l.rate)}</td><td class="num">${money(l.quantity * l.rate)}</td></tr>`).join("")
      || `<tr><td colspan="7" class="empty">No lines.</td></tr>`;
    totals();
    $$("#poLines tr[data-i]", el).forEach((tr) => {
      const l = lines[Number(tr.dataset.i)];
      $$("input", tr).forEach((inp) => (inp.oninput = () => {
        const k = inp.dataset.k; l[k] = ["quantity", "rate"].includes(k) ? Number(inp.value) : inp.value;
        $("[data-amt]", tr).textContent = money(l.quantity * l.rate);
        inp.classList.toggle("invalid", k === "quantity" && (!(l.quantity > 0) || l.quantity > maxFor(l) + 1e-6));
        totals();
      }));
      $("[data-rm]", tr).onclick = () => { lines.splice(Number(tr.dataset.i), 1); draw(); };
    });
  };
  const totals = () => {
    const tax = Number($("[name=tax_pct]", el).value || 0), sub = lines.reduce((a, l) => a + (l.quantity || 0) * (l.rate || 0), 0);
    $("#poTotals", el).innerHTML = `<div><span>Subtotal</span><span>${money(sub)}</span></div><div><span>Tax (${tax}%)</span><span>${money(sub * tax / 100)}</span></div><div class="grand"><span>Total</span><span>${money(sub + sub * tax / 100)}</span></div>`;
  };
  $("[name=tax_pct]", el).oninput = totals;
  draw();
  if (ed) {
    $("#addLineSel", el).onchange = (e) => {
      const it = itemById[Number(e.target.value)]; if (!it) return;
      lines.push({ procurement_item_id: it.id, description: it.name, specification: it.specification, quantity: it.remaining, unit: it.unit, rate: 0, quotation_id: null });
      e.target.querySelector(`option[value="${it.id}"]`).remove(); e.target.value = ""; draw();
    };
    $("#savePO", el).onclick = async () => {
      const bad = lines.find((l) => !(l.quantity > 0) || l.rate < 0 || isNaN(l.rate));
      if (bad) { toast(`Check quantity / rate for “${esc(bad.description)}”`, true); return; }
      try {
        await api("PUT", `/api/purchase-orders/${po.id}`, {
          delivery_address: $("[name=delivery_address]", el).value.trim() || null, delivery_date: $("[name=delivery_date]", el).value || null,
          payment_terms: $("[name=payment_terms]", el).value.trim() || null, tax_pct: Number($("[name=tax_pct]", el).value || 0),
          notes: $("[name=notes]", el).value.trim() || null, actor: getActor() || null,
          lines: lines.map((l) => ({ procurement_item_id: l.procurement_item_id, quantity: l.quantity, rate: l.rate, quotation_id: l.quotation_id, description: l.description, specification: l.specification || null, unit: l.unit })),
        });
        toast("PO saved"); render();
      } catch (e) { fail(e); }
    };
  }
  bindActions(el, "po", po, render);
  $("#poHist", el).innerHTML = await docTimeline("po", po.id);
}

// ---------------------------------------------------------------- 6. material packages
async function packageForm(pk) {
  await Promise.all([loadVendors(), refreshList()]);
  const listId = pk ? pk.list_id : S.list?.id;
  if (!listId) { toast("Select a procurement list first.", true); return; }
  const pl = await api("GET", `/api/procurement-lists/${listId}`);
  const own = Object.fromEntries((pk?.lines || []).map((l) => [l.procurement_item_id, l.quantity]));
  const avail = (it) => it.remaining + (own[it.id] || 0);
  let lines = (pk?.lines || []).map((l) => ({ ...l }));
  let ms = (pk?.milestones || []).map((x) => ({ ...x }));
  const cOpts = [["", "— not assigned yet —"], ...S.vendors.filter((x) => (x.active || x.id === pk?.contractor_id)).map((x) => [x.id, `${x.name} (${{ supplier: "supplier", contractor: "contractor", both: "supplier & contractor" }[x.kind]})`])];
  const F = [
    { name: "name", label: "Package name", required: true },
    { name: "arrangement", label: "Arrangement", type: "select", options: [["supply_only", "Supply only"], ["supply_and_execute", "Supply and execute"]] },
    { name: "contractor_id", label: "Assigned contractor", type: "select", numeric: true, options: cOpts, hint: "Required before review for supply-and-execute" },
    { name: "execution_amount", label: "Execution / labour value", type: "number", min: 0, step: "any", zeroDefault: true },
    { name: "tax_pct", label: "Tax %", type: "number", min: 0, max: 100, step: "any", zeroDefault: true },
    { name: "start_date", label: "Start date", type: "date" }, { name: "completion_date", label: "Completion date", type: "date" },
    { name: "scope_of_work", label: "Scope of work", type: "textarea", full: true, rows: 4 },
    { name: "payment_terms", label: "Payment terms", type: "textarea" }, { name: "notes", label: "Notes / special conditions", type: "textarea" },
  ];
  openModal({
    title: pk ? `Edit package ${esc(pk.package_code)}` : "New material package", wide: true, submitLabel: pk ? "Save package" : "Create package",
    body: `${formHTML(F, pk || { arrangement: "supply_only", execution_amount: 0, tax_pct: 0 })}
      <h2 style="margin:18px 0 8px">Materials in this package</h2>
      <div class="tbl-wrap"><table><thead><tr><th>Material</th><th>Specification</th><th class="num">Qty</th><th>Unit</th><th class="num">Rate</th><th class="num">Amount</th><th></th></tr></thead><tbody id="pkLines"></tbody></table></div>
      <div class="row" style="margin-top:8px"><select id="pkAdd"></select></div>
      <h2 style="margin:18px 0 8px">Milestones</h2>
      <div class="tbl-wrap"><table><thead><tr><th>Milestone</th><th>Due date</th><th class="num">Payment %</th><th></th></tr></thead><tbody id="pkMs"></tbody></table></div>
      <div class="row" style="margin-top:8px"><button class="btn sm" id="msAdd">+ Add milestone</button><span class="spacer"></span><span id="msTot" class="spec"></span></div>
      <div class="row"><div class="totals" id="pkTot"></div></div>`,
    onOpen: (m) => {
      const draw = () => {
        $("#pkLines", m).innerHTML = lines.map((l, i) => { const it = pl.items.find((x) => x.id === l.procurement_item_id); return `<tr data-i="${i}"><td><input data-k="description" value="${esc(l.description)}"></td>
          <td><input data-k="specification" value="${esc(l.specification || "")}"></td><td class="num"><input data-k="quantity" type="number" step="any" min="0" value="${l.quantity}" style="width:100px"><div class="spec">max ${qty(it ? avail(it) : 0)}</div></td>
          <td>${esc(l.unit)}</td><td class="num"><input data-k="rate" type="number" step="any" min="0" value="${l.rate ?? ""}" placeholder="agreed rate" style="width:110px"></td><td class="num" data-amt>${l.rate == null ? "—" : money(l.quantity * l.rate)}</td><td><button class="btn sm danger" data-rm>✕</button></td></tr>`; }).join("")
          || `<tr><td colspan="7" class="empty">Add materials from the procurement list.</td></tr>`;
        $("#pkAdd", m).innerHTML = `<option value="">+ Add material…</option>` + pl.items.filter((it) => avail(it) > 0 && !lines.some((l) => l.procurement_item_id === it.id)).map((it) => `<option value="${it.id}">${esc(it.name)} (available ${qty(avail(it))} ${esc(it.unit)})</option>`).join("");
        $("#pkMs", m).innerHTML = ms.map((x, i) => `<tr data-m="${i}"><td><input data-k="name" value="${esc(x.name)}"></td><td><input data-k="due_date" type="date" value="${x.due_date || ""}"></td>
          <td class="num"><input data-k="payment_pct" type="number" step="any" min="0" max="100" value="${x.payment_pct}" style="width:90px"></td><td><button class="btn sm danger" data-rmm>✕</button></td></tr>`).join("") || `<tr><td colspan="4" class="spec">No milestones.</td></tr>`;
        bind(); tot();
      };
      const tot = () => {
        const mat = lines.reduce((a, l) => a + (l.quantity || 0) * (l.rate || 0), 0), exe = Number($("[name=execution_amount]", m).value || 0), tax = Number($("[name=tax_pct]", m).value || 0);
        const sub = mat + exe;
        $("#pkTot", m).innerHTML = `<div><span>Material value</span><span>${money(mat)}</span></div><div><span>Execution value</span><span>${money(exe)}</span></div><div><span>Subtotal</span><span>${money(sub)}</span></div><div><span>Tax (${tax}%)</span><span>${money(sub * tax / 100)}</span></div><div class="grand"><span>Total</span><span>${money(sub * (1 + tax / 100))}</span></div>`;
        const p = ms.reduce((a, x) => a + (Number(x.payment_pct) || 0), 0);
        $("#msTot", m).innerHTML = ms.length ? `Milestones total <span class="badge ${Math.abs(p - 100) < 0.01 ? "ok" : p > 100 ? "bad" : "warn"}">${r2(p)}%</span>` : "";
      };
      const bind = () => {
        $$("#pkLines tr[data-i]", m).forEach((tr) => {
          const l = lines[Number(tr.dataset.i)];
          $$("input", tr).forEach((inp) => (inp.oninput = () => { const k = inp.dataset.k; l[k] = k === "rate" ? (inp.value === "" ? null : Number(inp.value)) : k === "quantity" ? Number(inp.value) : inp.value; $("[data-amt]", tr).textContent = l.rate == null ? "—" : money(l.quantity * l.rate); tot(); }));
          $("[data-rm]", tr).onclick = () => { lines.splice(Number(tr.dataset.i), 1); draw(); };
        });
        $$("#pkMs tr[data-m]", m).forEach((tr) => {
          const x = ms[Number(tr.dataset.m)];
          $$("input", tr).forEach((inp) => (inp.oninput = () => { x[inp.dataset.k] = inp.dataset.k === "payment_pct" ? Number(inp.value) : inp.value; tot(); }));
          $("[data-rmm]", tr).onclick = () => { ms.splice(Number(tr.dataset.m), 1); draw(); };
        });
      };
      $("#pkAdd", m).onchange = (e) => { const it = pl.items.find((x) => x.id === Number(e.target.value)); if (!it) return; lines.push({ procurement_item_id: it.id, description: it.name, specification: it.specification, quantity: avail(it), unit: it.unit, rate: null }); draw(); };
      $("#msAdd", m).onclick = () => { ms.push({ name: "", due_date: "", payment_pct: 0 }); draw(); };
      $("[name=execution_amount]", m).oninput = tot; $("[name=tax_pct]", m).oninput = tot;
      draw();
    },
    onSubmit: async (m, showErr) => {
      const vals = readForm(m, F); if (!vals) return false;
      const errs = [];
      lines.forEach((l) => { const it = pl.items.find((x) => x.id === l.procurement_item_id); if (!(l.quantity > 0)) errs.push(`${l.description}: quantity must be > 0`); else if (it && l.quantity > avail(it) + 1e-6) errs.push(`${l.description}: only ${qty(avail(it))} ${it.unit} available`); if (l.rate == null) errs.push(`${l.description}: enter the agreed rate (0 if included in execution value)`); else if (l.rate < 0 || isNaN(l.rate)) errs.push(`${l.description}: rate must be ≥ 0`); });
      ms.forEach((x, i) => { if (!x.name) errs.push(`Milestone ${i + 1}: name required`); });
      if (ms.reduce((a, x) => a + (Number(x.payment_pct) || 0), 0) > 100.0001) errs.push("Milestone percentages exceed 100%");
      if (vals.start_date && vals.completion_date && vals.completion_date < vals.start_date) errs.push("Completion date is before start date");
      if (errs.length) { showErr(`<ul>${errs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`); return false; }
      const body = { ...vals, list_id: listId, actor: getActor() || null,
        lines: lines.map((l) => ({ procurement_item_id: l.procurement_item_id, quantity: l.quantity, rate: l.rate || 0, description: l.description, specification: l.specification || null, unit: l.unit })),
        milestones: ms.map((x) => ({ name: x.name, due_date: x.due_date || null, payment_pct: Number(x.payment_pct) || 0 })) };
      const res = pk ? await api("PUT", `/api/packages/${pk.id}`, body) : await api("POST", "/api/packages", body);
      S.sel.package = res.id; toast(pk ? "Package saved" : `Package ${res.package_code} created`); render();
    },
  });
}

const viewPackageList = async (v) => {
  if (needProject(v)) return;
  const pks = await api("GET", `/api/projects/${S.project.id}/packages?include_superseded=${S.showSuperseded}`);
  v.innerHTML = `<h1>Material packages</h1><p class="sub">Club materials into work packages (optional). Quantities in packages count against the same allocation as POs, so nothing is ordered twice.</p>
    <div class="card"><div class="card-h"><h2>${pks.length} package(s)</h2><label class="check"><input type="checkbox" id="ss" ${S.showSuperseded ? "checked" : ""}> Show superseded revisions</label>
      <div class="spacer"></div><button class="btn primary" id="newPk" ${S.list ? "" : "disabled"}>+ New package</button></div>
      <div class="card-b flush">${pks.length ? `<div class="tbl-wrap"><table><thead><tr><th>Package</th><th>Name</th><th>Arrangement</th><th>Contractor</th><th class="num">Materials</th><th class="num">Total</th><th>Status</th></tr></thead><tbody>
      ${pks.map((p) => `<tr class="clickable ${S.sel.package === p.id ? "selected" : ""} ${p.superseded_by_id ? "muted" : ""}" data-pk="${p.id}"><td><b>${revLabel(p.package_code, p.revision)}</b></td><td>${esc(p.name)}</td>
        <td>${p.arrangement === "supply_and_execute" ? "Supply & execute" : "Supply only"}</td><td>${p.contractor ? esc(p.contractor.name) : `<span class="badge warn">Unassigned</span>`}</td>
        <td class="num">${p.lines.length}</td><td class="num">${money(p.total)}</td><td>${statusBadge(p.status, p.superseded_by_id)}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty"><b>No packages</b>Packages are optional — use them to group materials for a contractor (e.g. “RCC works”, “Plumbing supply & install”).</div>`}</div></div>
    <div id="pkDetail"></div>`;
  $("#ss").onchange = (e) => { S.showSuperseded = e.target.checked; render(); };
  $("#newPk").onclick = () => packageForm(null);
  $$("[data-pk]", v).forEach((tr) => (tr.onclick = () => { S.sel.package = Number(tr.dataset.pk); render(); }));
  if (S.sel.package && pks.some((p) => p.id === S.sel.package)) await packageDetail($("#pkDetail", v), S.sel.package);
};

async function packageDetail(el, id) {
  const p = await api("GET", `/api/packages/${id}`);
  el.innerHTML = `<div class="card"><div class="card-h"><h2>${revLabel(p.package_code, p.revision)} · ${esc(p.name)}</h2>${statusBadge(p.status, p.superseded_by_id)}<div class="spacer"></div>
    <div class="actions">${p.editable ? `<button class="btn sm" id="editPk">Edit package</button>` : ""}${actionButtons("package", p)}</div></div>
    <div class="card-b"><div class="doc-head">
      <div class="kv"><div class="k">Arrangement</div><div class="v">${p.arrangement === "supply_and_execute" ? "Supply and execute" : "Supply only"}</div></div>
      <div class="kv"><div class="k">Contractor</div><div class="v">${p.contractor ? esc(p.contractor.name) : "Not assigned"}</div></div>
      <div class="kv"><div class="k">Programme</div><div class="v">${dt(p.start_date)} → ${dt(p.completion_date)}</div></div>
      <div class="kv"><div class="k">Payment terms</div><div class="v">${esc(p.payment_terms || "—")}</div></div>
      <div class="kv" style="grid-column:1/-1"><div class="k">Scope of work</div><div class="v">${esc(p.scope_of_work || "—")}</div></div></div><br>
      <div class="tbl-wrap"><table><thead><tr><th>Material</th><th>Specification</th><th class="num">Qty</th><th>Unit</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead><tbody>
        ${p.lines.map((l) => `<tr><td>${esc(l.description)}</td><td class="spec">${esc(l.specification || "")}</td><td class="num">${qty(l.quantity)}</td><td>${esc(l.unit)}</td><td class="num">${money(l.rate)}</td><td class="num">${money(l.amount)}</td></tr>`).join("") || `<tr><td colspan="6" class="empty">No materials.</td></tr>`}</tbody></table></div>
      <div class="split" style="margin-top:12px"><div>${p.milestones.length ? `<table><thead><tr><th>Milestone</th><th>Due</th><th class="num">%</th><th class="num">Amount</th></tr></thead><tbody>${p.milestones.map((x) => `<tr><td>${esc(x.name)}</td><td>${dt(x.due_date)}</td><td class="num">${x.payment_pct}%</td><td class="num">${money(p.total * x.payment_pct / 100)}</td></tr>`).join("")}</tbody></table>` : `<span class="spec">No milestones.</span>`}</div>
        <div class="totals"><div><span>Material value</span><span>${money(p.material_value)}</span></div><div><span>Execution value</span><span>${money(p.execution_value)}</span></div><div><span>Tax (${p.tax_pct}%)</span><span>${money(p.tax)}</span></div><div class="grand"><span>Total</span><span>${money(p.total)}</span></div></div></div>
      <h2 style="margin-top:18px">Document history</h2><div id="pkHist"></div></div></div>`;
  const e = $("#editPk", el); if (e) e.onclick = () => packageForm(p);
  bindActions(el, "package", p, render);
  $("#pkHist", el).innerHTML = await docTimeline("package", p.id);
}

// ---------------------------------------------------------------- 7. contractor assignments
const viewContractors = async (v) => {
  if (needProject(v)) return;
  await loadVendors();
  const pks = await api("GET", `/api/projects/${S.project.id}/packages`);
  const groups = {};
  pks.forEach((p) => { const k = p.contractor ? p.contractor.name : ""; (groups[k] = groups[k] || []).push(p); });
  const names = Object.keys(groups).filter(Boolean).sort();
  const cons = S.vendors.filter((x) => x.kind !== "supplier");
  const cOpts = `<option value="">— choose contractor —</option>` + contractors().map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  v.innerHTML = `<h1>Contractor assignments</h1><p class="sub">Who is responsible for each package. Assign or change contractors while a package is in Draft or Returned.</p>
    ${groups[""] ? `<div class="card"><div class="card-h"><h2>Unassigned packages</h2><span class="badge warn">${groups[""].length}</span></div><div class="card-b flush"><div class="tbl-wrap"><table><thead><tr><th>Package</th><th>Arrangement</th><th class="num">Value</th><th>Status</th><th>Assign</th></tr></thead><tbody>
      ${groups[""].map((p) => `<tr><td><b>${esc(p.package_code)}</b> ${esc(p.name)}</td><td>${p.arrangement === "supply_and_execute" ? "Supply & execute" : "Supply only"}</td><td class="num">${money(p.total)}</td><td>${statusBadge(p.status)}</td>
        <td>${p.editable ? `<div class="row"><select data-asel="${p.id}">${cOpts}</select><button class="btn sm primary" data-assign="${p.id}">Assign</button></div>` : `<span class="spec">Locked (${STATUS_LABEL[p.status]})</span>`}</td></tr>`).join("")}</tbody></table></div></div></div>` : ""}
    <div class="card"><div class="card-h"><h2>Assignments by contractor</h2></div><div class="card-b flush">${names.length ? `<div class="tbl-wrap"><table><thead><tr><th>Contractor</th><th>Package</th><th>Arrangement</th><th>Scope</th><th>Programme</th><th class="num">Value</th><th>Status</th><th></th></tr></thead><tbody>
      ${names.map((n) => groups[n].map((p, i) => `<tr>${i === 0 ? `<td rowspan="${groups[n].length}"><b>${esc(n)}</b><div class="spec">${groups[n].length} package(s) · ${money(groups[n].reduce((a, x) => a + x.total, 0))}</div></td>` : ""}
        <td>${revLabel(p.package_code, p.revision)} ${esc(p.name)}</td><td>${p.arrangement === "supply_and_execute" ? "Supply & execute" : "Supply only"}</td><td class="spec">${esc((p.scope_of_work || "—").slice(0, 120))}</td>
        <td>${dt(p.start_date)} → ${dt(p.completion_date)}</td><td class="num">${money(p.total)}</td><td>${statusBadge(p.status)}</td><td><button class="btn sm" data-open="${p.id}">Open</button></td></tr>`).join("")).join("")}</tbody></table></div>`
      : `<div class="empty">No packages have a contractor yet.</div>`}</div></div>
    <div class="card"><div class="card-h"><h2>Contractors on record</h2><div class="spacer"></div><button class="btn" id="newC">+ Add contractor</button></div><div class="card-b flush">
      ${cons.length ? `<div class="tbl-wrap"><table><thead><tr><th>Contractor</th><th>Trades</th><th>Contact</th><th>Status</th></tr></thead><tbody>${cons.map((c) => `<tr class="${c.active ? "" : "muted"}"><td><b>${esc(c.name)}</b></td><td class="spec">${esc(c.trades || "—")}</td><td>${esc([c.contact_person, c.phone].filter(Boolean).join(" · "))}</td><td>${c.active ? `<span class="badge ok">Active</span>` : `<span class="badge">Inactive</span>`}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">No contractors yet.</div>`}</div></div>`;
  $("#newC").onclick = () => vendorForm(null, { kind: "contractor" });
  $$("[data-open]", v).forEach((b) => (b.onclick = () => { S.sel.package = Number(b.dataset.open); S.packageTab = "list"; go("packages"); }));
  $$("[data-assign]", v).forEach((b) => (b.onclick = async () => {
    const cid = Number($(`[data-asel="${b.dataset.assign}"]`, v).value);
    if (!cid) { toast("Choose a contractor first.", true); return; }
    const p = pks.find((x) => x.id === Number(b.dataset.assign));
    try {
      await api("PUT", `/api/packages/${p.id}`, { list_id: p.list_id, name: p.name, contractor_id: cid, arrangement: p.arrangement, scope_of_work: p.scope_of_work,
        execution_amount: p.execution_amount, tax_pct: p.tax_pct, payment_terms: p.payment_terms, start_date: p.start_date, completion_date: p.completion_date, notes: p.notes, actor: getActor() || null,
        lines: p.lines.map((l) => ({ procurement_item_id: l.procurement_item_id, quantity: l.quantity, rate: l.rate, description: l.description, specification: l.specification, unit: l.unit })),
        milestones: p.milestones.map((x) => ({ name: x.name, due_date: x.due_date, payment_pct: x.payment_pct })) });
      toast("Contractor assigned"); render();
    } catch (e) { fail(e); }
  }));
};

// ---------------------------------------------------------------- 8. contracts
const MARKER = "[[MATERIAL_SCHEDULE]]";
function previewHTML(c) {
  const out = []; let para = [];
  const flush = () => { if (para.length) { out.push(`<p>${esc(para.join("\n"))}</p>`); para = []; } };
  (c.body || "").split("\n").forEach((raw) => {
    const line = raw.replace(/\s+$/, "");
    if (line.trim() === MARKER) {
      flush();
      const sub = (c.schedule || []).reduce((a, i) => a + i.amount, 0);
      out.push(`<table><thead><tr><th>#</th><th>Description</th><th class="num">Qty</th><th>Unit</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead><tbody>
        ${(c.schedule || []).map((i, n) => `<tr><td>${n + 1}</td><td>${esc(i.description)}<div class="spec">${esc(i.specification || "")}</div></td><td class="num">${qty(i.quantity)}</td><td>${esc(i.unit)}</td><td class="num">${money(i.rate)}</td><td class="num">${money(i.amount)}</td></tr>`).join("")}
        </tbody><tfoot><tr><td colspan="5">Subtotal</td><td class="num">${money(sub)}</td></tr></tfoot></table>`);
    } else if (line.startsWith("## ")) { flush(); out.push(`<h4>${esc(line.slice(3))}</h4>`); }
    else if (line.startsWith("# ")) { flush(); out.push(`<h3>${esc(line.slice(2))}</h3>`); }
    else if (!line.trim()) flush();
    else para.push(line);
  });
  flush();
  return `${["approved", "finalized"].includes(c.status) ? "" : `<div class="wm">DRAFT — NOT FOR EXECUTION</div>`}<h3 style="margin-top:0">${esc(c.title)}</h3>${out.join("")}
    <p style="margin-top:24px">Signature blocks are left blank in the exported PDF. Nothing is signed or sent automatically.</p>`;
}

const TEMPLATE_HELP = `<div class="alert info" style="font-size:12.5px"><b>Template syntax</b> (Jinja2): <code>{{ field }}</code>, <code>{% if x %}…{% endif %}</code>, <code>{% for ms in milestones %}…{% endfor %}</code>.
  Filters: <code>| money</code>, <code>| date</code>, <code>| or_tba</code>. Lines starting with <code>#</code> / <code>##</code> become headings. Put <code>{{ schedule }}</code> on its own line to insert the material schedule table.<br>
  <b>Both kinds:</b> project.name/code/client/location/description · buyer.name/address/tax_id/contact · party.* · items · totals.subtotal/tax/total · today<br>
  <b>Supply:</b> vendor.name/address/tax_id/contact_person/phone/email · po.number/revision/delivery_address/delivery_date/payment_terms/tax_pct/notes<br>
  <b>Contractor:</b> contractor.* · package.code/name/arrangement/arrangement_label/scope_of_work/payment_terms/start_date/completion_date/tax_pct/notes · milestones[].name/due_date/payment_pct · totals.material_value/execution_value</div>`;

function templateForm(t) {
  const F = [{ name: "name", label: "Template name", required: true }, { name: "kind", label: "Contract type", type: "select", options: [["supply", "Vendor supply contract"], ["contractor", "Contractor agreement"]] },
    { name: "active", label: "Active", type: "checkbox", default: true }, { name: "body", label: "Template body", type: "textarea", full: true, rows: 22, code: true, required: true }];
  openModal({ title: t ? `Edit template · ${esc(t.name)}` : "New contract template", wide: true, body: TEMPLATE_HELP + formHTML(F, t || { kind: "supply", active: true, body: "# 1. Parties\n{{ buyer.name }} and {{ party.name }}\n\n# 2. Schedule\n{{ schedule }}\n" }),
    onSubmit: async (m) => { const vals = readForm(m, F); if (!vals) return false; t ? await api("PUT", `/api/contract-templates/${t.id}`, vals) : await api("POST", "/api/contract-templates", vals); toast("Template saved"); render(); } });
}

async function generateContractModal() {
  const [pos, pks, tpls] = await Promise.all([api("GET", `/api/projects/${S.project.id}/purchase-orders`), api("GET", `/api/projects/${S.project.id}/packages`), api("GET", "/api/contract-templates")]);
  const okPos = pos.filter((p) => p.status !== "rejected"), okPks = pks.filter((p) => p.status !== "rejected");
  if (!okPos.length && !okPks.length) { toast("Create a purchase order or package first.", true); return; }
  openModal({
    title: "Generate contract draft", submitLabel: "Generate draft",
    body: `<p class="spec">The draft is built from saved records (project, parties, schedule, prices, dates, terms) using a configurable template. You can edit everything before review.</p>
      <div class="form"><label class="field full"><span>Source document <span class="req">*</span></span><select name="src"><option value="">— select —</option>
        ${okPos.length ? `<optgroup label="Purchase orders → vendor supply contract">${okPos.map((p) => `<option value="po:${p.id}">${esc(p.po_number)}${p.revision ? " R" + p.revision : ""} · ${esc(p.vendor.name)} · ${money(p.total)} (${STATUS_LABEL[p.status]})</option>`).join("")}</optgroup>` : ""}
        ${okPks.length ? `<optgroup label="Packages → contractor agreement">${okPks.map((p) => `<option value="package:${p.id}" ${p.contractor ? "" : "disabled"}>${esc(p.package_code)} · ${esc(p.name)} · ${p.contractor ? esc(p.contractor.name) : "no contractor assigned"} (${STATUS_LABEL[p.status]})</option>`).join("")}</optgroup>` : ""}
      </select></label>
      <label class="field full"><span>Template <span class="req">*</span></span><select name="tpl"></select></label>
      <label class="field full"><span>Title (optional)</span><input name="title" placeholder="Generated from parties if left blank"></label></div>`,
    onOpen: (m) => {
      const upd = () => { const kind = $("[name=src]", m).value.startsWith("po:") ? "supply" : "contractor"; const list = tpls.filter((t) => t.kind === kind && t.active);
        $("[name=tpl]", m).innerHTML = list.length ? list.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("") : `<option value="">No active ${kind} template — create one in Templates</option>`; };
      $("[name=src]", m).onchange = upd; upd();
    },
    onSubmit: async (m, showErr) => {
      const src = $("[name=src]", m).value, tpl = Number($("[name=tpl]", m).value);
      if (!src || !tpl) { showErr("Choose a source document and a template."); return false; }
      const [type, id] = src.split(":");
      const c = await api("POST", "/api/contracts/generate", { template_id: tpl, [type === "po" ? "po_id" : "package_id"]: Number(id), title: $("[name=title]", m).value.trim() || null, actor: getActor() || null });
      S.sel.contract = c.id; S.contractTab = "drafts"; toast(`Contract ${c.contract_number} drafted`); render();
    },
  });
}

VIEWS.contracts = async (v) => {
  if (needProject(v)) return;
  const tab = S.contractTab;
  v.innerHTML = `<h1>Contracts & approvals</h1><p class="sub">Vendor supply contracts and contractor agreements generated from saved records and editable templates, plus the review queue and audit trail. Approval is manual; nothing is signed or sent.</p>
    <div class="row" style="margin-bottom:14px"><div class="pill-tabs"><button data-tab="drafts" class="${tab === "drafts" ? "active" : ""}">Drafts</button><button data-tab="templates" class="${tab === "templates" ? "active" : ""}">Templates</button><button data-tab="approvals" class="${tab === "approvals" ? "active" : ""}">Approvals & history</button></div></div><div id="ctBody"></div>`;
  $$("[data-tab]", v).forEach((b) => (b.onclick = () => { S.contractTab = b.dataset.tab; render(); }));
  const body = $("#ctBody", v);
  if (tab === "approvals") { await viewHistory(body); return; }
  if (tab === "templates") {
    const tpls = await api("GET", "/api/contract-templates");
    body.innerHTML = `<div class="card"><div class="card-h"><h2>Contract templates</h2><span class="spec">Starter templates are generic and fully editable</span><div class="spacer"></div><button class="btn primary" id="newT">+ New template</button></div><div class="card-b flush">
      ${tpls.length ? `<div class="tbl-wrap"><table><thead><tr><th>Template</th><th>Type</th><th>Status</th><th>Updated</th><th></th></tr></thead><tbody>${tpls.map((t) => `<tr><td><b>${esc(t.name)}</b></td><td>${t.kind === "supply" ? "Vendor supply contract" : "Contractor agreement"}</td>
        <td>${t.active ? `<span class="badge ok">Active</span>` : `<span class="badge">Inactive</span>`}</td><td>${dt(t.updated_at)}</td><td><div class="actions"><button class="btn sm" data-et="${t.id}">Edit</button><button class="btn sm danger" data-dt="${t.id}">Delete</button></div></td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">No templates. Create one to generate contracts.</div>`}</div></div>`;
    $("#newT").onclick = () => templateForm(null);
    $$("[data-et]", body).forEach((b) => (b.onclick = () => templateForm(tpls.find((t) => t.id === Number(b.dataset.et)))));
    $$("[data-dt]", body).forEach((b) => (b.onclick = async () => { if (!confirm("Delete this template? Existing contracts keep their text.")) return; try { await api("DELETE", `/api/contract-templates/${b.dataset.dt}`); render(); } catch (e) { fail(e); } }));
    return;
  }
  const cts = await api("GET", `/api/projects/${S.project.id}/contracts?include_superseded=${S.showSuperseded}`);
  body.innerHTML = `<div class="card"><div class="card-h"><h2>${cts.length} contract(s)</h2><label class="check"><input type="checkbox" id="ss" ${S.showSuperseded ? "checked" : ""}> Show superseded revisions</label><div class="spacer"></div><button class="btn primary" id="genC">+ Generate contract</button></div>
    <div class="card-b flush">${cts.length ? `<div class="tbl-wrap"><table><thead><tr><th>Contract</th><th>Title</th><th>Type</th><th>Counterparty</th><th>Status</th><th>Updated</th></tr></thead><tbody>
    ${cts.map((c) => `<tr class="clickable ${S.sel.contract === c.id ? "selected" : ""} ${c.superseded_by_id ? "muted" : ""}" data-ct="${c.id}"><td><b>${revLabel(c.contract_number, c.revision)}</b></td><td>${esc(c.title)}</td><td>${c.kind === "supply" ? "Supply" : "Contractor"}</td><td>${esc(c.party_name || "—")}</td><td>${statusBadge(c.status, c.superseded_by_id)}</td><td>${dt(c.updated_at)}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><b>No contracts yet</b>Generate one from a purchase order or a package with an assigned contractor.</div>`}</div></div><div id="ctDetail"></div>`;
  $("#ss", body).onchange = (e) => { S.showSuperseded = e.target.checked; render(); };
  $("#genC", body).onclick = generateContractModal;
  $$("[data-ct]", body).forEach((tr) => (tr.onclick = () => { S.sel.contract = Number(tr.dataset.ct); render(); }));
  if (S.sel.contract && cts.some((c) => c.id === S.sel.contract)) await contractDetail($("#ctDetail", body), S.sel.contract);
};

async function contractDetail(el, id) {
  const c = await api("GET", `/api/contracts/${id}`);
  const ed = c.editable;
  el.innerHTML = `<div class="card"><div class="card-h"><h2>${revLabel(c.contract_number, c.revision)}</h2>${statusBadge(c.status, c.superseded_by_id)}<span class="spec">${c.kind === "supply" ? "Vendor supply contract" : "Contractor agreement"} · ${esc(c.party_name || "")}</span>
    <div class="spacer"></div><div class="actions">${ed ? `<button class="btn sm" id="regen">Regenerate from source</button>` : ""}${actionButtons("contract", c)}</div></div>
    <div class="card-b">${ed ? "" : `<div class="alert info">This contract is ${STATUS_LABEL[c.status].toLowerCase()} and read-only${c.superseded_by_id ? " (superseded)" : ". Create a revision to make changes"}.</div>`}
      <div class="split"><div><label class="field"><span>Title</span><input id="ctTitle" value="${esc(c.title)}" ${ed ? "" : "disabled"}></label><br>
        <label class="field"><span>Contract text <small class="spec">— keep <code>${MARKER}</code> on its own line for the schedule table</small></span><textarea id="ctBodyTxt" class="code" rows="28" ${ed ? "" : "disabled"}>${esc(c.body)}</textarea></label>
        ${ed ? `<div class="row" style="margin-top:10px"><span class="spec" id="dirty"></span><div class="spacer"></div><button class="btn primary" id="saveCt">Save draft</button></div>` : ""}</div>
        <div><div class="field"><span>Live preview</span></div><div class="preview" id="ctPrev">${previewHTML(c)}</div></div></div>
      <h2 style="margin-top:18px">Document history</h2><div id="ctHist"></div></div></div>`;
  const live = () => { $("#ctPrev", el).innerHTML = previewHTML({ ...c, title: $("#ctTitle", el).value, body: $("#ctBodyTxt", el).value }); const d = $("#dirty", el); if (d) d.textContent = "Unsaved changes"; };
  if (ed) {
    $("#ctTitle", el).oninput = live; $("#ctBodyTxt", el).oninput = live;
    $("#saveCt", el).onclick = async () => {
      const title = $("#ctTitle", el).value.trim(), body = $("#ctBodyTxt", el).value;
      if (!title || !body.trim()) { toast("Title and contract text are required.", true); return; }
      try { await api("PUT", `/api/contracts/${c.id}`, { title, body, actor: getActor() || null }); toast("Contract draft saved"); render(); } catch (e) { fail(e); }
    };
    $("#regen", el).onclick = async () => {
      if (!confirm("Re-render this draft from its template using the latest saved PO/package data? Manual text edits in this draft will be replaced.")) return;
      try { await api("POST", `/api/contracts/${c.id}/regenerate`, { actor: getActor() || "", comment: null }); toast("Contract regenerated"); render(); } catch (e) { fail(e); }
    };
  }
  bindActions(el, "contract", c, render);
  $("#ctHist", el).innerHTML = await docTimeline("contract", c.id);
}

// ---------------------------------------------------------------- 9. approvals & history
const viewHistory = async (v) => {
  if (needProject(v)) return;
  const pid = S.project.id, f = store.get("hfilter", "");
  const [pos, pks, cts, ev] = await Promise.all([api("GET", `/api/projects/${pid}/purchase-orders`), api("GET", `/api/projects/${pid}/packages`), api("GET", `/api/projects/${pid}/contracts`),
    api("GET", `/api/projects/${pid}/history${f ? `?doc_type=${f}` : ""}`)]);
  const queue = [...pos.map((d) => ({ t: "po", label: d.po_number, rev: d.revision, d, who: d.vendor.name, val: d.total, view: "pos" })),
    ...pks.map((d) => ({ t: "package", label: d.package_code, rev: d.revision, d, who: d.contractor?.name || "—", val: d.total, view: "packages" })),
    ...cts.map((d) => ({ t: "contract", label: d.contract_number, rev: d.revision, d, who: d.party_name || "—", val: null, view: "contracts" }))];
  const pending = queue.filter((q) => q.d.status === "pending_review"), returned = queue.filter((q) => q.d.status === "returned");
  const TL = { po: "Purchase order", package: "Package", contract: "Contract" };
  const qTable = (rows) => `<div class="tbl-wrap"><table><thead><tr><th>Document</th><th>Type</th><th>Party</th><th class="num">Value</th><th>Status</th><th>Updated</th><th></th></tr></thead><tbody>
    ${rows.map((q, i) => `<tr><td><b>${revLabel(q.label, q.rev)}</b></td><td>${TL[q.t]}</td><td>${esc(q.who)}</td><td class="num">${q.val == null ? "—" : money(q.val)}</td><td>${statusBadge(q.d.status)}</td><td>${dt(q.d.updated_at)}</td>
      <td><button class="btn sm primary" data-goq="${q.t}:${q.d.id}:${q.view}">Open & review</button></td></tr>`).join("")}</tbody></table></div>`;
  v.innerHTML = `<h1>Approval & document history</h1><p class="sub">Every submission, approval, return, rejection, edit and revision is recorded with who did it and why.</p>
    <div class="card"><div class="card-h"><h2>Awaiting review</h2><span class="badge ${pending.length ? "pending_review" : ""}">${pending.length}</span></div><div class="card-b flush">${pending.length ? qTable(pending) : `<div class="empty">Nothing is waiting for review.</div>`}</div></div>
    ${returned.length ? `<div class="card"><div class="card-h"><h2>Returned for revision</h2><span class="badge returned">${returned.length}</span></div><div class="card-b flush">${qTable(returned)}</div></div>` : ""}
    <div class="card"><div class="card-h"><h2>Audit trail</h2><div class="spacer"></div><select id="hf"><option value="">All documents</option>${Object.entries(TL).map(([k, l]) => `<option value="${k}" ${f === k ? "selected" : ""}>${l}s</option>`).join("")}</select></div>
      <div class="card-b flush">${ev.length ? `<div class="tbl-wrap"><table><thead><tr><th>When</th><th>Document</th><th>Action</th><th>Status change</th><th>By</th><th>Comment</th></tr></thead><tbody>
      ${ev.map((e) => `<tr><td>${dt(e.created_at)}</td><td>${TL[e.doc_type]} <b>${esc(e.doc_label || "#" + e.doc_id)}</b></td><td>${esc(e.action.replace(/_/g, " "))}</td>
        <td>${e.from_status ? statusBadge(e.from_status) : ""}${e.to_status && e.to_status !== e.from_status ? ` → ${statusBadge(e.to_status)}` : ""}</td><td>${esc(e.actor || "—")}</td><td>${esc(e.comment || "")}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">No document activity yet.</div>`}</div></div>`;
  $("#hf").onchange = (e) => { store.set("hfilter", e.target.value); render(); };
  $$("[data-goq]", v).forEach((b) => (b.onclick = () => { const [t, id, view] = b.dataset.goq.split(":"); S.sel[t] = Number(id); if (t === "contract") S.contractTab = "drafts"; if (t === "package") S.packageTab = "list"; go(view); }));
};

// ---------------------------------------------------------------- purchase history (all projects)
VIEWS.purchases = async (v) => {
  const f = store.get("phFilter", { project: "", vendor: "", q: "", status: "" });
  await loadVendors();
  const params = new URLSearchParams();
  if (f.project) params.set("project_id", f.project);
  if (f.vendor) params.set("vendor_id", f.vendor);
  if (f.q) params.set("q", f.q);
  if (f.status) params.set("status", f.status);
  const h = await api("GET", `/api/purchase-history?${params}`);
  const opt = (val, label, cur) => `<option value="${esc(val)}" ${String(cur) === String(val) ? "selected" : ""}>${esc(label)}</option>`;
  v.innerHTML = `<h1>Purchase history</h1><p class="sub">Every purchase order line from all projects, newest first. Rejected orders are hidden unless you filter for them; superseded revisions show only their latest version.</p>
    <div class="stats"><div class="stat"><div class="k">Purchase orders</div><div class="v">${h.po_count}</div></div>
      <div class="stat"><div class="k">Lines</div><div class="v">${h.rows.length}</div></div>
      <div class="stat"><div class="k">Total value (before tax)</div><div class="v">${money(h.total)}</div></div>
      ${h.by_vendor[0] ? `<div class="stat"><div class="k">Top vendor</div><div class="v" style="font-size:15px">${esc(h.by_vendor[0].vendor)}<br>${money(h.by_vendor[0].amount)}</div></div>` : ""}</div>
    <div class="card"><div class="card-h">
      <select id="phProject">${opt("", "All projects", f.project)}${S.projects.map((x) => opt(x.id, `${x.name}${x.code ? " · " + x.code : ""}`, f.project)).join("")}</select>
      <select id="phVendor">${opt("", "All vendors", f.vendor)}${S.vendors.map((x) => opt(x.id, x.name, f.vendor)).join("")}</select>
      <select id="phStatus">${opt("", "All except rejected", f.status)}${Object.entries(STATUS_LABEL).map(([k, l]) => opt(k, l, f.status)).join("")}</select>
      <input id="phQ" placeholder="Search material…" value="${esc(f.q)}" style="min-width:200px">
      <div class="spacer"></div><button class="btn" id="phCsv" ${h.rows.length ? "" : "disabled"}>Download CSV</button></div>
      <div class="card-b flush">${h.rows.length ? `<div class="tbl-wrap"><table><thead><tr><th>Date</th><th>PO</th><th>Project</th><th>Vendor</th><th>Material</th><th class="num">Qty</th><th>Unit</th><th class="num">Rate</th><th class="num">Amount</th><th>Status</th><th></th></tr></thead><tbody>
      ${h.rows.map((r) => `<tr><td>${dt(r.date)}</td><td><b>${revLabel(r.po_number, r.revision)}</b></td><td>${esc(r.project_name)}</td><td>${esc(r.vendor_name)}</td>
        <td>${esc(r.material)}${r.specification ? `<div class="spec">${esc(r.specification)}</div>` : ""}</td><td class="num">${qty(r.quantity)}</td><td>${esc(r.unit)}</td>
        <td class="num">${money(r.rate)}</td><td class="num">${money(r.amount)}</td><td>${statusBadge(r.status)}</td>
        <td><a class="btn sm" href="${BASE}/api/purchase-orders/${r.po_id}/pdf" target="_blank" rel="noopener">PDF</a></td></tr>`).join("")}
      </tbody><tfoot><tr><td colspan="8">Total</td><td class="num">${money(h.total)}</td><td colspan="2"></td></tr></tfoot></table></div>`
        : `<div class="empty"><b>No purchase orders found</b>Orders you place from Materials appear here, across all projects.</div>`}</div></div>`;
  const setF = (k, val) => { store.set("phFilter", { ...f, [k]: val }); render(); };
  $("#phProject").onchange = (e) => setF("project", e.target.value);
  $("#phVendor").onchange = (e) => setF("vendor", e.target.value);
  $("#phStatus").onchange = (e) => setF("status", e.target.value);
  let t; $("#phQ").oninput = (e) => { clearTimeout(t); t = setTimeout(() => setF("q", e.target.value.trim()), 400); };
  $("#phCsv").onclick = () => {
    const head = ["Date", "PO", "Revision", "Project", "Vendor", "Material", "Specification", "Qty", "Unit", "Rate", "Amount", "Status"];
    const cell = (x) => `"${String(x ?? "").replace(/"/g, '""')}"`;
    const csv = [head, ...h.rows.map((r) => [r.date.slice(0, 10), r.po_number, r.revision, r.project_name, r.vendor_name, r.material,
      r.specification, r.quantity, r.unit, r.rate, r.amount, STATUS_LABEL[r.status] || r.status])].map((row) => row.map(cell).join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "purchase_history.csv"; a.click();
  };
};

// ============================================================ merged sections
function tabBar(v, key, items) {
  v.innerHTML = `<div class="row" style="margin-bottom:14px"><div class="pill-tabs">${items.map(([id, label]) =>
    `<button data-sub="${id}" class="${S[key] === id ? "active" : ""}">${label}</button>`).join("")}</div></div><div class="tab-body"></div>`;
  $$("[data-sub]", v).forEach((b) => (b.onclick = () => { S[key] = b.dataset.sub; render(); }));
  return $(".tab-body", v);
}

VIEWS.vendors = async (v) => {
  const body = tabBar(v, "vendorTab", [["directory", "Vendor directory"], ["quotes", "Quotation comparison"]]);
  await (S.vendorTab === "quotes" ? viewQuotes : viewVendorDirectory)(body);
};

VIEWS.packages = async (v) => {
  const body = tabBar(v, "packageTab", [["list", "Packages"], ["contractors", "By contractor"]]);
  await (S.packageTab === "contractors" ? viewContractors : viewPackageList)(body);
};

// ============================================================ boot
async function boot() {
  const qs = new URLSearchParams(location.search);
  if (qs.get("project")) store.set("projectId", Number(qs.get("project")));
  if (qs.get("list")) store.set("listId", Number(qs.get("list")));
  if (qs.get("view")) { store.set("view", qs.get("view")); S.view = qs.get("view"); }
  if (qs.get("actor") && !store.get("actor")) store.set("actor", qs.get("actor"));
  if (qs.get("embedded")) { document.body.classList.add("embedded"); S.embedded = true; }
  S.view = resolveView(S.view);
  $("#actorInput").value = store.get("actor", "");
  $("#actorInput").oninput = (e) => store.set("actor", e.target.value);
  $("#menuBtn").onclick = () => $("#sidebar").classList.toggle("open");
  $("#projectSel").onchange = async (e) => { store.set("projectId", Number(e.target.value)); S.sel = { po: null, package: null, contract: null }; await loadProjects(); render(); };
  $("#listSel").onchange = async (e) => { store.set("listId", Number(e.target.value)); await loadLists(); render(); };
  try { await Promise.all([loadProjects(), loadVendors()]); } catch (e) { $("#view").innerHTML = `<div class="alert error">Cannot reach the backend: ${errHTML(e)}</div>`; return; }
  render();
}
boot();
