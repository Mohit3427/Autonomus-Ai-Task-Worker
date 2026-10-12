// Live trace UI for the AI task worker. No framework - one EventSource per viewed run.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STATUS_LABEL = {
  running: "running", verified: "done · verified", completed_unverified: "done · unverified",
  verification_failed: "verification failed", blocked: "blocked", failed: "failed", cancelled: "cancelled",
};

let current = null; // { id, es, calls: Map, pending: Map }

async function init() {
  const cfg = await (await fetch("/api/config")).json();
  $("meta").innerHTML = `${esc(cfg.organization)} · model ${esc(cfg.model)} (${esc(cfg.effort)}) · today ${esc(cfg.today)}` +
    cfg.apps.map((a) => ` <a href="${esc(a.url)}" target="_blank">${esc(a.name)}</a>`).join("");
  for (const ex of cfg.examples) {
    const b = document.createElement("button");
    b.className = "chip"; b.textContent = ex.label; b.title = ex.task;
    b.onclick = () => { $("task").value = ex.task; };
    $("examples").appendChild(b);
  }
  $("runBtn").onclick = startRun;
  $("cancelBtn").onclick = () => current && fetch(`/api/runs/${current.id}/cancel`, { method: "POST" });
  $("lightbox").onclick = () => ($("lightbox").hidden = true);
  $("task").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) startRun(); });
  refreshHistory();
  const fromHash = location.hash.slice(1);
  if (fromHash) openRun(fromHash);
}

async function startRun() {
  const task = $("task").value.trim();
  if (!task) return;
  $("runBtn").disabled = true;
  try {
    const r = await fetch("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, autonomy: $("autonomy").value, verify: $("verify").checked,
        reset_env: $("reset").checked, chaos: $("chaos").checked }),
    });
    const { run_id } = await r.json();
    openRun(run_id);
    refreshHistory();
  } finally { $("runBtn").disabled = false; }
}

async function refreshHistory() {
  const rows = await (await fetch("/api/runs")).json();
  $("history").innerHTML = rows.map((r) => `<li data-id="${esc(r.run_id)}" class="${current && current.id === r.run_id ? "active" : ""}">
      <span class="t">${esc(r.task)}</span><span class="pill ${esc(r.status)}" style="justify-self:start">${esc(STATUS_LABEL[r.status] || r.status)}</span></li>`).join("")
    || `<span class="muted">No runs yet</span>`;
  for (const li of $("history").querySelectorAll("li")) li.onclick = () => openRun(li.dataset.id);
}

function openRun(id) {
  if (current?.es) current.es.close();
  current = { id, es: null, calls: new Map(), pending: new Map(), usage: {}, started: null };
  location.hash = id;
  $("empty").hidden = true; $("runView").hidden = false;
  $("timeline").innerHTML = ""; $("plan").innerHTML = ""; $("planBox").hidden = true; $("pending").innerHTML = "";
  $("memory").innerHTML = `<span class="muted">Nothing remembered yet</span>`;
  $("screen").innerHTML = `<span class="muted">No browser activity yet</span>`; $("shotUrl").textContent = "";
  $("reportBox").hidden = true; $("report").innerHTML = "";
  setStatus("running"); $("cancelBtn").hidden = false;
  const es = new EventSource(`/api/runs/${id}/events`);
  current.es = es;
  es.onmessage = (m) => handle(JSON.parse(m.data));
  es.onerror = () => { /* stream ends after run_finished; browser will retry otherwise */ };
  refreshHistory();
}

function setStatus(status) {
  const pill = $("statusPill");
  pill.className = `pill ${status}`; pill.textContent = STATUS_LABEL[status] || status;
}

function add(html, cls = "ev") {
  const div = document.createElement("div");
  div.className = cls; div.innerHTML = html;
  $("timeline").appendChild(div);
  const center = document.querySelector(".center");
  if (center.scrollHeight - center.scrollTop - center.clientHeight < 400) center.scrollTop = center.scrollHeight;
  return div;
}

const shotUrl = (rel) => `/runs/${current.id}/${rel}`;
function zoom(src) { $("lightboxImg").src = src; $("lightbox").hidden = false; }
window.zoom = zoom;

function showScreen(rel, url) {
  $("screen").innerHTML = `<img src="${shotUrl(rel)}" onclick="zoom(this.src)" alt="browser screenshot">`;
  $("shotUrl").textContent = url ? `· ${url.replace(/^https?:\/\//, "")}` : "";
}

