"""Agent control server: start runs, stream their events (SSE), answer questions/approvals."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from worker.runner import RunOptions, Runner

STATIC = Path(__file__).parent / "static"


class StartRun(BaseModel):
    task: str
    autonomy: str = "autonomous"
    verify: bool = True
    reset_env: bool = True
    chaos: bool = False


class Respond(BaseModel):
    request_id: str
    answer: str | None = None
    approved: bool | None = None
    comment: str | None = None


def create_app(runner: Runner | None = None) -> FastAPI:
    app = FastAPI(title="Autonomous AI Task Worker")
    runner = runner or Runner()
    app.state.runner = runner

    async def control_sim(chaos: bool, reset: bool) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            for a in runner.env.apps:
                try:
                    await client.post(f"{a['url']}/__control/chaos", json={"enabled": chaos})
                    if reset:
                        await client.post(f"{a['url']}/__control/reset")
                except httpx.HTTPError:
                    pass  # app without a control plane (e.g. a real system) - nothing to do

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    async def config():
        s = runner.settings
        return {
            "organization": runner.env.organization,
            "apps": [{"name": a["name"], "url": a["url"]} for a in runner.env.apps],
            "examples": runner.env.example_tasks,
            "provider": s.provider, "model": s.model, "effort": s.effort, "today": s.today,
        }

    @app.post("/api/runs")
    async def start_run(body: StartRun):
        if not body.task.strip():
            raise HTTPException(400, "Task is empty")
        await control_sim(body.chaos, body.reset_env)
        run = runner.create(body.task.strip(), RunOptions(autonomy=body.autonomy, verify=body.verify))
        if body.chaos:
            run.ctx.emit("harness_note", text="Chaos mode: transient failures will be injected into the apps.")
        runner.start(run)
        return {"run_id": run.id}

    @app.get("/api/runs")
    async def list_runs():
        rows = []
        for d in sorted(runner.settings.runs_dir.glob("*"), reverse=True)[:30]:
            live = runner.runs.get(d.name)
            report_file = d / "report.json"
            if live and live.report is None:
                rows.append({"run_id": d.name, "task": live.task, "status": "running"})
            elif report_file.exists():
                r = json.loads(report_file.read_text())
                rows.append({"run_id": d.name, "task": r["task"], "status": r["status"]})
        return rows

    @app.get("/api/runs/{run_id}/events")
    async def stream(run_id: str):
        run = runner.runs.get(run_id)
        if run is None:
            return StreamingResponse(_replay_file(runner.settings.runs_dir / run_id / "events.jsonl"),
                                     media_type="text/event-stream")

        async def gen():
            q = run.ctx.events.subscribe()
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                    if event["type"] == "run_finished":
                        break
            finally:
                run.ctx.events.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/respond")
    async def respond(run_id: str, body: Respond):
        run = runner.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "Run not active")
        payload = {k: v for k, v in body.model_dump().items() if v is not None and k != "request_id"}
        if not run.respond(body.request_id, payload):
            raise HTTPException(409, "No pending request with that id")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        run = runner.runs.get(run_id)
        if run is None or run.task_handle is None:
            raise HTTPException(404, "Run not active")
        run.task_handle.cancel()
        return {"ok": True}

    @app.post("/api/env/reset")
    async def reset_env():
        await control_sim(chaos=False, reset=True)
        return {"ok": True}

    app.mount("/runs", StaticFiles(directory=str(runner.settings.runs_dir), check_dir=False), name="runs")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


async def _replay_file(path: Path):
    if path.exists():
        for line in path.read_text().splitlines():
            yield f"data: {line}\n\n"
