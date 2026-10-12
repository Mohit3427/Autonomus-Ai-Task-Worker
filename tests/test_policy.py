"""The approval/deny policy is enforced in code, independent of what the model decides."""
from worker.context import ApprovalGrant, RunContext
from worker.events import EventLog
from worker.policy import Policy
from worker.tools.base import Assessment


def make_ctx(env, settings):
    return RunContext("r", "t", settings, env, EventLog(), settings.runs_dir)


def test_financial_and_destructive_need_approval(env, settings):
    ctx, p = make_ctx(env, settings), Policy("worker", "autonomous")
    erp = env.apps[1]["url"]
    assert p.decide(ctx, "browser_click", Assessment("submit", 'button "Save bill"', f"{erp}/bills/new")).action == "allow"
    assert p.decide(ctx, "browser_click", Assessment("submit", 'button "Approve for payment"', f"{erp}/bills/9/approve")).action == "ask"
    assert p.decide(ctx, "browser_click", Assessment("submit", 'button "Delete bill"', f"{erp}/bills/9/delete")).action == "ask"
    assert p.decide(ctx, "http_request", Assessment("destructive", "DELETE x", f"{erp}/api/bills/1")).action == "ask"


def test_supervised_gates_every_submit(env, settings):
    ctx, p = make_ctx(env, settings), Policy("worker", "supervised")
    erp = env.apps[1]["url"]
    assert p.decide(ctx, "browser_click", Assessment("submit", 'button "Save bill"', f"{erp}/bills/new")).action == "ask"
    assert p.decide(ctx, "browser_click", Assessment("auth", 'button "Log in"', f"{erp}/login")).action == "allow"


def test_outside_hosts_denied(env, settings):
    ctx = make_ctx(env, settings)
    d = Policy("worker").decide(ctx, "browser_navigate", Assessment("read", "x", "https://evil.example.com/"))
    assert d.action == "deny"


def test_verifier_is_read_only(env, settings):
    ctx, p = make_ctx(env, settings), Policy("verifier")
    erp = env.apps[1]["url"]
    assert p.decide(ctx, "browser_click", Assessment("auth", "Log in", f"{erp}/login")).action == "allow"
    assert p.decide(ctx, "browser_click", Assessment("submit", "Save bill", f"{erp}/bills/new")).action == "deny"
    assert p.decide(ctx, "http_request", Assessment("external_write", "POST", f"{erp}/api/bills")).action == "deny"


def test_agent_approval_grant_covers_one_action(env, settings):
    ctx, p = make_ctx(env, settings), Policy("worker")
    erp = env.apps[1]["url"]
    ctx.turn, ctx.approval_grant = 3, ApprovalGrant("approve BILL-1006", expires_turn=6)
    a = Assessment("submit", 'button "Approve for payment"', f"{erp}/bills/1006/approve")
    assert p.decide(ctx, "browser_click", a).action == "allow"
    b = Assessment("submit", 'button "Delete bill"', f"{erp}/bills/1006/delete")
    assert p.decide(ctx, "browser_click", b).action == "ask"  # grant was consumed
