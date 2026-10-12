"""Browser tools: a real Chromium (Playwright) driven through a text snapshot with element refs.

Design: rather than pixel-level computer use, the model sees a compact DOM-derived view
where every interactive element has a ref (e.g. [e12]). Actions target refs, and every
action returns the *new* page state, so the model always observes the effect of what it did.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from .base import Assessment, Tool, ToolError, ToolResult, TransientToolError, schema
from .http import SECRET_RE, substitute

if TYPE_CHECKING:
    from ..context import RunContext

SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text()
SNAPSHOT_MAX_CHARS = 12000
ACTION_TIMEOUT_MS = 5000

DESCRIBE_JS = """el => {
  const form = el.form || el.closest('form');
  const t = (el.type || '').toLowerCase();
  const isSubmit = (el.tagName === 'BUTTON' && (t === 'submit' || t === '')) ||
                   (el.tagName === 'INPUT' && ['submit', 'image'].includes(t));
  return {
    tag: el.tagName, type: t,
    text: ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').trim().slice(0, 100),
    href: el.getAttribute('href'),
    is_submit: !!(isSubmit && form),
    form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : null,
    form_action: form ? form.action : null,
    form_has_password: form ? !!form.querySelector('input[type=password]') : false,
  };
}"""


class BrowserSession:
    """One browser context + page per agent, with download capture and status tracking."""

    def __init__(self, ctx: "RunContext"):
        self.ctx = ctx
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None
        self.last_status: int | None = None
        self.downloads: list[str] = []
        self._pending_downloads: list[asyncio.Task] = []

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.ctx.settings.headless,
                                                       slow_mo=0 if self.ctx.settings.headless else 150)
        self.context = await self._browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 860})
        self.page = await self.context.new_page()
        self.page.on("response", self._on_response)
        self.page.on("download", lambda d: self._pending_downloads.append(asyncio.create_task(self._save(d))))

    async def close(self) -> None:
        for closer in (self.context, self._browser):
            try:
                if closer:
                    await closer.close()
            except PlaywrightError:
                pass
        if self._pw:
            await self._pw.stop()

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
        except PlaywrightError:
            pass

    async def _save(self, download) -> str:
        name = Path(download.suggested_filename).name or "download.bin"
        target = self.ctx.workspace / name
        await download.save_as(target)
        rel = str(target.relative_to(self.ctx.workspace))
        self.downloads.append(rel)
        return rel

    async def drain_downloads(self) -> list[str]:
        if not self._pending_downloads:
            return []
        tasks, self._pending_downloads = self._pending_downloads, []
        done = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=20)
        return [d for d in done if isinstance(d, str)]

    async def settle(self) -> None:
        try:
            await self.page.wait_for_load_state("load", timeout=10000)
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(0.25)

    def locator(self, ref: str):
        return self.page.locator(f'[data-agent-ref="{ref}"]')

    async def resolve(self, ref: str):
        loc = self.locator(ref)
        if await loc.count() == 0:
            raise ToolError(f"Element {ref} not found on the current page. Refs change after every page update - "
                            "use the refs from the most recent page state (call browser_snapshot if unsure).")
        return loc.first

    async def describe(self, ref: str) -> dict | None:
        loc = self.locator(ref)
        if await loc.count() == 0:
            return None
        return await loc.first.evaluate(DESCRIBE_JS)

    async def observe(self, header: str = "", screenshot: bool = True) -> ToolResult:
        """Return the page state after an action: URL, status, downloads, alerts and snapshot."""
        new_downloads = await self.drain_downloads()
        snap = await self.page.evaluate(SNAPSHOT_JS, SNAPSHOT_MAX_CHARS)
        title = await self.page.title()
        parts = [header] if header else []
        parts.append(f"URL: {self.page.url}\nTitle: {title}")
        is_error = False
        if self.last_status and self.last_status >= 400:
            parts.append(f"WARNING: last page load returned HTTP {self.last_status}.")
            is_error = self.last_status >= 500
        if new_downloads:
            parts.append("Downloaded to workspace: " + ", ".join(new_downloads) + " (read with read_file)")
        if snap["overlay"]:
            parts.append(f"NOTE: a full-screen overlay/modal is covering the page: \"{snap['overlay']}\". "
                         "It will block clicks on elements behind it until dismissed.")
        parts.append("--- page ---\n" + snap["text"])
        if snap["truncated"]:
            parts.append(f"[... {snap['truncated']} more characters truncated. Narrow the view, e.g. with filters.]")
        data = {"url": self.page.url, "status": self.last_status, "downloads": new_downloads}
        if screenshot:
            path = self.ctx.next_shot_path()
            try:
                await self.page.screenshot(path=str(path), type="jpeg", quality=60)
                data["screenshot"] = self.ctx.rel(path)
            except PlaywrightError:
                pass
        return ToolResult("\n".join(parts), is_error=is_error, data=data)


def _explain(e: Exception, ref: str = "") -> ToolError:
    msg = str(e)
    if "intercepts pointer events" in msg:
        return ToolError(f"Could not click {ref}: another element (likely a modal or overlay) is covering it. "
                         "Dismiss the overlay first, then retry.")
    if "not visible" in msg or "outside of the viewport" in msg:
        return ToolError(f"Element {ref} is not visible/interactable right now.")
    if "Timeout" in msg or isinstance(e, PlaywrightTimeout):
        return TransientToolError(f"Timed out waiting for {ref or 'the page'}: {msg.splitlines()[0][:200]}")
    if "net::ERR" in msg:
        return TransientToolError(f"Network error: {msg.splitlines()[0][:200]}")
    return ToolError(msg.splitlines()[0][:300])


# --------------------------------------------------------------------------- tools


class BrowserNavigate(Tool):
    name = "browser_navigate"
    description = ("Open a URL in the browser (only the company apps listed in the environment are allowed). "
                   "Returns the resulting page state with element refs. Download links are saved to the workspace.")
    input_schema = schema({"url": {"type": "string", "description": "Absolute URL"}}, ["url"])

    async def assess(self, ctx, args):
        return Assessment("read", target=args.get("url", ""), url=args.get("url", ""))

    async def run(self, ctx, url: str) -> ToolResult:
        b = await ctx.browser()
        b.last_status = None
        try:
            await b.page.goto(url, wait_until="load", timeout=15000)
        except PlaywrightError as e:
            if "Download is starting" in str(e):
                await asyncio.sleep(0.5)
                return await b.observe("Navigation triggered a file download.")
            raise _explain(e)
        return await b.observe()


class BrowserClick(Tool):
    name = "browser_click"
    description = "Click an element (link, button, checkbox...) by its ref from the latest page state."
    input_schema = schema({"ref": {"type": "string", "description": "Element ref, e.g. e12"}}, ["ref"])

    async def assess(self, ctx, args):
        b = await ctx.browser()
        info = await b.describe(args.get("ref", ""))
        if info is None:
            return Assessment("read", target=f"unknown element {args.get('ref')}")
        target = f"{info['tag'].lower()} \"{info['text']}\""
        if info["is_submit"]:
            if info["form_has_password"]:
                return Assessment("auth", target=target, url=info["form_action"] or "")
            if info["form_method"] == "post":
                return Assessment("submit", target=target, url=info["form_action"] or "")
        return Assessment("read", target=target, url=info.get("href") or "")

    async def run(self, ctx, ref: str) -> ToolResult:
        b = await ctx.browser()
        el = await b.resolve(ref)
        b.last_status = None
        try:
            await el.click(timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as e:
            raise _explain(e, ref)
        await b.settle()
        return await b.observe(f"Clicked {ref}.")


class BrowserType(Tool):
    name = "browser_type"
    description = ("Replace the contents of a text field with the given text. Set press_enter to submit the "
                   "field's form afterwards.")
    input_schema = schema({
        "ref": {"type": "string"},
        "text": {"type": "string"},
        "press_enter": {"type": "boolean", "description": "Press Enter after typing (submits the form)"},
    }, ["ref", "text"])

    async def assess(self, ctx, args):
        if not args.get("press_enter"):
            return Assessment("input", target=f"field {args.get('ref')}")
        b = await ctx.browser()
        info = await b.describe(args.get("ref", "")) or {}
        if info.get("form_has_password"):
            return Assessment("auth", target="login form")
        if info.get("form_method") == "post":
            return Assessment("submit", target=f"form via field {args.get('ref')}", url=info.get("form_action") or "")
        return Assessment("read", target="search/filter form")

    async def run(self, ctx, ref: str, text: str, press_enter: bool = False) -> ToolResult:
        b = await ctx.browser()
        el = await b.resolve(ref)
        has_secret = bool(SECRET_RE.search(text))
        if has_secret:  # models sometimes use API-style placeholders here; honour them, never echo values
            text = substitute(ctx, text)
        try:
            if has_secret:
                await el.evaluate("el => el.setAttribute('data-agent-secret', '1')")
            await el.fill(text, timeout=ACTION_TIMEOUT_MS)
            if press_enter:
                b.last_status = None
                await el.press("Enter")
                await b.settle()
        except PlaywrightError as e:
            raise _explain(e, ref)
        actual = await el.input_value() if not (press_enter or has_secret) else None
        note = f"Typed into {ref}." + (f" Field now contains: {actual!r}" if actual is not None else "")
        return await b.observe(note, screenshot=press_enter)


class BrowserTypeSecret(Tool):
    name = "browser_type_secret"
    description = ("Type a stored secret (password, username, token) into a field without revealing it. "
                   "Use the secret NAME from the environment, e.g. 'erp.password'.")
    input_schema = schema({"ref": {"type": "string"}, "secret": {"type": "string", "description": "Secret name"}},
                          ["ref", "secret"])

    async def assess(self, ctx, args):
        return Assessment("input", target=f"secret {args.get('secret')} into {args.get('ref')}")

    async def run(self, ctx, ref: str, secret: str) -> ToolResult:
        value = ctx.env.secret(secret)
        if value is None:
            raise ToolError(f"Unknown secret '{secret}'. Available: {', '.join(sorted(ctx.env.secrets))}")
        b = await ctx.browser()
        el = await b.resolve(ref)
        try:
            await el.evaluate("el => el.setAttribute('data-agent-secret', '1')")
            await el.fill(value, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError as e:
            raise _explain(e, ref)
        return await b.observe(f"Entered secret '{secret}' into {ref}.", screenshot=False)


class BrowserSelect(Tool):
    name = "browser_select"
    description = "Choose an option in a <select> dropdown by its visible label (or value)."
    input_schema = schema({"ref": {"type": "string"}, "option": {"type": "string"}}, ["ref", "option"])

    async def assess(self, ctx, args):
        return Assessment("input", target=f"select {args.get('ref')} = {args.get('option')}")

    async def run(self, ctx, ref: str, option: str) -> ToolResult:
        b = await ctx.browser()
        el = await b.resolve(ref)
        try:
            try:
                await el.select_option(label=option, timeout=ACTION_TIMEOUT_MS)
            except PlaywrightError:
                await el.select_option(value=option, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightError:
            opts = await el.evaluate("el => [...el.options].map(o => o.text.trim())")
            raise ToolError(f"No option matching {option!r}. Available options: {opts}")
        return await b.observe(f"Selected {option!r} in {ref}.", screenshot=False)


class BrowserSnapshot(Tool):
    name = "browser_snapshot"
    description = "Re-read the current page state (URL, alerts, text and element refs) without acting."
    input_schema = schema({})

    async def run(self, ctx) -> ToolResult:
        b = await ctx.browser()
        return await b.observe(screenshot=False)


class BrowserBack(Tool):
    name = "browser_back"
    description = "Go back to the previous page in browser history."
    input_schema = schema({})

    async def run(self, ctx) -> ToolResult:
        b = await ctx.browser()
        try:
            await b.page.go_back(wait_until="load", timeout=10000)
        except PlaywrightError as e:
            raise _explain(e)
        return await b.observe("Went back.")


class BrowserScreenshot(Tool):
    name = "browser_screenshot"
    description = ("Capture a full-page screenshot as evidence for the final report (e.g. the source document, "
                   "the saved record). Give it a short descriptive label.")
    input_schema = schema({"label": {"type": "string"}}, ["label"])

    async def run(self, ctx, label: str) -> ToolResult:
        b = await ctx.browser()
        path = ctx.next_shot_path(label, ext="png")
        await b.page.screenshot(path=str(path), full_page=True)
        rel = ctx.rel(path)
        return ToolResult(f"Saved evidence screenshot '{label}' as {rel} (page: {b.page.url}).",
                          data={"screenshot": rel, "evidence": True, "label": label, "url": b.page.url})


BROWSER_TOOLS = [BrowserNavigate, BrowserClick, BrowserType, BrowserTypeSecret, BrowserSelect, BrowserSnapshot,
                 BrowserBack, BrowserScreenshot]
