"""Per-run state shared by the loop, tools and policy."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from .config import Environment, Settings
from .events import EventLog

if TYPE_CHECKING:
    from .tools.browser import BrowserSession

# (kind, payload) -> response; used by headless scenario runs instead of a human.
AutoResponder = Callable[[str, dict], Awaitable[dict] | dict]


@dataclass
class ApprovalGrant:
    """A user approval that covers the next gated action within a few turns."""

    description: str
    expires_turn: int


@dataclass
class RunContext:
    run_id: str
    task: str
    settings: Settings
    env: Environment
    events: EventLog
    run_dir: Path
    phase: str = "worker"  # "worker" | "verifier"
    memory: dict[str, str] = field(default_factory=dict)
    plan: list[dict] = field(default_factory=list)
    auto_responder: AutoResponder | None = None
    turn: int = 0
    approval_grant: ApprovalGrant | None = None
    approved_signatures: set[str] = field(default_factory=set)
    _browser: "BrowserSession | None" = None
    _pending: dict[str, asyncio.Future] = field(default_factory=dict)
    _shot_seq: int = 0

    # ----------------------------------------------------------- paths
    @property
    def workspace(self) -> Path:
        p = self.run_dir / "workspace"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def shots_dir(self) -> Path:
        p = self.run_dir / "shots"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def next_shot_path(self, label: str = "", ext: str = "jpg") -> Path:
        self._shot_seq += 1
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        return self.shots_dir / f"{self.phase[:1]}{self._shot_seq:03d}{'_' + safe if safe else ''}.{ext}"

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.run_dir))

    # ----------------------------------------------------------- events
    def emit(self, type: str, **data) -> dict:
        return self.events.emit(type, phase=self.phase, **data)

    # ----------------------------------------------------------- browser
    async def browser(self) -> "BrowserSession":
        if self._browser is None:
            from .tools.browser import BrowserSession

            self._browser = BrowserSession(self)
            await self._browser.start()
        return self._browser

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None

    def child(self, phase: str) -> "RunContext":
        """A context for another agent in the same run (e.g. the verifier): shared log, workspace
        and memory snapshot, but its own browser session and no pending interactions."""
        return RunContext(run_id=self.run_id, task=self.task, settings=self.settings, env=self.env,
                          events=self.events, run_dir=self.run_dir, phase=phase, memory=dict(self.memory),
                          plan=list(self.plan), auto_responder=self.auto_responder, _shot_seq=self._shot_seq + 500)

    # ----------------------------------------------------------- human in the loop
    async def interact(self, kind: str, payload: dict) -> dict:
        """Ask the human something (question or approval) and wait for the answer.

        Returns {"timeout": True} if nobody answers within the configured window.
        """
        req_id = uuid.uuid4().hex[:10]
        self.emit(f"{kind}_request", request_id=req_id, **payload)
        if self.auto_responder is not None:
            resp = self.auto_responder(kind, payload)
            if asyncio.iscoroutine(resp):
                resp = await resp
        else:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = fut
            try:
                resp = await asyncio.wait_for(fut, timeout=self.settings.user_response_timeout)
            except asyncio.TimeoutError:
                resp = {"timeout": True}
            finally:
                self._pending.pop(req_id, None)
        self.emit(f"{kind}_resolved", request_id=req_id, response=resp)
        return resp

    def respond(self, request_id: str, response: dict) -> bool:
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(response)
        return True

    @property
    def pending_requests(self) -> list[str]:
        return list(self._pending)
