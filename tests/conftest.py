"""Shared fixtures: live sim apps on test ports (real HTTP, so browser + http tools work)."""
from __future__ import annotations

import os
import socket
import threading
import time

os.environ.setdefault("SIM_TODAY", "2026-10-12")

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

from simenv.erp import create_app as erp_app  # noqa: E402
from simenv.vendor_portal import create_app as portal_app  # noqa: E402
from worker.config import Environment, Settings  # noqa: E402

SECRETS = {
    "supplyhub.email": "ap@northwind.example", "supplyhub.password": "portal-pass-2026",
    "erp.username": "jdoe", "erp.password": "erp-pass-2026", "erp.api_token": "nw-api-7f3a9c",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/__control/status", timeout=0.5)
            return server
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError("sim app did not start")


@pytest.fixture(scope="session")
def sim():
    portal_port, erp_port = _free_port(), _free_port()
    servers = [_serve(portal_app(), portal_port), _serve(erp_app(), erp_port)]
    yield {"portal": f"http://127.0.0.1:{portal_port}", "erp": f"http://127.0.0.1:{erp_port}"}
    for s in servers:
        s.should_exit = True


@pytest.fixture
def env(sim):
    for url in sim.values():
        httpx.post(f"{url}/__control/chaos", json={"enabled": False})
        httpx.post(f"{url}/__control/reset")
    return Environment(
        organization="Northwind Traders Inc.", operator="AP",
        apps=[{"name": "SupplyHub", "url": sim["portal"], "description": "portal", "auth": "form"},
              {"name": "Northwind AP", "url": sim["erp"], "description": "erp", "auth": "form"}],
        secrets=dict(SECRETS),
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(runs_dir=tmp_path / "runs", max_turns=15, verifier_max_turns=8, user_response_timeout=2)
