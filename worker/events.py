"""Append-only event log for a run: persisted to JSONL and fanned out to live subscribers (SSE)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, path: Path | None = None):
        self.events: list[dict] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._path = path
        self._seq = 0

    def emit(self, type: str, phase: str = "", **data: Any) -> dict:
        self._seq += 1
        event = {"seq": self._seq, "ts": time.time(), "type": type, "phase": phase, **data}
        self.events.append(event)
        if self._path:
            with self._path.open("a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        for q in list(self._subscribers):
            q.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events:  # replay history first so late subscribers see everything
            q.put_nowait(e)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
