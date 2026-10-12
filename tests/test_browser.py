"""Browser tools against the live sim apps: refs, secrets, downloads, failure explanations."""
import re

import httpx
import pytest

from worker.context import RunContext
from worker.events import EventLog
from worker.tools.base import ToolError
from worker.tools.browser import BrowserClick, BrowserNavigate, BrowserSelect, BrowserTypeSecret
from worker.tools.files import ReadFile


def ref(label, content):
    return re.search(r"\[(e\d+)\] " + label, content).group(1)


@pytest.mark.asyncio
async def test_portal_login_filter_download_and_read_pdf(env, settings):
    ctx = RunContext("b1", "t", settings, env, EventLog(), settings.runs_dir / "b1")
    try:
        r = await BrowserNavigate().run(ctx, url=f"{env.apps[0]['url']}/documents")
        r = await BrowserTypeSecret().run(ctx, ref=ref('textbox "Email address"', r.content), secret="supplyhub.email")
        r = await BrowserTypeSecret().run(ctx, ref=ref('textbox "Password"', r.content), secret="supplyhub.password")
        assert "portal-pass-2026" not in r.content and "ap@northwind.example" not in r.content
        r = await BrowserClick().run(ctx, ref=ref('button "Sign in"', r.content))
        r = await BrowserSelect().run(ctx, ref=ref('select "Supplier"', r.content), option="Acme Supplies Ltd")
        r = await BrowserClick().run(ctx, ref=ref('button "Apply filters"', r.content))
        r = await BrowserClick().run(ctx, ref=ref('link "INV-20761"', r.content))
        r = await BrowserClick().run(ctx, ref=ref('link "Download PDF"', r.content))
        assert "INV-20761.pdf" in r.content
        pdf = await ReadFile().run(ctx, path="INV-20761.pdf")
        assert "TOTAL DUE" in pdf.content and "5,322.11" in pdf.content and "October 31, 2026" in pdf.content
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_overlay_is_reported_and_explained(env, settings):
    erp = env.apps[1]["url"]
    httpx.post(f"{erp}/__control/chaos", json={"enabled": True})
    ctx = RunContext("b2", "t", settings, env, EventLog(), settings.runs_dir / "b2")
    try:
        r = await BrowserNavigate().run(ctx, url=erp)
        r = await BrowserTypeSecret().run(ctx, ref=ref('textbox "Username"', r.content), secret="erp.username")
        r = await BrowserTypeSecret().run(ctx, ref=ref('textbox "Password"', r.content), secret="erp.password")
        r = await BrowserClick().run(ctx, ref=ref('button "Log in"', r.content))
        assert "overlay/modal is covering the page" in r.content
        with pytest.raises(ToolError, match="covering it"):
            await BrowserClick().run(ctx, ref=ref('link "Bills"', r.content))
    finally:
        await ctx.close()
        httpx.post(f"{erp}/__control/chaos", json={"enabled": False})


@pytest.mark.asyncio
async def test_browser_type_honours_secret_placeholders(env, settings):
    from worker.tools.browser import BrowserType

    ctx = RunContext("b3", "t", settings, env, EventLog(), settings.runs_dir / "b3")
    try:
        r = await BrowserNavigate().run(ctx, url=f"{env.apps[0]['url']}/login")
        r = await BrowserType().run(ctx, ref=ref('textbox "Email address"', r.content),
                                    text="{{secret:supplyhub.email}}")
        assert "ap@northwind.example" not in r.content and 'value="[hidden]"' in r.content
        r = await BrowserType().run(ctx, ref=ref('textbox "Password"', r.content),
                                    text="{{secret:supplyhub.password}}", press_enter=True)
        assert "/documents" in r.content and "Sign out" in r.content  # logged in with the real secrets
    finally:
        await ctx.close()
