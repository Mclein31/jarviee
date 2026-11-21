/* palette.js: the command palette (⌘K / Ctrl+K). One box that reaches every page, live agent, project and action. */
"use strict";
(() => {
const {$, esc} = J;
let dlg = null, input = null, list = null, items = [], sel = 0;

const go = hash => () => { location.hash = hash; };
async function post(path, body, ok) {
  try { const r = await J.api(path, body || {}); J.toast(r?.message || ok); } catch (e) { J.toast(e.message, true); }
  J.poll();
}

/* everything the palette can do right now, built from the current state each time it opens */
function build() {
  const S = J.S || {}, out = [], g = J.groups();
  const add = (group, icon, label, hint, run, words = "") => out.push({group, icon, label, hint, run, hay: `${label} ${hint} ${words}`.toLowerCase()});

  for (const a of g.need) add("Needs you", "i-pane", J.titleOf(a), `${J.place(a) || "no project"} · go to its window`, () => J.focusAgent(a.id), "waiting reply answer");

  add("Actions", "i-plus", "New task", "/", () => J.newTask(), "run launch start");
  add("Actions", "i-split", "Plan a goal", "split into subtasks", () => { location.hash = "#/"; requestAnimationFrame(() => J.composer.plan()); }, "queue pipeline");
  if (g.need.length) add("Actions", "i-pane", "Go to the oldest waiting agent", J.titleOf(g.need[0]), () => J.focusAgent(g.need[0].id), "next needs you");
  if (S.queue) add("Actions", S.queue.paused ? "i-play" : "i-pause", S.queue.paused ? "Resume the queue" : "Pause the queue", S.queue.paused ? "paused" : "running",
    () => post(S.queue.paused ? "/api/queue/resume" : "/api/queue/pause", {}, S.queue.paused ? "Queue resumed" : "Queue paused"));
  if (g.done.length) add("Actions", "i-broom", "Clear finished agents", `${g.done.length} finished`, () => post("/api/clear", {}, "Cleared finished agents"), "clean tidy");
  const T = S.terminal;
  if (T && T.installed && T.mode === "iterm2" && T.enabled) add("Actions", "i-retile", "Re-tile iTerm2 panes", "", () => post("/api/terminal", {action: "retile"}, "Re-tiled"), "window layout");
  const al = J.alerts?.();
  if (al?.enabled) add("Actions", "i-bell", al.muted ? "Turn alerts on" : "Mute alerts", al.muted ? "muted" : "banner and sound", () => J.setAlerts(!al.muted), "notifications sound quiet");
  add("Actions", "i-key", "Keyboard shortcuts", "?", () => $("#keys-dlg").showModal(), "help keys");

  for (const [hash, icon, label, hint] of [["#/", "i-command", "Command Center", "g c"], ["#/agents", "i-agents", "Agents", "g a"], ["#/review", "i-check", "Review inbox", "g r"],
    ["#/board", "n-board", "Board", ""], ["#/workflows", "i-flow", "Workflows", "g w"],
    ["#/tasks", "i-tasks", "Task Queue", "g t"], ["#/knowledge", "i-book", "Knowledge", "g k"], ["#/maps", "i-map", "Project Maps", "g m"],
    ["#/analytics", "i-chart", "Analytics", "g y"], ["#/settings", "i-gear", "Settings", "g s"]]) add("Go to", icon, label, hint, go(hash), "page open");

  for (const a of g.run) add("Running", "i-log", J.titleOf(a), `${J.place(a) || "no project"} · ${J.modelLabel(a.model) || a.tool || ""}`, go("#/agents/" + encodeURIComponent(a.id)), a.task || "");
  for (const p of S.projects || []) {
    add("Projects", "i-book", p.name, p.owner_count ? `${p.owner_count} waiting on you` : "brain", go("#/knowledge"), "project knowledge todo");
    if (p.flow) add("Projects", "i-map", `${p.name} map`, "project map", go("#/maps/" + encodeURIComponent(p.name)), "flow diagram");
  }
  for (const p of (S.queue?.plans || []).slice(0, 8)) add("Plans", "i-flow", J.makeTitle(p.goal, 70) || p.id, p.status, go("#/workflows/" + encodeURIComponent(p.id)), "workflow graph");
  for (const a of g.done.slice(0, 12)) add("Finished", "i-log", J.titleOf(a), `${J.STAT[J.statusKey(a)]} · ${J.place(a) || "no project"}`, go("#/agents/" + encodeURIComponent(a.id)), a.task || "");
  return out;
}

/* every typed word must appear; matches at the start of the label come first */
function filter(text) {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return items.filter(i => i.group !== "Finished" && i.group !== "Plans");
  return items.filter(i => words.every(w => i.hay.includes(w)))
    .map((i, n) => [i.label.toLowerCase().startsWith(words[0]) ? 0 : i.label.toLowerCase().includes(words[0]) ? 1 : 2, n, i])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(x => x[2]);
}

let shown = [];
function render() {
  shown = filter(input.value).slice(0, 60);
  sel = Math.min(sel, Math.max(0, shown.length - 1));
  if (!shown.length) { list.innerHTML = `<div class="pal-empty">Nothing matches "${esc(input.value)}". Press Enter to start it as a new task.</div>`; return; }
  let group = "", html = "";
  const grouped = !input.value.trim();
  shown.forEach((i, n) => {
    if (grouped && i.group !== group) { group = i.group; html += `<div class="pal-g" role="presentation">${esc(group)}</div>`; }
    html += `<button type="button" class="pal-i" role="option" id="pal-${n}" data-n="${n}" aria-selected="${n === sel}"><svg class="i" aria-hidden="true"><use href="#${i.icon}"/></svg>`
      + `<span class="pl">${esc(i.label)}</span><span class="ph2">${esc(grouped ? i.hint : i.hint || i.group)}</span></button>`;
  });
  list.innerHTML = html;
  input.setAttribute("aria-activedescendant", "pal-" + sel);
}
function move(d) {
  if (!shown.length) return;
  sel = (sel + d + shown.length) % shown.length;
  for (const b of list.querySelectorAll(".pal-i")) b.setAttribute("aria-selected", String(+b.dataset.n === sel));
  list.querySelector(`#pal-${sel}`)?.scrollIntoView({block: "nearest"});
  input.setAttribute("aria-activedescendant", "pal-" + sel);
}
function run(n) {
  const it = shown[n], text = input.value.trim();
  dlg.close();
  if (it) { try { it.run(); } catch (e) { console.error(e); } }
  else if (text) { J.newTask(); requestAnimationFrame(() => J.composer?.setText?.(text)); }
}

function mount() {
  dlg = document.createElement("dialog");
  dlg.className = "pal"; dlg.setAttribute("aria-label", "Command palette");
  dlg.innerHTML = `<div class="pal-in"><svg class="i" aria-hidden="true"><use href="#i-search"/></svg>
      <input type="text" id="pal-q" placeholder="Search agents, projects and actions" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="true" aria-controls="pal-list" aria-label="Search agents, projects and actions"></div>
    <div class="pal-list" id="pal-list" role="listbox" aria-label="Results"></div>
    <div class="pal-foot"><span><span class="kbd">↑</span><span class="kbd">↓</span>move</span><span><span class="kbd">Enter</span>run</span><span><span class="kbd">Esc</span>close</span></div>`;
  document.body.append(dlg);
  input = dlg.querySelector("input"); list = dlg.querySelector(".pal-list");
  input.addEventListener("input", () => { sel = 0; render(); });
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "Enter") { e.preventDefault(); run(sel); }
  });
  list.addEventListener("click", e => { const b = e.target.closest(".pal-i"); if (b) run(+b.dataset.n); });
  list.addEventListener("mousemove", e => { const b = e.target.closest(".pal-i"); if (b && +b.dataset.n !== sel) { sel = +b.dataset.n; move(0); } });
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });
}

J.palette = {
  open() {
    if (!dlg) mount();
    if (dlg.open) { dlg.close(); return; }
    items = build(); sel = 0; input.value = "";
    render();
    dlg.showModal();
    input.focus();
  },
};
document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); J.palette.open(); }
});
})();
