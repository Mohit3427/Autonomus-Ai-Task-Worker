"""Render a portal document as a realistic-looking invoice PDF."""
from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .data import CUSTOMER_NAME, PortalDocument


def _fmt_date(d) -> str:
    return d.strftime("%B %-d, %Y")  # e.g. "October 31, 2026" - deliberately not ISO


def _fmt_money(x) -> str:
    return f"{x:,.2f}"


def render_pdf(doc: PortalDocument) -> bytes:
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    pdf = SimpleDocTemplate(buf, pagesize=LETTER, title=f"{doc.doc_type} {doc.number}")
    story = [
        Paragraph(f"<b>{doc.vendor.upper()}</b>", styles["Title"]),
        Paragraph(doc.doc_type.upper(), styles["Heading2"]),
        Spacer(1, 8),
    ]
    meta = [
        [f"{doc.doc_type} No.:", doc.number],
        [f"{doc.doc_type} Date:", _fmt_date(doc.issue_date)],
        ["Bill To:", CUSTOMER_NAME],
        ["Currency:", doc.currency],
    ]
    if doc.doc_type == "Invoice":
        meta.insert(2, ["Payment Terms:", f"Net {doc.terms_days}"])
        meta.insert(3, ["Due Date:", _fmt_date(doc.due_date)])
    t = Table(meta, colWidths=[110, 300])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")]))
    story += [t, Spacer(1, 16)]

    rows = [["Description", "Qty", "Unit Price", "Amount"]]
    for li in doc.lines:
        rows.append([li.description, str(li.qty), _fmt_money(li.unit_price), _fmt_money(li.amount)])
    rows.append(["", "", "Subtotal", _fmt_money(doc.subtotal)])
    rows.append(["", "", f"Tax ({doc.tax_rate * 100:.2f}%)", _fmt_money(doc.tax)])
    label = "TOTAL DUE" if doc.doc_type == "Invoice" else "TOTAL CREDIT"
    rows.append(["", "", label, f"{doc.currency} {_fmt_money(doc.total)}"])
    lt = Table(rows, colWidths=[260, 40, 90, 100])
    lt.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("LINEABOVE", (2, -3), (-1, -3), 0.5, colors.grey),
                ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    story += [lt, Spacer(1, 20)]
    story.append(Paragraph("Please reference the invoice number with your remittance. Thank you for your business.",
                           styles["Italic"]))
    pdf.build(story)
    return buf.getvalue()
