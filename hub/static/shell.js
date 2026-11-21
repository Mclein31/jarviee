/* shell.js: routing, the sidebar's system panel and counts, the top bar, shortcuts and announcements. */
"use strict";
(() => {
const {$, $$, esc} = J;
const ROUTES = {command: "Command Center", agents: "Agents", review: "Review", board: "Board", workflows: "Workflows", tasks: "Task Queue", knowledge: "Knowledge", maps: "Project Maps",
  analytics: "Analytics", settings: "Settings"};
let cur = "command", params = [];
const lastStatus = new Map();

function parse() {
  const [h, qs] = (location.hash || "#/").replace(/^#\/?/, "").split("?");
  J.query = new URLSearchParams(qs || "");
  const parts = h.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "run") return ["workflows", parts.slice(1)];        // links from before the redesign
  if (parts[0] === "runs") return ["workflows", []];
  if (parts[0] === "queue") return ["tasks", []];
  if (parts[0] === "projects") return ["knowledge", []];
  if (parts[0] === "usage") return ["analytics", []];
  return [ROUTES[parts[0]] ? parts[0] : "command", parts.slice(1)];
}

function route() {
  const [page, p] = parse();
  const changed = page !== cur;
  cur = page; params = p;
  for (const el of $$(".page")) el.hidden = el.dataset.page !== page;
  for (const a of $$(".side .nav a")) { if (a.dataset.route === page) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); }
  $("#title").textContent = ROUTES[page];
  closeMenu();
  const P = J.pages[page];
  try { P?.show?.(params); P?.render?.(); } catch (e) { console.error(e); }
  crumb();
  if (changed) window.scrollTo({top: 0});
}
function crumb() { const c = J.pages[cur]?.crumb?.() || ""; if ($("#crumb").textContent !== c) $("#crumb").textContent = c; }

/* ---------- sidebar: system panel and counts ---------- */
function sideStatus() {
  const S = J.S, ts = S?.tool_status || {};
  const rows = [];
  rows.push(`<div class="sysrow">${J.sd(J.online ? "run" : "fail")}<span>Jarviee server</span><span class="v${J.online ? "" : " bad"}">${J.online ? ":" + (location.port || 80) : "offline"}</span></div>`);
  for (const [tool, t] of Object.entries(ts)) {
    const left = t.until ? J.span(t.until - J.now()) : "";
    rows.push(`<div class="sysrow" title="${esc(t.reason || "Available")}">${J.sd(t.available ? "done" : "out")}<span>${esc(J.TOOL[tool] || tool)}</span>`
      + `<span class="v${t.available ? "" : " bad"}">${t.available ? "ready" : left ? "out · " + left : "out"}</span></div>`);
  }
  const fb = S?.fallback || {};
  rows.push(`<div class="sysrow" title="When one tool is out, tasks go to the other one at the same level">${J.sd(fb.use_available_tool !== false ? "run" : "idle")}`
    + `<span>Use available tool</span><span class="v">${fb.use_available_tool !== false ? "on" : "off"}</span></div>`);
  if (S?.queue) rows.push(`<div class="sysrow">${J.sd(S.queue.paused ? "need" : "done")}<span>Task queue</span><span class="v">${S.queue.paused ? "paused" : "running"}</span></div>`);
  J.setHTML($("#sys"), rows.join(""));
  /* the collapsed line: only what is wrong, or "all ready" */
  const out = Object.entries(ts).filter(([, t]) => !t.available).map(([tool]) => J.TOOL_SHORT?.[tool] || J.TOOL[tool] || tool);
  const bad = !J.online || out.length, warn = !bad && S?.queue?.paused;
  const sum = !J.online ? "offline" : out.length ? `${out.join(", ")} out` : warn ? "queue paused" : "all ready";
  const box = $("#sys-sum"), k = bad ? "fail" : warn ? "need" : "done";
  J.setHTML(box, `${J.sd(k)}<span>${esc(sum)}</span>`);
  box.dataset.s = k;
}
function counts() {
  const g = J.groups(), q = J.S?.queue;
  const set = (id, v) => { const el = $(id); const t = v ? String(v) : ""; if (el.textContent !== t) el.textContent = t; };
  set("#c-command", g.need.length);
  set("#c-agents", g.need.length + g.run.length);
  set("#c-review", J.review?.count() || 0);
  set("#c-workflows", (q?.plans || []).filter(p => ["running", "draft", "approved"].includes(p.status)).length);
  set("#c-tasks", q ? Object.values(q.lanes).reduce((s, l) => s + l.running + l.queued, 0) : 0);
  set("#c-knowledge", (J.S?.projects || []).reduce((s, p) => s + (p.owner_count || 0), 0));
  document.title = g.need.length ? `(${g.need.length}) Jarviee` : "Jarviee";
}
function banner() {
  const b = J.S?.brain, msgs = [];
  if (b && !b.ok) msgs.push(`Workspace not found: <code>${esc(b.workspace)}</code>. Set <code>obsidian.workspace_root</code> in config.json.`);
  else if (b && !b.status_ok) msgs.push(`No <code>${esc(b.status_file)}</code>. Projects still load from each <code>.brain/</code>.`);
  J.setHTML($("#banner"), msgs.join(" "));
}
function announce() {
  for (const a of J.S.agents) {
    const before = lastStatus.get(a.id);
    if (before && before !== a.status) {
      if (["waiting", "done", "failed", "stopped"].includes(a.status)) J.announce(`${J.SECTION[J.sectionOf(a)]}: ${J.titleOf(a)}`);
    }
    lastStatus.set(a.id, a.status);
  }
}
function fresh() {
  const el = $("#fresh"), ago = J.lastOk ? Math.round((Date.now() - J.lastOk) / 1000) : null;
  const txt = !J.lastOk ? (J.online ? "connecting" : "Offline") : !J.online ? `Offline since ${new Date(J.lastOk).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`
    : `Live · ${ago < 3 ? "now" : ago + "s ago"}`;
  if ($("#freshtxt").textContent !== txt) $("#freshtxt").textContent = txt;
  el.classList.toggle("off", !J.online && !!J.lastOk);
  el.querySelector(".sd").dataset.s = J.online ? "run" : J.lastOk ? "fail" : "idle";
  el.title = J.online ? "Polling /api/state every 2 s" : "Start it with: jarviee";
}

/* ---------- alerts toggle (top bar) ---------- */
let alerts = null;
async function loadAlerts() { try { alerts = await J.api("/api/alerts"); renderAlerts(); } catch (e) { /* older server */ } }
function renderAlerts() {
  const b = $("#t-alerts");
  b.hidden = !alerts || !alerts.enabled;
  if (!alerts) return;
  b.setAttribute("aria-pressed", String(!alerts.muted));
  b.title = alerts.muted ? "Alerts are muted. Click to turn banners and sounds back on." : "Alerts on (macOS banner + sound). Click to mute.";
  b.classList.toggle("muted", !!alerts.muted);
}
J.setAlerts = async muted => {
  try { alerts = await J.api("/api/alerts", {muted}); renderAlerts(); J.emit("alerts", alerts); J.toast(muted ? "Alerts muted" : "Alerts on"); }
  catch (e) { J.toast(`Couldn't change alerts: ${e.message}`, true); }
};
J.alerts = () => alerts;

/* ---------- menu (narrow screens) ---------- */
function openMenu() { $("#side").classList.add("open"); $("#menu").setAttribute("aria-expanded", "true");
  if (!$(".side-scrim")) { const s = document.createElement("div"); s.className = "side-scrim"; s.addEventListener("click", closeMenu); document.body.append(s); } }
function closeMenu() { $("#side").classList.remove("open"); $("#menu")?.setAttribute("aria-expanded", "false"); $(".side-scrim")?.remove(); }

/* ---------- keyboard ---------- */
let gPrefix = 0;
const G = {c: "", a: "agents", r: "review", w: "workflows", t: "tasks", k: "knowledge", m: "maps", y: "analytics", s: "settings"};
function keys(e) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const inField = e.target.closest?.("input, textarea, select, [contenteditable]");
  if (e.key === "Escape") { if ($("#side").classList.contains("open")) { closeMenu(); return; } J.emit("escape", e); return; }
  if (inField) return;
  if (Date.now() - gPrefix < 900 && G[e.key] !== undefined) { e.preventDefault(); gPrefix = 0; location.hash = "#/" + G[e.key]; return; }
  if (e.key === "g") { gPrefix = Date.now(); return; }
  if (e.key === "/" || e.key === "n") { e.preventDefault(); J.newTask(); return; }
  if (e.key === "?") { e.preventDefault(); $("#keys-dlg").showModal(); return; }
  J.pages[cur]?.key?.(e);
}
J.newTask = () => {
  if (cur !== "command") location.hash = "#/";
  requestAnimationFrame(() => J.composer?.focus());
};

