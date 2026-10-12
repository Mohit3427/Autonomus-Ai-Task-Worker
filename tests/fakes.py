"""A scripted stand-in for the Claude client, so the loop can be tested deterministically."""
from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Callable

from worker.llm import Usage

_ids = itertools.count(1)


def call(name: str, **inp) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=inp)


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


class FakeLLM:
    """Each step is a list of blocks, or a callable(messages) -> list of blocks (to react to tool results)."""

    def __init__(self, steps: list, model: str = "claude-opus-5-5"):
        self.steps = list(steps)
        self.model = model
        self.usage = Usage()
        self.seen: list[list] = []

    async def complete(self, *, system, tools, messages, effort):
        self.seen.append(list(messages))
        step = self.steps.pop(0) if self.steps else [text("(script exhausted)")]
        blocks = step(messages) if isinstance(step, Callable) else step
        self.usage.add(SimpleNamespace(input_tokens=100, output_tokens=20))
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop, usage=None)


def last_tool_results(messages) -> list[dict]:
    content = messages[-1]["content"]
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
