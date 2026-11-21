/* core.js: helpers, formatting, the API, polling and in-place patching. Everything hangs off window.J. */
"use strict";
(() => {
const J = window.J = {pages: {}, S: null, U: null, A: [], RUNS: null, skew: 0, online: false, lastOk: 0, firstLoad: true};
const $ = J.$ = (s, r = document) => r.querySelector(s);
J.$$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = J.esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
J.noEmoji = s => String(s ?? "").replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}]/gu, "").replace(/\s+/g, " ").trim();
J.DEMO = new URLSearchParams(location.search).get("demo");
/* interface scale: 85% unless the viewer picked another in Settings (per browser, never required) */
J.SCALES = [.8, .85, .9, 1];
J.setScale = z => { document.documentElement.style.setProperty("--z", String(z)); try { localStorage.setItem("jarviee.scale", String(z)); } catch (e) { /* storage blocked */ } };
try { const z = parseFloat(localStorage.getItem("jarviee.scale")); if (J.SCALES.includes(z)) document.documentElement.style.setProperty("--z", String(z)); } catch (e) { /* default scale */ }

/* ---------- numbers and time ---------- */
J.now = () => Date.now() / 1000 + J.skew;
J.dur = s => {
  if (!(s >= 0)) return "—";
  s = Math.round(s);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h}h ${String(m % 60).padStart(2, "0")}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
};
J.span = s => s >= 86400 ? `${Math.floor(s / 86400)}d ${Math.floor(s % 86400 / 3600)}h` : J.dur(s).replace(/ \d+s$/, "");
J.ago = t => { const s = J.now() - t; return s < 45 ? "just now" : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`; };
J.fmtTok = n => n == null ? "—" : n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(Math.round(n));
J.fmtUsd = n => n == null ? "—" : "$" + (n >= 100 ? Math.round(n) : n.toFixed(2));
J.clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
J.clockS = t => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"});
J.dayStart = () => J.U?.day_start || new Date().setHours(0, 0, 0, 0) / 1000;
J.pct = (a, b) => b ? Math.round(100 * a / b) : 0;

/* ---------- agents ---------- */
J.RANK = {basic: 0, light: 1, standard: 1, complex: 2, heavy: 3};
J.TIERS = ["basic", "light", "complex", "heavy"];
J.TIER_WORD = {basic: "Basic", light: "Light", standard: "Light", complex: "Complex", heavy: "Heavy"};
J.TOOL = {agy: "Antigravity", claude: "Claude Code", omniroute: "OmniRoute", echo: "echo"};
J.TOOL_SHORT = {agy: "agy", claude: "Claude", omniroute: "OmniRoute", echo: "echo"};
J.STAT = {need: "needs you", unverified: "check result", run: "running", queue: "queued", done: "done", fail: "failed",
  fb: "fallback", stale: "stale", stopped: "stopped", out: "unavailable", idle: "idle"};
J.statusKey = a => {
  if (a.status === "stale") return "stale";
  if (a.status === "waiting") return "need";
  if (a.status === "running") return "run";
  if (a.status === "queued") return "queue";
  if (a.status === "done") return a.verify === "unverified" ? "unverified" : "done";
  if (a.status === "failed") return "fail";
  return "stopped";
};
J.groupOf = a => { const k = J.statusKey(a); return k === "need" || k === "unverified" ? "need" : k === "run" || k === "queue" ? "run" : k === "stale" ? "stale" : "done"; };
J.sd = k => `<span class="sd" data-s="${k === "unverified" ? "need" : k}" aria-hidden="true"></span>`;
J.st = (k, word) => `<span class="st" data-s="${k === "unverified" ? "need" : k}">${J.sd(k)}${esc(word || J.STAT[k] || k)}</span>`;
J.place = a => a.project || (a.workspace ? a.workspace.split(/[\\/]/).filter(Boolean).pop() : "");
J.waitReason = s => {
  if (!s || /^turn finished, waiting for you$/i.test(s) || /^claude is waiting for your input$/i.test(s)) return "Waiting for your reply";
  if (/^turn finished, waiting for you \(/i.test(s)) return s.replace(/^turn finished, waiting for you/i, "Waiting for your reply");
  return s.charAt(0).toUpperCase() + s.slice(1);
};

/* the section an alert lands in (mirrors hub/alerts.py section_of): a finished turn is "Responded", a block is "Needs your response" */
J.SECTION = {needs: "Needs your response", responded: "Responded", done: "Done", failed: "Failed", stopped: "Stopped"};
J.sectionOf = a => a.status === "waiting"
  ? (/^(turn finished|claude is waiting for your input)/i.test(String(a.activity || "").trim()) ? "responded" : "needs")
  : a.status;

/* where the owner can answer a waiting agent: Jarviee sees every session but can only type into its own panes */
J.replyWhere = a => {
  if (a.source === "launcher") return a.pane ? {text: "Reply in its iTerm2 pane. Go to its window brings it forward.", pane: true}
    : {text: "This run is headless and can't take a reply here. Run it again with the missing detail."};
  if (a.agent_host === "antigravity-app") return {text: "Reply in the Antigravity app, in this conversation."};
  if (a.tool === "claude") return {text: "Reply in the Claude Code window it runs in. Go to its window brings that window forward."};
  if (a.tool === "agy") return {text: "Reply in the terminal running this agy session."};
  return {text: "Reply in the app this session runs in."};
};
/* titles: same rules as hub/titles.py, for rows stored before titles existed (and IDE wrapper tags) */
J.makeTitle = (text, limit = 60) => {
  const lines = [];
  const clean = String(text ?? "").replace(/```[\s\S]*?(?:```|$)/g, " ")
    .replace(/<(ide_selection|ide_opened_file|system-reminder)>[\s\S]*?<\/\1>/g, " ");
  for (let line of clean.split("\n")) {
    if (/^\s*(?:-{3,}|\*{3,}|_{3,}|={3,})\s*$/.test(line)) continue;
    const heading = /^\s*#{1,6}\s/.test(line);
    line = line.replace(/^\s*(?:>\s*)*(?:#{1,6}\s+|[-*+•]\s+|\d+[.)]\s+|\[[ xX]\]\s+)*/, "").replace(/<[^>]+>/g, " ")
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[`*_~]/g, "").trim();
    if (line) lines.push([line, heading]);
  }
  if (lines.length > 1 && lines[0][1] && lines[0][0].split(/\s+/).length < 3) lines.shift();
  if (!lines.length) return "";
  let t = lines[0][0].replace(/\s+/g, " ");
  const stop = t.search(/[.!?](\s|$)/);
  if (stop >= 8) t = t.slice(0, stop);
  t = t.charAt(0).toUpperCase() + t.slice(1);
  if (t.length <= limit) return t;
  const cut = t.slice(0, limit - 1), sp = cut.lastIndexOf(" ");
  return (sp > limit * .5 ? cut.slice(0, sp) : cut).replace(/[ ,;:\-–—(]+$/, "") + "…";
};
J.titleOf = a => (a.title && !/^</.test(a.title) ? a.title : J.makeTitle(a.task)) || "Untitled task";

