"""HTTP tool for the company apps' APIs, with {{secret:NAME}} substitution so the model never sees secrets."""
from __future__ import annotations

import json
import re

import httpx

from .base import Assessment, Tool, ToolError, ToolResult, TransientToolError, schema

SECRET_RE = re.compile(r"\{\{secret:([\w.\-]+)\}\}")
MAX_BODY_CHARS = 10000
FINANCIAL_RE = re.compile(r"(approve|payment|pay\b|transfer|wire|remit|release)", re.I)


def substitute(ctx, value: str) -> str:
    def repl(m):
        secret = ctx.env.secret(m.group(1))
        if secret is None:
            raise ToolError(f"Unknown secret '{m.group(1)}'.")
        return secret

    return SECRET_RE.sub(repl, value)


class HttpRequest(Tool):
    name = "http_request"
    description = ("Call an HTTP API of one of the company apps. Use {{secret:NAME}} placeholders for credentials "
                   "in headers, e.g. {\"Authorization\": \"Bearer {{secret:erp.api_token}}\"}. JSON responses are "
                   "returned pretty-printed.")
    input_schema = schema({
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
        "url": {"type": "string"},
        "headers": {"type": "object", "description": "Optional request headers"},
        "json_body": {"type": "object", "description": "Optional JSON body"},
    }, ["method", "url"])

    def __init__(self, read_only: bool = False):
        self.read_only = read_only
        if read_only:
            self.description += " (Read-only: GET only.)"

    async def assess(self, ctx, args):
        method, url = args.get("method", "GET"), args.get("url", "")
        if method == "GET":
            return Assessment("read", target=f"GET {url}", url=url)
        if method == "DELETE":
            return Assessment("destructive", target=f"DELETE {url}", url=url)
        if FINANCIAL_RE.search(url):
            return Assessment("financial", target=f"{method} {url}", url=url)
        return Assessment("external_write", target=f"{method} {url}", url=url)

    async def run(self, ctx, method: str, url: str, headers: dict | None = None,
                  json_body: dict | None = None) -> ToolResult:
        if self.read_only and method != "GET":
            raise ToolError("This tool is read-only in the current role; only GET is permitted.")
        if not ctx.env.is_allowed_url(url):
            raise ToolError(f"URL not allowed. Allowed hosts: {sorted(ctx.env.allowed_hosts)}")
        real_headers = {k: substitute(ctx, str(v)) for k, v in (headers or {}).items()}
        body = json.loads(substitute(ctx, json.dumps(json_body))) if json_body is not None else None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.request(method, url, headers=real_headers, json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise TransientToolError(f"Network error calling {url}: {e!r}")
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            try:
                text = json.dumps(resp.json(), indent=2)
            except ValueError:
                text = resp.text
        else:
            text = resp.text
        if len(text) > MAX_BODY_CHARS:
            text = text[:MAX_BODY_CHARS] + f"\n[... truncated {len(text) - MAX_BODY_CHARS} chars]"
        return ToolResult(f"HTTP {resp.status_code}\n{text}", is_error=resp.status_code >= 400,
                          data={"status": resp.status_code, "url": url})
