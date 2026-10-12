"""The agent loop: think -> act -> observe -> adapt, until the agent finishes or runs out of budget.

The same engine runs both the worker and the verifier; only the system prompt, tool set and
policy differ. Conversation history is append-only (thinking blocks stay valid and the prompt
cache stays warm); harness guidance is appended as text after tool results, never edited in.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field

from .context import RunContext
from .llm import ModelClient
from .policy import Policy
from .tools.base import Tool, ToolError, ToolResult, TransientToolError

REDACT_KEYS = {"content"}  # long tool inputs we shorten in the trace


@dataclass
class LoopOutcome:
    status: str  # completed | blocked | failed | (verifier) pass | fail | uncertain
    data: dict = field(default_factory=dict)
    turns: int = 0


@dataclass
class LoopStats:
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    recovered_errors: int = 0
    approvals_requested: int = 0
    policy_blocks: int = 0


class AgentLoop:
    def __init__(self, *, ctx: RunContext, llm: ModelClient, system: str, tools: list[Tool], policy: Policy,
                 max_turns: int, effort: str, terminal_tool: str):
        self.ctx = ctx
        self.llm = llm
        self.system = system
        self.tools = {t.name: t for t in tools}
        self.tool_specs = [t.spec() for t in tools]  # fixed for the whole session (cache + thinking validity)
        self.policy = policy
        self.max_turns = max_turns
        self.effort = effort
        self.terminal_tool = terminal_tool
        self.messages: list = []
        self.stats = LoopStats()
        self._failures: Counter = Counter()
        self._consecutive_errors = 0
        self._pending_error = False

    # ------------------------------------------------------------------ public
    async def run(self, user_content: str) -> LoopOutcome:
        self.messages.append({"role": "user", "content": user_content})
        return await self._drive()

    async def resume(self, user_content: str, extra_turns: int) -> LoopOutcome:
        """Continue the same conversation (e.g. with verifier feedback) for a bounded number of turns."""
        self.messages.append({"role": "user", "content": user_content})
        self.max_turns = self.stats.turns + extra_turns
        return await self._drive()

    # ------------------------------------------------------------------ core
    async def _drive(self) -> LoopOutcome:
        idle_nudges = 0
        while self.stats.turns < self.max_turns:
            self.stats.turns += 1
            self.ctx.turn = self.stats.turns
            resp = await self.llm.complete(system=self.system, tools=self.tool_specs, messages=self.messages,
                                           effort=self.effort)
            self.messages.append({"role": "assistant", "content": resp.content})
            self._emit_reasoning(resp)
            self.ctx.emit("usage", usage=self.llm.usage.to_dict(self.llm.model))

            if resp.stop_reason == "refusal":
                details = getattr(resp, "stop_details", None)
                why = getattr(details, "explanation", None) or "the model declined this request"
                return LoopOutcome("failed", {"summary": f"Stopped: {why}"}, self.stats.turns)

            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                if resp.stop_reason == "max_tokens":
                    self._append_user_text("[harness] Your output was cut off. Continue, keeping steps small.")
                    continue
                idle_nudges += 1
                if idle_nudges > 2:
                    text = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                    return LoopOutcome("failed", {"summary": text or "Agent stopped without finishing."},
                                       self.stats.turns)
                self._append_user_text(f"[harness] You ended your turn without calling a tool. Nothing happens "
                                       f"unless you use tools. If you are done or blocked, call "
                                       f"{self.terminal_tool}; otherwise continue.")
                continue
            idle_nudges = 0

            results, outcome = [], None
            for tu in tool_uses:
                result = await self._execute(tu)
                block = {"type": "tool_result", "tool_use_id": tu.id, "content": result.content or "(no output)"}
                if result.is_error:
                    block["is_error"] = True
                results.append(block)
                if result.terminal and outcome is None:
                    outcome = result.data.get("outcome", {})
            notes = self._harness_notes()
            self.messages.append({"role": "user", "content": results + [{"type": "text", "text": n} for n in notes]})
            for n in notes:
                self.ctx.emit("harness_note", text=n)
            if outcome is not None:
                status = outcome.get("status") or outcome.get("verdict") or "completed"
                return LoopOutcome(status, outcome, self.stats.turns)
        return LoopOutcome("failed", {"summary": f"Turn budget of {self.max_turns} exhausted before finishing."},
                           self.stats.turns)

    def _emit_reasoning(self, resp) -> None:
        for b in resp.content:
            t = getattr(b, "type", "")
            if t == "thinking" and getattr(b, "thinking", ""):
                self.ctx.emit("thinking", text=b.thinking, turn=self.stats.turns)
            elif t == "text" and b.text.strip():
                self.ctx.emit("message", text=b.text, turn=self.stats.turns)
            elif t == "fallback":
                self.ctx.emit("harness_note", text="Model safety fallback engaged for this turn.")

    def _append_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        self.ctx.emit("harness_note", text=text)

    # ------------------------------------------------------------------ tool execution
    async def _execute(self, tu) -> ToolResult:
        name, args = tu.name, tu.input if isinstance(tu.input, dict) else {}
        self.stats.tool_calls += 1
        self.ctx.emit("tool_call", call_id=tu.id, tool=name, input=_trace_input(args), turn=self.stats.turns)
        tool = self.tools.get(name)
        if tool is None:
            result = ToolResult(f"Unknown tool '{name}'. Available: {', '.join(self.tools)}", is_error=True)
        elif parse_error := getattr(tu, "parse_error", None):
            result = ToolResult(f"Your arguments for {name} were not valid JSON ({parse_error}). Call it again "
                                f"with a JSON object matching the schema.", is_error=True)
        elif err := tool.validate(args):
            result = ToolResult(f"Invalid input for {name}: {err}", is_error=True)
        else:
            result = await self._guarded_run(tool, args)
        self._track(name, args, result)
        self.ctx.emit("tool_result", call_id=tu.id, tool=name, ok=not result.is_error,
                      content=_preview(result.content), data=result.data, turn=self.stats.turns)
        return result

    async def _guarded_run(self, tool: Tool, args: dict) -> ToolResult:
        try:
            assessment = await tool.assess(self.ctx, args)
        except Exception as e:  # assessment must never crash the loop
            return ToolResult(f"Could not evaluate action: {e}", is_error=True)
        decision = self.policy.decide(self.ctx, tool.name, assessment)
        if decision.action == "deny":
            self.stats.policy_blocks += 1
            self.ctx.emit("policy", decision="deny", reason=decision.reason, tool=tool.name, target=decision.target)
            return ToolResult(f"BLOCKED by policy: {decision.reason}", is_error=True)
        if decision.action == "ask":
            self.stats.approvals_requested += 1
            resp = await self.ctx.interact("approval", {
                "action": f"{tool.name}: {decision.target}", "reason": decision.reason,
                "kind": decision.kind, "source": "policy",
            })
            if not resp.get("approved"):
                why = "no response" if resp.get("timeout") else (resp.get("comment") or "denied")
                return ToolResult(f"Action held for human approval and NOT executed ({why}). Do not retry it; "
                                  f"choose another approach or finish and explain.", is_error=True)
            self.ctx.approved_signatures.add(f"{decision.kind}|{decision.target}|{assessment.url}")
        return await self._run_with_retry(tool, args)

    async def _run_with_retry(self, tool: Tool, args: dict) -> ToolResult:
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(tool.run(self.ctx, **args), timeout=self.ctx.settings.tool_timeout)
            except TransientToolError as e:
                if attempt == 1:
                    self.ctx.emit("harness_note", text=f"{tool.name}: transient error, retrying once ({e})")
                    await asyncio.sleep(1.0)
                    continue
                return ToolResult(f"Error (after automatic retry): {e}", is_error=True)
            except ToolError as e:
                return ToolResult(f"Error: {e}", is_error=True)
            except asyncio.TimeoutError:
                return ToolResult(f"Error: {tool.name} timed out after {self.ctx.settings.tool_timeout}s.",
                                  is_error=True)
            except Exception as e:  # unexpected tool bug - report to the model rather than crash the run
                return ToolResult(f"Unexpected error in {tool.name}: {type(e).__name__}: {e}", is_error=True)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ stuck detection
    def _track(self, name: str, args: dict, result: ToolResult) -> None:
        if result.is_error:
            self.stats.tool_errors += 1
            self._consecutive_errors += 1
            self._failures[_signature(name, args)] += 1
            self._pending_error = True
        else:
            if self._pending_error:
                self.stats.recovered_errors += 1
                self._pending_error = False
            self._consecutive_errors = 0

    def _harness_notes(self) -> list[str]:
        notes = []
        repeated = [sig for sig, n in self._failures.items() if n == 3]
        for sig in repeated:
            self._failures[sig] += 1  # only warn once per signature
            notes.append(f"[harness] The same action has now failed 3 times ({sig[:120]}). Stop repeating it: "
                         "change approach (different page/route/input), ask the user, or finish as blocked.")
        if self._consecutive_errors >= 5:
            notes.append("[harness] Five consecutive tool errors. Step back, re-read the current state with a "
                         "snapshot, and reconsider your plan.")
            self._consecutive_errors = 0
        left = self.max_turns - self.stats.turns
        if left == 5:
            notes.append(f"[harness] Only {left} turns left. Wrap up: verify what you have and call "
                         f"{self.terminal_tool}.")
        return notes


def _signature(name: str, args: dict) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True)[:200]}"


def _trace_input(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if k in REDACT_KEYS and isinstance(v, str) and len(v) > 400:
            out[k] = v[:400] + f"... [{len(v)} chars]"
        else:
            out[k] = v
    return out


def _preview(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} more chars]"
