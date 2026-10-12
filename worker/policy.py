"""Action policy - a guardrail enforced in code, outside the model.

The model is *asked* to request approval for consequential actions, but we don't rely on
that: every tool call is assessed before it runs, and the harness itself pauses for human
approval (or blocks) based on what the action would actually do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .context import RunContext
from .tools.base import Assessment

FINANCIAL = re.compile(r"\b(approv\w*|pay|payment|transfer|wire|remit\w*|release)\b|/approve\b", re.I)
DESTRUCTIVE = re.compile(r"\b(delete|remove|void|purge|deactivate|terminate)\b|/delete\b", re.I)

AUTONOMY_LEVELS = {
    # name: kinds that require explicit human approval
    "autonomous": {"financial", "destructive"},
    "supervised": {"financial", "destructive", "submit", "external_write"},
}


@dataclass
class Decision:
    action: str  # allow | ask | deny
    reason: str = ""
    kind: str = ""
    target: str = ""


class Policy:
    def __init__(self, role: str = "worker", autonomy: str = "autonomous"):
        self.role = role  # "worker" | "verifier"
        self.autonomy = autonomy if autonomy in AUTONOMY_LEVELS else "autonomous"

    @staticmethod
    def refine(a: Assessment) -> Assessment:
        """Upgrade generic writes to financial/destructive based on what they target."""
        if a.kind in ("submit", "external_write"):
            text = f"{a.target} {a.url}"
            if FINANCIAL.search(text):
                return Assessment("financial", a.target, a.url)
            if DESTRUCTIVE.search(text):
                return Assessment("destructive", a.target, a.url)
        return a

    def decide(self, ctx: RunContext, tool_name: str, a: Assessment) -> Decision:
        a = self.refine(a)
        if a.url and a.url.startswith("http") and not ctx.env.is_allowed_url(a.url):
            return Decision("deny", f"{a.url} is outside the allowed company apps.", a.kind, a.target)

        if self.role == "verifier":
            if a.kind in ("read", "input", "auth", "control"):
                return Decision("allow", kind=a.kind, target=a.target)
            return Decision("deny", "The verifier is read-only: it may log in and look, but not change anything.",
                            a.kind, a.target)

        if a.kind in AUTONOMY_LEVELS[self.autonomy]:
            sig = f"{a.kind}|{a.target}|{a.url}"
            if sig in ctx.approved_signatures:
                return Decision("allow", "previously approved", a.kind, a.target)
            grant = ctx.approval_grant
            if grant and ctx.turn <= grant.expires_turn:
                ctx.approval_grant = None  # a grant covers one gated action
                ctx.approved_signatures.add(sig)
                return Decision("allow", f"covered by user approval: {grant.description}", a.kind, a.target)
            return Decision("ask", f"{a.kind.replace('_', ' ')} action requires approval in "
                                   f"{self.autonomy} mode", a.kind, a.target)
        return Decision("allow", kind=a.kind, target=a.target)
