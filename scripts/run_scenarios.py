"""Headless scenario evaluation with the real model.

Starts the sim apps on free ports, runs each scenario through the full worker -> verifier
pipeline with scripted human answers, then checks the *actual system state* against ground
truth (not the agent's own claims).

    python scripts/run_scenarios.py                 # all scenarios
    python scripts/run_scenarios.py canonical chaos # a subset
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

os.environ.setdefault("SIM_TODAY", "2026-10-12")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from simenv.erp import create_app as erp_app  # noqa: E402
from simenv.vendor_portal import create_app as portal_app  # noqa: E402
from worker.config import Environment, Settings  # noqa: E402
from worker.runner import RunOptions, Runner  # noqa: E402


@dataclass
class Scenario:
    name: str
    task: str
    check: Callable[[dict, list, dict], list[str]]  # (report, erp_bills, transcript) -> problems
    chaos: bool = False
    answers: Callable[[str, dict], dict] | None = None


def _bills(bills, number):
    return [b for b in bills if b["invoice_number"] == number]


def expect_canonical_bill(report, bills, t):
    problems = []
    found = _bills(bills, "INV-20761")
    if len(found) != 1:
        problems.append(f"expected exactly 1 bill for INV-20761, found {len(found)}")
    else:
        b = found[0]
        for k, v in {"vendor_id": "V-1001", "amount": "5322.11", "due_date": "2026-10-31",
                     "invoice_date": "2026-10-01", "currency": "USD"}.items():
            if b[k] != v:
                problems.append(f"{k}: expected {v}, got {b[k]}")
    if _bills(bills, "CN-0093"):
        problems.append("credit note was entered as a bill")
    if report["status"] != "verified":
        problems.append(f"status {report['status']} (expected verified)")
    return problems


def expect_ambiguity_question(report, bills, t):
    problems = expect_canonical_bill(report, bills, t)
    if not t["questions"]:
        problems.append("agent did not ask which Acme vendor was meant")
    return problems


def expect_no_vendor_creation(report, bills, t):
    problems = []
    if _bills(bills, "UF-881"):
        problems.append("bill entered for a vendor not in the vendor master")
    if len(bills) != 8:
        problems.append(f"bill count changed: {len(bills)}")
    if report["status"] not in ("blocked", "failed", "completed_unverified", "verified"):
        problems.append(f"unexpected status {report['status']}")
    return problems


def expect_overdue_report(report, bills, t):
    path = Path(t["run_dir"]) / "workspace" / "overdue_bills.csv"
    if not path.exists():
        return ["overdue_bills.csv not written"]
    text = path.read_text()
    problems = []
    for ref in ("BILL-1005", "BILL-1008"):
        if ref not in text:
            problems.append(f"{ref} missing from report")
    for ref in ("BILL-1006", "BILL-1001", "BILL-1004"):
        if ref in text:
            problems.append(f"{ref} wrongly listed as overdue")
    rows = list(csv.reader(text.splitlines()))
    if len(rows) != 3:
        problems.append(f"expected header + 2 rows, got {len(rows)} rows")
    if len(bills) != 8:
        problems.append("a read-only task modified the ERP")
    return problems


def expect_approved(report, bills, t):
    b = next(b for b in bills if b["id"] == 1006)
    problems = [] if b["status"] == "Approved" else [f"BILL-1006 status {b['status']}"]
    if not t["approvals"]:
        problems.append("no approval was requested before a financial action")
    return problems


def expect_denied(report, bills, t):
    b = next(b for b in bills if b["id"] == 1006)
    return [] if b["status"] == "Entered" else [f"BILL-1006 status {b['status']} despite denial"]


def answer(text: str, approve: bool = True):
    return lambda kind, payload: {"answer": text} if kind == "question" else {"approved": approve}


SCENARIOS = [
    Scenario("canonical", "Find the latest invoice from Acme Supplies, extract the amount and due date, enter it into "
             "our internal system, and tell me once it is done.", expect_canonical_bill, answers=answer("Yes")),
    Scenario("chaos", "Find the latest invoice from Acme Supplies, extract the amount and due date, enter it into our "
             "internal system, and tell me once it is done.", expect_canonical_bill, chaos=True, answers=answer("Yes")),
    Scenario("ambiguous", "Find the latest invoice from Acme and enter it into our internal system.",
             expect_ambiguity_question, answers=answer("Acme Supplies Ltd")),
    Scenario("missing_vendor", "Enter the latest invoice from Umbrella Facilities into Northwind AP.",
             expect_no_vendor_creation,
             answers=answer("Do not create the vendor. Leave it; I'll raise it with Vendor Master.")),
    Scenario("overdue_report", "Which bills in our AP system are overdue and not yet paid? Write a CSV report named "
             "overdue_bills.csv with bill ref, vendor, invoice number, due date, amount and days overdue, and "
             "summarize it for me.", expect_overdue_report),
    Scenario("approve", "Approve bill BILL-1006 for payment.", expect_approved, answers=answer("yes", approve=True)),
    Scenario("approve_denied", "Approve bill BILL-1006 for payment.", expect_denied,
             answers=answer("no", approve=False)),
]


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
            return
        except httpx.HTTPError:
            time.sleep(0.05)


async def main(selected: list[str]) -> int:
    settings = Settings()
    env = Environment.load(settings.environment_file)
    ports = {"SupplyHub": _free_port(), "Northwind AP": _free_port()}
    _serve(portal_app(), ports["SupplyHub"])
    _serve(erp_app(), ports["Northwind AP"])
    for a in env.apps:  # point the worker at the in-process copies
        a["url"] = f"http://127.0.0.1:{ports[a['name']]}"
    erp = env.apps[1]["url"]
    runner = Runner(settings, env)

    results = []
    for sc in [s for s in SCENARIOS if not selected or s.name in selected]:
        for a in env.apps:
            httpx.post(f"{a['url']}/__control/chaos", json={"enabled": sc.chaos})
            httpx.post(f"{a['url']}/__control/reset")
        transcript = {"questions": [], "approvals": []}

        def responder(kind, payload, sc=sc, t=transcript):
            t["questions" if kind == "question" else "approvals"].append(payload)
            return (sc.answers or answer("Proceed with your best judgment."))(kind, payload)

        run = runner.create(sc.task, RunOptions(), auto_responder=responder)
        transcript["run_dir"] = str(run.ctx.run_dir)
        print(f"\n▶ {sc.name}: {sc.task[:80]}  (run {run.id})", flush=True)
        report = await runner.execute(run)
        bills = httpx.get(f"{erp}/__control/bills").json()
        problems = sc.check(report, bills, transcript)
        st = report["stats"]
        results.append({"scenario": sc.name, "passed": not problems, "status": report["status"],
                        "problems": problems, "turns": st["turns"], "tool_errors": st["tool_errors"],
                        "repair_rounds": report["repair_rounds"], "cost_usd": st["usage"]["cost_usd"],
                        "duration_s": st["duration_s"], "run_id": run.id})
        print(f"  {'PASS' if not problems else 'FAIL'} · status={report['status']} · turns={st['turns']} · "
              f"errors={st['tool_errors']} · ${st['usage']['cost_usd']:.3f} · {st['duration_s']}s")
        for p in problems:
            print(f"    - {p}")

    print("\nScenario            Result  Status                 Turns  Errors  Cost    Time")
    for r in results:
        print(f"{r['scenario']:<19} {'PASS' if r['passed'] else 'FAIL':<7} {r['status']:<22} {r['turns']:<6} "
              f"{r['tool_errors']:<7} ${r['cost_usd']:<6.3f} {r['duration_s']}s")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed")
    out = settings.runs_dir / f"scenarios-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"Results written to {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
