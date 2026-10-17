"""PDF export using ReportLab. Drafts carry a watermark; nothing is signed automatically."""
from __future__ import annotations

import io
from datetime import date
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, KeepTogether

from . import models as m
from .services import SCHEDULE_MARKER, po_totals, package_totals

CUR = "INR"
INK = colors.HexColor("#1f2933")
LINE = colors.HexColor("#c9d1d9")
HEAD_BG = colors.HexColor("#eef2f6")

_ss = getSampleStyleSheet()
S = {
    "title": ParagraphStyle("t", parent=_ss["Title"], fontSize=16, leading=20, textColor=INK, spaceAfter=4),
    "h1": ParagraphStyle("h1", parent=_ss["Heading2"], fontSize=12.5, leading=16, textColor=INK, spaceBefore=8, spaceAfter=4),
    "h2": ParagraphStyle("h2", parent=_ss["Heading3"], fontSize=11, leading=14, textColor=INK, spaceBefore=6, spaceAfter=3),
    "body": ParagraphStyle("b", parent=_ss["BodyText"], fontSize=9.5, leading=13, textColor=INK),
    "small": ParagraphStyle("s", parent=_ss["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#52606d")),
    "cell": ParagraphStyle("c", parent=_ss["BodyText"], fontSize=8.5, leading=10.5),
    "cellr": ParagraphStyle("cr", parent=_ss["BodyText"], fontSize=8.5, leading=10.5, alignment=TA_RIGHT),
}


def money(v) -> str:
    return f"{float(v or 0):,.2f}"


def qty(v) -> str:
    v = float(v or 0)
    return f"{v:,.3f}".rstrip("0").rstrip(".")


def P(text, style="body"):
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), S[style])


def _watermark_factory(status: str, label: str):
    show = status not in (m.APPROVED, m.FINALIZED)

    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#7b8794"))
        canvas.drawString(15 * mm, 10 * mm, f"{label}  |  Status: {status.replace('_', ' ').upper()}  |  Generated {date.today():%d %b %Y}")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
        if show:
            canvas.setFont("Helvetica-Bold", 40)
            canvas.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.12))
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(40)
            canvas.drawCentredString(0, 0, "DRAFT - NOT FOR EXECUTION")
        canvas.restoreState()
    return draw


def _build(story, status, label) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=18 * mm, title=label)
    wm = _watermark_factory(status, label)
    doc.build(story, onFirstPage=wm, onLaterPages=wm)
    return buf.getvalue()


def _table(rows, widths, header=True, align_right_cols=()):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    for c in align_right_cols:
        st.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(st))
    return t


def schedule_table(items: list[dict], totals: dict | None = None):
    rows = [["#", "Description / Specification", "Qty", "Unit", f"Rate ({CUR})", f"Amount ({CUR})"]]
    for i, it in enumerate(items, 1):
        desc = escape(it["description"]) + (f"<br/><font size=7.5 color='#52606d'>{escape(it['specification'])}</font>"
                                            if it.get("specification") else "")
        rows.append([str(i), Paragraph(desc, S["cell"]), qty(it["quantity"]), it["unit"],
                     money(it["rate"]), money(it["amount"])])
    if totals:
        for k, lbl in (("material_value", "Material value"), ("execution_value", "Execution / labour value"),
                       ("subtotal", "Subtotal"), ("tax", "Tax"), ("total", "TOTAL")):
            if k in totals:
                rows.append(["", "", "", "", lbl, money(totals[k])])
    t = _table(rows, [8 * mm, 82 * mm, 18 * mm, 14 * mm, 28 * mm, 30 * mm], align_right_cols=(2, 4, 5))
    if totals:
        n = len(items) + 1
        t.setStyle(TableStyle([("FONTNAME", (4, n), (5, -1), "Helvetica-Bold"), ("SPAN", (0, n), (3, -1))]))
    return t


def _party_block(title, name, lines):
    body = f"<b>{escape(title)}</b><br/>{escape(name or '-')}"
    for ln in lines:
        if ln:
            body += "<br/>" + escape(str(ln)).replace("\n", "<br/>")
    return Paragraph(body, S["body"])


def _buyer(project: m.Project):
    return _party_block("Buyer / Employer", project.buyer_name or "(buyer details not set on project)",
                        [project.buyer_address, f"Tax ID: {project.buyer_tax_id}" if project.buyer_tax_id else None,
                         project.buyer_contact])


def _vendor_lines(v: m.Vendor | None):
    if not v:
        return []
    return [v.address, f"Contact: {v.contact_person}" if v.contact_person else None,
            " / ".join(x for x in (v.phone, v.email) if x) or None, f"Tax ID: {v.tax_id}" if v.tax_id else None]


