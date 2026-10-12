// Produces a compact, text-only view of the visible page for the model.
// Interactive elements get stable refs (data-agent-ref="eN") the model uses to act.
(maxChars) => {
  document.querySelectorAll("[data-agent-ref]").forEach((e) => e.removeAttribute("data-agent-ref"));
  let n = 0;
  const lines = [];
  let cur = "";
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const flush = () => { const t = clean(cur); if (t && t !== "|") lines.push(t); cur = ""; };
  const ref = (el) => { const r = "e" + ++n; el.setAttribute("data-agent-ref", r); return r; };
  const q = (s) => JSON.stringify(clean(s).slice(0, 120));

  const visible = (el) => {
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || parseFloat(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 || s.display === "contents";
  };
  const labelFor = (el) => {
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const pl = el.closest("label");
    if (pl) return clean(pl.innerText);
    return el.getAttribute("placeholder") || el.getAttribute("name") || "";
  };

  function describe(el) {
    const tag = el.tagName;
    if (tag === "A") {
      const href = el.getAttribute("href");
      if (!href) return clean(el.innerText);
      return `[${ref(el)}] link ${q(el.innerText)} -> ${href}`;
    }
    if (tag === "BUTTON") return `[${ref(el)}] button ${q(el.innerText)}${el.disabled ? " (disabled)" : ""}`;
    if (tag === "SELECT") {
      const opts = [...el.options].map((o) => (o.selected ? "(selected) " : "") + clean(o.text));
      return `[${ref(el)}] select ${q(labelFor(el))} options: [${opts.join(" | ")}]`;
    }
    if (tag === "TEXTAREA") return `[${ref(el)}] textarea ${q(labelFor(el))} value=${q(el.value)}`;
    if (tag === "INPUT") {
      const type = (el.type || "text").toLowerCase();
      if (type === "hidden") return null;
      if (["submit", "button", "reset", "image"].includes(type)) return `[${ref(el)}] button ${q(el.value)}`;
      if (type === "checkbox" || type === "radio")
        return `[${ref(el)}] ${type} ${q(labelFor(el))} ${el.checked ? "checked" : "unchecked"}`;
      const secret = type === "password" || el.hasAttribute("data-agent-secret");
      const val = secret ? (el.value ? "\"[hidden]\"" : "\"\"") : q(el.value);
      const ph = el.placeholder ? ` placeholder=${q(el.placeholder)}` : "";
      return `[${ref(el)}] textbox ${q(labelFor(el))} type=${type} value=${val}${ph}`;
    }
    return null;
  }

  const INTERACTIVE = new Set(["A", "BUTTON", "INPUT", "SELECT", "TEXTAREA"]);
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "SVG"]);

  function walk(node) {
    if (node.nodeType === Node.TEXT_NODE) { cur += " " + node.textContent; return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node;
    if (SKIP.has(el.tagName)) return;
    if (el.tagName === "LABEL" && el.htmlFor) return; // folded into the control's description
    if (!visible(el)) return;
    if (INTERACTIVE.has(el.tagName)) { const d = describe(el); if (d) cur += " " + d + " "; return; }

    const display = getComputedStyle(el).display;
    const isCell = display === "table-cell";
    const isBlock = !isCell && !display.startsWith("inline") && display !== "contents";
    let prefix = "";
    if (/^H[1-6]$/.test(el.tagName)) prefix = "#".repeat(+el.tagName[1]) + " ";
    const role = el.getAttribute("role");
    if (role === "alert") prefix = "[ALERT] ";
    else if (role === "status") prefix = "[STATUS] ";
    else if (role === "dialog") prefix = `[DIALOG ${q(el.getAttribute("aria-label") || "")}] `;
    if (el.tagName === "DT") prefix = "";

    if (isBlock) flush();
    cur += prefix;
    for (const c of el.childNodes) walk(c);
    if (el.tagName === "DT") cur += ":";
    if (isCell) cur += " |";
    if (isBlock) flush();
  }

  walk(document.body);
  flush();

  // Overlays (fixed, high z-index, covering the viewport) block clicks underneath - call them out.
  const overlays = [...document.querySelectorAll("body *")].filter((el) => {
    const s = getComputedStyle(el);
    if (s.position !== "fixed" || !visible(el)) return false;
    const r = el.getBoundingClientRect();
    return r.width >= innerWidth * 0.8 && r.height >= innerHeight * 0.8;
  });

  let text = lines.join("\n");
  let truncated = 0;
  if (text.length > maxChars) { truncated = text.length - maxChars; text = text.slice(0, maxChars); }
  return {
    text,
    truncated,
    overlay: overlays.length ? clean(overlays[0].innerText).slice(0, 200) : null,
    refs: n,
  };
}