/* model slugs → "Gemini 3.8 Flash · Low", "Claude Opus 5.5 · High"; config "model_labels" wins */
const EFFORT = {"extra-low": "Extra low", low: "Low", medium: "Medium", high: "High", xhigh: "XHigh", max: "Max"};
J.modelLabel = slug => {
  const labels = J.S?.model_labels || {};
  if (labels[slug]) return labels[slug];
  let s = String(slug || "").trim();
  if (!s) return "";
  if (/^(haiku|sonnet|opus)$/i.test(s)) return s.charAt(0).toUpperCase() + s.slice(1);
  /* agy's Orchestrator: a planner model plus a Hydrogen executor, no effort suffix (g1-pro is the Argon planner) */
  const orch = s.match(/^gemini-pro-orchestrator(?:-(.+))?$/i);
  if (orch) { const p = (orch[1] || "").toLowerCase(); return `Orchestrator (${p === "g1-pro" ? "Argon" : p ? p.charAt(0).toUpperCase() + p.slice(1) : "Carbon"})`; }
  let eff = "";
  const em = s.match(/-(extra-low|low|medium|high|xhigh|max)$/i);
  if (em) { eff = EFFORT[em[1].toLowerCase()]; s = s.slice(0, em.index); }
  const parts = s.split("-"), out = [];
  for (let i = 0; i < parts.length; i++) {
    const w = parts[i];
    if (/^\d+$/.test(w) && /^\d$/.test(parts[i + 1] || "")) { out.push(w + "." + parts[++i]); continue; }
    out.push(/^\d/.test(w) ? w : w.charAt(0).toUpperCase() + w.slice(1));
  }
  return out.join(" ") + (eff ? " · " + eff : "");
};
J.effortLabel = e => EFFORT[String(e || "").toLowerCase()] || "";
J.tierPill = (tier, sug, reasons, effort) => {
  const word = J.TIER_WORD[tier];
  if (!word) return `<span class="pill" title="Tier unknown for this model">—</span>`;
  const diff = sug in J.RANK && J.RANK[sug] !== J.RANK[tier];
  const mk = !diff ? "" : J.RANK[sug] > J.RANK[tier] ? "↑" : "↓";
  const tip = [`Ran ${tier}` + (diff ? `; router suggested ${sug}` : ""), effort ? `effort ${effort}` : "", (reasons || []).join("; ")].filter(Boolean).join(" · ");
  return `<span class="pill t-${esc(tier)}" title="${esc(tip)}">${word}${mk ? `<svg class="i mk" role="img" aria-label="${mk === "↑" ? "router suggested heavier" : "router suggested lighter"}"><use href="#${mk === "↑" ? "i-up" : "i-down"}"/></svg>` : ""}</span>`;
};
J.agentPill = a => J.tierPill(a.tier, a.suggested_tier, a.reasons, a.effort);
/* a tool's badge: its own app logo for Claude and agy (served from hub/static), two letters for anything else */
J.TOOL_LOGO = {claude: "/static/tool-claude.png", agy: "/static/tool-agy.png"};
J.toolMono = tool => `<span class="tmono" data-tool="${esc(tool)}" aria-hidden="true">${J.TOOL_LOGO[tool] ? `<img src="${J.TOOL_LOGO[tool]}" alt="" width="28" height="28" decoding="async">` : esc(String(tool || "?").slice(0, 2).toUpperCase())}</span>`;