def _signatures(left, right):
    rows = [[P(f"For {left}", "body"), P(f"For {right}", "body")],
            [P("\n\n\nAuthorised signatory: ____________________\nName / Date:", "small"),
             P("\n\n\nAuthorised signatory: ____________________\nName / Date:", "small")]]
    t = Table(rows, colWidths=[90 * mm, 90 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return KeepTogether([Spacer(1, 10), t])


def po_pdf(po: m.PurchaseOrder, project: m.Project) -> bytes:
    t = po_totals(po)
    label = f"{po.po_number}" + (f" R{po.revision}" if po.revision else "")
    story = [P("PURCHASE ORDER", "title"),
             P(f"PO No: {label}    Date: {po.created_at:%d %b %Y}    Project: {project.name}"
               + (f" ({project.code})" if project.code else ""), "small"), Spacer(1, 8)]
    pt = Table([[_buyer(project), _party_block("Vendor", po.vendor.name, _vendor_lines(po.vendor))]],
               colWidths=[90 * mm, 90 * mm])
    pt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                            ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [pt, Spacer(1, 8), P("Item schedule", "h1")]
    items = [{"description": l.description, "specification": l.specification, "quantity": l.quantity,
              "unit": l.unit, "rate": l.rate, "amount": l.quantity * l.rate} for l in po.lines]
    story.append(schedule_table(items, {"subtotal": t["subtotal"], "tax": t["tax"], "total": t["total"]}))
    story += [Spacer(1, 6), P(f"Tax rate applied: {po.tax_pct or 0}%", "small"), P("Delivery & commercial terms", "h1")]
    terms = [["Delivery address", P(po.delivery_address or project.default_delivery_address or "To be agreed", "cell")],
             ["Delivery date", po.delivery_date.strftime("%d %b %Y") if po.delivery_date else "To be agreed"],
             ["Payment terms", P(po.payment_terms or "To be agreed", "cell")]]
    if po.notes:
        terms.append(["Notes", P(po.notes, "cell")])
    story.append(_table(terms, [40 * mm, 140 * mm], header=False))
    story.append(_signatures(project.buyer_name or "Buyer", po.vendor.name))
    return _build(story, po.status, f"PO {label}")


def package_pdf(pk: m.Package, project: m.Project) -> bytes:
    t = package_totals(pk)
    label = pk.package_code + (f" R{pk.revision}" if pk.revision else "")
    arr = "Supply and Execute" if pk.arrangement == "supply_and_execute" else "Supply Only"
    story = [P(f"WORK PACKAGE - {pk.name}", "title"),
             P(f"Package: {label}    Arrangement: {arr}    Project: {project.name}", "small"), Spacer(1, 8)]
    pt = Table([[_buyer(project), _party_block("Contractor", pk.contractor.name if pk.contractor else "Not assigned",
                                               _vendor_lines(pk.contractor))]], colWidths=[90 * mm, 90 * mm])
    pt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                            ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [pt]
    if pk.scope_of_work:
        story += [P("Scope of work", "h1"), P(pk.scope_of_work)]
    story.append(P("Material schedule", "h1"))
    items = [{"description": l.description, "specification": l.specification, "quantity": l.quantity,
              "unit": l.unit, "rate": l.rate, "amount": l.quantity * l.rate} for l in pk.lines]
    story.append(schedule_table(items, {k: t[k] for k in ("material_value", "execution_value", "subtotal", "tax", "total")}))
    if pk.milestones:
        story.append(P("Milestones", "h1"))
        rows = [["Milestone", "Due date", "Payment %", f"Amount ({CUR})"]]
        for ms in pk.milestones:
            rows.append([P(ms.name, "cell"), ms.due_date.strftime("%d %b %Y") if ms.due_date else "TBA",
                         f"{ms.payment_pct:g}%", money(t["total"] * ms.payment_pct / 100)])
        story.append(_table(rows, [80 * mm, 30 * mm, 30 * mm, 40 * mm], align_right_cols=(2, 3)))
    story.append(P("Commercial terms", "h1"))
    story.append(_table([["Start date", pk.start_date.strftime("%d %b %Y") if pk.start_date else "To be agreed"],
                         ["Completion date", pk.completion_date.strftime("%d %b %Y") if pk.completion_date else "To be agreed"],
                         ["Payment terms", P(pk.payment_terms or "To be agreed", "cell")]],
                        [40 * mm, 140 * mm], header=False))
    story.append(_signatures(project.buyer_name or "Employer", pk.contractor.name if pk.contractor else "Contractor"))
    return _build(story, pk.status, f"Package {label}")


def contract_pdf(c: m.Contract, project: m.Project) -> bytes:
    label = c.contract_number + (f" R{c.revision}" if c.revision else "")
    story = [P(c.title, "title"), P(f"Contract No: {label}    Project: {project.name}", "small"), Spacer(1, 6)]
    para: list[str] = []

    def flush():
        if para:
            story.append(P("\n".join(para)))
            story.append(Spacer(1, 4))
            para.clear()

    for raw in (c.body or "").splitlines():
        line = raw.rstrip()
        if line.strip() == SCHEDULE_MARKER:
            flush()
            sched = c.schedule or []
            sub = sum(i["amount"] for i in sched)
            story.append(schedule_table(sched, {"subtotal": sub}) if sched else P("(No material schedule)"))
            story.append(Spacer(1, 4))
        elif line.startswith("## "):
            flush(); story.append(P(line[3:], "h2"))
        elif line.startswith("# "):
            flush(); story.append(P(line[2:], "h1"))
        elif not line.strip():
            flush()
        else:
            para.append(line)
    flush()
    story.append(_signatures(project.buyer_name or "Buyer", c.party.name if c.party else "Counterparty"))
    return _build(story, c.status, f"Contract {label}")
