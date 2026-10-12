"""Control tools: planning, working memory, human-in-the-loop and termination."""
from __future__ import annotations

from ..context import ApprovalGrant
from .base import Assessment, Tool, ToolResult, schema

STEP_STATUSES = ["pending", "in_progress", "done", "failed", "skipped"]


class UpdatePlan(Tool):
    name = "update_plan"
    description = ("Create or update your step-by-step plan for the task. Send the full list every time. "
                   "Keep exactly one step in_progress while working; mark steps done/failed/skipped as you go.")
    input_schema = schema({
        "steps": {
            "type": "array",
            "items": schema({"step": {"type": "string"}, "status": {"type": "string", "enum": STEP_STATUSES}},
                            ["step", "status"]),
        },
    }, ["steps"])

    async def assess(self, ctx, args):
        return Assessment("control")

    async def run(self, ctx, steps: list) -> ToolResult:
        clean = [{"step": str(s.get("step", "")), "status": s.get("status", "pending")}
                 for s in steps if isinstance(s, dict)]
        ctx.plan[:] = clean
        ctx.emit("plan_updated", plan=clean)
        done = sum(s["status"] == "done" for s in clean)
        return ToolResult(f"Plan updated ({done}/{len(clean)} done).")


class Remember(Tool):
    name = "remember"
    description = ("Save an important fact discovered during the task to working memory (e.g. invoice number, "
                   "amount, due date, record id). Memory is shown to the user and the verifier, and echoed back "
                   "to you. Re-using a key overwrites it.")
    input_schema = schema({"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"])

    async def assess(self, ctx, args):
        return Assessment("control")

    async def run(self, ctx, key: str, value: str) -> ToolResult:
        ctx.memory[key] = value
        ctx.emit("memory_updated", memory=dict(ctx.memory))
        listing = "\n".join(f"- {k}: {v}" for k, v in ctx.memory.items())
        return ToolResult(f"Saved. Working memory now:\n{listing}")


class AskUser(Tool):
    name = "ask_user"
    description = ("Ask the user a question and wait for the answer. Use ONLY when you cannot proceed safely: "
                   "the request is ambiguous in a way that changes the outcome, required information is missing, "
                   "or the next step goes beyond what was asked. Offer concrete options when possible.")
    input_schema = schema({
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}, "description": "Suggested answers"},
    }, ["question"])

    async def assess(self, ctx, args):
        return Assessment("control")

    async def run(self, ctx, question: str, options: list | None = None) -> ToolResult:
        resp = await ctx.interact("question", {"question": question, "options": options or []})
        if resp.get("timeout"):
            return ToolResult("The user did not respond in time. Do not guess: finish with status 'blocked' and "
                              "explain what you need, unless a safe default clearly exists.", is_error=True)
        return ToolResult(f"User answered: {resp.get('answer', '')}")


class RequestApproval(Tool):
    name = "request_approval"
    description = ("Ask the user to approve a consequential action BEFORE doing it (e.g. approving a payment, "
                   "deleting a record, anything irreversible). If approved, the approval covers that action for the "
                   "next few turns.")
    input_schema = schema({
        "action": {"type": "string", "description": "Exactly what you are about to do"},
        "reason": {"type": "string", "description": "Why it is needed for the user's goal"},
    }, ["action", "reason"])

    async def assess(self, ctx, args):
        return Assessment("control")

    async def run(self, ctx, action: str, reason: str) -> ToolResult:
        resp = await ctx.interact("approval", {"action": action, "reason": reason, "source": "agent"})
        if resp.get("approved"):
            ctx.approval_grant = ApprovalGrant(action, expires_turn=ctx.turn + 4)
            return ToolResult(f"Approved by user. {resp.get('comment', '')}".strip())
        if resp.get("timeout"):
            return ToolResult("No response to the approval request. Do not perform the action.", is_error=True)
        return ToolResult(f"User DENIED the action. {resp.get('comment', '')} Do not perform it.".strip(),
                          is_error=True)


class Finish(Tool):
    name = "finish"
    description = ("End the task. status: 'completed' (goal achieved and checked), 'blocked' (cannot continue "
                   "without something from the user), or 'failed'. summary: concise report for the user. result: "
                   "key structured data (values extracted, record ids...). evidence: screenshot paths, file names, "
                   "record URLs that prove the outcome.")
    input_schema = schema({
        "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
        "summary": {"type": "string"},
        "result": {"type": "object"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    }, ["status", "summary"])

    async def assess(self, ctx, args):
        return Assessment("control")

    async def run(self, ctx, status: str, summary: str, result: dict | None = None,
                  evidence: list | None = None) -> ToolResult:
        outcome = {"status": status, "summary": summary, "result": result or {}, "evidence": evidence or []}
        return ToolResult("Recorded final report.", data={"outcome": outcome}, terminal=True)


class SubmitVerdict(Tool):
    name = "submit_verdict"
    description = ("Submit your independent verification verdict. verdict: 'pass' only if every check is "
                   "confirmed from the systems themselves; 'fail' if anything is wrong or missing; 'uncertain' if "
                   "you could not check. checks: one entry per claim. feedback: concrete fix instructions for the "
                   "worker when failing.")
    input_schema = schema({
        "verdict": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
        "checks": {
            "type": "array",
            "items": schema({
                "check": {"type": "string"},
                "expected": {"type": "string"},
                "observed": {"type": "string"},
                "ok": {"type": "boolean"},
            }, ["check", "ok"]),
        },
        "feedback": {"type": "string"},
    }, ["verdict", "checks"])

    async def assess(self, ctx, args):
        return Assessment("control")

    async def run(self, ctx, verdict: str, checks: list, feedback: str = "") -> ToolResult:
        outcome = {"verdict": verdict, "checks": checks, "feedback": feedback}
        return ToolResult("Verdict recorded.", data={"outcome": outcome}, terminal=True)