/* ---------- per-viewer conveniences (the page works without them) ---------- */
J.saved = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } },
};

/* ---------- events ---------- */
const bus = new EventTarget();
J.on = (name, fn) => bus.addEventListener(name, e => fn(e.detail));
J.emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, {detail}));

/* ---------- API ---------- */
J.api = async (path, body) => {
  if (J.demoApi) return J.demoApi(path, body);
  const token = window.JARVIEE_TOKEN || ($('meta[name="jarviee-token"]') || {}).content || "";
  const headers = {"Content-Type": "application/json"};
  if (token) headers["X-Jarviee-Token"] = token;
  const r = await fetch(path, body === undefined ? {} : {method: "POST", headers, body: JSON.stringify(body)});
  const data = (r.headers.get("content-type") || "").includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
};

/* ---------- rendering helpers ---------- */
/* Replace an element's HTML only when it changed, and put focus back on the same keyed control. */
J.setHTML = (el, html) => {
  if (!el || el._html === html) return false;
  const key = el.contains(document.activeElement) && document.activeElement.dataset.key;
  const scroll = el.scrollTop;
  el.innerHTML = html;
  el._html = html;
  el.scrollTop = scroll;
  if (key) el.querySelector(`[data-key="${CSS.escape(key)}"]`)?.focus({preventScroll: true});
  return true;
};
/* Keyed children patched in place: unchanged rows are never rebuilt, so focus, hover and selection survive polls. */
J.patch = (box, items, {key, html, sig, tag = "div", cls, attrs} = {}) => {
  const want = new Set(), cache = box._rows || (box._rows = new Map());
  let prev = null;
  for (const it of items) {
    const k = String(key(it)), s = sig ? sig(it) : JSON.stringify(it);
    want.add(k);
    let r = cache.get(k);
    if (!r) {
      const el = document.createElement(tag); el.dataset.id = k; r = {el, s: ""}; cache.set(k, r);
      if (!J.firstLoad && box._seeded) el.classList.add("arrive");
    }
    if (r.s !== s) {
      const act = r.el.contains(document.activeElement) && document.activeElement !== r.el ? document.activeElement.dataset.key : null;
      r.el.innerHTML = html(it);
      r.el.className = (typeof cls === "function" ? cls(it) : (cls || "")) + (r.el.classList.contains("arrive") ? " arrive" : "");
      if (attrs) for (const [a, v] of Object.entries(attrs(it))) v == null ? r.el.removeAttribute(a) : r.el.setAttribute(a, v);
      r.s = s;
      if (act) r.el.querySelector(`[data-key="${CSS.escape(act)}"]`)?.focus({preventScroll: true});
    }
    const at = prev ? prev.nextSibling : box.firstChild;
    if (at !== r.el) { const f = r.el.contains(document.activeElement) ? document.activeElement : null; box.insertBefore(r.el, at); f?.focus({preventScroll: true}); }
    prev = r.el;
  }
  for (const [k, r] of cache) if (!want.has(k)) { r.el.remove(); cache.delete(k); }
  box._seeded = true;
};
J.announce = text => { const el = $("#announce"); if (el) { el.textContent = ""; requestAnimationFrame(() => { el.textContent = text; }); } };
let toastT = null;
J.toast = (text, err) => {
  const el = $("#toast");
  el.textContent = text;
  el.classList.toggle("err", !!err);
  el.classList.add("on");
  clearTimeout(toastT);
  toastT = setTimeout(() => el.classList.remove("on"), 4200);
};
J.sparkPath = (values, w, h, pad = 1.5) => {
  const max = Math.max(1, ...values), n = values.length;
  return values.map((v, i) => [n > 1 ? i * w / (n - 1) : 0, h - pad - (v / max) * (h - pad * 2)])
    .map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
};
J.spark = (values, cls = "") => {
  const w = 96, h = 26, d = J.sparkPath(values, w, h);
  return `<svg class="spark ${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path class="a" d="${d}L${w} ${h}L0 ${h}Z"/><path class="l" d="${d}"/></svg>`;
};
J.bars = (values, cls = "") => {
  const n = values.length || 1, max = Math.max(1, ...values), bw = 96 / n;
  return `<svg class="mbars ${cls}" viewBox="0 0 96 26" preserveAspectRatio="none" aria-hidden="true">${values.map((v, i) => {
    const h = v ? Math.max(3, v / max * 24) : 1.2;
    return `<rect x="${(i * bw + .45).toFixed(2)}" y="${(26 - h).toFixed(2)}" width="${(bw - .9).toFixed(2)}" height="${h.toFixed(2)}" class="${v ? "on" : ""}"/>`;
  }).join("")}</svg>`;
};
/* donut segments [{v, cls}] */
J.donut = (segs, size = 132, stroke = 12) => {
  const total = segs.reduce((s, x) => s + x.v, 0), r = (size - stroke) / 2, c = 2 * Math.PI * r, live = segs.filter(s => s.v);
  let off = 0;
  const arcs = live.map(s => {
    const len = c * s.v / total, gap = live.length > 1 ? Math.min(4, len / 3) : 0;
    const el = `<circle class="${s.cls}" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}" stroke-dasharray="${(len - gap).toFixed(2)} ${(c - len + gap).toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}"/>`;
    off += len;
    return el;
  }).join("");
  return `<svg class="donut" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" aria-hidden="true"><circle class="track" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}"/><g transform="rotate(-90 ${size / 2} ${size / 2})">${arcs}</g></svg>`;
};
/* small, safe markdown for prompts: everything is escaped first */
J.md = src => {
  const blocks = [], text = String(src ?? "").replace(/```([\w-]*)\n?([\s\S]*?)(?:```|$)/g, (_, l, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
  const inline = t => esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>").replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = []; let list = null, para = [];
  const flush = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; }
    if (list) { out.push(`<${list.t}>${list.items.map(i => `<li>${inline(i)}</li>`).join("")}</${list.t}>`); list = null; } };
  for (const line of text.split("\n")) {
    let m;
    if (/^\u0000\d+\u0000$/.test(line.trim())) { flush(); out.push(blocks[+line.trim().slice(1, -1)]); }
    else if (!line.trim()) flush();
    else if ((m = line.match(/^\s*(#{1,6})\s+(.*)$/))) { flush(); const n = Math.min(4, m[1].length + 1); out.push(`<h${n}>${inline(m[2])}</h${n}>`); }
    else if ((m = line.match(/^\s*>\s?(.*)$/))) { flush(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
    else if ((m = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/))) {
      const t = /\d/.test(m[1]) ? "ol" : "ul";
      if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; }
      if (!list || list.t !== t) { if (list) flush(); list = {t, items: []}; }
      list.items.push(m[2]);
    } else { if (list) flush(); para.push(line.trim()); }
  }
  flush();
  return out.join("") || `<p class="muted">No prompt recorded.</p>`;
};

/* ---------- derived views of state ---------- */
J.groups = () => {
  const g = {need: [], run: [], done: [], stale: []};
  for (const a of J.S?.agents || []) g[J.groupOf(a)].push(a);
  g.done.sort((x, y) => (y.ended || y.updated || 0) - (x.ended || x.updated || 0));
  g.need.sort((x, y) => (x.updated || 0) - (y.updated || 0));
  return g;
};
J.endOf = a => a.ended || (["done", "failed", "stopped"].includes(a.status) ? a.updated : 0);
J.today = () => {
  const d0 = J.dayStart(), out = {done: 0, failed: 0, stopped: 0, byTool: {}};
  for (const a of J.S?.agents || []) {
    const end = J.endOf(a);
    if (!end || end < d0) continue;
    const t = out.byTool[a.tool] || (out.byTool[a.tool] = {done: 0, failed: 0, secs: 0, n: 0});
    if (a.status === "done") { out.done++; t.done++; }
    if (a.status === "failed") { out.failed++; t.failed++; }
    if (a.status === "stopped") out.stopped++;
    if (a.started && end > a.started) { t.secs += end - a.started; t.n++; }
  }
  return out;
};
J.planTasks = () => (J.S?.queue?.plans || []).flatMap(p => p.tasks);

/* ---------- polling ---------- */
/* Conditional GET: the server tags each answer (ETag). An unchanged answer comes back as an empty 304,
   so a quiet desk costs no JSON parse and no re-render. Older servers send no ETag and this is a plain fetch. */
const etags = new Map(), kept = new Map();
J.fresh = async path => {
  if (J.demoApi) return {data: await J.demoApi(path), changed: true};
  const tag = etags.get(path);
  const r = await fetch(path, tag ? {headers: {"If-None-Match": tag}} : {});
  if (r.status === 304 && kept.has(path)) return {data: kept.get(path), changed: false};
  const data = (r.headers.get("content-type") || "").includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  const next = r.headers.get("ETag");
  if (next) { etags.set(path, next); kept.set(path, data); } else { etags.delete(path); kept.delete(path); }
  return {data, changed: true};
};
let tState = null, tUsage = null, tAct = null, misses = 0, lastEmit = 0;
J.poll = async () => {
  clearTimeout(tState);
  try {
    const {data: next, changed} = await J.fresh("/api/state");
    J.lastOk = Date.now(); misses = 0;
    /* an unchanged desk still redraws every 10 s, for the few labels that count time between ticks */
    if (changed || !J.online || Date.now() - lastEmit > 10000) {
      if (changed) { J.S = next; J.skew = next.now - Date.now() / 1000; }
      lastEmit = Date.now();
      J.setOnline(true);
      J.emit("state", J.S);
    }
    J.firstLoad = false;
  } catch (e) { misses++; J.setOnline(false); }
  /* 2 s while watched; slower in a background tab (the tab title still counts "needs you") and while the server is away */
  tState = setTimeout(J.poll, misses ? Math.min(10000, 2000 * misses) : document.hidden ? 6000 : 2000);
};
document.addEventListener("visibilitychange", () => { if (!document.hidden) { J.poll(); J.pollActivity?.(); } });
J.pollUsage = async () => {
  clearTimeout(tUsage);
  let wait = 30000;
  try { J.U = await J.api("/api/usage"); J.U_err = ""; J.emit("usage", J.U); }
  catch (e) { wait = 10000; J.U_err = e.message; J.emit("usage", null); }
  tUsage = setTimeout(J.pollUsage, wait);
};
J.pollActivity = async () => {
  clearTimeout(tAct);
  try { const {data, changed} = await J.fresh("/api/activity?limit=40"); if (changed) { J.A = data.items || []; J.emit("activity", J.A); } } catch (e) { /* the feed falls back to status events */ }
  tAct = setTimeout(J.pollActivity, document.hidden ? 15000 : 4000);
};
J.pollRuns = async () => {
  if (document.hidden && J.RUNS) return;
  try { const {data, changed} = await J.fresh("/api/runs"); if (changed) { J.RUNS = data.runs || []; J.emit("runs", J.RUNS); } } catch (e) { J.RUNS = J.RUNS || []; }
};
J.setOnline = ok => {
  J.online = ok;
  document.body.classList.toggle("stale", !ok && !!J.S);
  J.emit("online", ok);
};

J.loadScript = src => new Promise((res, rej) => { const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = rej; document.head.append(s); });
J.start = async () => {
  if (J.DEMO) { try { await J.loadScript("/static/demo.js"); } catch (e) { console.error("demo data failed to load", e); } }
  J.shell.init();
  for (const p of Object.values(J.pages)) { try { p.init?.(); } catch (e) { console.error(e); } }
  J.shell.route();
  J.poll();
  J.pollUsage();
  J.pollActivity();
  J.pollRuns();
  setInterval(J.pollRuns, 15000);
  setInterval(() => J.emit("tick"), 1000);
};
})();