function argSummary(tool, input) {
  if (tool === "browser_navigate") return input.url;
  if (tool === "browser_type") return `${input.ref} ← "${input.text}"${input.press_enter ? " ⏎" : ""}`;
  if (tool === "browser_type_secret") return `${input.ref} ← [secret ${input.secret}]`;
  if (tool === "browser_select") return `${input.ref} = "${input.option}"`;
  if (tool === "http_request") return `${input.method} ${input.url}`;
  if (tool === "remember") return `${input.key} = ${input.value}`;
  if (tool === "update_plan") return `${(input.steps || []).length} steps`;
  return Object.entries(input).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ");
}

function handle(e) {
  const phaseTag = e.phase === "verifier" ? `<span class="pill" style="font-size:10px">verifier</span> ` : "";
  switch (e.type) {
    case "run_started":
      current.started = e.ts;
      $("runTask").textContent = e.task;
      break;
    case "phase":
      add(esc(e.label), "phase");
      break;
    case "thinking":
      add(`<details><summary>${phaseTag}Reasoning</summary><div style="white-space:pre-wrap;margin-top:6px">${esc(e.text)}</div></details>`, "ev thinking");
      break;
    case "message":
      add(`${phaseTag}<div style="white-space:pre-wrap">${esc(e.text)}</div>`, "ev message");
      break;
    case "tool_call": {
      const el = add(`<div class="head"><span class="icon"><span class="spin">◌</span></span>${phaseTag}<span class="name">${esc(e.tool)}</span>
        <span class="args">${esc(argSummary(e.tool, e.input))}</span></div><div class="body"></div>`, "ev tool");
      el.querySelector(".head").onclick = () => el.classList.toggle("open");
      current.calls.set(e.call_id, el);
      break;
    }
    case "tool_result": {
      const el = current.calls.get(e.call_id);
      if (!el) break;
      el.classList.add(e.ok ? "ok" : "err");
      el.querySelector(".icon").textContent = e.ok ? "✓" : "✗";
      const body = el.querySelector(".body");
      const shot = e.data?.screenshot;
      body.innerHTML = `<details ${e.ok ? "" : "open"}><summary class="muted small">output</summary><pre>${esc(e.content)}</pre></details>` +
        (shot ? `<img class="thumb" src="${shotUrl(shot)}" onclick="zoom(this.src)" alt="">` : "");
      if (shot) showScreen(shot, e.data.url);
      break;
    }
    case "plan_updated":
      $("planBox").hidden = false;
      $("plan").innerHTML = e.plan.map((s) => `<li class="${esc(s.status)}">${esc(s.step)}</li>`).join("");
      break;
    case "memory_updated":
      $("memory").innerHTML = Object.entries(e.memory).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
      break;
    case "harness_note":
      add(`${phaseTag}${esc(e.text)}`, "ev note");
      break;
    case "policy":
      add(`${phaseTag}<b>Policy blocked</b> ${esc(e.tool)} (${esc(e.target)}): ${esc(e.reason)}`, "ev deny");
      break;
    case "llm_retry":
      add(`Model API hiccup: ${esc(e.message)}`, "ev note");
      break;
    case "error":
      add(`<b>Error:</b> ${esc(e.message)}`, "ev error");
      break;
    case "approval_request": renderApproval(e); break;
    case "question_request": renderQuestion(e); break;
    case "approval_resolved":
    case "question_resolved": resolvePending(e); break;
    case "verification": renderVerification(e); break;
    case "usage": current.usage[e.phase] = e.usage; updateStats(e.ts); break;
    case "run_finished": finish(e.report); break;
  }
}

function updateStats(ts) {
  const parts = Object.values(current.usage); if (!parts.length) return;
  const u = parts.reduce((a, b) => Object.fromEntries(Object.keys(b).map((k) => [k, (a[k] || 0) + b[k]])), {});
  const secs = current.started ? Math.round(ts - current.started) : 0;
  $("stats").textContent = `${u.calls} model calls · ${(u.input_tokens + u.cache_read_tokens + u.cache_write_tokens).toLocaleString()} in / ${u.output_tokens.toLocaleString()} out tokens · ~$${u.cost_usd.toFixed(3)} · ${secs}s`;
}

function renderApproval(e) {
  setStatus("waiting"); $("statusPill").textContent = "waiting for approval";
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `<div class="q">Approval needed${e.source === "policy" ? " (policy gate)" : ""}</div>
    <div><b>Action:</b> ${esc(e.action)}</div><div><b>Why:</b> ${esc(e.reason)}</div>
    <input type="text" placeholder="Optional comment">
    <div class="actions"><button class="ok">Approve</button><button class="bad">Deny</button></div>`;
  const send = (approved) => fetch(`/api/runs/${current.id}/respond`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: e.request_id, approved, comment: card.querySelector("input").value || null }),
  });
  card.querySelector(".ok").onclick = () => send(true);
  card.querySelector(".bad").onclick = () => send(false);
  $("pending").appendChild(card);
  current.pending.set(e.request_id, card);
  add(`Asked for approval: ${esc(e.action)}`, "ev note");
}

