"""Orchestrates one task: worker loop -> independent verification -> repair rounds -> report."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

from .config import PROVIDERS, Environment, Settings
from .context import AutoResponder, RunContext
from .events import EventLog
from .llm import ClaudeClient, LLMError, ModelClient, Usage
from .llm_openai import OpenAICompatClient
from .loop import AgentLoop, LoopOutcome, LoopStats
from .policy import Policy
from .prompts import verifier_brief, verifier_system, worker_system
from .tools.browser import BROWSER_TOOLS, BrowserClick, BrowserNavigate, BrowserSelect, BrowserSnapshot, \
    BrowserType, BrowserTypeSecret
from .tools.control import AskUser, Finish, Remember, RequestApproval, SubmitVerdict, UpdatePlan
from .tools.files import FILE_TOOLS, ListFiles, ReadFile
from .tools.http import HttpRequest


def make_client(s: Settings, on_retry) -> ModelClient:
    """Pick the model backend. The loop is backend-agnostic; only this function knows the difference."""
    if s.provider == "anthropic":
        return ClaudeClient(s.model, use_fallbacks=s.use_fallbacks, on_retry=on_retry)
    preset = PROVIDERS.get(s.provider)
    if preset is None:
        raise LLMError(f"Unknown LLM_PROVIDER '{s.provider}'. Use anthropic, gemini or openai_compat.")
    if not s.api_key:
        raise LLMError(f"No API key: set {preset['key_env']} in .env")
    return OpenAICompatClient(s.model, preset["base_url"], s.api_key, rpm=s.llm_rpm, on_retry=on_retry,
                              send_reasoning_effort=s.provider == "gemini", fallback_models=s.fallback_models)


def worker_tools():
    return [UpdatePlan(), Remember(), *[t() for t in BROWSER_TOOLS], HttpRequest(), *[t() for t in FILE_TOOLS],
            AskUser(), RequestApproval(), Finish()]


def verifier_tools():
    return [BrowserNavigate(), BrowserSnapshot(), BrowserClick(), BrowserType(), BrowserTypeSecret(), BrowserSelect(),
            HttpRequest(read_only=True), ListFiles(), ReadFile(), SubmitVerdict()]


@dataclass
class RunOptions:
    autonomy: str = "autonomous"
    verify: bool = True


@dataclass
class Run:
    id: str
    task: str
    options: RunOptions
    ctx: RunContext
    status: str = "running"
    report: dict | None = None
    started: float = field(default_factory=time.time)
    task_handle: asyncio.Task | None = None
    verifier_ctx: RunContext | None = None

    def respond(self, request_id: str, response: dict) -> bool:
        return any(c.respond(request_id, response) for c in (self.ctx, self.verifier_ctx) if c is not None)


class Runner:
    """Creates and executes runs. `llm_factory` lets tests inject a scripted model."""

    def __init__(self, settings: Settings | None = None, env: Environment | None = None,
                 llm_factory=None):
        self.settings = settings or Settings()
        self.env = env or Environment.load(self.settings.environment_file)
        self.llm_factory = llm_factory or (lambda on_retry: make_client(self.settings, on_retry))
        self.runs: dict[str, Run] = {}

    def create(self, task: str, options: RunOptions | None = None,
               auto_responder: AutoResponder | None = None) -> Run:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        run_dir = self.settings.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        events = EventLog(run_dir / "events.jsonl")
        ctx = RunContext(run_id=run_id, task=task, settings=self.settings, env=self.env, events=events,
                         run_dir=run_dir, auto_responder=auto_responder)
        run = Run(run_id, task, options or RunOptions(), ctx)
        self.runs[run_id] = run
        return run

    def start(self, run: Run) -> asyncio.Task:
        run.task_handle = asyncio.create_task(self.execute(run))
        return run.task_handle

    async def execute(self, run: Run) -> dict:
        ctx, opts, s = run.ctx, run.options, self.settings
        today = s.today
        ctx.emit("run_started", task=run.task, autonomy=opts.autonomy, provider=s.provider, model=s.model,
                 effort=s.effort)
        verification: dict | None = None
        outcome: LoopOutcome | None = None
        verifier_llm: ModelClient | None = None
        llm: ModelClient | None = None
        worker: AgentLoop | None = None
        rounds = 0
        try:
            llm = self.llm_factory(lambda msg: ctx.emit("llm_retry", message=msg))
            worker = AgentLoop(ctx=ctx, llm=llm, system=worker_system(self.env, today), tools=worker_tools(),
                               policy=Policy("worker", opts.autonomy), max_turns=s.max_turns, effort=s.effort,
                               terminal_tool="finish")
            ctx.emit("phase", name="work", label="Working on the task")
            outcome = await worker.run(run.task)
            while opts.verify and outcome.status == "completed":
                ctx.emit("phase", name="verify", label="Independent verification")
                verifier_llm = verifier_llm or self.llm_factory(lambda msg: ctx.emit("llm_retry", message=msg))
                verification = await self._verify(run, outcome, verifier_llm, today)
                ctx.emit("verification", **verification)
                if verification["verdict"] != "fail" or rounds >= s.max_repair_rounds:
                    break
                rounds += 1
                ctx.emit("phase", name="repair", label=f"Fixing issues found by verifier (round {rounds})")
                outcome = await worker.resume(_repair_message(verification), extra_turns=max(12, s.max_turns // 3))
            status = _final_status(outcome, verification, opts.verify)
        except asyncio.CancelledError:
            status, outcome = "cancelled", outcome or LoopOutcome("cancelled", {"summary": "Cancelled by user."})
        except LLMError as e:
            ctx.emit("error", message=str(e))
            status, outcome = "failed", LoopOutcome("failed", {"summary": str(e)})
        except Exception as e:  # last-resort guard so the UI always gets a report
            ctx.emit("error", message=f"Internal error: {type(e).__name__}: {e}")
            status, outcome = "failed", LoopOutcome("failed", {"summary": f"Internal error: {e}"})
        finally:
            await ctx.close()
            if run.verifier_ctx:
                await run.verifier_ctx.close()

        usage = llm.usage.to_dict(llm.model) if llm else Usage().to_dict(s.model)
        if verifier_llm is not None:
            vu = verifier_llm.usage.to_dict(verifier_llm.model)
            usage = {k: usage[k] + vu[k] for k in usage}
            usage["cost_usd"] = round(usage["cost_usd"], 4)
        report = {
            "run_id": run.id, "task": run.task, "status": status,
            "summary": outcome.data.get("summary", ""), "result": outcome.data.get("result", {}),
            "evidence": outcome.data.get("evidence", []),
            "evidence_files": _evidence_files(ctx), "verification": verification, "repair_rounds": rounds,
            "memory": ctx.memory, "plan": ctx.plan,
            "stats": {**(worker.stats if worker else LoopStats()).__dict__, "duration_s": round(time.time() - run.started, 1), "usage": usage},
            "settings": {"provider": s.provider, "model": s.model, "effort": s.effort, "autonomy": opts.autonomy},
        }
        run.status, run.report = status, report
        (ctx.run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
        ctx.emit("run_finished", report=report)
        return report

    async def _verify(self, run: Run, outcome: LoopOutcome, llm: ModelClient, today: str) -> dict:
        vctx = run.ctx.child("verifier")
        run.verifier_ctx = vctx
        files = [str(p.relative_to(vctx.workspace)) for p in vctx.workspace.rglob("*") if p.is_file()]
        loop = AgentLoop(ctx=vctx, llm=llm, system=verifier_system(self.env, today), tools=verifier_tools(),
                         policy=Policy("verifier"), max_turns=self.settings.verifier_max_turns,
                         effort=self.settings.verifier_effort, terminal_tool="submit_verdict")
        try:
            v = await loop.run(verifier_brief(run.task, outcome.data, run.ctx.memory, files))
        finally:
            await vctx.close()
            run.verifier_ctx = None
        if v.status not in ("pass", "fail", "uncertain"):
            return {"verdict": "uncertain", "checks": [], "feedback": v.data.get("summary", "Verifier did not finish.")}
        return {"verdict": v.status, "checks": v.data.get("checks", []), "feedback": v.data.get("feedback", "")}


def _repair_message(v: dict) -> str:
    failed = [c for c in v.get("checks", []) if not c.get("ok")]
    lines = "\n".join(f"- {c.get('check')}: expected {c.get('expected', '?')}, observed {c.get('observed', '?')}"
                      for c in failed)
    return ("[verifier] An independent check of the real systems found that the task is NOT correctly "
            f"complete.\nFailed checks:\n{lines or '(none listed)'}\nFeedback: {v.get('feedback', '')}\n\n"
            "Fix these problems. Check the current state first and do not create duplicates. Re-verify, then call "
            "finish again.")


def _final_status(outcome: LoopOutcome, verification: dict | None, verify: bool) -> str:
    if outcome.status != "completed":
        return outcome.status  # blocked | failed
    if not verify or verification is None:
        return "completed_unverified"
    return {"pass": "verified", "fail": "verification_failed"}.get(verification["verdict"], "completed_unverified")


def _evidence_files(ctx: RunContext) -> dict:
    shots = sorted(str(p.relative_to(ctx.run_dir)) for p in ctx.shots_dir.glob("*.png"))
    files = sorted(str(p.relative_to(ctx.run_dir)) for p in ctx.workspace.rglob("*") if p.is_file())
    return {"screenshots": shots, "files": files}
