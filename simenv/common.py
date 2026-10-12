"""Shared plumbing for the simulated apps: sessions, failure injection, control API."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

TEMPLATE_ROOT = Path(__file__).parent / "templates"


def templates_for(app_name: str) -> Jinja2Templates:
    return Jinja2Templates(directory=str(TEMPLATE_ROOT / app_name))


class Sessions:
    """Tiny in-memory cookie session store."""

    def __init__(self, cookie_name: str):
        self.cookie_name = cookie_name
        self._store: dict[str, str] = {}

    def create(self, user: str) -> str:
        token = secrets.token_urlsafe(16)
        self._store[token] = user
        return token

    def user(self, request: Request) -> str | None:
        return self._store.get(request.cookies.get(self.cookie_name, ""))

    def destroy(self, request: Request) -> None:
        self._store.pop(request.cookies.get(self.cookie_name, ""), None)

    def clear(self) -> None:
        self._store.clear()


class Chaos:
    """One-shot failure injection.

    When enabled, each named fault fires exactly once (the first time its code path
    is hit) and then disarms itself - mimicking transient real-world failures the
    agent should detect and recover from.
    """

    def __init__(self, faults: list[str]):
        self.all_faults = list(faults)
        self.enabled = False
        self._armed: set[str] = set()

    def configure(self, enabled: bool) -> None:
        self.enabled = enabled
        self._armed = set(self.all_faults) if enabled else set()

    def fire(self, fault: str) -> bool:
        if fault in self._armed:
            self._armed.discard(fault)
            return True
        return False

    def status(self) -> dict:
        return {"enabled": self.enabled, "armed": sorted(self._armed)}