J.shell = {
  init() {
    window.addEventListener("hashchange", route);
    document.addEventListener("keydown", keys);
    $("#menu").addEventListener("click", () => $("#side").classList.contains("open") ? closeMenu() : openMenu());
    $("#t-new").addEventListener("click", J.newTask);
    $("#t-alerts").addEventListener("click", () => alerts && J.setAlerts(!alerts.muted));
    $("#t-search").addEventListener("click", () => J.palette?.open());
    $("#keys-open").addEventListener("click", () => $("#keys-dlg").showModal());
    const sys = $("#sys-box");
    sys.open = J.saved.get("jarviee.sys") === "1";
    sys.addEventListener("toggle", () => J.saved.set("jarviee.sys", sys.open ? "1" : "0"));
    document.addEventListener("click", e => { const c = e.target.closest("dialog [data-close]"); if (c) c.closest("dialog").close(); });
    for (const d of $$("dialog")) d.addEventListener("click", e => { if (e.target === d) d.close(); });
    J.on("state", () => {
      for (const fn of [sideStatus, counts, banner, announce, crumb]) { try { fn(); } catch (e) { console.error(e); } }
      try { J.pages[cur]?.render?.("state"); } catch (e) { console.error(`${cur} render failed`, e); }
    });
    for (const ev of ["usage", "activity", "runs", "alerts"]) J.on(ev, () => { try { J.pages[cur]?.render?.(ev); } catch (e) { console.error(e); } });
    J.on("online", () => { fresh(); sideStatus(); });
    J.on("tick", () => { fresh(); try { J.pages[cur]?.tick?.(); } catch (e) { console.error(e); } });
    loadAlerts();
  },
  route,
  current: () => cur,
  params: () => params,
};
})();
