"""SupplyHub - a simulated third-party vendor billing portal."""
from __future__ import annotations

import math

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .common import Chaos, Sessions, templates_for
from .data import portal_documents
from .pdfgen import render_pdf

USERS = {"ap@northwind.example": "portal-pass-2026"}
PAGE_SIZE = 8


def create_app() -> FastAPI:
    app = FastAPI(title="SupplyHub Vendor Portal", docs_url=None, redoc_url=None)
    tpl = templates_for("portal")
    sessions = Sessions("supplyhub_session")
    chaos = Chaos(["pdf_download_500"])
    state = {"docs": portal_documents()}

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        return tpl.TemplateResponse(request, name, {"user": sessions.user(request), **ctx}, status_code=status_code)

    def require_login(request: Request):
        if not sessions.user(request):
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return None

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return RedirectResponse("/documents" if sessions.user(request) else "/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/documents"):
        return render(request, "login.html", next=next, error=None)

    @app.post("/login")
    async def login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/documents")):
        if USERS.get(email.strip().lower()) != password:
            return render(request, "login.html", next=next, error="Invalid email or password.")
        resp = RedirectResponse(next or "/documents", status_code=303)
        resp.set_cookie(sessions.cookie_name, sessions.create(email))
        return resp

    @app.get("/logout")
    async def logout(request: Request):
        sessions.destroy(request)
        return RedirectResponse("/login", status_code=303)

    @app.get("/documents", response_class=HTMLResponse)
    async def documents(request: Request, vendor: str = "", doc_type: str = "", sort: str = "date_asc", page: int = 1):
        if r := require_login(request):
            return r
        docs = state["docs"]
        if vendor:
            docs = [d for d in docs if d.vendor == vendor]
        if doc_type:
            docs = [d for d in docs if d.doc_type == doc_type]
        docs = sorted(docs, key=lambda d: (d.issue_date, d.number), reverse=(sort == "date_desc"))
        pages = max(1, math.ceil(len(docs) / PAGE_SIZE))
        page = min(max(page, 1), pages)
        vendors = sorted({d.vendor for d in state["docs"]})
        return render(
            request, "documents.html",
            docs=docs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
            vendors=vendors, vendor=vendor, doc_type=doc_type, sort=sort, page=page, pages=pages,
            total=len(docs),
        )

    def find(doc_id: int):
        return next((d for d in state["docs"] if d.id == doc_id), None)

    @app.get("/documents/{doc_id}", response_class=HTMLResponse)
    async def document_detail(request: Request, doc_id: int):
        if r := require_login(request):
            return r
        doc = find(doc_id)
        if not doc:
            return render(request, "error.html", status_code=404, message="Document not found.")
        return render(request, "detail.html", doc=doc)

    @app.get("/documents/{doc_id}/pdf")
    async def document_pdf(request: Request, doc_id: int):
        if r := require_login(request):
            return r
        doc = find(doc_id)
        if not doc:
            return Response("Not found", status_code=404)
        if chaos.fire("pdf_download_500"):
            return render(request, "error.html", status_code=500,
                          message="Document service error (DS-500): the file could not be generated. Please try again.")
        return Response(
            render_pdf(doc),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{doc.number}.pdf"'},
        )

    # ---- control plane used by the harness (not visible to the agent) ----
    @app.post("/__control/reset")
    async def reset():
        state["docs"] = portal_documents()
        sessions.clear()
        chaos.configure(chaos.enabled)
        return {"ok": True}

    @app.post("/__control/chaos")
    async def set_chaos(payload: dict):
        chaos.configure(bool(payload.get("enabled")))
        return chaos.status()

    @app.get("/__control/status")
    async def status():
        return JSONResponse({"chaos": chaos.status(), "documents": len(state["docs"])})

    return app
