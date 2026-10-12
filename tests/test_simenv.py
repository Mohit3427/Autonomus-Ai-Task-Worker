"""The simulated apps behave like the real systems the agent must cope with."""
from fastapi.testclient import TestClient

from simenv.data import portal_documents
from simenv.erp import create_app as erp_app
from simenv.vendor_portal import create_app as portal_app

TOKEN = {"Authorization": "Bearer nw-api-7f3a9c"}


def erp_client():
    c = TestClient(erp_app())
    c.post("/login", data={"username": "jdoe", "password": "erp-pass-2026"})
    return c


def test_ground_truth_latest_acme_supplies_invoice():
    docs = [d for d in portal_documents() if d.vendor == "Acme Supplies Ltd" and d.doc_type == "Invoice"]
    latest = max(docs, key=lambda d: d.issue_date)
    assert (latest.number, str(latest.total), latest.due_date.isoformat()) == ("INV-20761", "5322.11", "2026-10-31")


def test_portal_requires_login_and_serves_pdf():
    c = TestClient(portal_app())
    assert "/login" in str(c.get("/documents").url)
    c.post("/login", data={"email": "ap@northwind.example", "password": "portal-pass-2026"})
    r = c.get("/documents/4/pdf")
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")


def test_erp_validation_rejects_non_iso_dates_and_duplicates():
    c = erp_client()
    base = {"vendor_id": "V-1001", "invoice_number": "INV-20761", "invoice_date": "2026-10-01",
            "due_date": "2026-10-31", "amount": "5322.11", "currency": "USD"}
    r = c.post("/bills/new", data={**base, "due_date": "October 31, 2026"})
    assert r.status_code == 422 and "YYYY-MM-DD" in r.text
    r = c.post("/bills/new", data={**base, "invoice_number": "INV-20649"})
    assert r.status_code == 422 and "Duplicate" in r.text
    assert c.post("/bills/new", data=base).status_code == 200
    bills = c.get("/api/bills?invoice_number=INV-20761", headers=TOKEN).json()
    assert len(bills) == 1 and bills[0]["amount"] == "5322.11"


def test_api_requires_token():
    c = TestClient(erp_app())
    assert c.get("/api/bills").status_code == 401


def test_chaos_faults_fire_once():
    c = TestClient(erp_app())
    c.post("/__control/chaos", json={"enabled": True})
    c.post("/login", data={"username": "jdoe", "password": "erp-pass-2026"}, follow_redirects=False)
    r = c.post("/api/bills", headers=TOKEN, json={"vendor_id": "V-1003", "invoice_number": "GLX-7901",
                                                  "invoice_date": "2026-10-09", "due_date": "2026-10-24",
                                                  "amount": "2160.00"})
    assert r.status_code == 503
    r = c.post("/api/bills", headers=TOKEN, json={"vendor_id": "V-1003", "invoice_number": "GLX-7901",
                                                  "invoice_date": "2026-10-09", "due_date": "2026-10-24",
                                                  "amount": "2160.00"})
    assert r.status_code == 201
