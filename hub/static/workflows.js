/* workflows.js: one plan or run as a top-down orchestration tree (orchestrator -> phase columns -> tasks), a detail
   panel per card (Overview / Tasks / Context) and a Trace / Tokens & cost / Events panel. Data: /api/run/<id>/tree and
   /trace. Live numbers (time, tokens, cost) and the trace playhead are filled by one animation loop, so the HTML only
   changes when the data does: polls never restart the spinners or the edge flow. */
"use strict";
(() => {
const {esc} = J;
let el = null, selId = null, selNode = null, sideTab = "overview", tree = null, trace = null, loadedId = null, at = 0, zoom = 1, fitted = false, hover = null, busy = false, err = "";
let tlTab = "trace", tlFresh = true;
const collapsed = new Set(), expanded = new Set();   // trace rows: agents start open, model turns start closed
const q = s => el.querySelector(s);
const SKEY = {done: "done", running: "run", failed: "fail", needs_you: "need", queued: "queue", blocked: "queue", retry: "fb", fallback: "fb", cancelled: "stopped", stopped: "stopped", waiting: "need", draft: "queue", skipped: "stopped"};
const sk = s => SKEY[s] || "queue";
const WORD = {run: "Running", queue: "Queued", done: "Done", fail: "Failed", need: "Needs you", fb: "Retry", stopped: "Skipped"};
const PHASE = {plan: "Plan", research: "Research", analysis: "Analysis", fact_check: "Fact check", build: "Build", test: "Test", review: "Review", fix: "Fix", brain: "Brain sync"};

/* ---------- icons and formatting ---------- */
const ICO = {
  docs: '<path d="M4 2.5h5.2L12 5.3v8.2H4z"/><path d="M9 2.5v3h3M6 8.2h4M6 10.7h4"/>',
  edit: '<path d="M10.4 2.8l2.8 2.8-7.4 7.4H3v-2.8z"/><path d="M8.8 4.4l2.8 2.8"/>',
  term: '<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="2"/><path d="M4.6 6.4l2 1.6-2 1.6M8.2 10h3.2"/>',
  search: '<circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2L13.8 13.8"/>',
  globe: '<circle cx="8" cy="8" r="5.8"/><path d="M2.2 8h11.6M8 2.2c1.9 2.1 1.9 9.5 0 11.6M8 2.2c-1.9 2.1-1.9 9.5 0 11.6"/>',
  handoff: '<path d="M8 2.2l5.8 3L8 8.2l-5.8-3z"/><path d="M2.2 8.2L8 11.2l5.8-3M2.2 11L8 14l5.8-3"/>',
  code: '<path d="M5.6 4.6L2.2 8l3.4 3.4M10.4 4.6L13.8 8l-3.4 3.4"/>',
  clock: '<circle cx="8" cy="8" r="5.8"/><path d="M8 4.8V8l2.2 1.4"/>',
  spark: '<path d="M8 2l1.3 4.7L14 8l-4.7 1.3L8 14l-1.3-4.7L2 8l4.7-1.3z"/>',
  db: '<ellipse cx="8" cy="4" rx="5" ry="1.9"/><path d="M3 4v8c0 1 2.2 1.9 5 1.9s5-.9 5-1.9V4M3 8c0 1 2.2 1.9 5 1.9s5-.9 5-1.9"/>',
  msg: '<path d="M2.6 3.2h10.8v7.4H6.2l-3.6 2.6z"/>',
  trace: '<path d="M2.5 4.5h6M4.5 8h8M7 11.5h6.5"/>',
  pin: '<path d="M8 14s4.6-4.2 4.6-7.6a4.6 4.6 0 0 0-9.2 0C3.4 9.8 8 14 8 14z"/><circle cx="8" cy="6.4" r="1.6"/>',
  fit: '<path d="M2.2 3v10M13.8 3v10M4.6 8h6.8M6.4 6.2L4.6 8l1.8 1.8M9.6 6.2L11.4 8 9.6 9.8"/>',
  spin: '<path d="M13.4 8A5.4 5.4 0 1 1 8 2.6"/>',
  check: '<path d="M3.4 8.4l3 3 6.2-6.8"/>',
  user: '<circle cx="8" cy="5.4" r="2.6"/><path d="M3 13.6c.6-2.6 2.6-4 5-4s4.4 1.4 5 4"/>',
  file: '<path d="M4 2.5h5.2L12 5.3v8.2H4z"/><path d="M9 2.5v3h3"/>',
  route: '<circle cx="4" cy="4" r="1.6"/><circle cx="12" cy="12" r="1.6"/><path d="M5.6 4H10a2 2 0 0 1 0 4H6a2 2 0 0 0 0 4h4.4"/>',
  chev: '<path d="M6 4l4 4-4 4"/>',
  hub: '<rect x="2.6" y="2.6" width="10.8" height="10.8" rx="3"/><path d="M5.6 8h4.8M8 5.6v4.8"/>',
};
const ico = (k, cls = "sm") => `<svg class="i ${cls}" viewBox="0 0 16 16" aria-hidden="true">${ICO[k] || ICO.code}</svg>`;
const TOOL_CAT = [   // first match wins: browser before web, web search before plain search, reads before greps
  [/^(task|agent|handoff|subagent|send_?message|spawn|delegate)/i, "Helper agents", "handoff"],
  [/(playwright|browser_|chrome|computer)/i, "Browser", "globe"],
  [/(web_?search|search_?web|web_?fetch|fetch|browse|url|http)/i, "Web", "globe"],
  [/^(read|view|cat|notebookread|ls|list|glob|open)/i, "Reading files", "docs"],
  [/(grep|search|find|codebase|ripgrep)/i, "Searching code", "search"],
  [/(edit|write|replace|create|patch|apply|notebookedit|insert)/i, "Editing files", "edit"],
  [/(bash|shell|run_?command|terminal|exec|command|python|node)/i, "Running commands", "term"],
];
const toolCat = name => { for (const [re, label, ic] of TOOL_CAT) if (re.test(name || "")) return {label, ic}; return {label: "Other", ic: "code"}; };

/* ---------- plain language: what a tool call means, without commands, paths or tool ids ---------- */
const SCRATCH = /(^|\/)(tmp|private\/tmp|var\/folders|scratchpad|\.cache)(\/|$)|claude-\d+\//i;
const baseName = f => String(f || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();
function fileIn(args) {   // the first real project file an argument names (scratch / temp files are not "the work")
  for (const m of String(args || "").matchAll(/(?:~|\.{0,2}\/)?[\w@.+-]+(?:\/[\w@.+-]+)*\.[A-Za-z][A-Za-z0-9]{0,6}\b/g)) {
    if (SCRATCH.test(m[0]) || /^\d/.test(m[0]) || /^(e\.g|i\.e)\./.test(m[0])) continue;
    return baseName(m[0]);
  }
  return "";
}
const BROWSER = {navigate: ["Opening a page in the browser", "Opened a page in the browser"], click: ["Clicking around the page", "Clicked around the page"],
  take_screenshot: ["Taking a screenshot of the page", "Took screenshots of the page"], screenshot: ["Taking a screenshot of the page", "Took screenshots of the page"],
  evaluate: ["Inspecting the page", "Inspected the page"], snapshot: ["Reading the page", "Read the page"], console_messages: ["Checking the page for errors", "Checked the page for errors"],
  resize: ["Resizing the browser", "Resized the browser"], type: ["Typing into the page", "Typed into the page"], fill_form: ["Filling in a form", "Filled in a form"]};
/* [present, past, group key, file] for one tool call */
function plain(tool, args) {
  const t = String(tool || ""), a = String(args || ""), low = t.toLowerCase(), f = fileIn(a);
  const pair = (now, past, key, file = "") => [now, past, key, file];
  if (/playwright|browser_|chrome/.test(low)) { const k = Object.keys(BROWSER).find(x => low.endsWith(x)); return pair(...(BROWSER[k] || ["Using the browser", "Used the browser"]), "browser:" + (k || "")); }
  if (/^(task|agent|subagent|spawn|delegate)/.test(low)) return pair("Handing part of the work to a helper agent", "Handed work to a helper agent", "helper");
  if (/todo/.test(low)) return pair("Updating its to-do list", "Updated its to-do list", "todo");
  if (/web_?search|search_?web/.test(low)) return pair("Searching the web", "Searched the web", "web");
  if (/web_?fetch|fetch|url/.test(low)) return pair("Reading a web page", "Read a web page", "webread");
  if (/^skill/.test(low)) return pair("Loading a skill", "Loaded a skill", "skill");
  if (/\.(png|jpe?g|gif|webp|heic)$/i.test(f) && /^(read|view|open)/.test(low)) return pair("Looking at a screenshot", "Looked at screenshots", "image");
  if (/^(read|view|cat|notebookread|open)/.test(low)) return f ? pair(`Reading ${f}`, "Read", "read", f) : pair("Reading files", "Read some files", "read");
  if (/^(ls|list|glob)/.test(low)) return pair("Looking through the project files", "Looked through the project files", "browse");
  if (/grep|search|find|codebase/.test(low)) return pair("Searching the code", "Searched the code", "search");
  if (/^write|create/.test(low)) return f ? pair(`Writing ${f}`, "Wrote", "edit", f) : pair("Writing a file", "Wrote a file", "edit");
  if (/edit|replace|patch|apply|insert/.test(low)) return f ? pair(`Editing ${f}`, "Edited", "edit", f) : pair("Editing files", "Edited files", "edit");
  if (/bash|shell|run_?command|terminal|exec|command/.test(low) || !t) {
    const c = a.trim().replace(/^(cd \S+ && )+/, "");
    if (/\b(unittest|pytest|npm (run )?test|jest|vitest|go test|cargo test)\b/.test(c)) return pair("Running the tests", "Ran the tests", "tests");
    if (/node --check|tsc\b|eslint|ruff|flake8|py_compile/.test(c)) return pair("Checking the code for mistakes", "Checked the code for mistakes", "lint");
    if (/^git (commit|push)/.test(c)) return pair("Saving the work to git", "Saved the work to git", "git-save");
    if (/^git /.test(c)) return pair("Checking what changed", "Checked what changed", "git");
    if (/launchctl|systemctl|kickstart|restart/.test(c)) return pair("Restarting the app", "Restarted the app", "restart");
    if (/^curl\b/.test(c)) return /127\.0\.0\.1|localhost/.test(c) ? pair("Checking the running app", "Checked the running app", "api") : pair("Fetching something from the web", "Fetched something from the web", "web");
    if (/^(grep|rg|find)\b/.test(c)) return pair("Searching the code", "Searched the code", "search");
    if (/^(cat|sed -n|head|tail|less|wc)\b/.test(c)) return f ? pair(`Reading ${f}`, "Read", "read", f) : pair("Reading files", "Read some files", "read");
    if (/^(ls|tree)\b/.test(c)) return pair("Looking through the project files", "Looked through the project files", "browse");
    if (/^(npm|pip|brew|yarn|pnpm) (i|install|add)\b/.test(c)) return pair("Installing packages", "Installed packages", "install");
    if (/^python3? -|^python3? <</.test(c) || /<<\s*'?EOF/.test(c)) return pair("Running a small script", "Ran a small script", "script");
    return pair("Running a command", "Ran a command", "cmd");
  }
  const m = t.match(/^mcp__([^_]+(?:_[^_]+)*)__/);
  if (m) { const svc = m[1].replace(/^(claude_ai_|plugin_[^_]+_)/, "").replace(/_/g, " "); return pair(`Using ${svc}`, `Used ${svc}`, "mcp:" + svc); }
  return pair(`Using ${t.replace(/_/g, " ")}`, `Used ${t.replace(/_/g, " ")}`, "t:" + t);
}
/* activity lines from the hooks ("using Bash", "ran Read") said plainly */
function plainActivity(text) {
  const m = String(text || "").match(/^(?:using|ran|running|calling|called)\s+(\S+)\s*(.*)$/i);
  return m ? plain(m[1], m[2])[0] : text;
}
/* routing notes ("fallback: agy quota exhausted (429 ...); routed to claude:haiku") in plain words */
function plainRoute(note) {
  const t = String(note || "").trim();
  if (!t) return "";
  if (/quota|429|exhausted|rate.?limit/i.test(t)) return "Moved to another model because the first one ran out of quota";
  if (/^fallback/i.test(t)) return "Moved to another model after the first one failed";
  if (/^waiting for/i.test(t)) return t.charAt(0).toUpperCase() + t.slice(1);
  return /[:_(){}\[\]]/.test(t) ? "" : t;
}
/* consecutive steps of the same kind become one line: "Read workflows.js, graph.css and runtree.py" */
function story(calls, live) {
  const out = [];
  for (const c of calls) {
    const [now, past, key, file] = plain(c.tool, c.args), open = c.end == null && live, last = out[out.length - 1];
    if (last && last.key === key && !open && !last.open) { last.n++; if (file && !last.files.includes(file)) last.files.push(file); last.end = c.end; last.bad ||= c.status === "failed"; continue; }
    out.push({key, now, past, files: file ? [file] : [], n: 1, open, start: c.start, end: c.end, bad: c.status === "failed"});
  }
  const list = fs => fs.length <= 3 ? fs.join(fs.length === 2 ? " and " : ", ").replace(/, ([^,]*)$/, " and $1") : `${fs.slice(0, 2).join(", ")} and ${fs.length - 2} more files`;
  return out.map(g => ({...g, text: g.open ? g.now : g.files.length ? `${g.past} ${list(g.files)}` : g.n > 2 && !/s$/.test(g.past) ? `${g.past} (${g.n} times)` : g.past}));
}
const toolName = t => t === "claude" ? "Claude" : t === "agy" ? "agy" : t || "Jarviee";
const fmtSec = s => {
  if (s == null || !isFinite(s)) return "";
  s = Math.max(0, s);
  if (s < 60) return s.toFixed(1) + "s";
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m`;
};
const fmtK = n => n == null ? "—" : n < 1000 ? String(Math.round(n)) : n < 1e6 ? (n / 1e3).toFixed(1) + "K" : n < 1e9 ? (n / 1e6).toFixed(1) + "M" : (n / 1e9).toFixed(1) + "B";
const fmtUsd = n => n == null ? "—" : n < 10 ? "$" + n.toFixed(2) : J.fmtUsd(n);
function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : String(m).padStart(2, "0")) + ":" + String(s).padStart(2, "0");
}
const fmtTick = t => t < 60 ? `${t}s` : t < 3600 ? `${Math.floor(t / 60)}m${t % 60 ? ` ${t % 60}s` : ""}` : `${Math.floor(t / 3600)}h${t % 3600 ? ` ${Math.floor(t % 3600 / 60)}m` : ""}`;
const clip = (t, n) => { t = String(t || ""); return t.length > n ? t.slice(0, n - 1) + "…" : t; };

/* ---------- live values: one loop ticks durations and eases token / cost changes ---------- */
const nums = new Map();          // "tok:<node>" | "cost:<node>" -> {from, to, t}
let nodeMap = new Map(), toolIdx = new Map(), spanIdx = new Map(), raf = 0, lastText = 0, scrubT = null, drag = false, follow = true;
const EASE_MS = 700;
function numNow(key, pn = performance.now()) {
  const s = nums.get(key); if (!s) return null;
  if (s.from == null || s.to == null) return s.to;
  const p = Math.min(1, (pn - s.t) / EASE_MS);
  return s.from + (s.to - s.from) * (1 - Math.pow(1 - p, 3));
}
function setNum(key, v) {
  v = v == null ? null : +v;
  const s = nums.get(key);
  if (!s) { nums.set(key, {from: v, to: v, t: 0}); return; }
  if (s.to === v) return;
  s.from = numNow(key) ?? v; s.to = v; s.t = performance.now();
}
const easing = () => { const pn = performance.now(); for (const s of nums.values()) if (s.t && pn - s.t < EASE_MS) return true; return false; };
const isOpen = n => n && n.start && n.end == null && (["running", "needs_you"].includes(n.state || n.status) || (n.role === "orchestrator" && tree?.live));
function durNow(id, now) {
  if (id === "__sum") return tree ? (tree.live ? now - tree.t0 : tree.summary?.elapsed) : null;
  const n = nodeMap.get(id);
  if (!n || !n.start) return null;
  if (isOpen(n)) return now - n.start;
  return n.end != null ? n.end - n.start : n.duration;
}
function fillLive() {
  if (!el || !tree) return;
  const now = J.now(), pn = performance.now();
  for (const e of el.querySelectorAll("[data-lv]")) {
    const v = e.dataset.lv, i = v.indexOf(":"), kind = v.slice(0, i), id = v.slice(i + 1);
    let txt = "—";
    if (kind === "dur") txt = fmtSec(durNow(id, now)) || "—";
    else if (kind === "cd") { const c = numNow("cost:" + id, pn), d = fmtSec(durNow(id, now)); txt = [c != null ? fmtUsd(c) : "", d].filter(Boolean).join(" · ") || "—"; }
    else if (kind === "tok") txt = fmtK(numNow("tok:" + id, pn));
    else if (kind === "cost") txt = fmtUsd(numNow("cost:" + id, pn));
    else if (kind === "sp") { const s = spanIdx.get(id); txt = s ? fmtSec((s.end ?? (now - trace.t0)) - s.start) : ""; }
    if (e.textContent !== txt) e.textContent = txt;
  }
}
function frame(ts) {
  raf = 0;
  if (!el || J.shell.current() !== "workflows" || document.hidden) return;
  if (ts - lastText > 90) { fillLive(); lastText = ts; }
  tlUpdate();
  if (tree?.live || drag || easing()) raf = requestAnimationFrame(frame);
}
const kick = () => { if (!raf) raf = requestAnimationFrame(frame); };

/* ---------- trace timeline (also used by the Agents page, compact) ----------
   One continuous bar per row, banded every 10 s (two alternating colours), axis labels every 30 s. The bands come
   from a gradient sized in container units of the track, so they line up across rows without a DOM node per band. */
/* Adaptive stamps: the interval grows with the run so the axis keeps about 6-10 labels across the panel (a 40 s
   task reads in 5 s steps, a 15 min one in 2 min steps). Colour bands are a third of a stamp: 10 s at 30 s stamps. */
const TICK_PX = 64;   // narrowest a stamp may get before the track scrolls sideways
const TICKS = [[5, 5], [10, 5], [15, 5], [30, 10], [60, 20], [120, 40], [300, 100], [600, 200], [900, 300], [1800, 600],
  [3600, 1200], [7200, 2400], [14400, 3600], [28800, 7200], [86400, 28800]];
function tlScale(span) { const [tick, band] = TICKS.find(([t]) => span / t <= 10) || TICKS[TICKS.length - 1]; return {tick, band}; }
J.timelineHtml = (tr, {compact, focus, treeData, sel} = {}) => {
  const liveT = tr.live ? tr.now : null;
  const raw = (tr.rows || []).slice(0, compact ? 80 : 600);
  const rawEnd = Math.max(1, tr.end || 0, liveT || 0, ...raw.map(r => r.end ?? r.start ?? 0));
  const want = rawEnd + (tr.live ? Math.max(3, rawEnd * .08) : 0);   // live runs keep a little headroom past "now"
  const {tick, band} = tlScale(want);
  const D = Math.max(tick, Math.ceil(want / tick) * tick);
  const kids = new Map();
  for (const r of raw) { const p = r.parent || "__root__"; if (!kids.has(p)) kids.set(p, []); kids.get(p).push(r); }
  const rows = [], useOrch = !compact && treeData;
  const open = r => compact || (r.kind === "turn" ? expanded.has(r.id) : !collapsed.has(r.id));
  if (useOrch) {
    const o = (treeData.nodes || []).find(n => n.role === "orchestrator");
    if (o) rows.push({id: "orch", _key: "orch", _depth: 0, label: o.name || "Orchestrator", summary: o.summary || "", agent: o.tool || "claude",
      status: o.state || o.status, start: 0, end: o.end != null ? o.end - treeData.t0 : (tr.live ? null : rawEnd), _isOrch: true,
      _hasKids: raw.length > 0, _open: !collapsed.has("orch")});
  }
  const walk = (pid, depth, key) => {
    for (const r of kids.get(pid) || []) {
      const has = (kids.get(r.id) || []).length > 0, o = open(r);
      rows.push({...r, _depth: Math.min(4, depth), _key: key || r.id, _hasKids: has && !compact, _open: o, _top: !key});
      if (o) walk(r.id, depth + 1, key || r.id);
    }
  };
  if (!useOrch || !collapsed.has("orch")) walk("__root__", useOrch ? 1 : 0, null);

  const pct = t => (100 * t / D).toFixed(4) + "%";
  const bar = r => {
    if (r.start == null || r.queued) return "";
    const s = Math.max(0, r.start), live = r.end == null && tr.live, e = live ? liveT : Math.max(s, r.end ?? s);
    const st = sk(r.status), cls = ["fail", "need", "fb"].includes(st) ? st : st === "done" ? "ok done" : st === "stopped" ? "off" : "ok";
    return `<i class="tb b-${cls}" data-s="${s}" data-e="${live ? "" : e}" style="left:${pct(s)};width:${pct(e - s)};--bs:${(s / D).toFixed(6)}" title="${esc(`${r.kind === "tool" ? plain(r.tool, r.args)[0] : r.kind === "turn" ? "Turn" : r.label}: ${fmtSec(e - s)}`)}"></i>`;
  };
  const cls = r => `${focus && r._key === focus ? " hot" : ""}${sel && r._key === sel ? " sel" : ""}`;
  const lab = r => {
    const chev = r._hasKids ? `<button type="button" class="tl-tog" data-tl-tog="${esc(r.id)}" data-turn="${r.kind === "turn" ? 1 : ""}" aria-expanded="${r._open}" aria-label="${r._open ? "Collapse" : "Expand"} ${esc(r.label)}">${ico("chev", "xs")}</button>` : `<span class="tl-tog-sp"></span>`;
    const icon = (r._isOrch || r._top) && r.agent ? `<span class="tl-ico">${J.toolMono(r.agent)}</span>`
      : r.kind === "tool" ? `<span class="tl-cat">${ico(toolCat(r.tool).ic, "xs")}</span>` : `<span class="tl-cat">${ico("spark", "xs")}</span>`;
    const name = r.kind === "tool" ? plain(r.tool, r.args)[0] : r.kind === "turn" ? "Turn" : r.label;   // plain words; the raw call stays in the tooltip
    const note = r.kind === "turn" && r.args ? r.args : "";
    return `<div class="tl-l${r._isOrch ? " orch" : ""}${r._top ? " top" : ""}${cls(r)}" data-node="${esc(r._key)}" style="--d:${r._depth}" title="${esc(r.kind === "tool" ? `${r.tool} ${r.args || ""}`.trim() : r.summary || r.label)}">${chev}${icon}<span class="tl-name">${esc(name)}</span>${note ? `<span class="tl-args">${esc(clip(note, 90))}</span>` : ""}</div>`;
  };
  let axis = "";
  for (let t = 0; t <= D + .001; t += tick) axis += `<span class="tk${t === 0 ? " first" : t >= D ? " last" : ""}" style="left:${pct(t)}">${fmtTick(t)}</span>`;
  const endT = tr.live ? liveT : rawEnd;
  const ph = `<div class="tl-ph" style="left:${pct(endT)}"><span class="tl-ph-t num">${fmtClock(endT)}</span>${compact ? "" : `<button type="button" class="tl-knob" role="slider" aria-label="Trace playhead" aria-valuemin="0" aria-valuemax="${Math.round(endT)}" aria-valuenow="${Math.round(endT)}" aria-valuetext="${fmtClock(endT)}"></button>`}</div>`;
  return `<div class="tl2${compact ? " compact" : ""}" data-d="${D}" data-live="${tr.live ? 1 : ""}" data-t0="${tr.t0}" data-endt="${rawEnd}" style="--D:${D};--ticks:${D / tick};--band2:${(2 * band / D).toFixed(6)};--tickpx:${TICK_PX}">
    <div class="tl-labs"><div class="tl-ah"><span class="tl-colh">CALL</span></div>${rows.map(lab).join("")}</div>
    <div class="tl-scroll"><div class="tl-inner"><div class="tl-ah tl-axis2" data-tl-axis title="Drag to scrub through the run">${axis}</div>${rows.map(r => `<div class="tl-r${cls(r)}" data-node="${esc(r._key)}">${bar(r)}</div>`).join("")}${ph}</div></div>
  </div>`;
};
/* bars and playhead follow the clock (live runs) or the scrubbed time; called every animation frame */
function tlUpdate() {
  const box = el && q("#wf-tl .tl2"); if (!box) return;
  const D = +box.dataset.d, live = !!box.dataset.live;
  const endT = live ? J.now() - +box.dataset.t0 : +box.dataset.endt;
  const T = Math.max(0, scrubT != null ? Math.min(scrubT, endT) : endT);
  const bars = box._bars || (box._bars = [...box.querySelectorAll(".tb")]);
  for (const b of bars) {
    const s = +b.dataset.s, e = b.dataset.e === "" ? endT : +b.dataset.e, ve = Math.min(e, T);
    const gone = s > T;
    if (b._gone !== gone) { b._gone = gone; b.classList.toggle("gone", gone); }
    const w = (100 * Math.max(0, ve - s) / D).toFixed(4) + "%";
    if (b._w !== w) { b._w = w; b.style.width = w; }
  }
  const ph = box.querySelector(".tl-ph"), left = (100 * Math.min(T, D) / D).toFixed(4) + "%";
  if (ph && ph._l !== left) {
    ph._l = left; ph.style.left = left;
    ph.querySelector(".tl-ph-t").textContent = fmtClock(T);
    const k = ph.querySelector(".tl-knob");
    if (k) { k.setAttribute("aria-valuemax", Math.round(endT)); k.setAttribute("aria-valuenow", Math.round(T)); k.setAttribute("aria-valuetext", fmtClock(T)); }
  }
  box.classList.toggle("scrub", scrubT != null);
  const clk = q("#wf-tl-clock"); if (clk) { const c = fmtClock(T); if (clk.textContent !== c) clk.textContent = c; }
  if (live && scrubT == null && !drag && follow) {   // keep the playhead in view while the run grows
    const sc = box.querySelector(".tl-scroll"), x = T / D * sc.scrollWidth;
    if (x > sc.scrollLeft + sc.clientWidth - 48) sc.scrollLeft = x - sc.clientWidth * .6;
  }
}
function scrubFrom(ev) {
  const box = q("#wf-tl .tl2"); if (!box) return;
  const r = box.querySelector(".tl-inner").getBoundingClientRect(), D = +box.dataset.d;
  const endT = box.dataset.live ? J.now() - +box.dataset.t0 : +box.dataset.endt;
  const t = Math.max(0, Math.min(endT, (ev.clientX - r.left) / r.width * D));
  scrubT = t >= endT - .3 ? null : t;
  paintTlStats(); kick();
}

/* ---------- critical path ---------- */
function critical(t) {
  const agents = t.nodes.filter(n => n.role === "agent");
  const now = t.now || J.now();
  const endOf = n => Math.max(n.end || (n.start ? now : 0), ...(n.children || []).map(c => c.end || (c.start ? now : 0)));
  const durOf = n => n.start ? Math.max(0, endOf(n) - n.start) : 0;
  const deps = new Map(agents.map(x => [x.id, x.depends_on || []]));
  const byId = new Map(agents.map(n => [n.id, n]));
  let path = [];
  if ([...deps.values()].some(d => d.length)) {
    const memo = new Map();
    const best = (id, seen = new Set()) => {
      if (memo.has(id)) return memo.get(id);
      const n = byId.get(id); if (!n || seen.has(id)) return {len: 0, chain: []};
      seen.add(id);
      let top = {len: 0, chain: []};
      for (const d of deps.get(id) || []) { const b = best(d, seen); if (b.len > top.len) top = b; }
      const r = {len: top.len + durOf(n), chain: [...top.chain, id]}; memo.set(id, r); return r;
    };
    path = agents.map(n => best(n.id)).sort((a, b) => b.len - a.len)[0]?.chain || [];
  } else {
    for (const ph of (t.columns || []).map(c => c.phase)) { const inPh = agents.filter(n => n.phase === ph); if (inPh.length) path.push(inPh.sort((a, b) => durOf(b) - durOf(a))[0].id); }
  }
  const set = new Set(path);
  for (const n of agents) if (set.has(n.id)) for (const c of n.children || []) set.add(c.id);
  return {ids: set, chain: path, durOf};
}

/* ---------- indexes over the trace: tool calls per card ---------- */
function indexTrace() {
  toolIdx = new Map(); spanIdx = new Map();
  if (!trace?.rows) return;
  const by = new Map(trace.rows.map(r => [r.id, r]));
  const top = r => { let c = r; for (let i = 0; i < 8 && c?.parent; i++) c = by.get(c.parent); return c?.id; };
  const add = (k, r) => {
    let m = toolIdx.get(k); if (!m) toolIdx.set(k, m = {calls: [], cats: new Map()});
    m.calls.push(r);
    const c = toolCat(r.tool), e = m.cats.get(c.label) || {label: c.label, ic: c.ic, n: 0}; e.n++; m.cats.set(c.label, e);
  };
  for (const r of trace.rows) {
    if (r.kind !== "tool") continue;
    spanIdx.set(r.id, {start: r.start, end: r.end});
    const k = top(r); if (k) add(k, r);
    add("orch", r);
  }
}
const catsOf = id => [...(toolIdx.get(id)?.cats.values() || [])].sort((a, b) => b.n - a.n);

/* ---------- graph (top-down orchestrator -> phase columns -> task cards) ---------- */
const PAD = 28, TOP = 26;
function pill(k, n) {
  const word = k === "fb" ? (n.kind === "fallback" ? "Fallback" : "Retry") : WORD[k] || n.status;
  return `<span class="gn-pill p-${k}">${k === "run" ? `<span class="spin">${ico("spin", "xs")}</span>` : ""}${esc(word)}</span>`;
}
function brow(n, first) {
  const txt = n.role === "orchestrator" ? "Orchestrator" : n.role === "attempt" ? (n.kind === "fallback" ? "Fallback" : "Retry") : PHASE[n.phase] || n.phase || n.role;
  return `<div class="gn-brow${first ? " first" : ""}" style="left:${n.x}px;top:${n.y - 23}px">${esc(String(txt).toUpperCase())}</div>`;
}
function nodeHtml(n, crit, on) {
  const k = sk(n.state || n.status), id = esc(n.id);
  const cats = catsOf(n.role === "orchestrator" ? "orch" : n.id).slice(0, 3);
  const model = n.chip && n.chip !== "—" ? n.chip : toolName(n.tool);
  return `<button class="gn r-${esc(n.role)} s-${k}${crit.ids.has(n.id) ? " crit" : ""}${selNode === n.id ? " sel" : ""}${on.has(n.id) ? " on" : ""}${hover === n.id ? " hot" : ""}" type="button" data-node="${id}" data-key="gn:${id}"
      style="left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px" aria-label="${esc(`${n.name}: ${WORD[k] || n.status}`)}" aria-pressed="${selNode === n.id}">
    <span class="gn-top"><span class="gn-ico">${n.tool ? J.toolMono(n.tool) : `<span class="gn-hub">${ico("hub", "xs")}</span>`}</span><span class="gn-name">${esc(String(n.name).replace(/\s*↪\s*/g, " to "))}</span>${pill(k, n)}</span>
    ${n.role === "attempt" ? "" : `<span class="gn-sum">${esc(n.summary || "")}</span>`}
    <span class="gn-foot">${cats.length ? `<span class="gn-tools">${cats.map(c => `<span class="gn-tb" title="${esc(`${c.label} · ${c.n}`)}">${ico(c.ic, "xs")}</span>`).join("")}</span>` : ""}<span class="gn-tool">${esc(model)}</span><span class="gn-d num" data-lv="cd:${id}"></span></span>
  </button>`;
}
function edgePath(pts) {
  const r = 8; let d = `M${pts[0][0]} ${pts[0][1]}`;
  for (let i = 1; i < pts.length; i++) {
    const [x, y] = pts[i], next = pts[i + 1];
    if (!next) { d += `L${x} ${y}`; break; }
    const [px, py] = pts[i - 1], [nx, ny] = next;
    const l1 = Math.hypot(x - px, y - py), l2 = Math.hypot(nx - x, ny - y), rr = Math.min(r, l1 / 2, l2 / 2);
    const ax = x - (x - px) / (l1 || 1) * rr, ay = y - (y - py) / (l1 || 1) * rr, bx = x + (nx - x) / (l2 || 1) * rr, by = y + (ny - y) / (l2 || 1) * rr;
    d += `L${ax.toFixed(1)} ${ay.toFixed(1)}Q${x} ${y} ${bx.toFixed(1)} ${by.toFixed(1)}`;
  }
  return d;
}
/* the selected card and the cards its edges run through */
function pathTo(t, id) {
  const on = new Set(); if (!id) return on;
  on.add(id);
  const nodes = t.nodes || [];
  const parent = nodes.find(n => (n.children || []).some(c => c.id === id));
  if (parent) on.add(parent.id);
  for (const a of [...on]) {
    const n = nodes.find(x => x.id === a);
    const first = n && n.role === "agent" ? nodes.find(x => (x.role === "agent" || x.kind === "hub") && x.phase === n.phase) : null;
    if (first) on.add(first.id);
  }
  return on;
}
function graphHtml(t, crit) {
  const nodes = (t.nodes || []).map(n => ({...n, x: n.x + PAD, y: n.y + TOP + PAD}));
  const W = (t.size?.w || 600) + PAD * 2, H = (t.size?.h || 300) + PAD * 2 + TOP;
  const on = pathTo(t, selNode);
  const firstOfPhase = new Set();
  const seen = new Set();
  for (const n of nodes) if (n.role !== "attempt" && n.role !== "orchestrator" && !seen.has(n.phase)) { seen.add(n.phase); firstOfPhase.add(n.id); }
  const edges = (t.edges || []).map(e => {
    const pts = (e.points || []).map(([x, y]) => [x + PAD, y + TOP + PAD]);
    if (pts.length < 2) return "";
    const d = edgePath(pts), s = sk(e.status), hit = on.has(e.to), [ex, ey] = pts[pts.length - 1];
    return `<path class="ge s-${s}${crit.ids.has(e.to) ? " crit" : ""}${hit ? " on" : ""}" d="${d}"/>${s === "run" ? `<path class="ge flow${hit ? " on" : ""}" d="${d}"/>` : ""}<circle class="gp end${hit ? " on" : ""}" cx="${ex}" cy="${ey}" r="2.6"/>`;
  }).join("");
  const ports = nodes.map(n => `<circle class="gp${on.has(n.id) ? " on" : ""}" cx="${n.x + 12}" cy="${n.y + n.h}" r="3.6"/>`).join("");
  const orch = nodes.find(n => n.role === "orchestrator");
  const oPort = orch ? `<circle class="gp hub" cx="${orch.x + orch.w / 2}" cy="${orch.y + orch.h}" r="3.6"/>` : "";
  return `<div class="gsizer" style="width:${(W * zoom).toFixed(0)}px;height:${(H * zoom).toFixed(0)}px"><div class="gcanvas${selNode ? " has-sel" : ""}" style="width:${W}px;height:${H}px;transform:scale(${zoom})">
    <svg class="gedges" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">${edges}${ports}${oPort}</svg>
    ${nodes.map(n => brow(n, firstOfPhase.has(n.id)) + nodeHtml(n, crit, on)).join("")}</div></div>`;
}

/* ---------- card detail panel: Overview / Tasks / Context ---------- */
function briefSteps(n) {
  const text = String(n.prompt || n.summary || "").trim();
  if (!text) return [];
  let parts = text.split(/\n+/).map(s => s.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, "").trim()).filter(Boolean);
  if (parts.length === 1) parts = parts[0].split(/(?<=[.!?])\s+(?=[A-Z(`"'])/).map(s => s.trim()).filter(Boolean);
  return parts.slice(0, 12).map(s => clip(s, 260));
}
function filesOf(n, calls) {   // file names only, the project's own files: no temp or scratch paths
  const out = [];
  const add = f => { if (!f || SCRATCH.test(f)) return; const b = baseName(f); if (b && !out.includes(b)) out.push(b); };
  for (const f of n.files || []) add(f);
  for (const c of calls) { const [, , key, f] = plain(c.tool, c.args); if (key === "edit") add(f); }
  return out.slice(0, 10);
}
const NOW_HEAD = {run: "Working now", need: "Needs you", done: "Finished", fail: "Stopped with an error", queue: "Not started yet", fb: "Retrying", stopped: "Skipped"};
function sideHtml(t) {
  const n = nodeMap.get(selNode); if (!n) return "";
  const id = esc(n.id), k = sk(n.state || n.status), isOrch = n.role === "orchestrator";
  const agents = (t.nodes || []).filter(x => x.role === "agent");
  const ti = toolIdx.get(isOrch ? "orch" : n.id), calls = ti?.calls || [], cats = catsOf(isOrch ? "orch" : n.id);
  const steps = briefSteps(n);
  const deps = (n.depends_on || []).map(d => nodeMap.get(d)).filter(Boolean);
  const kids = n.children || [];
  const tasksN = isOrch ? agents.length : steps.length;
  const sub = isOrch ? `${toolName(n.tool)} · ${agents.length} agent${agents.length === 1 ? "" : "s"}` : [toolName(n.tool), PHASE[n.phase] || n.phase || n.role].filter(Boolean).join(" · ");
  const title = J.makeTitle(t.title) || t.title || "";
  const liveAgent = n.run_id && (J.S?.agents || []).find(a => a.id === n.run_id);
  const item = c => `<button type="button" class="sd-item" data-node="${esc(c.id)}">
      <span class="sd-i-ico">${c.tool ? J.toolMono(c.tool) : ico("hub")}</span>
      <span class="sd-i-main"><b>${esc(c.name)}</b><span>${esc(c.summary || c.chip || "")}</span></span>${pill(sk(c.state || c.status), c)}</button>`;
  let body = "";
  if (sideTab === "tasks") {
    if (isOrch) {
      body = `<div class="sd-sec"><div class="sd-sh">Goal</div><p class="sd-goal">${esc(title)}</p></div>
        <div class="sd-sec"><div class="sd-sh">Tasks it handed out</div>${agents.length ? `<div class="sd-list">${agents.map(item).join("")}</div>` : `<div class="sd-empty">No tasks yet: the planner is still splitting the goal.</div>`}</div>`;
    } else {
      const doneAll = k === "done";
      body = `<div class="sd-sec"><div class="sd-sh">Brief</div>${steps.length ? `<ol class="sd-steps">${steps.map((s, i) => `<li class="${doneAll ? "ok" : ""}"><span class="sd-n">${doneAll ? ico("check", "xs") : i + 1}</span><span>${esc(s)}</span></li>`).join("")}</ol>` : `<div class="sd-empty">No brief recorded for this card.</div>`}</div>
        ${n.acceptance ? `<div class="sd-sec"><div class="sd-sh">Done when</div><p class="sd-acc">${ico("check")}<span>${esc(n.acceptance)}</span></p></div>` : ""}
        ${deps.length ? `<div class="sd-sec"><div class="sd-sh">Waits for</div><div class="sd-chips">${deps.map(d => `<button type="button" class="sd-chip" data-node="${esc(d.id)}">${J.sd(sk(d.state || d.status))}${esc(d.name)} · ${esc(clip(d.summary, 34))}</button>`).join("")}</div></div>` : ""}
        ${kids.length ? `<div class="sd-sec"><div class="sd-sh">Attempts</div><div class="sd-list">${kids.map(item).join("")}</div></div>` : ""}`;
    }
  } else if (sideTab === "context") {   // what it is doing, in plain words: no tool ids, commands or paths
    const live = !!trace?.live;
    const openCall = [...calls].reverse().find(c => c.end == null && live);
    const running = agents.filter(a => ["run", "need"].includes(sk(a.state || a.status)));
    const doing = isOrch ? (running.length ? `Coordinating ${running.map(a => a.name).join(", ")}` : t.live ? "Waiting for the next task to start" : "All tasks have finished")
      : openCall ? plain(openCall.tool, openCall.args)[0]
      : k === "need" ? "Waiting for you to answer or approve something"
      : k === "queue" ? (deps.length ? `Waiting for ${deps.map(d => d.name).join(" and ")} to finish first` : "Queued: it starts as soon as a lane frees up")
      : k === "done" ? "Finished its part of the work"
      : k === "fail" ? "Stopped before it finished"
      : n.activity && !/^done$/i.test(n.activity) ? plainActivity(n.activity) : "Working on its task";
    const lines = isOrch ? [] : story(calls, live).slice(-6).reverse();
    const files = isOrch ? [] : filesOf(n, calls);
    const model = n.chip && n.chip !== "—" ? `${toolName(n.tool)} ${n.chip.replace(/ · agy$/, "")}` : toolName(n.tool);
    const foot = isOrch ? "" : [`Running on ${model}`, n.steps ? `${n.steps} steps so far` : ""].filter(Boolean).join(" · ");
    body = `<div class="sd-now s-${k}"><div class="sd-now-h">${k === "run" ? `<span class="sd-pulse" aria-hidden="true"></span>` : J.sd(k)}<span>${NOW_HEAD[k] || "Status"}</span><span class="num" data-lv="dur:${id}"></span></div>
        <p>${esc(doing)}</p>${!isOrch && n.summary ? `<span class="sd-now-for">Task: ${esc(n.summary)}</span>` : ""}</div>
      ${isOrch ? `<div class="sd-sec"><div class="sd-sh">Progress</div><div class="sd-prog"><span class="bar"><i style="width:${J.pct(t.summary?.done || 0, t.summary?.total || 1)}%"></i></span><span>${t.summary?.done ?? 0} of ${t.summary?.total ?? 0} tasks done</span></div></div>
        ${running.length ? `<div class="sd-sec"><div class="sd-sh">Working right now</div><div class="sd-list">${running.map(a => { const ac = [...(toolIdx.get(a.id)?.calls || [])].reverse().find(c => c.end == null && live); return `<button type="button" class="sd-item" data-node="${esc(a.id)}"><span class="sd-i-ico">${a.tool ? J.toolMono(a.tool) : ico("hub")}</span><span class="sd-i-main"><b>${esc(a.name)}</b><span>${esc(ac ? plain(ac.tool, ac.args)[0] : a.summary || "")}</span></span></button>`; }).join("")}</div></div>` : ""}` : ""}
      ${lines.length ? `<div class="sd-sec"><div class="sd-sh">What it has been doing</div><div class="sd-story">${lines.map(g => `<div class="sd-line${g.open ? " open" : ""}${g.bad ? " bad" : ""}"><span class="sd-dot" aria-hidden="true"></span><span class="sd-l-t">${esc(g.text)}${g.bad ? ` <span class="sd-l-bad">· hit an error</span>` : ""}</span></div>`).join("")}</div></div>` : ""}
      ${files.length ? `<div class="sd-sec"><div class="sd-sh">Files it is working on</div><div class="sd-chips">${files.map(f => `<span class="sd-tag">${ico("file", "xs")}${esc(f)}</span>`).join("")}</div></div>` : ""}
      ${plainRoute(n.route_note) ? `<p class="sd-note">${ico("route")}<span>${esc(plainRoute(n.route_note))}</span></p>` : ""}
      ${foot ? `<p class="sd-foot">${esc(foot)}</p>` : ""}`;
  } else {
    const parent = (t.nodes || []).find(x => (x.children || []).some(c => c.id === n.id));
    const by = isOrch ? null : parent || nodeMap.get("orch");
    body = `<p class="sd-desc">${esc(isOrch ? title : n.summary || title)}</p>
      <div class="sd-grid">
        <div><span>Time</span><b class="num" data-lv="dur:${id}"></b></div>
        <div><span>Tokens</span><b class="num" data-lv="tok:${id}"></b></div>
        <div><span>Cost</span><b class="num" data-lv="cost:${id}"></b></div>
        <div><span>${isOrch ? "Agents" : "Steps"}</span><b class="num">${isOrch ? agents.length : calls.length}</b></div>
      </div>
      <div class="sd-sec"><div class="sd-sh">Tools</div>${cats.length ? `<div class="sd-tools">${cats.slice(0, 6).map(c => `<div class="sd-trow">${ico(c.ic)}<span>${esc(c.label)}</span><span class="num">${c.n}</span></div>`).join("")}</div>` : `<div class="sd-empty">${k === "queue" ? "Not started yet." : "No tool calls recorded."}</div>`}</div>
      <div class="sd-sec"><div class="sd-sh">Started by</div>${by ? `<button type="button" class="sd-by" data-node="${esc(by.id)}"><span class="sd-by-ico">${by.tool ? J.toolMono(by.tool) : ico("hub")}</span><b>${esc(by.name)}</b><span>${esc(clip(by.role === "orchestrator" ? title : by.summary, 40))}</span></button>`
        : `<div class="sd-by"><span class="sd-by-ico you">${ico("user")}</span><b>You</b><span>${esc(t.project ? `in ${t.project}` : "from the command bar")}</span></div>`}</div>
      ${liveAgent ? `<div class="sd-actions"><a class="btn sm ghost" href="#/agents/${encodeURIComponent(n.run_id)}">Open full agent log ${ico("chev", "xs")}</a></div>` : ""}`;
  }
  return `<div class="sd-head">
      <div class="sd-title-wrap"><span class="sd-logo">${n.tool ? J.toolMono(n.tool) : ico("hub")}</span><div><h3>${esc(n.name)}</h3><div class="sd-sub">${esc(sub)}</div></div></div>
      <div class="sd-head-r">${pill(k, n)}<button type="button" class="btn icon sm ghost" data-sd-close aria-label="Close details"><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg></button></div>
    </div>
    <div class="sd-tabs" role="tablist" aria-label="Card details">
      <button type="button" role="tab" data-sd-tab="overview" aria-selected="${sideTab === "overview"}">Overview</button>
      <button type="button" role="tab" data-sd-tab="tasks" aria-selected="${sideTab === "tasks"}">Tasks${tasksN ? ` <span class="num">${tasksN}</span>` : ""}</button>
      <button type="button" role="tab" data-sd-tab="context" aria-selected="${sideTab === "context"}">Context</button>
    </div>
    <div class="sd-body" role="tabpanel">${body}</div>`;
}
function paintSide() {
  const box = q("#wf-side"), body = q(".wf-body"); if (!box) return;
  const html = selNode && tree ? sideHtml(tree) : "";
  if (!html) { box.hidden = true; body?.classList.remove("side-open"); J.setHTML(box, ""); return; }
  if (box.hidden) { box.hidden = false; box.classList.remove("in"); void box.offsetWidth; box.classList.add("in"); }
  body?.classList.add("side-open");
  J.setHTML(box, html);
}

/* ---------- bottom panel tabs: Tokens & cost + Events ---------- */
function usageTabHtml(t) {
  const u = t.usage || {}, agents = u.agents || [], models = u.models || [];
  if (!agents.length && !models.length) return `<div class="empty">No token or cost data recorded yet.</div>`;
  return `<div class="tl-usage">
    ${agents.length ? `<table class="tbl"><thead><tr><th>Agent</th><th>Model</th><th class="r">Tokens</th><th class="r">Cost</th></tr></thead><tbody>
      ${agents.map(a => `<tr data-node="${esc(a.node)}"><td>${esc(a.name)}</td><td><span class="chip mono">${esc(a.chip)}</span></td><td class="r num">${a.tokens != null ? fmtK(a.tokens) : "—"}</td><td class="r num">${a.cost != null ? fmtUsd(a.cost) : "—"}</td></tr>`).join("")}
    </tbody></table>` : ""}
    ${models.length ? `<table class="tbl"><thead><tr><th>Model</th><th class="r">Input</th><th class="r">Output</th><th class="r">Cache</th><th class="r">Cost</th></tr></thead><tbody>
      ${models.map(m => `<tr><td>${esc(m.chip || m.model)}</td><td class="r num">${fmtK(m.input)}</td><td class="r num">${fmtK(m.output)}</td><td class="r num">${fmtK((m.cache_read || 0) + (m.cache_write || 0))}</td><td class="r num">${m.cost != null ? fmtUsd(m.cost) : "—"}</td></tr>`).join("")}
    </tbody></table>` : ""}
  </div>`;
}
function eventsTabHtml(t) {
  const ev = (t.events || []).slice().sort((a, b) => b.ts - a.ts);
  const EV = {done: "done", start: "run", plan: "queue", fail: "fail", failed: "fail", retry: "fb", fallback: "fb", needs_you: "need", review: "queue", stopped: "stopped"};
  if (!ev.length) return `<div class="empty">No events recorded yet.</div>`;
  return `<div class="ledger">${ev.map(e => `<div class="lg" data-node="${esc(e.node || "")}">${J.sd(EV[e.kind] || "queue")}<span class="lg-t">${esc(e.text)}</span><span class="num faint">${J.clockS(e.ts)}</span></div>`).join("")}</div>`;
}

/* ---------- pipeline strip (auto pipelines only) ---------- */
const PIPE_STEPS = [["research", "Researcher"], ["analysis", "Data analyst"], ["planning", "Project-manager"], ["build", "Coder"],
  ["fact_check", "Fact-checker"], ["test", "Tester"], ["review", "Code reviewer"], ["fix", "Fix"]];
const VERDICT = {pass: "green", fail: "red", unknown: "", skipped: ""};
function pipeHtml(pl) {
  if (!pl) return "";
  const end = pl.phase === "done" || pl.phase === "failed", cur = pl.phase === "fix" ? "fix" : pl.phase;
  const order = PIPE_STEPS.map(x => x[0]), ci = order.indexOf(cur);
  const ran = k => cur === k || (pl.rounds || []).some(r => r[k] && r[k] !== "skipped");
  const show = k => k === "fix" ? pl.round > 0 || cur === "fix"            // optional roles appear once they have run
    : k === "analysis" ? cur === k || !!pl.analysis : k === "fact_check" || k === "test" ? ran(k) : true;
  const steps = PIPE_STEPS.filter(([k]) => show(k)).map(([k, label]) => {
    const i = order.indexOf(k), state = pl.phase === "done" ? "done" : pl.phase === "failed" ? (i < ci || ci < 0 ? "done" : i === ci ? "fail" : "queue") : i < ci ? "done" : i === ci ? "run" : "queue";
    return `<span class="pl-step s-${state}" data-key="pl:${k}">${J.sd(state)}${label}</span>`;
  }).join("");
  const chip = (word, v) => v && v !== "skipped" ? `<span class="chip ${VERDICT[v] || ""}">${word} ${esc(v)}</span>` : "";
  const rounds = (pl.rounds || []).map(r => `<span class="pl-round" data-key="plr:${r.round}"><span class="faint">Round ${r.round + 1}</span>
      ${chip("fact-check", r.fact_check)}${chip("test", r.test)}${chip("review", r.review)}</span>`).join("");
  const label = pl.phase === "done" ? "Pipeline done" : pl.phase === "failed" ? "Pipeline failed" : "Pipeline";
  return `<div class="wf-pipe"><div class="pl-line"><b>${label}</b>${steps}${end ? "" : `<span class="faint num">round ${(pl.round || 0) + 1} / ${(pl.max_rounds || 0) + 1}</span>`}</div>
    ${rounds ? `<div class="pl-line">${rounds}</div>` : ""}${pl.note ? `<div class="pl-note">${esc(pl.note)}</div>` : ""}</div>`;
}

/* ---------- job tabs: one tab per complex job (GET /api/jobs), "All" keeps the combined overview ---------- */
/* Status, colour and progress come from the server (hub/jobstatus.py + hub/jobs_api.py), the same source as the pane
   headers and the combined report. Tabs are patched in place so a poll never rebuilds them: no flicker, the strip keeps
   its scroll position and the progress bar animates from its old width. */
const HK = {running: "run", waiting: "need", done: "done", failed: "fail"};   // header kind -> the tab colour state
const DKEY = "jv.wf.dismissed";
let jobs = [], jobsAt = 0, jobsBusy = false, jobsLoaded = false, jobsSeq = 0, order = [], everTab = new Set(), lastSel = null;
let stages = null, stagesAt = 0, stagesBusy = false;
const tabEls = new Map();
const dismissed = (() => { try { return new Set(JSON.parse(localStorage.getItem(DKEY) || "[]")); } catch (e) { return new Set(); } })();
const saveDismissed = () => { try { localStorage.setItem(DKEY, JSON.stringify([...dismissed])); } catch (e) { /* storage blocked: dismissals last for this page only */ } };
const visibleJobs = () => { const by = new Map(jobs.filter(j => !dismissed.has(j.key)).map(j => [j.key, j])); return order.filter(k => by.has(k)).map(k => by.get(k)); };
const selJob = () => visibleJobs().find(j => j.id === selId) || null;
const setAttr = (el, k, v) => { if (v == null || v === false) { if (el.hasAttribute(k)) el.removeAttribute(k); } else if (el.getAttribute(k) !== String(v)) el.setAttribute(k, v); };

function makeTab(key) {
  const el = document.createElement("div");
  el.className = "job-tab" + (key === "all" ? " tab-all" : ""); el.setAttribute("role", "tab"); el.dataset.tab = key; el.dataset.key = "tab:" + key;
  el.innerHTML = key === "all" ? `<span class="lbl">All</span><span class="tab-badge"></span>`
    : `<span class="ico"></span><span class="lbl"></span><span class="tab-badge" aria-hidden="true"></span><button class="tab-close" type="button" tabindex="-1" hidden><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg></button><span class="tab-progress" aria-hidden="true"><i></i></span>`;
  return el;
}
function fillTab(el, j, i) {
  const all = j.key === "all", sel = all ? !selJob() : selJob()?.key === j.key;
  setAttr(el, "aria-selected", sel ? "true" : "false"); setAttr(el, "tabindex", sel ? "0" : "-1");
  if (all) { const b = el.querySelector(".tab-badge"); const n = String(visibleJobs().length); if (b.textContent !== n) b.textContent = n; return; }
  const st = HK[j.header] || "run", fin = st === "done" || st === "fail";
  setAttr(el, "data-s", st);
  setAttr(el, "title", `${j.title} · ${j.agent || "agent"} · ${j.status === "waiting" ? "needs you" : j.status}${j.total ? ` · ${j.done}/${j.total} steps` : j.steps ? ` · ${j.steps} steps` : ""}${j.current_step ? " · " + j.current_step : ""}`);
  setAttr(el, "aria-label", `${j.title}, ${j.agent || "agent"}, ${j.status === "waiting" ? "needs you" : j.status}${j.pct != null ? `, ${j.pct}%` : ""}`);
  if (el._tool !== j.tool) { el._tool = j.tool; el.querySelector(".ico").innerHTML = J.toolMono(j.tool || "?"); }
  const lbl = el.querySelector(".lbl"), t = clip(j.title, 26); if (lbl.textContent !== t) lbl.textContent = t;
  const badge = el.querySelector(".tab-badge"), k = i < 9 ? String(i + 1) : ""; if (badge.textContent !== k) badge.textContent = k; badge.hidden = !k;
  const x = el.querySelector(".tab-close"); x.hidden = false; setAttr(x, "aria-label", `${fin ? "Dismiss" : "Hide"} ${j.title}`);
  setAttr(x, "title", fin ? "Dismiss this tab" : "Hide this tab (the job keeps running; stop it from the header)");
  const bar = el.querySelector(".tab-progress"); bar.classList.toggle("indet", j.pct == null && !fin);
  const w = (fin ? 100 : j.pct ?? 100) + "%"; const fill = bar.firstElementChild; if (fill.style.width !== w) fill.style.width = w;   // the CSS transition animates the change
}
function arrows() {
  const box = q("#wf-tabs"), prev = q("#wf-tab-prev"), next = q("#wf-tab-next"); if (!box || !prev) return;
  const more = box.scrollWidth - box.clientWidth > 4;
  prev.hidden = next.hidden = !more;
  prev.disabled = box.scrollLeft < 3; next.disabled = box.scrollLeft + box.clientWidth >= box.scrollWidth - 3;
}
function paintTabs() {
  const box = q("#wf-tabs"); if (!box || J.shell.current() !== "workflows") return;
  const items = [{key: "all"}, ...visibleJobs()], want = new Set(items.map(j => j.key));
  for (const [k, e] of tabEls) if (!want.has(k)) { e.remove(); tabEls.delete(k); }
  let prev = null;
  items.forEach((j, i) => {
    let e = tabEls.get(j.key); if (!e) { e = makeTab(j.key); tabEls.set(j.key, e); }
    const at = prev ? prev.nextSibling : box.firstChild; if (at !== e) box.insertBefore(e, at);
    fillTab(e, j, i - 1); prev = e;
  });
  let empty = box.querySelector(".tab-strip-empty");
  if (jobsLoaded && items.length === 1) { if (!empty) { empty = document.createElement("span"); empty.className = "tab-strip-empty"; empty.setAttribute("role", "status"); empty.textContent = "No complex jobs running"; box.append(empty); } }
  else empty?.remove();
  const selKey = selJob()?.key || "all";
  if (selKey !== lastSel) { lastSel = selKey; const t = tabEls.get(selKey); if (t) { const b = box.getBoundingClientRect(), r = t.getBoundingClientRect(); if (r.left < b.left) box.scrollBy({left: r.left - b.left - 8}); else if (r.right > b.right) box.scrollBy({left: r.right - b.right + 8}); } }
  arrows();
}
function selectTab(key) {
  const j = key === "all" ? null : visibleJobs().find(x => x.key === key);
  if (key !== "all" && !j) return;
  const hash = j ? "#/workflows/" + encodeURIComponent(j.id) : "#/workflows";
  if (location.hash !== hash) location.hash = hash; else paintTabs();
}
function dismiss(key) {
  dismissed.add(key); saveDismissed();
  if (selJob() === null && selId && everTab.has(selId)) { selId = null; if (location.hash !== "#/workflows") location.hash = "#/workflows"; }
  paintTabs(); paintStages();
}
async function loadJobs(force) {
  if (jobsBusy || (!force && Date.now() - jobsAt < 2500)) return;
  jobsBusy = true; jobsAt = Date.now(); const seq = ++jobsSeq;
  try {
    const r = await J.api("/api/jobs");
    if (seq !== jobsSeq) return;
    jobs = r.jobs || []; jobsLoaded = true;
    const have = new Set(jobs.map(j => j.key));
    order = order.filter(k => have.has(k));
    for (const j of jobs.slice().sort((a, b) => (a.started || 0) - (b.started || 0))) if (!order.includes(j.key)) order.push(j.key);   // new tabs go last: the strip never reshuffles
    for (const k of [...dismissed]) if (!have.has(k)) dismissed.delete(k);
    saveDismissed();
    for (const j of jobs) everTab.add(j.id);
    if (selId && everTab.has(selId) && !selJob()) { selId = null; if (location.hash !== "#/workflows") location.hash = "#/workflows"; }   // its job was closed or dismissed: its tab is gone for good
  } catch (e) { /* keep the tabs we have; the next poll retries */ }
  jobsBusy = false;
  paintTabs(); loadStages();
}
async function loadStages(force) {
  const j = selJob();
  if (!j) { if (stages) { stages = null; paintStages(); } return; }
  if (stagesBusy || (!force && Date.now() - stagesAt < 2500)) return;
  stagesBusy = true; stagesAt = Date.now();
  try {
    const r = await J.api(`/api/jobs/${encodeURIComponent(j.id)}?job_id=${encodeURIComponent(j.job_id || "")}`);
    stages = selJob()?.key === j.key ? r : stages;
  } catch (e) { stages = null; jobsAt = 0; }   // closed or replaced by a newer job in that pane: the next jobs poll drops its tab
  stagesBusy = false;
  paintStages();
}
function stageState(steps) {
  const ss = steps.map(x => sk(x.status));
  return ss.includes("fail") ? "fail" : ss.includes("need") ? "need" : ss.includes("run") ? "run" : ss.length && ss.every(x => x === "done") ? "done" : "queue";
}
function paintStages() {
  const box = q("#wf-stages"); if (!box) return;
  const j = selJob();
  if (!j || !stages || stages.job?.key !== j.key) { J.setHTML(box, ""); return; }
  const line = stages.stages.map(st => { const k = stageState(st.steps), d = st.steps.filter(x => sk(x.status) === "done").length;
    return `<span class="pl-step s-${k}" data-key="stg:${esc(st.phase)}">${J.sd(k)}${esc(st.label)} <span class="num faint">${d}/${st.steps.length}</span></span>`; }).join("");
  const cur = stages.job.current_step;
  J.setHTML(box, `<div class="wf-pipe"><div class="pl-line"><b>${esc(stages.job.agent || "Job")}</b>${line}</div>${cur ? `<div class="pl-note">Now: ${esc(plainActivity(cur))}</div>` : ""}</div>`);
}

/* ---------- library ---------- */
function libHtml() {
  const plans = (J.S?.queue?.plans || []).filter(p => p.status !== "cancelled");
  const runs = (J.RUNS || []).filter(r => !plans.some(p => p.id === r.id)).slice(0, 30);
  const PST = {running: "run", draft: "queue", approved: "queue", done: "done", failed: "fail", cancelled: "stopped"};
  const cur = selId || loadedId;
  const item = (id, title, sub, s, extra) => `<a class="lib-i${id === cur ? " sel" : ""}" href="#/workflows/${encodeURIComponent(id)}" data-key="lib:${esc(id)}" ${id === cur ? 'aria-current="true"' : ""}>
      ${J.sd(s)}<span class="lib-t"><b>${esc(title)}</b><span>${esc(sub)}</span></span>${extra || ""}</a>`;
  return `<h4 class="sect-h">Plans</h4>${plans.length ? plans.map(p => { const d = p.tasks.filter(t => t.status === "done").length;
      return item(p.id, p.goal, `${p.project || "no project"} · ${p.status}`, PST[p.status] || "queue", `<span class="lib-n num">${d}/${p.tasks.length}</span>`); }).join("") : `<div class="empty">No plans yet. Use Plan in the command bar.</div>`}
    <h4 class="sect-h">Runs</h4>${runs.length ? runs.map(r => item(r.id, J.makeTitle(r.title) || r.title || r.id, `${r.project || "no project"} · ${J.dur(r.duration)}`, sk(r.status === "cancelled" ? "stopped" : r.status),
      r.cost ? `<span class="lib-n num">${J.fmtUsd(r.cost)}</span>` : "")).join("") : J.RUNS ? `<div class="empty">No runs recorded yet.</div>` : `<div class="empty">Loading…</div>`}
    <h4 class="sect-h">Sample</h4>${item("demo", "Sample workflow", "synthetic data, for a full graph", "queue")}`;
}

/* ---------- header controls: stop the agent behind a run, or cancel a plan ---------- */
const wacc = k => J.saved.get("jarviee.wacc." + k) === "1";   // diagnostics and timeline start collapsed
const LIVE_PLAN = ["draft", "approved", "running"];
function headActs(t) {
  const ag = (J.S?.agents || []).find(a => a.id === t.id);
  if (ag) return `<span class="wf-acts" data-id="${esc(ag.id)}">${J.act.html(ag)}</span>`;
  const plan = (J.S?.queue?.plans || []).find(p => p.id === t.id);
  if (plan && LIVE_PLAN.includes(plan.status)) return `<span class="wf-acts"><button class="btn sm danger" type="button" data-plan-cancel="${esc(plan.id)}" data-key="plan-cancel">
    <svg class="i sm" aria-hidden="true"><use href="#i-stop"/></svg><span>Cancel plan</span></button></span>`;
  return "";
}
let cancelArm = null, pruneArm = false;
async function pruneOld(b) {
  if (!pruneArm) {
    pruneArm = true; b.textContent = "Confirm >7d?";
    setTimeout(() => { if (pruneArm) { pruneArm = false; b.textContent = "Clear >7d"; } }, 4000);
    return;
  }
  pruneArm = false; b.disabled = true; b.textContent = "Clearing…";
  try {
    const r = await J.api("/api/plan/prune", {days: 7});
    J.toast(r.removed ? `Removed ${r.removed} old workflow${r.removed === 1 ? "" : "s"}` : "No workflows older than 7 days");
    if (selId && (r.ids || []).includes(selId)) { selId = null; location.hash = "#/workflows"; }
    J.poll(); load(true);
  } catch (e) { J.toast(e.message, true); }
  b.disabled = false; b.textContent = "Clear >7d";
}
async function cancelPlan(b) {
  const id = b.dataset.planCancel;
  if (cancelArm !== id) { cancelArm = id; b.querySelector("span").textContent = "Click again to cancel"; setTimeout(() => { if (cancelArm === id) { cancelArm = null; paint(); } }, 4000); return; }
  cancelArm = null; b.disabled = true;
  try { await J.api("/api/plan/cancel", {id}); J.toast("Plan cancelled"); J.poll(); load(true); } catch (e) { J.toast(e.message, true); b.disabled = false; }
}

/* ---------- data ---------- */
function pick() {
  if (selId) return selId;
  const plans = J.S?.queue?.plans || [];
  return plans.find(p => p.status === "running")?.id || (J.RUNS || []).find(r => r.status === "running" && r.kind === "plan")?.id || plans[0]?.id || (J.RUNS || [])[0]?.id || (J.RUNS ? "demo" : null);
}
function absorb() {   // fresh tree + trace: index them and hand the new numbers to the easing
  nodeMap = new Map((tree?.nodes || []).map(n => [n.id, n]));
  indexTrace();
  for (const n of tree?.nodes || []) { setNum("tok:" + n.id, n.tokens); setNum("cost:" + n.id, n.cost); }
  setNum("tok:__sum", tree?.summary?.tokens); setNum("cost:__sum", tree?.summary?.cost);
}
async function load(force) {
  const id = pick();
  if (!id || busy) return;
  if (!force && loadedId === id && Date.now() - at < (tree?.live ? 2500 : 15000)) return;
  busy = true; at = Date.now();
  try {
    const [t, tr] = await Promise.all([J.api(`/api/run/${encodeURIComponent(id)}/tree`), J.api(`/api/run/${encodeURIComponent(id)}/trace`).catch(() => null)]);
    if (loadedId !== id) { fitted = false; hover = null; selNode = t.focus || null; scrubT = null; follow = true; tlFresh = true; nums.clear(); }
    const clean = o => o && JSON.parse(JSON.stringify(o).replace(/\s*↪\s*/g, " to ").replace(/\[(?:Image|Pasted text) #\d+[^\]]*\]\s*/g, ""));   // arrow glyphs and paste markers out of the labels
    tree = clean(t); trace = clean(tr); loadedId = id; err = "";
    absorb();
  } catch (e) { err = e.message; if (loadedId !== id) { tree = null; trace = null; loadedId = id; } }
  busy = false;
  paint();
}
function fit(both) {
  const wrap = q("#wf-graph"); if (!wrap || !tree) return;
  const side = q(".wf-body.side-open") ? Math.min(400, wrap.clientWidth * .45) : 0;
  const W = tree.size.w + PAD * 2, H = tree.size.h + PAD * 2 + TOP;
  const zx = (wrap.clientWidth - side - 24) / W, zy = (wrap.clientHeight - 70) / H;
  zoom = Math.max(.35, Math.min(1, both ? Math.min(zx, zy) : Math.max(.7, zx)));
  fitted = true;
}
function center(id) {
  const wrap = q("#wf-graph"), n = nodeMap.get(id || selNode || "orch"); if (!wrap || !n) return;
  const side = q(".wf-body.side-open") ? Math.min(400, wrap.clientWidth * .45) : 0;
  const cx = (n.x + PAD + n.w / 2) * zoom, cy = (n.y + PAD + TOP + n.h / 2) * zoom;
  wrap.scrollTo({left: cx - (wrap.clientWidth - side) / 2, top: cy - wrap.clientHeight / 2, behavior: "smooth"});
}
function paintTlStats() {
  const stats = q("#wf-tl-stats"); if (!stats || !tree) return;
  const s = tree.summary || {};
  J.setHTML(stats, `${s.total ? `<span title="Tasks done">${ico("clock")}<span class="num">${s.done ?? 0}/${s.total}</span></span>` : ""}
    <span class="num" id="wf-tl-clock" title="${scrubT != null ? "Playhead" : "Elapsed"}">${fmtClock(scrubT ?? s.elapsed)}</span>
    <span title="Tokens">${ico("spark")}<span class="num" data-lv="tok:__sum">${fmtK(numNow("tok:__sum"))}</span></span>
    ${scrubT != null ? `<button type="button" class="btn sm ghost tl-live" data-tl-live>${tree.live ? "Back to live" : "Reset"}</button>` : ""}`);
}
function paintTl() {
  const tl = q("#wf-tl"), evBadge = q("#wf-ev-cnt");
  if (!tl || !tree) return;
  const evCount = (tree.events || []).length;
  if (evBadge) { evBadge.textContent = evCount ? String(evCount) : ""; evBadge.hidden = !evCount; }
  paintTlStats();
  for (const b of el.querySelectorAll("[data-tl-tab]")) b.setAttribute("aria-selected", b.dataset.tlTab === tlTab ? "true" : "false");
  if (tlTab === "usage") { J.setHTML(tl, usageTabHtml(tree)); return; }
  if (tlTab === "events") { J.setHTML(tl, eventsTabHtml(tree)); return; }
  const old = tl.querySelector(".tl-scroll"), sl = old ? old.scrollLeft : null;
  const html = trace?.rows?.length ? J.timelineHtml(trace, {focus: hover, treeData: tree, sel: selNode}) : `<div class="empty">No timeline yet: spans appear once the agents report tool calls.</div>`;
  if (J.setHTML(tl, html)) {
    const sc = tl.querySelector(".tl-scroll");
    if (sc) {
      if (sl != null && !tlFresh) sc.scrollLeft = sl;
      sc.addEventListener("scroll", () => {   // follow the live edge only while the playhead is on screen
        const box = sc.closest(".tl2"), D = +box.dataset.d, endT = box.dataset.live ? J.now() - +box.dataset.t0 : +box.dataset.endt;
        const x = (scrubT ?? endT) / D * sc.scrollWidth;
        follow = x >= sc.scrollLeft - 4 && x <= sc.scrollLeft + sc.clientWidth + 4;
      }, {passive: true});
      if (tlFresh) { tlFresh = false; follow = true; requestAnimationFrame(() => { tlUpdate(); const box = sc.closest(".tl2"); const x = (+box.dataset.d ? (box.dataset.live ? J.now() - +box.dataset.t0 : +box.dataset.endt) / +box.dataset.d : 1) * sc.scrollWidth; sc.scrollLeft = Math.max(0, x - sc.clientWidth * .6); }); }
    }
  }
  tlUpdate();
}
function paint() {
  if (!el || J.shell.current() !== "workflows") return;
  J.setHTML(q("#wf-lib"), libHtml());
  const head = q("#wf-head"), g = q("#wf-graph"), tl = q("#wf-tl");
  if (!tree) { J.setHTML(head, `<h2>${err ? "Couldn't load this workflow" : "Loading…"}</h2>${err ? `<p class="err">${esc(err)}</p>` : ""}`); J.setHTML(g, ""); J.setHTML(tl, ""); return; }
  if (!fitted) fit(false);
  const crit = critical(tree), s = tree.summary || {};
  const st = tree.nodes.find(n => n.role === "orchestrator")?.status || "queued";
  J.setHTML(head, `<div class="wf-title"><h2>${esc(J.makeTitle(tree.title) || tree.title || tree.id)}</h2>${J.st(sk(st), tree.paused ? "paused" : st === "needs_you" ? "needs you" : st)}${tree.id === "demo" ? `<span class="chip">sample data</span>` : ""}${headActs(tree)}</div>
    <div class="wf-meta"><span>${esc(tree.kind === "plan" ? "Plan" : "Run")} · <span class="num">${esc(tree.id)}</span></span>${tree.project ? `<span>Project <b>${esc(tree.project)}</b></span>` : ""}
      <span class="wf-prog"><span class="bar"><i style="width:${J.pct(s.done || 0, s.total || 1)}%"></i></span><b class="num">${s.done ?? 0}/${s.total ?? 0}</b> done</span>
      <span>Elapsed <b class="num" data-lv="dur:__sum"></b></span><span>Tokens <b class="num" data-lv="tok:__sum"></b></span><span>Cost <b class="num"><span data-lv="cost:__sum"></span>${s.cost_partial ? "+" : ""}</b></span></div>${pipeHtml(tree.pipeline)}`);
  J.setHTML(g, graphHtml(tree, crit));
  edgeCue();
  paintSide();
  paintTl();
  q("#wf-zoom").textContent = Math.round(zoom * 100) + "%";
  fillLive(); kick();
}
/* a visible cue when the graph runs past the right edge */
function edgeCue() {
  const g = q("#wf-graph"); if (!g) return;
  g.parentElement.classList.toggle("more-right", g.scrollWidth - g.scrollLeft - g.clientWidth > 4 && !q(".wf-body.side-open"));
}
function setHover(id) {
  if (hover === id) return; hover = id;
  for (const n of el.querySelectorAll(".gn")) n.classList.toggle("hot", n.dataset.node === id);
  for (const r of el.querySelectorAll(".tl-l, .tl-r")) r.classList.toggle("hot", r.dataset.node === id);
}
function select(id) {
  if (!id || !nodeMap.has(id)) return;
  selNode = id; setHover(id);
  paint();
  const n = el.querySelector(`.gn[data-node="${CSS.escape(id)}"]`), wrap = q("#wf-graph");
  if (n && wrap) {   // bring the card into view, clear of the panel
    const r = n.getBoundingClientRect(), w = wrap.getBoundingClientRect(), side = Math.min(400, w.width * .45);
    if (r.right > w.right - side - 12 || r.left < w.left || r.top < w.top || r.bottom > w.bottom) center(id);
  }
}

J.pages.workflows = {
  init() {
    el = document.querySelector('[data-page="workflows"]');
    el.innerHTML = `
      <div class="grid wf-grid">
        <aside class="panel wf-lib-p" aria-label="Workflow library"><div class="ph"><h2>Workflow library</h2><button class="btn sm ghost" type="button" id="wf-prune" title="Remove finished, failed, or cancelled workflows older than 7 days">Clear &gt;7d</button></div><nav class="wf-lib" id="wf-lib"></nav></aside>
        <section class="panel wf-main" aria-label="Orchestration graph">
          <div class="tab-carousel wf-tabs-wrap" id="wf-tabs-wrap" aria-label="Complex jobs">
            <button class="tab-arrow" type="button" id="wf-tab-prev" aria-label="Scroll tabs left" hidden><svg class="i sm" aria-hidden="true" style="transform:rotate(180deg)"><use href="#i-chev"/></svg></button>
            <div class="tab-track" role="tablist" id="wf-tabs" aria-label="Complex jobs"></div>
            <button class="tab-arrow" type="button" id="wf-tab-next" aria-label="Scroll tabs right" hidden><svg class="i sm" aria-hidden="true"><use href="#i-chev"/></svg></button>
          </div>
          <div class="wf-head" id="wf-head"></div>
          <div id="wf-stages"></div>
          <div class="wf-body">
            <div class="wf-graph" id="wf-graph" tabindex="-1"></div>
            <div class="wf-zoomctl" role="toolbar" aria-label="Graph view">
              <button type="button" data-z="pin" aria-label="Center the selected card" title="Center the selected card">${ico("pin")}</button>
              <button type="button" data-z="fit" aria-label="Fit the graph" title="Fit the graph">${ico("fit")}</button>
              <span class="sep" aria-hidden="true"></span>
              <button type="button" data-z="-" aria-label="Zoom out">−</button><span class="num" id="wf-zoom">100%</span><button type="button" data-z="+" aria-label="Zoom in">+</button>
            </div>
            <aside class="wf-side" id="wf-side" aria-label="Card details" hidden></aside>
          </div>
        </section>
        <section class="panel wf-tl-p" aria-label="Trace timeline">
          <div class="tl-head">
            <div class="tl-tabs" role="tablist">
              <button type="button" role="tab" data-tl-tab="trace" aria-selected="true">${ico("trace")}Trace</button>
              <button type="button" role="tab" data-tl-tab="usage" aria-selected="false">${ico("db")}Tokens &amp; cost</button>
              <button type="button" role="tab" data-tl-tab="events" aria-selected="false">${ico("msg")}Events <span class="tl-ev-cnt" id="wf-ev-cnt" hidden></span></button>
            </div>
            <div class="tl-stats" id="wf-tl-stats"></div>
          </div>
          <div class="wf-tl" id="wf-tl"></div>
        </section>
      </div>`;
    let pan = null;   // drag the empty canvas to pan; a click on it closes the panel
    el.addEventListener("click", e => {
      const pr = e.target.closest("#wf-prune");
      if (pr) { pruneOld(pr); return; }
      if (e.target.closest("[data-sd-close]")) { selNode = null; paint(); return; }
      const st = e.target.closest("[data-sd-tab]");
      if (st) { sideTab = st.dataset.sdTab; paintSide(); fillLive(); return; }
      const tb = e.target.closest("[data-tl-tab]");
      if (tb) { tlTab = tb.dataset.tlTab; tlFresh = true; paintTl(); fillLive(); return; }
      if (e.target.closest("[data-tl-live]")) { scrubT = null; follow = true; paintTlStats(); fillLive(); kick(); return; }
      const tog = e.target.closest("[data-tl-tog]");
      if (tog) {
        e.stopPropagation(); const id = tog.dataset.tlTog;
        const set = tog.dataset.turn ? expanded : collapsed;
        if (set.has(id)) set.delete(id); else set.add(id);
        paintTl(); return;
      }
      const z = e.target.closest("[data-z]");
      if (z) {
        const k = z.dataset.z;
        if (k === "pin") { center(); return; }
        if (k === "fit") fit(true); else zoom = Math.max(.35, Math.min(1.6, Math.round((zoom + (k === "+" ? .1 : -.1)) * 10) / 10));
        paint(); return;
      }
      if (e.target.closest(".tl-axis2, .tl-knob")) return;
      const n = e.target.closest(".gn, .sd-item[data-node], .sd-by[data-node], .sd-chip[data-node], .lg[data-node], .tl-l[data-node], .tl-r[data-node], tr[data-node]");
      if (n) { if (n.dataset.node) { sideTab = n.matches(".sd-item, .sd-chip, .sd-by") ? sideTab : "overview"; select(n.dataset.node); } return; }
    });
    const g = q("#wf-graph");
    g.addEventListener("pointerdown", e => {
      if (e.button !== 0 || e.target.closest(".gn")) return;
      pan = {x: e.clientX, y: e.clientY, l: g.scrollLeft, t: g.scrollTop, moved: false, z: parseFloat(getComputedStyle(document.body).zoom) || 1};
      g.setPointerCapture(e.pointerId);
    });
    g.addEventListener("pointermove", e => {
      if (!pan) return;
      const dx = e.clientX - pan.x, dy = e.clientY - pan.y;
      if (!pan.moved && Math.hypot(dx, dy) < 4) return;
      pan.moved = true; g.classList.add("panning");
      g.scrollLeft = pan.l - dx / pan.z; g.scrollTop = pan.t - dy / pan.z;
    });
    const endPan = () => { if (!pan) return; const moved = pan.moved; pan = null; g.classList.remove("panning"); if (!moved && selNode) { selNode = null; paint(); } };
    g.addEventListener("pointerup", endPan); g.addEventListener("pointercancel", () => { pan = null; g.classList.remove("panning"); });
    // trace playhead: drag the axis or the knob; arrows nudge it 10 s (30 s with shift)
    const tlBox = q("#wf-tl");
    tlBox.addEventListener("pointerdown", e => {
      if (e.button !== 0 || !e.target.closest(".tl-axis2, .tl-knob, .tl-ph")) return;
      drag = true; tlBox.setPointerCapture(e.pointerId); tlBox.classList.add("dragging"); scrubFrom(e); e.preventDefault();
    });
    tlBox.addEventListener("pointermove", e => { if (drag) scrubFrom(e); });
    const endDrag = () => { if (!drag) return; drag = false; tlBox.classList.remove("dragging"); paintTlStats(); fillLive(); kick(); };
    tlBox.addEventListener("pointerup", endDrag); tlBox.addEventListener("pointercancel", endDrag);
    tlBox.addEventListener("keydown", e => {
      if (!e.target.closest(".tl-knob")) return;
      const box = q("#wf-tl .tl2"); if (!box) return;
      const endT = box.dataset.live ? J.now() - +box.dataset.t0 : +box.dataset.endt, cur = scrubT ?? endT, step = e.shiftKey ? 30 : 10;
      let t = null;
      if (e.key === "ArrowLeft") t = cur - step; else if (e.key === "ArrowRight") t = cur + step; else if (e.key === "Home") t = 0; else if (e.key === "End") t = endT;
      if (t == null) return;
      e.preventDefault(); t = Math.max(0, Math.min(endT, t)); scrubT = t >= endT - .3 ? null : t; paintTlStats(); kick();
    });
    const tabs = q("#wf-tabs");
    tabs.addEventListener("click", e => {
      const x = e.target.closest(".tab-close"); if (x) { e.stopPropagation(); dismiss(x.closest("[data-tab]").dataset.tab); return; }
      const t = e.target.closest("[data-tab]"); if (t) selectTab(t.dataset.tab);
    });
    tabs.addEventListener("scroll", arrows, {passive: true});
    new ResizeObserver(arrows).observe(tabs);
    q("#wf-tab-prev").addEventListener("click", () => tabs.scrollBy({left: -tabs.clientWidth * .8}));
    q("#wf-tab-next").addEventListener("click", () => tabs.scrollBy({left: tabs.clientWidth * .8}));
    paintTabs();
    g.addEventListener("scroll", edgeCue, {passive: true});
    new ResizeObserver(edgeCue).observe(g);
    el.addEventListener("pointerover", e => { const n = e.target.closest(".gn, .tl-l, .tl-r"); if (n?.dataset.node) setHover(n.dataset.node); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) kick(); });
  },
  show(params) {
    const id = params[0] || null;
    if (id !== selId) { selId = id; fitted = false; stages = null; }
    load(true); paintTabs(); paintStages(); loadJobs(true); loadStages(true); kick();
  },
  render(ev) { if (ev === "state" || ev === "runs") { load(); paint(); } },
  tick() { if (tree?.live) load(); loadJobs(); kick(); },
  key(e) {   // Esc closes the card panel; left/right switch tabs, 1-9 jump to a job tab, 0 to All
    if (e.key === "Escape" && selNode && !e.target.closest?.("input, textarea, select")) { selNode = null; paint(); e.preventDefault(); return; }
    if (e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return;
    const inStrip = e.target.closest?.("#wf-tabs-wrap");
    if (e.target !== document.body && !inStrip) return;
    const keys = ["all", ...visibleJobs().map(j => j.key)], at = Math.max(0, keys.indexOf(selJob()?.key || "all"));
    let to = null;
    if (e.key === "ArrowLeft") to = keys[Math.max(0, at - 1)];
    else if (e.key === "ArrowRight") to = keys[Math.min(keys.length - 1, at + 1)];
    else if (e.key === "0") to = "all";
    else if (/^[1-9]$/.test(e.key)) to = keys[+e.key];
    if (to == null) return;
    e.preventDefault(); selectTab(to);
    if (inStrip) tabEls.get(to)?.focus({preventScroll: true});
  },
  crumb() { return tree ? (J.makeTitle(tree.title) || tree.id) : ""; },
};
})();
