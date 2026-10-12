"""Northwind AP - the simulated internal accounts-payable system (the "ERP").

Exposes both a browser UI and a small REST API so the agent can choose either path.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .common import Chaos, Sessions, templates_for
from .data import Bill, erp_bills, erp_vendors, money, sim_today

USERS = {"jdoe": "erp-pass-2026"}
API_TOKEN = "nw-api-7f3a9c"
CURRENCIES = ["USD", "EUR", "GBP"]
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def create_app() -> FastAPI:
    app = FastAPI(title="Northwind AP", docs_url=None, redoc_url=None)
    tpl = templates_for("erp")
    sessions = Sessions("nwap_session")
    chaos = Chaos(["whats_new_modal", "session_expired", "submit_503"])
    state = {"vendors": erp_vendors(), "bills": erp_bills()}
    flashes: dict[str, str] = {}  # session token -> one-shot flash message

    # ------------------------------------------------------------ helpers
    def vendor_map():
        return {v.id: v for v in state["vendors"]}

    def bill_json(b: Bill) -> dict:
        return {
            "id": b.id, "ref": b.ref, "vendor_id": b.vendor_id, "vendor_name": vendor_map()[b.vendor_id].name,
            "invoice_number": b.invoice_number, "invoice_date": b.invoice_date.isoformat(),
            "due_date": b.due_date.isoformat(), "amount": f"{b.amount:.2f}", "currency": b.currency,
            "memo": b.memo, "status": b.status, "created_by": b.created_by,
        }

    def validate(data: dict) -> tuple[dict, dict]:
        """Return (clean, errors). Shared by the HTML form and the API."""
        errors: dict[str, str] = {}
        clean: dict = {}
        vid = (data.get("vendor_id") or "").strip()
        if vid not in vendor_map():
            errors["vendor_id"] = "Select a vendor from the vendor master."
        clean["vendor_id"] = vid
        inv = (data.get("invoice_number") or "").strip()
        if not inv:
            errors["invoice_number"] = "Vendor invoice number is required."
        clean["invoice_number"] = inv
        for f, label in (("invoice_date", "Invoice date"), ("due_date", "Due date")):
            raw = (data.get(f) or "").strip()
            try:
                if not ISO_DATE.match(raw):
                    raise ValueError
                clean[f] = date.fromisoformat(raw)
            except ValueError:
                errors[f] = f"{label} must be in YYYY-MM-DD format."
        if "invoice_date" in clean and "due_date" in clean and clean["due_date"] < clean["invoice_date"]:
            errors["due_date"] = "Due date cannot be before the invoice date."
        raw_amt = str(data.get("amount") or "").replace(",", "").replace("$", "").strip()
        try:
            amt = Decimal(raw_amt)
            if amt <= 0:
                errors["amount"] = "Amount must be greater than zero."
            elif amt != amt.quantize(Decimal("0.01")):
                errors["amount"] = "Amount can have at most 2 decimal places."
            clean["amount"] = money(amt)
        except InvalidOperation:
            errors["amount"] = "Amount must be a number, e.g. 1234.56"
        cur = (data.get("currency") or "USD").strip().upper()
        if cur not in CURRENCIES:
            errors["currency"] = f"Currency must be one of {', '.join(CURRENCIES)}."
        clean["currency"] = cur
        clean["memo"] = (data.get("memo") or "").strip()[:200]
        if not errors:
            dup = next((b for b in state["bills"]
                        if b.vendor_id == vid and b.invoice_number.lower() == inv.lower()), None)
            if dup:
                errors["invoice_number"] = (f"Duplicate: invoice {inv} for this vendor is already recorded "
                                            f"as {dup.ref}.")
        return clean, errors

    def create_bill(clean: dict, user: str) -> Bill:
        b = Bill(max(b.id for b in state["bills"]) + 1, clean["vendor_id"], clean["invoice_number"],
                 clean["invoice_date"], clean["due_date"], clean["amount"], clean["currency"], clean["memo"],
                 "Entered", created_by=user)
        state["bills"].append(b)
        return b

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        token = request.cookies.get(sessions.cookie_name, "")
        return tpl.TemplateResponse(
            request, name,
            {"user": sessions.user(request), "flash": flashes.pop(token, None), "today": sim_today(), **ctx},
            status_code=status_code,
        )

    def flash(request: Request, msg: str):
        flashes[request.cookies.get(sessions.cookie_name, "")] = msg

    def require_login(request: Request):
        if not sessions.user(request):
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return None

    def api_user(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {API_TOKEN}":
            return "api"
        return sessions.user(request)

    def show_modal(request: Request) -> bool:
        return request.cookies.get("nwap_whatsnew") != "dismissed" and chaos.fire("whats_new_modal")

    # ------------------------------------------------------------ auth
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return RedirectResponse("/dashboard" if sessions.user(request) else "/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/dashboard", reason: str = ""):
        msg = "Your session has expired. Please sign in again." if reason == "expired" else None
        return render(request, "login.html", next=next, error=None, notice=msg)

    @app.post("/login")
    async def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/dashboard")):
        if USERS.get(username.strip()) != password:
            return render(request, "login.html", next=next, error="Invalid username or password.", notice=None,
                          status_code=401)
        resp = RedirectResponse(next or "/dashboard", status_code=303)
        resp.set_cookie(sessions.cookie_name, sessions.create(username.strip()))
        return resp

    @app.get("/logout")
    async def logout(request: Request):
        sessions.destroy(request)
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------ UI
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if r := require_login(request):
            return r
        bills = state["bills"]
        open_bills = [b for b in bills if b.status != "Paid"]
        overdue = [b for b in open_bills if b.due_date < sim_today()]
        return render(request, "dashboard.html", modal=show_modal(request),
                      open_count=len(open_bills), overdue_count=len(overdue),
                      open_total=sum((b.amount for b in open_bills), Decimal("0")),
                      recent=sorted(bills, key=lambda b: b.id, reverse=True)[:5], vendors=vendor_map())

    @app.get("/bills", response_class=HTMLResponse)
    async def bills_list(request: Request, vendor_id: str = "", status: str = "", q: str = ""):
        if r := require_login(request):
            return r
        bills = state["bills"]
        if vendor_id:
            bills = [b for b in bills if b.vendor_id == vendor_id]
        if status:
            bills = [b for b in bills if b.status == status]
        if q:
            bills = [b for b in bills if q.lower() in b.invoice_number.lower() or q.lower() in b.memo.lower()]
        return render(request, "bills.html", bills=sorted(bills, key=lambda b: b.id, reverse=True),
                      vendors=vendor_map(), vendor_id=vendor_id, status=status, q=q, modal=show_modal(request))

    @app.get("/bills/new", response_class=HTMLResponse)
    async def new_bill_form(request: Request):
        if r := require_login(request):
            return r
        if chaos.fire("session_expired"):
            sessions.destroy(request)
            return RedirectResponse("/login?next=/bills/new&reason=expired", status_code=303)
        return render(request, "bill_form.html", vendors=state["vendors"], currencies=CURRENCIES, form={}, errors={})

    @app.post("/bills/new", response_class=HTMLResponse)
    async def create_bill_form(request: Request):
        if r := require_login(request):
            return r
        form = dict(await request.form())
        if chaos.fire("submit_503"):
            return render(request, "error.html", status_code=503, title="503 Service Unavailable",
                          message="The ledger service is temporarily unavailable. Your bill was NOT saved. "
                                  "Please go back and try again.")
        clean, errors = validate(form)
        if errors:
            return render(request, "bill_form.html", status_code=422, vendors=state["vendors"],
                          currencies=CURRENCIES, form=form, errors=errors)
        b = create_bill(clean, sessions.user(request))
        flash(request, f"Bill {b.ref} created for invoice {b.invoice_number} (status: Entered).")
        return RedirectResponse(f"/bills/{b.id}", status_code=303)

    def find(bill_id: int) -> Bill | None:
        return next((b for b in state["bills"] if b.id == bill_id), None)

    @app.get("/bills/{bill_id}", response_class=HTMLResponse)
    async def bill_detail(request: Request, bill_id: int):
        if r := require_login(request):
            return r
        b = find(bill_id)
        if not b:
            return render(request, "error.html", status_code=404, title="Not found", message="Bill not found.")
        return render(request, "bill_detail.html", bill=b, vendor=vendor_map()[b.vendor_id])

    @app.post("/bills/{bill_id}/approve")
    async def approve_bill(request: Request, bill_id: int):
        if r := require_login(request):
            return r
        b = find(bill_id)
        if b and b.status == "Entered":
            b.status = "Approved"
            flash(request, f"{b.ref} approved for payment.")
        return RedirectResponse(f"/bills/{bill_id}", status_code=303)

    @app.post("/bills/{bill_id}/delete")
    async def delete_bill(request: Request, bill_id: int):
        if r := require_login(request):
            return r
        b = find(bill_id)
        if b and b.status == "Entered":
            state["bills"].remove(b)
            flash(request, f"{b.ref} deleted.")
        return RedirectResponse("/bills", status_code=303)

    @app.get("/vendors", response_class=HTMLResponse)
    async def vendors_page(request: Request):
        if r := require_login(request):
            return r
        return render(request, "vendors.html", vendors=state["vendors"])

    @app.get("/dismiss-whats-new")
    async def dismiss(request: Request, next: str = "/dashboard"):
        resp = RedirectResponse(next, status_code=303)
        resp.set_cookie("nwap_whatsnew", "dismissed")
        return resp

    # ------------------------------------------------------------ REST API
    def unauthorized():
        return JSONResponse({"error": "unauthorized", "hint": "Send 'Authorization: Bearer <token>'"}, status_code=401)

    @app.get("/api/vendors")
    async def api_vendors(request: Request):
        if not api_user(request):
            return unauthorized()
        return [{"id": v.id, "name": v.name, "payment_terms": v.payment_terms} for v in state["vendors"]]

    @app.get("/api/bills")
    async def api_bills(request: Request, vendor_id: str = "", invoice_number: str = "", status: str = ""):
        if not api_user(request):
            return unauthorized()
        bills = state["bills"]
        if vendor_id:
            bills = [b for b in bills if b.vendor_id == vendor_id]
        if invoice_number:
            bills = [b for b in bills if b.invoice_number.lower() == invoice_number.lower()]
        if status:
            bills = [b for b in bills if b.status == status]
        return [bill_json(b) for b in bills]

    @app.get("/api/bills/{bill_id}")
    async def api_bill(request: Request, bill_id: int):
        if not api_user(request):
            return unauthorized()
        b = find(bill_id)
        return bill_json(b) if b else JSONResponse({"error": "not found"}, status_code=404)

    @app.post("/api/bills")
    async def api_create_bill(request: Request, payload: dict):
        user = api_user(request)
        if not user:
            return unauthorized()
        if chaos.fire("submit_503"):
            return JSONResponse({"error": "ledger service temporarily unavailable; bill NOT saved, retry"},
                                status_code=503)
        clean, errors = validate(payload)
        if errors:
            return JSONResponse({"error": "validation failed", "fields": errors}, status_code=422)
        return JSONResponse(bill_json(create_bill(clean, user)), status_code=201)

    @app.post("/api/bills/{bill_id}/approve")
    async def api_approve(request: Request, bill_id: int):
        if not api_user(request):
            return unauthorized()
        b = find(bill_id)
        if not b:
            return JSONResponse({"error": "not found"}, status_code=404)
        if b.status != "Entered":
            return JSONResponse({"error": f"cannot approve a bill in status {b.status}"}, status_code=409)
        b.status = "Approved"
        return bill_json(b)

    # ------------------------------------------------------------ control plane (harness only)
    @app.post("/__control/reset")
    async def reset():
        state["vendors"], state["bills"] = erp_vendors(), erp_bills()
        sessions.clear()
        flashes.clear()
        chaos.configure(chaos.enabled)
        return {"ok": True}

    @app.post("/__control/chaos")
    async def set_chaos(payload: dict):
        chaos.configure(bool(payload.get("enabled")))
        return chaos.status()

    @app.get("/__control/status")
    async def status():
        return {"chaos": chaos.status(), "bills": len(state["bills"])}

    @app.get("/__control/bills")
    async def control_bills():
        """Ground-truth dump for scenario checks (bypasses auth; never given to the agent)."""
        return [bill_json(b) for b in state["bills"]]

    return app
