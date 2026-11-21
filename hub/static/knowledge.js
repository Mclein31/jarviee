/* knowledge.js: project brains. Progress from TODO.md, owner items, priority TODOs with Run, and the ecosystem status table. */
"use strict";
(() => {
const {esc} = J;
let el = null, open = new Set(), prioOnly = J.saved.get("jarviee.prio") !== "all", allOwner = false;
const q = s => el.querySelector(s);
const PRIO_RE = /(?:\[\s*(P0|P1|P2|Urgent|Critical|High(?:\s+Priority)?|Priority|!+)\s*\]|\(\s*(P0|P1|P2|Urgent|Critical|High(?:\s+Priority)?|Priority|!+)\s*\)|^(P0|P1|P2|Urgent|Critical|High|Priority)\s*:\s*|^(!+)(?:\s+|$)|(?:\s+)(!+)\s*$)/i;
function prioTok(tok) {
  const t = String(tok || "").trim().toLowerCase();
  if (t === "p0") return ["P0", 0];
  if (t === "urgent" || t === "critical" || (t.startsWith("!") && t.length >= 2)) return ["Urgent", 0];
  if (t === "p1") return ["P1", 1];
  if (t === "priority") return ["Priority", 1];
  if (t === "p2") return ["P2", 2];
  return ["High", 1];
}
function norm(item, owner) {
  if (item && typeof item === "object") return {text: item.text || "", priority: item.priority || (owner ? "High" : ""), rank: typeof item.rank === "number" ? item.rank : owner ? 1 : 99, owner};
  let s = J.noEmoji(item), pr = owner ? "High" : "", rk = owner ? 1 : 99, m;
  while ((m = s.match(PRIO_RE))) {
    const [l, r] = prioTok(m[1] || m[2] || m[3] || m[4] || m[5]);
    if (r < rk) { pr = l; rk = r; }
    s = (s.slice(0, m.index) + " " + s.slice(m.index + m[0].length)).replace(/\s{2,}/g, " ").trim();
  }
  return {text: s, priority: pr, rank: rk, owner};
}
const items = (p, owner) => (owner ? (p.owner_task_items || p.owner_tasks || []) : (p.open_task_items || p.open_tasks || [])).map((x, i) => ({...norm(x, owner), _i: i})).sort((a, b) => a.rank - b.rank || a._i - b._i);
const prioCls = t => t.rank === 0 ? "p0" : t.rank === 1 ? "p1" : t.rank === 2 ? "p2" : "";
const todo = (t, proj, key, showProj) => `<div class="todo${t.owner ? " own" : ""}"><span class="pd ${prioCls(t)}" title="${esc(t.priority || "no priority")}"></span>
  <span class="tx">${showProj ? `<span class="pj">${esc(proj)}</span>` : ""}${esc(t.text)}</span>
  ${t.owner ? `<span class="chip ember">needs you</span>` : `<button class="btn sm" type="button" data-run="${esc(t.text)}" data-proj="${esc(proj)}" data-key="${esc(key)}" title="Launch an agent on this in ${esc(proj)}"><svg class="i sm" aria-hidden="true"><use href="#i-play"/></svg>Run</button>`}
  <button class="btn sm ghost" type="button" data-tick="${esc(t.text)}" data-proj="${esc(proj)}"${t.owner ? " data-owner" : ""} data-key="t:${esc(key)}" title="Mark this done in ${esc(proj)}/.brain/TODO.md (you can undo it)" aria-label="Mark done: ${esc(t.text)}">Done</button></div>`;

function projects() {
  const S = J.S, ghosts = S.status_rows.filter(r => !S.projects.some(p => p.name.toLowerCase() === r.folder.toLowerCase()));
  if (!S.projects.length && !ghosts.length) return `<div class="empty">No project folders with a <code>.brain/</code> in the workspace.</div>`;
  return S.projects.map(p => {
    const isOpen = open.has(p.name), pct = p.progress, blocker = J.noEmoji(p.blocker), status = J.noEmoji(p.status);
    const blocked = blocker && !/^none/i.test(blocker), own = items(p, true), opn = items(p, false);
    return `<div class="proj${isOpen ? " open" : ""}">
      <button class="proj-row" type="button" data-toggle="${esc(p.name)}" data-key="t:${esc(p.name)}" aria-expanded="${isOpen}">
        <span class="pr-name"><b>${esc(p.name)}</b><span>${esc(status || (p.in_status_table ? "" : "not in PROJECT_STATUS"))}</span></span>
        <span class="pr-bar"><span class="bar${pct >= 100 ? " green" : ""}"><i style="width:${pct || 0}%"></i></span><span class="num">${pct == null ? "—" : pct + "%"}</span></span>
        <span class="num pr-n">${pct == null ? "no markers" : `${p.tasks_done}/${p.tasks_total}`}</span>
        <span class="pr-own">${p.owner_count ? `<span class="chip ember">${p.owner_count} need you</span>` : ""}${blocked ? `<span class="chip red">blocked</span>` : ""}</span>
      </button>
      ${isOpen ? `<div class="proj-d">
        ${p.focus ? `<div class="kv"><span>Focus</span><b>${esc(J.noEmoji(p.focus))}</b>${blocked ? `<span>Blocker</span><b class="ink-red">${esc(blocker)}</b>` : ""}</div>` : ""}
        ${own.length ? `<h4 class="sect-h">Needs you</h4>${own.map((t, i) => todo(t, p.name, `o:${p.name}:${i}`)).join("")}` : ""}
        ${opn.length ? `<h4 class="sect-h">Open tasks</h4>${opn.slice(0, 12).map((t, i) => todo(t, p.name, `r:${p.name}:${i}`)).join("")}${opn.length > 12 ? `<div class="faint more">${opn.length - 12} more in ${esc(p.name)}/.brain/TODO.md</div>` : ""}` : ""}
        <div class="proj-acts"><button class="btn sm" type="button" data-here="${esc(p.name)}" data-key="h:${esc(p.name)}"><svg class="i sm" aria-hidden="true"><use href="#i-plus"/></svg>New task in ${esc(p.name)}</button>
        <a class="btn sm ghost" href="#/maps/${encodeURIComponent(p.name)}" data-key="m:${esc(p.name)}"><svg class="i sm" aria-hidden="true"><use href="#i-map"/></svg>${p.flow ? "Project map" : "Map this project"}</a></div></div>` : ""}
    </div>`;
  }).join("") + ghosts.map(r => `<div class="proj ghost"><div class="proj-row"><span class="pr-name"><b>${esc(r.folder)}</b><span>not in the workspace · ${esc(J.noEmoji(r.status))}</span></span></div></div>`).join("");
}
function brain() {
  const all = [];
  J.S.projects.forEach((p, pi) => {
    items(p, true).forEach((t, ti) => all.push({...t, p: p.name, _p: pi, _t: ti}));
    items(p, false).forEach((t, ti) => all.push({...t, p: p.name, _p: pi, _t: 1000 + ti}));
  });
  all.sort((a, b) => a.rank - b.rank || (b.owner ? 1 : 0) - (a.owner ? 1 : 0) || a._p - b._p || a._t - b._t);
  const list = prioOnly ? all.filter(x => x.rank < 99) : all, shown = allOwner ? list : list.slice(0, 14);
  q("#kn-n").textContent = all.length ? `${all.filter(x => x.rank < 99).length} priority / ${all.length}` : "";
  if (!list.length) return `<div class="empty">${all.length ? "No priority tasks in the project brains." : "No open tasks in the project brains."}</div>`;
  return shown.map((t, i) => todo(t, t.p, `b:${i}`, true)).join("") + (list.length > 14 ? `<button class="btn sm ghost more-btn" type="button" data-more data-key="more">${allOwner ? "Show fewer" : `Show all ${list.length}`}</button>` : "");
}
function ecosystem() {
  const rows = J.S.status_rows;
  if (!rows.length) return `<div class="empty">No rows in <code>_BRAIN/PROJECT_STATUS.md</code>.</div>`;
  return `<div class="tscroll"><table class="tbl"><thead><tr><th>Project</th><th>Category</th><th>Status</th><th>Focus</th><th>Blocker</th></tr></thead><tbody>${rows.map(r => {
    const b = J.noEmoji(r.blocker);
    return `<tr><td><b>${esc(r.name || r.folder)}</b></td><td class="muted">${esc(r.category || "")}</td><td>${esc(J.noEmoji(r.status))}</td><td class="muted">${esc(J.noEmoji(r.focus))}</td><td class="${b && !/^none/i.test(b) ? "ink-red" : "muted"}">${esc(b)}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}

let lastTick = null;   // {project, before, after, text}: the one change Undo can take back
function paintUndo() {
  const u = q("#kn-undo"); if (!u) return;
  u.hidden = !lastTick;
  u.innerHTML = lastTick ? `<span>Marked done in <b>${esc(lastTick.project)}</b>/.brain/TODO.md: ${esc(lastTick.text)}</span><button class="btn sm" type="button" data-undo data-key="undo">Undo</button>` : "";
}
async function tick(b) {
  const body = {project: b.dataset.proj, text: b.dataset.tick, owner: b.hasAttribute("data-owner")};
  b.disabled = true;
  try { const r = await J.api("/api/todo/tick", body); lastTick = {project: body.project, text: body.text, before: r.before, after: r.after}; paintUndo(); J.toast("Marked done"); J.poll(); }
  catch (e) { b.disabled = false; J.toast(e.message, true); }
}
async function undoTick() {
  if (!lastTick) return;
  try { await J.api("/api/todo/undo", {project: lastTick.project, before: lastTick.before, after: lastTick.after}); lastTick = null; paintUndo(); J.toast("Undone"); J.poll(); }
  catch (e) { J.toast(e.message, true); }
}

J.pages.knowledge = {
  init() {
    el = document.querySelector('[data-page="knowledge"]');
    el.innerHTML = `<div class="grid kn-grid">
        <section class="panel" aria-labelledby="kn-h1"><div class="ph"><h2 id="kn-h1">Project brains</h2><span class="n" id="kn-pc"></span><span class="act faint">progress from each <code>.brain/TODO.md</code></span></div><div class="projs" id="kn-proj"></div></section>
        <section class="panel" aria-labelledby="kn-h2"><div class="ph"><h2 id="kn-h2">Priority TODOs</h2><span class="n" id="kn-n"></span>
          <span class="act"><span class="seg" id="kn-f"><button type="button" data-f="prio">Priority</button><button type="button" data-f="all">All</button></span></span></div><div class="kn-undo" id="kn-undo" role="status" hidden></div><div class="pb todos" id="kn-brain"></div></section>
      </div>
      <section class="panel" aria-labelledby="kn-h3"><div class="ph"><h2 id="kn-h3">Ecosystem status</h2><span class="act faint">read-only · _BRAIN/PROJECT_STATUS.md</span></div><div id="kn-eco"></div></section>`;
    el.addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.toggle) { const n = b.dataset.toggle; open.has(n) ? open.delete(n) : open.add(n); this.render(); return; }
      if (b.dataset.tick !== undefined) { tick(b); return; }
      if (b.dataset.undo !== undefined) { undoTick(); return; }
      if (b.dataset.run !== undefined) { b.disabled = true; J.composer.runTask(b.dataset.proj, b.dataset.run).finally(() => { b.disabled = false; }); return; }
      if (b.dataset.here) { location.hash = "#/"; requestAnimationFrame(() => J.composer.setProject(b.dataset.here)); return; }
      if (b.dataset.f) { prioOnly = b.dataset.f === "prio"; J.saved.set("jarviee.prio", prioOnly ? "prio" : "all"); this.render(); return; }
      if (b.dataset.more !== undefined) { allOwner = !allOwner; this.render(); }
    });
  },
  render() {
    if (!J.S || !el) return;
    for (const b of q("#kn-f").children) b.setAttribute("aria-pressed", String((b.dataset.f === "prio") === prioOnly));
    q("#kn-pc").textContent = J.S.projects.length ? String(J.S.projects.length) : "";
    J.setHTML(q("#kn-proj"), projects());
    J.setHTML(q("#kn-brain"), brain());
    J.setHTML(q("#kn-eco"), ecosystem());
  },
  crumb() { const n = (J.S?.projects || []).reduce((s, p) => s + (p.owner_count || 0), 0); return `${(J.S?.projects || []).length} projects · ${n} owner items`; },
};
})();
