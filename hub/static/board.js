/* board.js: the per-project kanban board (five columns, drag and drop, keyboard moves, polish polling). */
"use strict";
(() => {
const {esc} = J;
const PKEY = "jarviee.board.project";
const RUN_ST = {running: "run", queued: "queue", waiting: "need", blocked: "queue", done: "done", failed: "fail", stopped: "stopped", stale: "stopped"};
const FALLBACK_COLS = [["todo", "To do"], ["plan", "Plan / conceptualize"], ["build", "Build"], ["review", "Code review / finalize"], ["done", "Implemented / done"]];
const FALLBACK_LABELS = ["feature", "bugfix", "error", "refactor", "chore", "docs", "research"];
let el = null, projects = [], project = J.saved.get(PKEY) || "";
let data = null, filter = new Set(), drag = null, loading = false, lastLoad = 0, seq = 0, loadErr = "";
const q = s => el.querySelector(s);
const cols = () => (data?.columns || FALLBACK_COLS.map(([key, label]) => ({key, label})));
const cards = () => data?.cards || [];
const byCol = key => cards().filter(c => c.column === key).sort((a, b) => a.position - b.position);
const pending = () => cards().some(c => c.polish === "pending");
const shown = c => !filter.size || filter.has(c.label);
const runOf = c => c.run_id ? (J.S?.agents || []).find(a => a.id === c.run_id) : null;

/* ---------- loading ---------- */
async function loadProjects() {
  try {
    projects = (await J.api("/api/board/projects")).projects || [];
    if (!projects.some(p => p.name === project)) project = projects[0]?.name || "";
  } catch (e) { projects = []; }
  const sel = q("#bd-project");
  if (sel) sel.innerHTML = projects.length ? projects.map(p => `<option value="${esc(p.name)}"${p.name === project ? " selected" : ""}>${esc(p.name)}${p.open ? ` (${p.open})` : ""}</option>`).join("") : `<option value="">No projects</option>`;
}
async function reload() {
  if (!el || loading) return;
  loading = true; const my = ++seq;
  try {
    if (!projects.length) await loadProjects();
    const d = await J.api("/api/board" + (project ? "?project=" + encodeURIComponent(project) : ""));
    if (my !== seq) return;
    if (!drag) { data = d; loadErr = ""; if (d.project && d.project !== project) { project = d.project; const s = q("#bd-project"); if (s) s.value = project; } }
  } catch (e) { loadErr = e.message || "Could not load the board"; }
  finally { loading = false; lastLoad = Date.now(); }
  draw();
}

/* ---------- drawing ---------- */
function badges(c) {
  const out = [`<span class="bd-label bd-l-${esc(c.label)}">${esc(c.label)}</span>`,
    `<span class="bd-pri bd-p-${esc(c.priority)}" title="${esc(c.priority)} priority" role="img" aria-label="${esc(c.priority)} priority"></span>`];
  if (c.run_id) {
    const r = runOf(c), s = r ? (RUN_ST[r.status] || "queue") : "queue";
    out.push(`<a class="bd-run" href="#/agents/${encodeURIComponent(c.run_id)}" title="Open run ${esc(c.run_id)}">${J.st(s, r ? r.status : "run")}</a>`);
  }
  if (c.polish === "pending") out.push(`<span class="bd-polish pending" role="status">improving…</span>`);
  else if (c.polish === "done") out.push(`<span class="bd-polish done" title="Improved by ${esc(c.polish_model || "a model")}; open the card to compare or revert">✓ improved${c.polish_model ? " · " + esc(c.polish_model) : ""}</span>`);
  else if (c.polish === "failed") out.push(`<span class="bd-polish failed" title="Improvement failed; your text is kept">improve failed</span>`);
  return out.join("");
}
function card(c, i, n, key) {
  const acts = [];
  if (key !== "done") acts.push(`<button class="btn sm icon ghost" type="button" data-bd="advance" data-id="${esc(c.id)}" title="Move to the next column" aria-label="Move ${esc(c.title)} to the next column">→</button>`);
  if ((key === "todo" || key === "plan") && !c.run_id) acts.push(`<button class="btn sm" type="button" data-bd="start" data-id="${esc(c.id)}" aria-label="Start an agent for ${esc(c.title)}">Start</button>`);
  acts.push(`<button class="btn sm ghost" type="button" data-bd="edit" data-id="${esc(c.id)}" aria-label="Edit ${esc(c.title)}">Edit</button>`);
  return `<div class="bd-card" role="listitem" tabindex="0" draggable="true" data-id="${esc(c.id)}" data-label="${esc(c.label)}" data-key="card:${esc(c.id)}" aria-label="${esc(c.title)}, ${esc(c.label)}, ${esc(c.priority)} priority, ${i + 1} of ${n}. Alt+Left or Alt+Right moves it, Enter opens it.">
    <div class="bd-card-title">${esc(c.title)}</div><div class="bd-card-meta">${badges(c)}</div><div class="bd-card-acts">${acts.join("")}</div></div>`;
}
function chips() {
  const labels = data?.labels || FALLBACK_LABELS, all = cards();
  return `<button class="chip bd-chip${filter.size ? "" : " on"}" type="button" data-bd="chip" data-label="" aria-pressed="${!filter.size}">All <span class="num">${all.length}</span></button>`
    + labels.map(l => `<button class="chip bd-chip bd-l-${esc(l)}${filter.has(l) ? " on" : ""}" type="button" data-bd="chip" data-label="${esc(l)}" aria-pressed="${filter.has(l)}">${esc(l)} <span class="num">${all.filter(c => c.label === l).length}</span></button>`).join("");
}
function board() {
  if (loadErr && !data) return `<div class="empty"><b>Board unavailable</b><span>${esc(loadErr)}</span></div>`;
  if (!project && !projects.length) return `<div class="empty"><b>No projects</b><span>Add a project folder to the workspace to start a board.</span></div>`;
  return cols().map(col => {
    const all = byCol(col.key), list = all.filter(shown);
    return `<section class="bd-col" data-col="${esc(col.key)}" aria-label="${esc(col.label)}"><div class="bd-col-h"><h3>${esc(col.label)}</h3><span class="bd-count num">${filter.size ? `${list.length}/${all.length}` : all.length}</span></div>
      <div class="bd-list" role="list" data-col="${esc(col.key)}">${list.map((c, i) => card(c, i, list.length, col.key)).join("") || `<div class="bd-empty faint">${all.length ? "Hidden by the filter" : "Drop a card here"}</div>`}</div></section>`;
  }).join("");
}
function draw() {
  if (!el || drag) return;
  J.setHTML(q("#bd-chips"), chips());
  J.setHTML(q(".bd-board"), board());
  J.setHTML(q("#bd-state"), pending() ? `<span class="bd-polish pending" role="status">improving cards…</span>` : "");
}

/* ---------- moving ---------- */
const find = id => cards().find(c => c.id === id);
function positionIn(key, index, skipId) {
  const list = byCol(key).filter(c => c.id !== skipId);
  if (!list.length) return 1024;
  if (index <= 0) return list[0].position / 2;
  if (index >= list.length) return list[list.length - 1].position + 1024;
  return (list[index - 1].position + list[index].position) / 2;
}
async function move(id, column, position) {
  const c = find(id); if (!c) return;
  const before = {column: c.column, position: c.position};
  if (before.column === column && before.position === position) return;
  c.column = column; c.position = position; draw();
  try {
    const r = await J.api("/api/board/move", {id, column, position});
    if (r.card) Object.assign(c, r.card);
    draw(); reload();
  } catch (e) {
    Object.assign(c, before); draw(); J.toast(e.message || "Could not move the card", true);
  }
}
/* index of the drop within the visible cards of a list, from the pointer's y; mapped onto the full column order */
function dropIndex(list, y, skipId) {
  const els = [...list.querySelectorAll(".bd-card")].filter(x => x.dataset.id !== skipId);
  let i = els.length;
  for (let k = 0; k < els.length; k++) { const r = els[k].getBoundingClientRect(); if (y < r.top + r.height / 2) { i = k; break; } }
  const full = byCol(list.dataset.col).filter(c => c.id !== skipId);
  if (i >= els.length) return full.length;
  const target = full.findIndex(c => c.id === els[i].dataset.id);
  return target < 0 ? full.length : target;
}
function clearOver() { for (const l of el.querySelectorAll(".bd-over")) l.classList.remove("bd-over"); }
function dragEnd() { drag = null; clearOver(); for (const x of el.querySelectorAll(".dragging")) x.classList.remove("dragging"); draw(); }

async function act(kind, id) {
  const c = find(id); if (!c) return;
  if (kind === "edit") { J.boardEditor ? J.boardEditor.open({card: c}) : J.toast("The card editor is not loaded", true); return; }
  try {
    if (kind === "advance") {
      const r = await J.api("/api/board/advance", {id});
      if (r.card) Object.assign(c, r.card); draw();
    } else if (kind === "start") {
      const r = await J.api("/api/board/start", {id});
      if (r.card) Object.assign(c, r.card); draw(); J.toast("Agent started"); J.poll?.();
    }
  } catch (e) { J.toast(e.message, true); }
  reload();
}
function keyMove(c, dir) {
  const keys = cols().map(x => x.key), i = keys.indexOf(c.column), j = i + dir;
  if (j < 0 || j >= keys.length) return;
  const to = keys[j];
  move(c.id, to, positionIn(to, Infinity, c.id));
  requestAnimationFrame(() => el.querySelector(`.bd-card[data-id="${CSS.escape(c.id)}"]`)?.focus());
}

/* ---------- page ---------- */
J.board = {reload};
J.pages.board = {
  init() {
    el = document.querySelector('[data-page="board"]');
    el.innerHTML = `<div class="toolbar bd-bar"><label class="bd-pick"><span class="faint">Project</span> <select class="select sm" id="bd-project" aria-label="Project"></select></label>
        <div class="bd-chips" id="bd-chips" role="group" aria-label="Filter by label"></div><span class="spacer"></span><span id="bd-state"></span>
        <button class="btn sm primary" type="button" id="bd-new">New card</button></div>
      <div class="bd-board" aria-label="Project board"></div>`;
    q("#bd-project").addEventListener("change", e => { project = e.target.value; J.saved.set(PKEY, project); data = null; filter.clear(); draw(); reload(); });
    q("#bd-new").addEventListener("click", () => { J.boardEditor ? J.boardEditor.open({project}) : J.toast("The card editor is not loaded", true); });
    el.addEventListener("click", e => {
      const b = e.target.closest("[data-bd]");
      if (b) {
        if (b.dataset.bd === "chip") {
          const l = b.dataset.label;
          if (!l) filter.clear(); else if (filter.has(l)) filter.delete(l); else filter.add(l);
          draw();
        } else act(b.dataset.bd, b.dataset.id);
        return;
      }
      if (e.target.closest("a")) return;
      const c = e.target.closest(".bd-card"); if (c) act("edit", c.dataset.id);
    });
    el.addEventListener("keydown", e => {
      const c = e.target.closest?.(".bd-card"); if (!c || e.target !== c) return;
      const card = find(c.dataset.id); if (!card) return;
      if (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) { e.preventDefault(); e.stopPropagation(); keyMove(card, e.key === "ArrowRight" ? 1 : -1); }
      else if (e.key === "Enter") { e.preventDefault(); act("edit", card.id); }
    });
    el.addEventListener("dragstart", e => {
      const c = e.target.closest?.(".bd-card"); if (!c) return;
      drag = {id: c.dataset.id};
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", drag.id); } catch (err) { /* some browsers refuse */ }
      requestAnimationFrame(() => c.classList.add("dragging"));
    });
    el.addEventListener("dragover", e => {
      if (!drag) return;
      const l = e.target.closest(".bd-col")?.querySelector(".bd-list"); if (!l) return;
      e.preventDefault(); e.dataTransfer.dropEffect = "move";
      if (!l.classList.contains("bd-over")) { clearOver(); l.classList.add("bd-over"); }
    });
    el.addEventListener("dragleave", e => { if (drag && !el.contains(e.relatedTarget)) clearOver(); });
    el.addEventListener("drop", e => {
      if (!drag) return;
      const l = e.target.closest(".bd-col")?.querySelector(".bd-list"); if (!l) { dragEnd(); return; }
      e.preventDefault();
      const id = drag.id, key = l.dataset.col, pos = positionIn(key, dropIndex(l, e.clientY, id), id);
      dragEnd(); move(id, key, pos);
    });
    el.addEventListener("dragend", () => { if (drag) dragEnd(); });
    loadProjects().then(reload);
  },
  show() { if (el) { loadProjects().then(reload); } },
  render(ev) {
    if (!el) return;
    if (ev === "state" || ev === undefined) {
      if (!drag) draw();
      if (!pending() && Date.now() - lastLoad > 3000) reload();
    }
  },
  tick() { if (el && !drag && pending() && Date.now() - lastLoad >= 4000) reload(); },
  crumb() { const n = cards().filter(c => c.column !== "done").length; return data ? `${project} · ${n} open` : ""; },
};
})();
