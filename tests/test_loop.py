"""End-to-end runs of the orchestrator with a scripted model against the live sim apps."""
import httpx
import pytest

from tests.fakes import FakeLLM, call, last_tool_results, text
from worker.runner import RunOptions, Runner

AUTH = {"Authorization": "Bearer {{secret:erp.api_token}}"}
BILL = {"vendor_id": "V-1003", "invoice_number": "GLX-7901", "invoice_date": "2026-10-09",
        "due_date": "2026-10-24", "amount": "2160.00", "currency": "USD"}


def runner_with(settings, env, *llms):
    queue = list(llms)
    return Runner(settings, env, llm_factory=lambda on_retry: queue.pop(0))


def erp_bills(env):
    return httpx.get(f"{env.apps[1]['url']}/__control/bills").json()


@pytest.mark.asyncio
async def test_happy_path_is_verified(env, settings):
    erp = env.apps[1]["url"]
    worker = FakeLLM([
        [call("update_plan", steps=[{"step": "Create bill", "status": "in_progress"}])],
        [call("http_request", method="POST", url=f"{erp}/api/bills", headers=AUTH, json_body=BILL)],
        [call("remember", key="bill", value="created")],
        [call("finish", status="completed", summary="Entered GLX-7901", result={"invoice": "GLX-7901"})],
    ])
    verifier = FakeLLM([
        [call("http_request", method="GET", url=f"{erp}/api/bills?invoice_number=GLX-7901", headers=AUTH)],
        [call("submit_verdict", verdict="pass", checks=[{"check": "bill exists", "ok": True}])],
    ])
    r = runner_with(settings, env, worker, verifier)
    report = await r.execute(r.create("enter GLX-7901"))
    assert report["status"] == "verified"
    assert report["memory"] == {"bill": "created"}
    assert sum(b["invoice_number"] == "GLX-7901" for b in erp_bills(env)) == 1
    # secrets never reach the model: the tool call carried a placeholder, the result has no token
    assert "nw-api-7f3a9c" not in str(worker.seen)


@pytest.mark.asyncio
async def test_policy_gate_denial_prevents_financial_action(env, settings):
    erp = env.apps[1]["url"]

    def after_denial(messages):
        res = last_tool_results(messages)[0]
        assert res.get("is_error") and "NOT executed" in res["content"]
        return [call("finish", status="blocked", summary="Approval was denied.")]

    worker = FakeLLM([
        [call("http_request", method="POST", url=f"{erp}/api/bills/1006/approve", headers=AUTH)],
        after_denial,
    ])
    asked = []
    r = runner_with(settings, env, worker)
    run = r.create("approve BILL-1006", auto_responder=lambda kind, p: asked.append(p) or {"approved": False})
    report = await r.execute(run)
    assert report["status"] == "blocked" and asked and asked[0]["kind"] == "financial"
    assert next(b for b in erp_bills(env) if b["id"] == 1006)["status"] == "Entered"


@pytest.mark.asyncio
async def test_stuck_detection_and_turn_budget(env, settings):
    settings.max_turns = 6
    bad = call("http_request", method="GET", url="https://elsewhere.example/api")
    worker = FakeLLM([[bad], [bad], [bad], [text("hmm")], [text("still thinking")], [text("...")]])
    r = runner_with(settings, env, worker)
    run = r.create("do something")
    report = await r.execute(run)
    notes = [e["text"] for e in run.ctx.events.events if e["type"] == "harness_note"]
    assert any("failed 3 times" in n for n in notes)
    assert any("without calling a tool" in n for n in notes)
    assert report["status"] == "failed"


@pytest.mark.asyncio
async def test_verifier_failure_triggers_repair_round(env, settings):
    erp = env.apps[1]["url"]

    def saw_feedback(messages):
        assert "[verifier]" in str(messages[-1]["content"])
        return [call("http_request", method="POST", url=f"{erp}/api/bills", headers=AUTH, json_body=BILL)]

    worker = FakeLLM([
        [call("finish", status="completed", summary="done (but nothing was entered)")],
        saw_feedback,
        [call("finish", status="completed", summary="Entered GLX-7901 after fix")],
    ])
    verifier = FakeLLM([
        [call("submit_verdict", verdict="fail", checks=[{"check": "bill exists", "expected": "1", "observed": "0",
                                                         "ok": False}], feedback="No bill found; enter it.")],
        # verifier attempts a write -> must be blocked by policy, then passes
        [call("http_request", method="POST", url=f"{erp}/api/bills", headers=AUTH, json_body=BILL)],
        lambda m: [call("submit_verdict", verdict="pass", checks=[{"check": "bill exists", "ok": True}])]
        if "BLOCKED" in last_tool_results(m)[0]["content"] or "read-only" in last_tool_results(m)[0]["content"]
        else [text("verifier was able to write!")],
    ])
    r = runner_with(settings, env, worker, verifier)
    report = await r.execute(r.create("enter GLX-7901", RunOptions()))
    assert report["status"] == "verified" and report["repair_rounds"] == 1
    assert sum(b["invoice_number"] == "GLX-7901" for b in erp_bills(env)) == 1


@pytest.mark.asyncio
async def test_question_timeout_returns_blocked_guidance(env, settings):
    def after_q(messages):
        assert "did not respond" in last_tool_results(messages)[0]["content"]
        return [call("finish", status="blocked", summary="Need to know which Acme.")]

    worker = FakeLLM([[call("ask_user", question="Which Acme?", options=["Acme Supplies Ltd", "Acme Corporation"])],
                      after_q])
    r = runner_with(settings, env, worker)
    report = await r.execute(r.create("latest Acme invoice", auto_responder=lambda k, p: {"timeout": True}))
    assert report["status"] == "blocked"
