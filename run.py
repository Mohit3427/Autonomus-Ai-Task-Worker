"""Start everything: the two simulated company apps and the agent UI, in one process.

    python run.py            # UI on http://localhost:8000
"""
from __future__ import annotations

import asyncio
import os

import uvicorn

os.environ.setdefault("SIM_TODAY", "2026-10-12")  # pin the simulated date so scenarios are reproducible

from server.app import create_app as agent_app  # noqa: E402
from simenv.erp import create_app as erp_app  # noqa: E402
from simenv.vendor_portal import create_app as portal_app  # noqa: E402
from worker.config import Settings  # noqa: E402

AGENT_PORT = int(os.environ.get("AGENT_PORT", "8000"))
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "8101"))
ERP_PORT = int(os.environ.get("ERP_PORT", "8102"))


async def main() -> None:
    Settings().runs_dir.mkdir(parents=True, exist_ok=True)
    servers = [
        uvicorn.Server(uvicorn.Config(portal_app(), port=PORTAL_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(erp_app(), port=ERP_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(agent_app(), port=AGENT_PORT, log_level="warning")),
    ]
    print(f"SupplyHub vendor portal  -> http://localhost:{PORTAL_PORT}")
    print(f"Northwind AP (ERP)       -> http://localhost:{ERP_PORT}")
    print(f"Agent UI                 -> http://localhost:{AGENT_PORT}")
    settings = Settings()
    rate = f" (max {settings.llm_rpm:g} requests/min)" if settings.llm_rpm else ""
    print(f"Model                    -> {settings.provider} / {settings.model}{rate}")
    if not settings.api_key:
        print("WARNING: no API key found for this provider - add it to .env (see .env.example)")
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
