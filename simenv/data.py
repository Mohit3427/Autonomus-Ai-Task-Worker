"""Seed data for the simulated company environment.

Two systems share a world:
  * SupplyHub      - a third-party vendor billing portal where suppliers publish
                     invoices and credit notes to their customers (us).
  * Northwind AP   - our internal accounts-payable system ("the ERP").

Everything is deterministic so scenario checks can compare against ground truth.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

CUSTOMER_NAME = "Northwind Traders Inc."


def money(x: str | float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class LineItem:
    description: str
    qty: int
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return money(self.unit_price * self.qty)


@dataclass
class PortalDocument:
    """An invoice or credit note published on the vendor portal."""

    id: int
    number: str
    vendor: str
    doc_type: str  # "Invoice" | "Credit Note"
    issue_date: date
    terms_days: int
    status: str  # "Open" | "Paid" | "Applied"
    tax_rate: Decimal
    lines: list[LineItem] = field(default_factory=list)
    currency: str = "USD"

    @property
    def subtotal(self) -> Decimal:
        return money(sum((li.amount for li in self.lines), Decimal("0")))

    @property
    def tax(self) -> Decimal:
        return money(self.subtotal * self.tax_rate)

    @property
    def total(self) -> Decimal:
        return money(self.subtotal + self.tax)

    @property
    def due_date(self) -> date:
        return self.issue_date + timedelta(days=self.terms_days)


def _doc(id, number, vendor, doc_type, issue, terms, status, tax, lines, currency="USD"):
    return PortalDocument(
        id=id,
        number=number,
        vendor=vendor,
        doc_type=doc_type,
        issue_date=date.fromisoformat(issue),
        terms_days=terms,
        status=status,
        tax_rate=Decimal(tax),
        lines=[LineItem(d, q, money(p)) for d, q, p in lines],
        currency=currency,
    )


def portal_documents() -> list[PortalDocument]:
    paper = ("A4 copy paper, 80gsm (box of 5 reams)", 40, "38.50")
    toner = ("Laser toner cartridge, black, high-yield", 12, "112.00")
    chairs = ("Ergonomic task chair, mesh back", 6, "289.00")
    freight = ("LTL freight, Chicago -> Columbus", 3, "640.00")
    docs = [
        # --- Acme Supplies Ltd ("Company X" in the canonical demo task) ---
        _doc(1, "INV-20417", "Acme Supplies Ltd", "Invoice", "2026-07-02", 30, "Paid", "0.0825", [paper, toner]),
        _doc(2, "INV-20533", "Acme Supplies Ltd", "Invoice", "2026-08-04", 30, "Paid", "0.0825", [paper]),
        _doc(3, "INV-20649", "Acme Supplies Ltd", "Invoice", "2026-09-03", 30, "Paid", "0.0825", [toner, ("Desk organizer set", 25, "18.75")]),
        _doc(4, "INV-20761", "Acme Supplies Ltd", "Invoice", "2026-10-01", 30, "Open", "0.0825",
             [paper, toner, chairs, ("Whiteboard markers, assorted (pack of 12)", 30, "9.95")]),
        # More recent than the latest invoice, but it is a credit note, not an invoice.
        _doc(5, "CN-0093", "Acme Supplies Ltd", "Credit Note", "2026-10-06", 0, "Open", "0.0825",
             [("Credit: damaged chair returned (RMA 5512)", 1, "-289.00")]),
        # --- Acme Corporation (a *different* vendor with a similar name) ---
        _doc(6, "AC-5531", "Acme Corporation", "Invoice", "2026-09-28", 45, "Open", "0.0",
             [("Industrial shelving units", 10, "415.00")]),
        _doc(7, "AC-5487", "Acme Corporation", "Invoice", "2026-08-15", 45, "Paid", "0.0",
             [("Pallet racking install", 1, "3200.00")]),
        # --- Globex Logistics ---
        _doc(8, "GLX-7710", "Globex Logistics", "Invoice", "2026-07-20", 15, "Paid", "0.0", [freight]),
        _doc(9, "GLX-7795", "Globex Logistics", "Invoice", "2026-08-22", 15, "Paid", "0.0", [freight, ("Liftgate surcharge", 3, "85.00")]),
        _doc(10, "GLX-7862", "Globex Logistics", "Invoice", "2026-09-25", 15, "Open", "0.0", [freight]),
        _doc(11, "GLX-7901", "Globex Logistics", "Invoice", "2026-10-09", 15, "Open", "0.0",
             [freight, ("Residential delivery fee", 2, "120.00")]),
        # --- Initech Software ---
        _doc(12, "IS-3001", "Initech Software", "Invoice", "2026-07-01", 30, "Paid", "0.0",
             [("TPS Reports Pro - annual license, 25 seats", 25, "96.00")]),
        _doc(13, "IS-3044", "Initech Software", "Invoice", "2026-10-02", 30, "Open", "0.0",
             [("Premium support add-on (Q4)", 1, "1850.00")]),
        # --- Umbrella Facilities (exists on the portal, NOT in our ERP vendor master) ---
        _doc(14, "UF-881", "Umbrella Facilities", "Invoice", "2026-10-08", 30, "Open", "0.0725",
             [("Office deep-clean, monthly", 1, "1400.00"), ("Window cleaning, exterior", 1, "650.00")]),
        _doc(15, "UF-842", "Umbrella Facilities", "Invoice", "2026-09-08", 30, "Paid", "0.0725",
             [("Office deep-clean, monthly", 1, "1400.00")]),
        # --- Older history so the unfiltered list paginates ---
        _doc(16, "INV-20302", "Acme Supplies Ltd", "Invoice", "2026-06-03", 30, "Paid", "0.0825", [paper]),
        _doc(17, "GLX-7655", "Globex Logistics", "Invoice", "2026-06-18", 15, "Paid", "0.0", [freight]),
        _doc(18, "IS-2950", "Initech Software", "Invoice", "2026-06-01", 30, "Paid", "0.0",
             [("Onboarding services", 1, "2400.00")]),
        _doc(19, "AC-5402", "Acme Corporation", "Invoice", "2026-06-25", 45, "Paid", "0.0",
             [("Forklift rental, 2 weeks", 1, "1750.00")]),
        _doc(20, "UF-801", "Umbrella Facilities", "Invoice", "2026-08-08", 30, "Paid", "0.0725",
             [("Office deep-clean, monthly", 1, "1400.00")]),
    ]
    return docs


# --------------------------------------------------------------------------- ERP


@dataclass
class Vendor:
    id: str
    name: str
    default_currency: str = "USD"
    payment_terms: str = "Net 30"


@dataclass
class Bill:
    id: int
    vendor_id: str
    invoice_number: str
    invoice_date: date
    due_date: date
    amount: Decimal
    currency: str
    memo: str
    status: str  # "Entered" | "Approved" | "Paid"
    created_by: str = "system"

    @property
    def ref(self) -> str:
        return f"BILL-{self.id}"


def erp_vendors() -> list[Vendor]:
    return [
        Vendor("V-1001", "Acme Supplies Ltd"),
        Vendor("V-1002", "Acme Corporation", payment_terms="Net 45"),
        Vendor("V-1003", "Globex Logistics", payment_terms="Net 15"),
        Vendor("V-1004", "Initech Software"),
    ]


def erp_bills() -> list[Bill]:
    docs = {d.number: d for d in portal_documents()}

    def from_doc(bill_id, number, vendor_id, status):
        d = docs[number]
        return Bill(bill_id, vendor_id, d.number, d.issue_date, d.due_date, d.total, d.currency,
                    f"{d.vendor} invoice {d.number}", status)

    return [
        from_doc(1001, "INV-20417", "V-1001", "Paid"),
        from_doc(1002, "INV-20533", "V-1001", "Paid"),
        from_doc(1003, "INV-20649", "V-1001", "Paid"),
        from_doc(1004, "GLX-7795", "V-1003", "Paid"),
        from_doc(1005, "GLX-7862", "V-1003", "Approved"),  # due 2026-10-10 -> overdue
        from_doc(1006, "AC-5531", "V-1002", "Entered"),
        from_doc(1007, "IS-3001", "V-1004", "Paid"),
        # Manually keyed bill, already overdue and still not approved
        Bill(1008, "V-1004", "IS-2990", date(2026, 8, 20), date(2026, 9, 19), money("780.00"), "USD",
             "Initech consulting hours (August)", "Entered"),
    ]


def fresh_state() -> dict:
    return copy.deepcopy({"documents": portal_documents(), "vendors": erp_vendors(), "bills": erp_bills()})


def sim_today() -> date:
    """The simulated 'today'. Pinned via SIM_TODAY so scenarios are reproducible."""
    import os

    raw = os.environ.get("SIM_TODAY")
    return date.fromisoformat(raw) if raw else date.today()