function renderQuestion(e) {
  setStatus("waiting"); $("statusPill").textContent = "waiting for your answer";
  const card = document.createElement("div");
  card.className = "card question";
  card.innerHTML = `<div class="q">The worker needs your input</div><div style="white-space:pre-wrap">${esc(e.question)}</div>
    <div class="actions">${(e.options || []).map((o) => `<button class="opt">${esc(o)}</button>`).join("")}</div>
    <input type="text" placeholder="Type an answer and press Enter">`;
  const send = (answer) => answer && fetch(`/api/runs/${current.id}/respond`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: e.request_id, answer }),
  });
  card.querySelectorAll(".opt").forEach((b) => (b.onclick = () => send(b.textContent)));
  card.querySelector("input").addEventListener("keydown", (k) => { if (k.key === "Enter") send(k.target.value.trim()); });
  $("pending").appendChild(card);
  current.pending.set(e.request_id, card);
  add(`Question to user: ${esc(e.question)}`, "ev message");
}

function resolvePending(e) {
  const card = current.pending.get(e.request_id);
  if (card) { card.remove(); current.pending.delete(e.request_id); }
  const r = e.response || {};
  let text;
  if (r.timeout) text = "No response (timed out)";
  else if (e.type === "approval_resolved") text = (r.approved ? "Approved" : "Denied") + (r.comment ? `: ${r.comment}` : "");
  else text = `Answer: ${r.answer}`;
  add(esc(text), "ev message");
  if (!current.pending.size) setStatus("running");
}

function checksTable(checks) {
  if (!checks?.length) return "";
  return `<table class="checks"><tr><th></th><th>Check</th><th>Expected</th><th>Observed</th></tr>` +
    checks.map((c) => `<tr><td>${c.ok ? "✓" : "✗"}</td><td>${esc(c.check)}</td><td>${esc(c.expected ?? "")}</td><td>${esc(c.observed ?? "")}</td></tr>`).join("") + `</table>`;
}

function renderVerification(e) {
  add(`<div><b>Verifier verdict:</b> <span class="verdict ${esc(e.verdict)}">${esc(e.verdict.toUpperCase())}</span></div>
    ${checksTable(e.checks)}${e.feedback ? `<div style="margin-top:6px"><b>Feedback:</b> ${esc(e.feedback)}</div>` : ""}`, "ev");
}

function finish(r) {
  setStatus(r.status); $("cancelBtn").hidden = true; $("pending").innerHTML = "";
  const v = r.verification;
  const shots = (r.evidence_files?.screenshots || []).map((s) => `<img src="${shotUrl(s)}" title="${esc(s)}" onclick="zoom(this.src)">`).join("");
  const files = (r.evidence_files?.files || []).map((f) => `<a href="${shotUrl(f)}" target="_blank">${esc(f.replace("workspace/", ""))}</a>`).join(" · ");
  const st = r.stats || {}, u = st.usage || {};
  $("report").innerHTML = `<div class="report">
    <span class="pill ${esc(r.status)}">${esc(STATUS_LABEL[r.status] || r.status)}</span>
    <div class="summary">${esc(r.summary)}</div>
    ${Object.keys(r.result || {}).length ? `<pre>${esc(JSON.stringify(r.result, null, 2))}</pre>` : ""}
    ${v ? `<div style="margin-top:8px"><b>Verification:</b> <span class="verdict ${esc(v.verdict)}">${esc(v.verdict)}</span>${checksTable(v.checks)}</div>` : ""}
    ${shots ? `<div class="evid">${shots}</div>` : ""}
    ${files ? `<div style="margin-top:6px">Files: ${files}</div>` : ""}
    <div class="kv"><span>Turns</span><span>${st.turns}</span><span>Tool calls</span><span>${st.tool_calls} (${st.tool_errors} errors, ${st.recovered_errors} recovered)</span>
      <span>Repair rounds</span><span>${r.repair_rounds}</span><span>Duration</span><span>${st.duration_s}s</span>
      <span>Cost</span><span>~$${(u.cost_usd ?? 0).toFixed(3)}</span></div>
  </div>`;
  $("reportBox").hidden = false;
  if (current.es) current.es.close();
  refreshHistory();
}

init();
