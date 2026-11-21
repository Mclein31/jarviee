/* agents.js: every agent session and run, filterable, with a detail panel (overview, prompt, log, trace). */
"use strict";
(() => {
const {esc} = J;
let el = null, selId = null, filter = "all", tool = "", text = "", tab = J.saved.get("jarviee.atab") || "overview";
let trace = null, traceId = null, traceAt = 0, logText = "", logId = null, logAt = 0;
const q = s => el.querySelector(s);
const PAGE = 40;
let limit = PAGE;
const FILTERS = [["all", "All"], ["need", "Needs you"], ["run", "Running"], ["done", "Finished"], ["fail", "Failed"]];

function list() {
  const all = J.S?.agents || [];
  const order = {need: 0, run: 1, stale: 2, done: 3};
  return all.filter(a => {
    const g = J.groupOf(a);
    if (filter === "need" && g !== "need") return false;
    if (filter === "run" && g !== "run") return false;
    if (filter === "done" && g !== "done") return false;
    if (filter === "fail" && a.status !== "failed") return false;
    if (tool && a.tool !== tool) return false;
    if (text) { const hay = `${J.titleOf(a)} ${a.task || ""} ${J.place(a)} ${a.model || ""}`.toLowerCase(); if (!hay.includes(text)) return false; }
    return true;
  }).sort((x, y) => order[J.groupOf(x)] - order[J.groupOf(y)] || (J.endOf(y) || y.updated || 0) - (J.endOf(x) || x.updated || 0));
}
function row(a) {
  const k = J.statusKey(a), g = J.groupOf(a), end = g === "done" ? J.endOf(a) : 0;
  /* a finished row's status already says it all ("finished", "session ended"); only live, waiting and failed rows say more */
  const sub = g === "need" ? `<span class="need">${esc(J.waitReason(a.activity))}</span>` : a.status === "failed" ? `<span class="err">${esc(a.activity || "failed")}</span>`
    : g === "done" ? "" : esc(a.activity || a.summary || "");
  return `${J.toolMono(a.tool)}
    <span class="ar-main"><span class="ar-t">${esc(J.titleOf(a))}</span><span class="ar-s">${esc(J.place(a) || "no project")} · ${esc(J.modelLabel(a.model) || a.tool || "—")}</span></span>
    <span class="ar-st">${J.st(k)}</span>
    <span class="ar-now">${sub}</span>
    <span class="ar-tier">${J.agentPill(a)}</span>
    <span class="ar-el num clock" data-start="${a.started || ""}" data-end="${end || ""}">${a.started ? J.dur((end || J.now()) - a.started) : "—"}</span>`;
}

/* ---------- detail ---------- */
function stackHtml(a) {
  const eq = (J.S?.fallback?.equivalents || {})[`${a.tool}:${a.model}`] || [];
  const other = Object.keys(J.S?.tool_status || {}).find(t => t !== a.tool);
  const oth = other && J.S?.tools?.[other]?.models?.[a.tier];
  const fbList = eq.length ? eq : J.S?.fallback?.use_available_tool !== false && oth ? [`${other}:${oth}`] : [];
  return `<div class="stack">
    <div class="stack-row"><span>Primary</span>${J.toolMono(a.tool)}<b class="num">${esc(a.model || "unknown")}</b></div>
    ${a.fallback_from ? `<div class="stack-row"><span>Came from</span>${J.sd("fb")}<b class="num">${esc(a.fallback_from)}</b></div>` : ""}
    ${fbList.map(f => `<div class="stack-row"><span>Fallback</span>${J.toolMono(f.split(":")[0])}<b class="num">${esc(f)}</b></div>`).join("")}
  </div>`;
}
function streamHtml() {
  if (!trace || traceId !== selId) return `<div class="empty">Loading…</div>`;
  /* the last 40 steps; the list takes the pane's free height and scrolls inside it (layout.css), Details stays below */
  const rows = trace.rows.filter(r => r.kind === "tool" || r.kind === "event" || r.depth === 0).slice(-40).reverse();
  if (!rows.length) return `<div class="empty">No tool calls recorded for this agent yet.</div>`;
  return `<div class="stream">${rows.map(r => `<div class="sr"><span class="num faint">${J.clockS(trace.t0 + r.start)}</span>${J.sd(r.status === "running" ? "run" : r.status === "failed" ? "fail" : r.status === "needs_you" ? "need" : "done")}
    <span class="sx"><b>${esc(r.label)}</b> ${esc(r.args || r.summary || "")}</span><span class="num faint">${r.end != null ? J.dur(r.end - r.start) : "…"}</span></div>`).join("")}</div>`;
}
let moreOpen = J.saved.get("jarviee.amore") === "1";
function overview(a) {
  const when = t => t ? new Date(t * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}) : "—";
  const run = (J.RUNS || []).find(r => r.id === a.id);
  const reasons = (a.reasons || []).filter(r => typeof r === "string");
  const done = J.groupOf(a) === "done";
  /* the stat strip only shows what is known: hook sessions have no token or cost figures */
  const stats = [`<div><b class="num">${a.steps || 0}</b><span>steps</span></div>`,
    `<div><b class="num clock" data-start="${a.started || ""}" data-end="${done ? J.endOf(a) || "" : ""}">${a.started ? J.dur((done ? J.endOf(a) : J.now()) - a.started) : "—"}</b><span>${done ? "took" : "elapsed"}</span></div>`];
  if (run?.tokens) stats.push(`<div><b class="num">${J.fmtTok(run.tokens)}</b><span>tokens</span></div>`);
  if (run?.cost != null) stats.push(`<div><b class="num">${J.fmtUsd(run.cost)}</b><span>cost</span></div>`);
  return `
    <div class="dt-stats n${stats.length}">${stats.join("")}</div>
    <h4 class="sect-h">${done ? "Last steps" : "Live steps"}</h4><div id="ag-stream">${streamHtml()}</div>
    <details class="acc-sm" id="ag-more"${moreOpen ? " open" : ""}><summary><svg class="i sm chev" aria-hidden="true"><use href="#i-chev"/></svg>Details<span class="faint">effort, folder, times, router reasons, fallbacks</span></summary>
      <div class="acc-sm-b">
      <div class="kv"><span>Tool</span><b>${esc(J.TOOL[a.tool] || a.tool || "—")}</b>
        <span>Effort</span><b>${esc(a.effort || "default")}</b>
        <span>Folder</span><b class="num" title="${esc(a.workspace || "")}">${esc(a.workspace || "—")}</b>
        <span>Source</span><b>${a.source === "launcher" ? "Launched from Jarviee" : "Reported by hooks"}</b>
        <span>Started</span><b>${when(a.started)}</b><span>Ended</span><b>${when(J.endOf(a))}</b><span>ID</span><b class="num" title="${esc(a.id)}">${esc(a.id)}</b></div>
      ${reasons.length ? `<h4 class="sect-h">Router reasons</h4><div class="chips">${reasons.map(r => `<span class="chip${r.startsWith("fallback") ? " violet" : r.startsWith("manual") ? " ember" : ""}">${esc(r)}</span>`).join("")}</div>` : ""}
      <h4 class="sect-h">Model stack</h4>${stackHtml(a)}
      </div></details>`;
}
function logFallback(a) {
  if (a.log_tail && String(a.log_tail).trim()) return a.log_tail;
  if (!a.log_path) return "Only runs launched from Jarviee keep a log. This session reports through hooks; its full transcript is in its own app.";
  return "(The log is empty so far.)";
}
async function loadLog(a, force) {
  if (!a || (!force && logId === a.id && Date.now() - logAt < 2500)) return;
  logAt = Date.now();
  let t = logFallback(a);
  if (a.log_path) { try { t = await J.api("/api/run-output?id=" + encodeURIComponent(a.id)); } catch (e) { /* keep the fallback */ } }
  if (selId !== a.id) return;
  logId = a.id; logText = String(t || "").trim() ? t : logFallback(a);
  const pre = q("#ag-log");
  if (pre && pre.textContent !== logText) { const end = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30; pre.textContent = logText; if (end) pre.scrollTop = pre.scrollHeight; }
}
let diffId = "", diffAt = 0;
function diffHtml(text) {
  return `<pre class="log diff" tabindex="0">${String(text).split("\n").map(l => {
    const c = /^(\+\+\+|---|diff |index |new file|deleted file)/.test(l) ? "dh" : l[0] === "+" ? "da" : l[0] === "-" ? "dd" : l.startsWith("@@") ? "dk" : "";
    return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
  }).join("\n")}</pre>`;
}
async function loadDiff(a) {
  if (diffId === a.id && Date.now() - diffAt < 5000) return;
  diffId = a.id; diffAt = Date.now();
  let t;
  try { t = await J.api("/api/diff?id=" + encodeURIComponent(a.id)); } catch (e) { t = "Could not load the diff: " + e.message; }
  if (selId !== a.id || tab !== "changes") return;
  const box = q("#ag-diff"); if (box) J.setHTML(box, diffHtml(t));
}
async function loadTrace(force) {
  if (!selId || (!force && traceId === selId && Date.now() - traceAt < 5000)) return;
  const id = selId; traceAt = Date.now();
  try { const t = await J.api(`/api/run/${encodeURIComponent(id)}/trace`); if (selId === id) { trace = t; traceId = id; paintTrace(); } }
  catch (e) { if (selId === id) { trace = {rows: [], t0: 0, end: 0}; traceId = id; paintTrace(); } }
}
function paintTrace() {
  const s = q("#ag-stream"); if (s) J.setHTML(s, streamHtml());
  const t = q("#ag-trace"); if (t && J.timelineHtml) J.setHTML(t, trace?.rows?.length ? J.timelineHtml(trace, {compact: true}) : `<div class="empty">No trace recorded for this agent.</div>`);
}
const FOLLOW_OK = ["done", "failed", "stopped"];
function followHtml(a) {
  if (a.source !== "launcher" || !FOLLOW_OK.includes(a.status)) return "";
  const hint = (a.tool === "claude" || a.tool === "omniroute") && a.session_id ? `Reply: continues this ${J.TOOL_SHORT[a.tool] || a.tool} conversation` : "Reply: runs it again with your note added";
  return `<form class="ad-follow" data-id="${esc(a.id)}"><label class="sr-only" for="ag-reply">Reply to this run</label>
    <textarea class="input" id="ag-reply" rows="2" maxlength="4000" placeholder="${esc(hint)}"></textarea>
    <div class="ad-follow-row"><button class="btn sm primary" type="submit" data-key="reply">Send reply</button><span class="spacer"></span>
      <select class="input sm" id="ag-rl" aria-label="Level for Run again"><option value="">Same level</option>${["basic", "light", "complex", "heavy"].map(l => `<option value="${l}">${l}</option>`).join("")}</select>
      <select class="input sm" id="ag-rt" aria-label="Tool for Run again"><option value="">Same tool</option><option value="agy">agy</option><option value="claude">Claude</option></select>
      <button class="btn sm" type="button" data-rerun data-key="rerun">Run again</button></div></form>`;
}
async function followUp(a, reply) {
  const body = {id: a.id};
  if (reply) body.reply = reply;
  else { body.tier = q("#ag-rl")?.value || undefined; body.tool = q("#ag-rt")?.value || undefined; }
  try {
    const r = await J.api("/api/rerun", body);
    J.toast(r.mode === "resume" ? "Reply sent to the same conversation" : reply ? "Re-running with your reply" : "Running again");
    J.poll(); location.hash = "#/agents/" + encodeURIComponent(r.id);
  } catch (e) { J.toast(e.message, true); }
}
function detail() {
  const box = q("#ag-detail"), a = (J.S?.agents || []).find(x => x.id === selId);
  el.classList.toggle("has-sel", !!a);
  if (!a) { J.setHTML(box, `<div class="empty pad">${selId ? "This agent isn't in the list any more (cleared or expired)." : "Select an agent to see its prompt, log and trace."}</div>`); return; }
  const g = J.groupOf(a);
  const why = a.suggested_tier && a.suggested_tier !== a.tier ? ` <span class="faint" title="The router suggested ${esc(a.suggested_tier)}">router said ${esc(a.suggested_tier)}</span>` : "";
  const head = `<div class="ad-head">${J.toolMono(a.tool)}<div class="ad-t"><h2>${esc(J.titleOf(a))}</h2><div class="ad-m">${J.st(J.statusKey(a))}${J.place(a) ? `<span class="muted">${esc(J.place(a))}</span>` : ""}</div>
      <div class="ad-m ad-model"><span class="num" title="${esc(a.model || "")}">${esc(J.modelLabel(a.model) || a.tool || "—")}</span>${J.agentPill(a)}${why}</div></div>
      <a class="btn icon sm ghost ad-close" href="#/agents" aria-label="Close"><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg></a></div>
    ${g === "need" ? (() => { const w = J.replyWhere(a); return `<div class="ad-need">${J.sd("need")}<div><b>${esc(J.waitReason(a.activity))}</b><span>${esc(w.text)}</span></div><button class="btn sm primary" type="button" data-act="focus" data-id="${esc(a.id)}" data-key="needfocus"><svg class="i sm" aria-hidden="true"><use href="#i-pane"/></svg>Go to its window</button></div>`; })() : ""}
    <div class="ad-acts" data-id="${esc(a.id)}">${J.act.html(a, {noOpen: true, noFocus: g === "need"})}<span class="spacer"></span><button class="btn icon sm ghost" type="button" data-copy data-key="copy" title="Copy prompt" aria-label="Copy prompt"><svg class="i sm" aria-hidden="true"><use href="#i-copy"/></svg></button>
      <a class="btn icon sm ghost" href="#/workflows/${encodeURIComponent(a.id)}" data-key="flow" title="Open workflow" aria-label="Open workflow"><svg class="i sm" aria-hidden="true"><use href="#i-flow"/></svg></a></div>
    ${followHtml(a)}
    <div class="tabs" role="tablist">${[["overview", "Overview"], ["prompt", "Prompt"], ["log", "Log"], ["changes", "Changes"], ["trace", "Trace"]].map(([k, l]) =>
      `<button type="button" role="tab" data-tab="${k}" data-key="tab:${k}" aria-selected="${tab === k}">${l}</button>`).join("")}</div>`;
  const body = tab === "prompt" ? `<div class="md">${J.md(a.task)}</div>`
    : tab === "log" ? `<pre class="log" id="ag-log" tabindex="0">${esc(logId === a.id ? logText : logFallback(a))}</pre>`
    : tab === "changes" ? `<div id="ag-diff"><div class="empty">Loading…</div></div>`
    : tab === "trace" ? `<div id="ag-trace"><div class="empty">Loading…</div></div>` : overview(a);
  const html = `${head}<div class="ad-body" role="tabpanel">${body}</div>`;
  const keep = tab === "log" || tab === "trace" || tab === "changes";
  if (keep && box._sel === a.id + tab) refreshHead(a, head);
  else { box._sel = a.id + tab; box._html = null; J.setHTML(box, html); }
  if (tab === "log") loadLog(a);
  if (tab === "changes") loadDiff(a);
  if (tab === "trace" || tab === "overview") { if (traceId === selId) paintTrace(); loadTrace(); }
}
/* on the Log and Trace tabs only the header is patched, so the scroll position and selection in the body survive polls */
function refreshHead(a, head) {
  const box = q("#ag-detail"), tmp = document.createElement("div"); tmp.innerHTML = head;
  for (const sel of [".ad-head", ".ad-acts"]) { const cur = box.querySelector(sel), nu = tmp.querySelector(sel); if (cur && nu && cur.innerHTML !== nu.innerHTML) cur.innerHTML = nu.innerHTML; }
  if (!!box.querySelector(".ad-need") !== !!tmp.querySelector(".ad-need")) box._sel = "";   // full redraw next poll
}

/* under the live rows: runs that ended in the last 24 hours (from /api/runs), so the list shows the day, not one row
   over empty glass. Only for the unfiltered list; they open in Workflows, where each run's steps live. */
function earlier(shown) {
  const box = q("#ag-earlier"); if (!box) return;
  const since = J.now() - 86400;
  const ids = new Set(J.S.agents.map(a => a.id));
  const runs = filter === "all" && !text && !tool ? (J.RUNS || []).filter(r => !ids.has(r.id) && (r.started || 0) >= since
    && ["done", "failed", "cancelled", "stopped"].includes(r.status)).slice(0, 30) : [];
  const SK = {done: "done", failed: "fail", cancelled: "stopped", stopped: "stopped"};
  J.setHTML(box, runs.length ? `<h4 class="sect-h">Earlier · last 24 h <span class="num">${runs.length}</span></h4>` + runs.map(r => `<a class="er" href="#/workflows/${encodeURIComponent(r.id)}">
      ${J.sd(SK[r.status])}<span class="er-t">${esc(J.makeTitle ? J.makeTitle(r.title || "") : r.title || "")}<small>${esc(r.project || "—")}</small></span>
      <span class="num faint">${r.cost != null ? J.fmtUsd(r.cost) : ""}</span><span class="num">${J.dur(r.duration || 0)}</span></a>`).join("") : "");
}

J.pages.agents = {
  init() {
    el = document.querySelector('[data-page="agents"]');
    el.innerHTML = `
      <div class="toolbar">
        <label class="search"><svg class="i" aria-hidden="true"><use href="#i-search"/></svg><span class="sr-only">Search agents</span><input class="input" id="ag-q" type="search" placeholder="Search agents, projects, models"></label>
        <span class="seg" id="ag-f" role="group" aria-label="Status">${FILTERS.map(([k, l]) => `<button type="button" data-f="${k}" aria-pressed="${k === filter}">${l}<span class="n num"></span></button>`).join("")}</span>
        <span class="seg" id="ag-tool" role="group" aria-label="Tool"><button type="button" data-t="" aria-pressed="true">All tools</button><button type="button" data-t="agy" aria-pressed="false">agy</button><button type="button" data-t="claude" aria-pressed="false">Claude</button></span>
        <span class="spacer"></span>
        <button class="btn sm ghost" type="button" id="ag-clear"><svg class="i sm" aria-hidden="true"><use href="#i-broom"/></svg>Clear finished</button>
      </div>
      <div class="grid ag-grid">
        <section class="panel" aria-label="Agent list"><div class="ag-list" id="ag-list" role="list"></div><div class="empty" id="ag-empty" hidden>No agents match.</div>
          <button class="btn sm ghost ag-more" type="button" id="ag-more" hidden></button><div class="ag-earlier" id="ag-earlier"></div></section>
        <section class="panel ag-detail" id="ag-detail" aria-label="Agent detail"></section>
      </div>`;
    q("#ag-q").addEventListener("input", e => { text = e.target.value.trim().toLowerCase(); this.render(); });
    q("#ag-f").addEventListener("click", e => { const b = e.target.closest("[data-f]"); if (b) { filter = b.dataset.f; this.render(); } });
    q("#ag-tool").addEventListener("click", e => { const b = e.target.closest("[data-t]"); if (b) { tool = b.dataset.t; this.render(); } });
    q("#ag-more").addEventListener("click", () => { limit += PAGE; this.render(); });
    q("#ag-q").addEventListener("input", () => { limit = PAGE; }, {capture: true});
    q("#ag-f").addEventListener("click", () => { limit = PAGE; }, true);
    q("#ag-tool").addEventListener("click", () => { limit = PAGE; }, true);
    q("#ag-clear").addEventListener("click", async () => { try { await J.api("/api/clear", {}); J.toast("Cleared finished agents"); J.poll(); } catch (e) { J.toast(e.message, true); } });
    q("#ag-list").addEventListener("click", e => { const r = e.target.closest("[data-id]"); if (r && !e.target.closest("a,button")) location.hash = "#/agents/" + encodeURIComponent(r.dataset.id); });
    q("#ag-list").addEventListener("keydown", e => { const r = e.target.closest("[data-id]"); if (r && e.key === "Enter") location.hash = "#/agents/" + encodeURIComponent(r.dataset.id); });
    const det = q("#ag-detail");
    J.act.bind(det);
    det.addEventListener("toggle", e => { if (e.target.id === "ag-more") { moreOpen = e.target.open; J.saved.set("jarviee.amore", moreOpen ? "1" : "0"); } }, true);
    det.addEventListener("submit", e => {
      const f = e.target.closest(".ad-follow"); if (!f) return;
      e.preventDefault(); const a = J.S.agents.find(x => x.id === selId), text = q("#ag-reply")?.value.trim();
      if (!a) return; if (!text) { J.toast("Type a reply first, or use Run again", true); return; }
      followUp(a, text);
    });
    det.addEventListener("click", e => {
      if (e.target.closest("[data-rerun]")) { const a = J.S.agents.find(x => x.id === selId); if (a) followUp(a, ""); return; }
      const t = e.target.closest("[data-tab]");
      if (t) { tab = t.dataset.tab; J.saved.set("jarviee.atab", tab); det._sel = ""; detail(); return; }
      if (e.target.closest("[data-copy]")) { const a = J.S.agents.find(x => x.id === selId); navigator.clipboard?.writeText(a?.task || "").then(() => J.toast("Prompt copied"), () => J.toast("Copy blocked by the browser", true)); }
    });
    J.on("escape", () => { if (J.shell.current() === "agents" && selId) location.hash = "#/agents"; });
  },
  show(params) {
    selId = params[0] || null;
    if (selId && J.query?.get("focus") === "1") { J.focusAgent(selId); history.replaceState(null, "", "#/agents/" + encodeURIComponent(selId)); }   // from a notification
    const f = J.query?.get("f"); if (f) filter = f;
    if (selId !== traceId) { trace = null; }
  },
  render() {
    if (!J.S || !el) return;
    for (const b of q("#ag-f").children) b.setAttribute("aria-pressed", String(b.dataset.f === filter));
    for (const b of q("#ag-tool").children) b.setAttribute("aria-pressed", String(b.dataset.t === tool));
    const all = J.S.agents, cnt = {all: all.length, need: 0, run: 0, done: 0, fail: 0};
    for (const a of all) { const g = J.groupOf(a); if (cnt[g] != null) cnt[g]++; if (a.status === "failed") cnt.fail++; }
    for (const b of q("#ag-f").children) { const n = b.querySelector(".n"), v = cnt[b.dataset.f] ? String(cnt[b.dataset.f]) : ""; if (n.textContent !== v) n.textContent = v; }
    const all_ = list(), si = all_.findIndex(a => a.id === selId);
    /* wide window: the detail pane is never an empty box; it opens the top agent (needs-you first) */
    if (!selId && all_.length && J.wide()) { location.replace("#/agents/" + encodeURIComponent(all_[0].id)); return; }
    const items = all_.slice(0, Math.max(limit, si + 1));   // the selected agent always stays in view
    const more = q("#ag-more"), rest = all_.length - items.length;
    more.hidden = rest <= 0;
    if (rest > 0) { const t = `Show ${Math.min(PAGE, rest)} more · ${rest} older`; if (more.textContent !== t) more.textContent = t; }
    J.patch(q("#ag-list"), items, {key: a => a.id, html: row, cls: a => `ar g-${J.groupOf(a)}${a.id === selId ? " sel" : ""}`,
      sig: a => JSON.stringify([a.status, a.title, a.activity, a.summary, a.model, a.tier, a.suggested_tier, a.ended, J.place(a), a.id === selId]),
      attrs: a => ({role: "listitem", tabindex: "0", "aria-current": a.id === selId ? "true" : null})});
    q("#ag-empty").hidden = all_.length > 0;
    earlier(all_);
    detail();
  },
  tick() {
    for (const c of el.querySelectorAll(".clock[data-start]")) { const s = +c.dataset.start, e = +c.dataset.end; if (!s) continue; const t = J.dur((e || J.now()) - s); if (c.textContent !== t) c.textContent = t; }
    const a = J.S?.agents.find(x => x.id === selId);
    if (a && tab === "log" && J.isLive?.(a) !== false) loadLog(a);
    if (a && (tab === "trace" || tab === "overview") && J.groupOf(a) !== "done") loadTrace();
  },
  key(e) {
    if (e.key !== "j" && e.key !== "k" && e.key !== "s") return;
    const ids = list().map(a => a.id); if (!ids.length) return;
    e.preventDefault();
    if (e.key === "s") { if (selId) J.act.pending.get(selId) === "confirm" ? J.act.stop(selId) : J.act.ask(selId); return; }
    const i = ids.indexOf(selId), n = Math.max(0, Math.min(ids.length - 1, i < 0 ? 0 : i + (e.key === "j" ? 1 : -1)));
    location.hash = "#/agents/" + encodeURIComponent(ids[n]);
    requestAnimationFrame(() => q(`#ag-list [data-id="${CSS.escape(ids[n])}"]`)?.scrollIntoView({block: "nearest"}));
  },
  crumb() { const a = J.S?.agents.find(x => x.id === selId); return a ? J.titleOf(a) : `${(J.S?.agents || []).length} agents`; },
};
J.isLive = a => ["running", "waiting", "queued"].includes(a.status);
J.wide = () => matchMedia("(min-width: 1101px)").matches;   // list and detail side by side (graph.css / review.css switch at 1100px)
})();
