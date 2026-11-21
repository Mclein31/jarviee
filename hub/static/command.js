/* command.js: the Command Center page, plus the agent actions (stop, pane, copy) every page shares. */
"use strict";
(() => {
const {$, esc} = J;
let el = null;
const q = s => el.querySelector(s);

/* ---------- jump to an agent's own window (iTerm2 pane / terminal tab / editor) ---------- */
J.focusAgent = async id => {
  try { const r = await J.api("/api/focus", {id}); J.toast(r.message || (r.ok ? "Done" : "Couldn't find its window"), !r.ok); return r.ok; }
  catch (e) { J.toast(e.message, true); return false; }
};

/* ---------- right-click menu on agent cards and rows: window, details, workflow, copy, stop ---------- */
J.ctx = (() => {
  let menu = null, armed = null, armT = 0;
  const close = () => { menu?.remove(); menu = null; armed = null; clearTimeout(armT); };
  const item = (act, icon, label, {danger, disabled, title} = {}) =>
    `<button type="button" role="menuitem" class="ctx-i${danger ? " danger" : ""}" data-ctx="${act}"${disabled ? ' aria-disabled="true"' : ""}${title ? ` title="${esc(title)}"` : ""}><svg class="i sm" aria-hidden="true"><use href="#${icon}"/></svg><span>${esc(label)}</span></button>`;
  function items(a) {
    const live = ["running", "waiting"].includes(a.status), sub = a.source === "subagent", out = [];
    if (live) out.push(item("focus", "i-pane", sub ? "Go to its session's window" : "Go to its window"));
    out.push(item("open", "i-log", "Open details"), item("flow", "i-flow", "Open workflow"));
    if (sub && a.parent) out.push(item("parent", "i-agents", "Open parent session"));
    out.push(item("copy", "i-copy", "Copy prompt"));
    if (live) {
      const verb = a.stop_kind === "dismiss" ? "Dismiss" : "Stop";
      out.push('<div class="ctx-sep" role="separator"></div>');
      if (sub) out.push(item("none", "i-stop", "Stop", {disabled: true, title: "A subagent runs inside its parent session. Stop the parent, or stop it from that session."}));
      else if (a.stoppable === false) out.push(item("none", "i-stop", verb, {disabled: true, title: a.stop_reason || "This agent can't be stopped from here."}));
      else out.push(item("stop", "i-stop", verb === "Dismiss" ? "Dismiss (move to Finished)" : "Stop this task", {danger: true, title: a.stop_kind === "dismiss" ? a.stop_reason : "Sends SIGTERM to the agent"}));
    }
    return out.join("");
  }
  function open(id, x, y) {
    const a = J.S?.agents.find(r => r.id === id); if (!a) return;
    close();
    menu = document.createElement("div");
    menu.className = "ctx"; menu.setAttribute("role", "menu"); menu.dataset.id = id;
    menu.innerHTML = `<div class="ctx-h"><b>${esc(J.titleOf(a))}</b><span>${J.st(J.statusKey(a))}</span></div>${items(a)}`;
    document.body.appendChild(menu);
    const z = document.body.getBoundingClientRect().width / (document.body.offsetWidth || 1) || 1;   // the interface scale zooms body
    const vw = innerWidth / z, vh = innerHeight / z, w = menu.offsetWidth, h = menu.offsetHeight;
    menu.style.left = Math.max(6, Math.min(x / z, vw - w - 6)) + "px";
    menu.style.top = Math.max(6, Math.min(y / z, vh - h - 6)) + "px";
    menu.querySelector(".ctx-i:not([aria-disabled])")?.focus();
  }
  async function run(btn) {
    const id = menu?.dataset.id, a = J.S?.agents.find(r => r.id === id), act = btn.dataset.ctx;
    if (!a || btn.getAttribute("aria-disabled") === "true") return;
    if (act === "stop") {   // two clicks: the first arms it
      if (armed !== btn) { armed = btn; btn.classList.add("armed"); btn.querySelector("span").textContent = a.stop_kind === "dismiss" ? "Click again to dismiss" : "Click again to stop";
        clearTimeout(armT); armT = setTimeout(() => { if (menu) close(); }, 4000); return; }
      close(); J.act.stop(id); return;
    }
    close();
    if (act === "focus") J.focusAgent(id);
    else if (act === "open") location.hash = "#/agents/" + encodeURIComponent(id);
    else if (act === "flow") location.hash = "#/workflows/" + encodeURIComponent(id);
    else if (act === "parent") location.hash = "#/agents/" + encodeURIComponent(a.parent);
    else if (act === "copy") navigator.clipboard?.writeText(a.task || a.title || "").then(() => J.toast("Prompt copied"), () => J.toast("Copy blocked by the browser", true));
  }
  document.addEventListener("contextmenu", e => {
    const card = e.target.closest("[data-job], .arow[data-id]"); if (!card) { close(); return; }
    const id = card.dataset.job || card.dataset.id; if (!J.S?.agents.some(r => r.id === id)) return;
    e.preventDefault();
    let x = e.clientX, y = e.clientY;
    if (!x && !y) { const r = card.getBoundingClientRect(); x = r.left + 24; y = r.top + r.height / 2; }   // opened from the keyboard
    open(id, x, y);
  });
  document.addEventListener("click", e => { if (!menu) return; const b = e.target.closest(".ctx-i"); if (b && menu.contains(b)) { e.preventDefault(); run(b); } else if (!menu.contains(e.target)) close(); }, true);
  document.addEventListener("keydown", e => {
    if (!menu) return;
    if (e.key === "Escape") { e.stopPropagation(); close(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const its = [...menu.querySelectorAll(".ctx-i:not([aria-disabled])")], i = its.indexOf(document.activeElement);
      its[(i + (e.key === "ArrowDown" ? 1 : -1) + its.length) % its.length]?.focus();
    }
  }, true);
  for (const ev of ["resize", "hashchange", "blur"]) window.addEventListener(ev, close);
  document.addEventListener("scroll", close, true);
  return {open, close};
})();

/* ---------- shared agent actions ---------- */
const pending = new Map(), stopErr = new Map();
J.act = {
  pending, stopErr,
  html(a, {compact, noOpen, noFocus} = {}) {
    const p = pending.get(a.id), t = J.titleOf(a), out = [];
    if (["running", "waiting"].includes(a.status) && !p && !noFocus) out.push(`<button class="btn sm${J.groupOf(a) === "need" ? " primary" : ""}${compact ? " icon" : ""}" type="button" data-act="focus" data-key="focus:${esc(a.id)}" title="Go to its window" aria-label="Go to the window of ${esc(t)}"><svg class="i sm" aria-hidden="true"><use href="#i-pane"/></svg>${compact ? "" : "<span>Go to window</span>"}</button>`);
    if (["running", "waiting"].includes(a.status) && a.source !== "subagent") {   // a subagent stops with its parent
      const dismiss = a.stop_kind === "dismiss", verb = dismiss ? "Dismiss" : "Stop";
      if (p === "stopping") out.push(`<span class="stopping" role="status">${dismiss ? "Dismissing…" : "Stopping…"}</span>`);
      else if (p === "confirm") out.push(`<span class="confirm" role="group" aria-label="Confirm ${verb.toLowerCase()}"><span>${verb}?</span><button class="btn sm danger" type="button" data-act="stop-yes" data-key="yes:${esc(a.id)}">Yes</button><button class="btn sm" type="button" data-act="stop-no" data-key="no:${esc(a.id)}">No</button></span>`);
      else {
        const why = a.stoppable === false ? (a.stop_reason || "This agent can't be stopped from here.") : "";
        const tip = why || (dismiss ? `Can't signal this agent: ${a.stop_reason} Dismiss moves it to Finished.` : "Stop this agent (SIGTERM)");
        out.push(`<button class="btn sm${compact ? " icon" : ""}" type="button" data-act="stop" data-key="stop:${esc(a.id)}" ${why ? 'aria-disabled="true" ' : ""}title="${esc(tip)}" aria-label="${verb} ${esc(t)}"><svg class="i sm" aria-hidden="true"><use href="#i-stop"/></svg>${compact ? "" : `<span>${verb}</span>`}</button>`);
      }
    }
    /* a stale session (still "waiting" long after its app went quiet) can always be closed: the server marks it stopped */
    if (a.status === "stale" && a.source !== "subagent") {
      if (p === "stopping") out.push(`<span class="stopping" role="status">Closing…</span>`);
      else if (p === "confirm") out.push(`<span class="confirm" role="group" aria-label="Confirm close"><span>Close?</span><button class="btn sm danger" type="button" data-act="stop-yes" data-key="yes:${esc(a.id)}">Yes</button><button class="btn sm" type="button" data-act="stop-no" data-key="no:${esc(a.id)}">No</button></span>`);
      else out.push(`<button class="btn sm${compact ? " icon" : ""}" type="button" data-act="stop" data-key="stop:${esc(a.id)}" title="Close this stale session (marks it stopped; nothing is signalled)" aria-label="Close ${esc(t)}"><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg>${compact ? "" : "<span>Close</span>"}</button>`);
    }
    if (!p && !noOpen) out.push(`<a class="btn icon sm ghost" href="#/agents/${encodeURIComponent(a.id)}" data-key="open:${esc(a.id)}" title="Open" aria-label="Open ${esc(t)}"><svg class="i sm" aria-hidden="true"><use href="#i-log"/></svg></a>`);
    return `<span class="acts">${out.join("")}</span>`;
  },
  ask(id) {
    const a = J.S?.agents.find(x => x.id === id);
    if (!a || !["running", "waiting", "stale"].includes(a.status)) return;
    if (a.stoppable === false && a.status !== "stale") { J.toast(a.stop_reason || "This agent can't be stopped from here."); return; }
    stopErr.delete(id); pending.set(id, "confirm"); J.emit("state", J.S);
    requestAnimationFrame(() => document.querySelector(`[data-key="yes:${CSS.escape(id)}"]`)?.focus());
  },
  async stop(id) {
    pending.set(id, "stopping"); J.emit("state", J.S);
    let res;
    try { res = await J.api("/api/stop", {id}); } catch (e) { res = {ok: false, reason: e.message}; }
    pending.delete(id);
    if (!res?.ok) { stopErr.set(id, `Not stopped: ${res?.reason || "unknown error"}`); J.toast(stopErr.get(id), true); setTimeout(() => stopErr.delete(id), 6000); }
    else { const a = J.S.agents.find(x => x.id === id); const was = a?.status; if (a) Object.assign(a, {status: "stopped", activity: a.stop_kind === "dismiss" ? "dismissed by you" : "stopped by you", ended: J.now()}); J.toast(was === "stale" ? "Closed" : "Stopped"); }
    J.emit("state", J.S); J.poll();
  },
  cancel(id) { pending.delete(id); J.emit("state", J.S); requestAnimationFrame(() => document.querySelector(`[data-key="stop:${CSS.escape(id)}"]`)?.focus()); },
  async pane(id) { try { const r = await J.api("/api/terminal", {action: "show", id}); if (r.message || r.reason) J.toast(r.message || r.reason, r.ok === false); } catch (e) { J.toast(e.message, true); } },
  /* one delegated handler for any container holding agent rows */
  bind(container) {
    container.addEventListener("click", e => {
      const b = e.target.closest("[data-act]"); if (!b) return;
      const id = b.closest("[data-id]")?.dataset.id || b.dataset.id; if (!id) return;
      e.preventDefault(); e.stopPropagation();
      const act = b.dataset.act;
      if (act === "stop") J.act.ask(id); else if (act === "stop-yes") J.act.stop(id); else if (act === "stop-no") J.act.cancel(id); else if (act === "pane") J.act.pane(id);
      else if (act === "focus") J.focusAgent(id);
    });
  },
};

/* ---------- KPI strip ---------- */
function kpis() {
  refreshOmni();
  const S = J.S, U = J.U, g = J.groups(), td = J.today();
  const tiles = [];
  const oldest = g.need[0];
  /* each tile owns one hue (amber you, blue running, green done, violet quota) for its icon, its figure's mini
     chart and, when hot, its edge. Charts draw only real data: one mark per waiting agent, one pip per lane slot,
     finished runs per hour. */
  const head = (icon, label, open) => `<span class="kh"><span class="kic"><svg class="i sm" aria-hidden="true"><use href="#${icon}"/></svg></span>
    <span class="kl">${label}</span>${open ? `<span class="kgo" aria-hidden="true"><svg class="i sm"><use href="#i-open"/></svg></span>` : ""}</span>`;
  const waitMarks = g.need.slice(0, 12).map(() => "<i></i>").join("");
  tiles.push(`<a class="kpi k-need need${g.need.length ? " hot" : ""}" href="#/agents?f=need" data-key="k-need">
    ${head("i-bell", "Needs you", true)}<span class="kv num">${g.need.length}</span>
    <span class="kmarks${g.need.length ? "" : " none"}" aria-hidden="true">${g.need.length ? waitMarks : ""}</span>
    <span class="ks">${oldest ? `oldest waiting ${J.span(J.now() - (oldest.updated || oldest.started || J.now()))}` : "nothing waiting"}</span></a>`);
  const lanes = Object.entries(S.queue?.lanes || {});
  const lanesTxt = lanes.map(([t, l]) => `${J.TOOL_SHORT[t] || t} ${l.running}/${l.capacity}`).join(" · ");
  /* one pip per running session, in its tool's colour; the queue's own slots are named in the sub-line */
  /* a slot gauge per tool: a lit pip per running session in the tool's colour, a faint pip per free slot */
  const byTool = {};
  for (const a of g.run) byTool[a.tool] = (byTool[a.tool] || 0) + 1;
  const caps = Object.fromEntries(lanes.map(([t, l]) => [t, l.capacity || 0]));
  const pips = [...new Set([...Object.keys(caps), ...Object.keys(byTool)])].map(t => {
    const n = byTool[t] || 0, slots = Math.min(8, Math.max(n, caps[t] || 0));
    return `<span class="kslot" title="${esc(J.TOOL[t] || t)}: ${n} running of ${caps[t] || n} slot${(caps[t] || n) === 1 ? "" : "s"}">${Array.from({length: slots}, (_, i) => i < n ? `<i class="t-${esc(t)}"></i>` : `<i class="free"></i>`).join("")}</span>`;
  }).join("");
  tiles.push(`<a class="kpi k-run" href="#/agents?f=run" data-key="k-run">${head("i-play", "Running", true)}<span class="kv num">${g.run.length}</span>
    ${pips ? `<span class="kslots" aria-hidden="true">${pips}</span>` : ""}<span class="ks">${g.run.length} session${g.run.length === 1 ? "" : "s"}${lanesTxt ? ` · queue ${esc(lanesTxt)}` : ""}</span></a>`);
  const hours = (U?.activity?.hours || []).map(h => h.agy + h.claude + h.other);
  tiles.push(`<a class="kpi k-done" href="#/analytics" data-key="k-done">${head("i-check", "Finished today", true)}<span class="kv num">${U?.activity?.finished_today ?? td.done + td.failed}</span>
    ${hours.some(v => v) ? J.bars(hours) : ""}
    <span class="ks">${td.failed ? `<span class="ink-red">${td.failed} failed</span> · ` : ""}${U?.activity ? `${U.activity.finished_24h} in 24 h` : "—"}</span></a>`);
  /* tokens and cost live on Analytics; here they are one line in Today */
  const c = U?.claude || {}, a = U?.agy || {};
  const gs = a.quota?.groups || [], isArgon = g => /argon/i.test(g.name), isHydro = g => /hydrogen/i.test(g.name), is3p = g => /claude|gpt/i.test(g.name);
  const win = (list, w) => list.find(x => x.window === w || x.label === w);
  const cw = c.limits?.windows || [], gH = gs.find(isHydro), gA = gs.find(isArgon) || {};
  const gAgy = gs.find(g => /gemini/i.test(g.name) && !isArgon(g) && !isHydro(g))
    || gs.find(g => !isArgon(g) && !isHydro(g) && !is3p(g))
    || gs.find(g => !isArgon(g) && !isHydro(g)) || {};
  const rows = [
    ["Claude", {"5h": cw.find(w => w.id === "five_hour"), wk: cw.find(w => w.id === "seven_day")}],
    ["agy", {"5h": win(gAgy.buckets || [], "5h"), wk: win(gAgy.buckets || [], "weekly")}],
    ["Argon", {"5h": win(gA.buckets || [], "5h"), wk: win(gA.buckets || [], "weekly")}],
  ];
  if (gH) rows.push(["Hydrogen", {"5h": win(gH.buckets || [], "5h"), wk: win(gH.buckets || [], "weekly")}]);
  const seg = (name, w) => {
    const used = w ? (w.used_pct ?? w.used) : null, left = used == null ? null : Math.max(0, 100 - used);
    const lvl = left == null ? "grey" : left <= 0 ? "red" : "";   // red only when it is used up; otherwise the tool's hue
    const out = left != null && left <= 0, reset = w?.resets_at ? "resets in " + J.span(w.resets_at - J.now()) : "";
    /* used up: the whole track turns into a red hatch (an empty 0% bar read as broken), the figure goes red */
    return `<span class="ql-seg${out ? " out" : ""}" title="${esc(out ? `Used up${reset ? ", " + reset : ""}` : reset)}"><small>${name}</small><span class="bar ${lvl}"><i style="width:${out ? 100 : left ?? 0}%"></i></span><b class="num">${left == null ? "—" : Math.round(left) + "%"}</b></span>`;
  };
  let omniChip = "";
  if (omniCache) {
    const ok = omniCache.enabled && omniCache.healthy;
    const state = !omniCache.enabled ? "off" : !omniCache.has_key ? "no key" : ok ? "ready" : "offline";
    const kind = !omniCache.enabled ? "idle" : ok ? "done" : "out";
    omniChip = `<span class="ql-row"><span class="ql-n">OmniRoute</span>${J.st(kind, state)}</span>`;
  }
  tiles.push(`<a class="kpi k-quota quota-kpi" href="#/analytics" data-key="k-quota" title="Open Analytics for tokens, cost and history">${head("i-chart", "Quota left · 5h / weekly", true)}
    <span class="ql">${rows.map(([n, w]) => `<span class="ql-row t-${n === "Claude" ? "claude" : "agy"}"><span class="ql-n">${n}</span>${seg("5h", w["5h"])}${seg("wk", w.wk)}</span>`).join("")}${omniChip}</span></a>`);
  J.setHTML(q("#kpis"), tiles.join(""));
}
let omniCache = null, omniAt = 0;
function refreshOmni() {
  if (Date.now() - omniAt < 30000) return;
  omniAt = Date.now();
  J.api("/api/omniroute/status").then(r => { omniCache = r; if (el && J.S) kpis(); }).catch(() => {});
}

/* ---------- today (standup) ---------- */
/* open owner items from the project brains: the words only (the brain's own marker glyph is dropped), project named once */
function ownerItems(rows) {
  const items = rows.flatMap(([n, p]) => (p.owner_items || []).map(i => [n, J.noEmoji(i).replace(/`/g, "")]));
  if (!items.length) return `<div class="owner-none">${J.sd("done")}<span>Nothing in the project brains is waiting on you.</span></div>`;
  return `<h4 class="sect-h owner-h">Waiting on you <span class="num">${items.length}</span></h4><ul class="owner-list">${items.slice(0, 4).map(([n, i]) =>
    `<li title="${esc(i)}"><svg class="i sm" aria-hidden="true"><use href="#i-agents"/></svg><span class="t">${esc(i)}</span><span class="p">${esc(n)}</span></li>`).join("")}</ul>`
    + (items.length > 4 ? `<a class="more" href="#/knowledge">${items.length - 4} more in Knowledge</a>` : "");
}
/* one line of spend: tokens and estimated cost today (details on Analytics) */
function spend() {
  const U = J.U; if (!U) return "";
  const c = U.claude || {}, a = U.agy || {}, tok = (c.tokens_today?.total || 0) + (a.tokens_today?.total || 0);
  return `<a class="today-spend" href="#/analytics" title="Claude ${J.fmtTok(c.tokens_today?.total)} · agy ${J.fmtTok(a.tokens_today?.total)}. Cost is Claude Code at API rates.">`
    + `<span><b class="num">${J.fmtTok(tok)}</b> tokens</span><span><b class="num">${c.cost_today != null ? J.fmtUsd(c.cost_today) : "—"}</b> est. cost</span></a>`;
}
let todayAt = 0;
async function today() {
  if (Date.now() - todayAt < 60000) return;
  todayAt = Date.now();
  try {
    const s = await J.api("/api/standup"), t = s.totals, rows = Object.entries(s.projects);
    J.setHTML(q("#cc-today"), `<div class="fy-col">${ownerItems(rows)}</div><div class="fy-col">`
      + `<h4 class="sect-h owner-h">Today</h4><div class="today-sum muted">${t.done} done${t.failed ? ` · <span class="ink-red">${t.failed} failed</span>` : ""} · ${Math.round(t.seconds / 60)} min agent time${t.escalations ? ` · ${t.escalations} rerouted` : ""}</div>`
      + spend()
      + rows.map(([n, p]) => `<div class="mini-row"><span class="t">${esc(n)}</span><span class="num muted">${p.done} done${p.failed ? ` · <span class="ink-red">${p.failed} failed</span>` : ""}</span></div>`).join("") + `</div>`);
  } catch (e) { todayAt = 0; J.setHTML(q("#cc-today"), `<span class="muted">Standup unavailable (restart the server?)</span>`); }
}

/* ---------- active agents ---------- */
let showDone = J.saved.get("jarviee.ccdone") !== "0";
function agentRow(a) {
  const k = J.statusKey(a), err = stopErr.get(a.id), g = J.groupOf(a);
  /* one quiet line under the title: where it runs, then what it is doing (or why it needs you) */
  const what = g === "run" ? (a.activity || a.summary || "") : (a.summary || a.activity || "");
  const proj = J.place(a), lead = proj ? `<span class="sub-p">${esc(proj)}</span>${what || a.status === "failed" ? '<span class="sub-dot" aria-hidden="true">·</span>' : ""}` : "";
  const sub = err ? `<span class="sub err">${esc(err)}</span>` : g === "need" ? `<span class="sub need" title="${esc(J.replyWhere(a).text)}">${esc(J.waitReason(a.activity))} · ${esc(J.replyWhere(a).text.split(".")[0])}</span>`
    : a.status === "failed" ? `<span class="sub err">${lead}${esc(a.activity || "failed")}</span>` : `<span class="sub">${lead}${esc(what)}</span>`;
  const fb = a.fallback_from ? `<span class="chip violet" title="Rerouted from ${esc(a.fallback_from)}"><svg class="i sm" aria-hidden="true"><use href="#i-route"/></svg>from ${esc(a.fallback_from.split(":")[0])}</span>` : "";
  const end = g === "done" ? J.endOf(a) : "";
  return `<span class="c-st">${J.sd(k)}</span>
    <span class="c-t"><a class="t" href="#/agents/${encodeURIComponent(a.id)}" title="${esc(J.titleOf(a))}">${esc(J.titleOf(a))}</a>${fb}${sub}</span>
    <span class="c-a">${J.toolMono(a.tool)}<span class="num" title="${esc(a.model || "")}">${esc(J.modelLabel(a.model) || "—")}</span></span>
    <span class="c-tier">${J.agentPill(a)}</span>
    <span class="c-n num clock" title="${a.steps || 0} steps" data-start="${a.started || ""}" data-end="${end || ""}">${a.started ? J.dur((end || J.now()) - a.started) : "—"}</span>
    ${J.act.html(a, {compact: true})}`;
}
function agents() {
  const g = J.groups(), live = [...g.need, ...g.run], done = showDone ? g.done.slice(0, Math.max(2, 5 - live.length)) : [];
  const list = [...live, ...done];
  q("#n-active").textContent = live.length ? String(live.length) : "";
  const box = q("#cc-agents");
  J.patch(box, list, {key: a => a.id, html: agentRow, cls: a => `arow g-${J.groupOf(a)}`,
    sig: a => JSON.stringify([a.status, a.title, a.activity, a.summary, a.model, a.tier, a.suggested_tier, a.steps, a.ended, a.pane, a.stoppable, a.stop_kind, J.place(a), a.fallback_from, J.act.pending.get(a.id), stopErr.get(a.id)]),
    attrs: a => ({tabindex: "0", "aria-label": `${J.titleOf(a)}, ${J.STAT[J.statusKey(a)]}`})});
  q("#cc-agents-empty").hidden = list.length > 0;
  q("#cc-done-toggle").setAttribute("aria-pressed", String(showDone));
}

/* ---------- coordination: the last combined report, who holds which files, and the agents' channel ---------- */
const KIND = {claim: ["run", "started"], change: ["queue", "editing"], note: ["need", "note"], finish: ["done", "finished"], wait: ["need", "waiting"], handoff: ["fb", "hand-off"]};
function coordination() {
  const S = J.S, C = S.coordination || {}, R = S.report || {}, byId = new Map(S.agents.map(a => [a.id, a]));
  const name = id => { const a = byId.get(id); return a ? J.titleOf(a) : String(id || "").replace(/^(claude|agy)-([0-9a-f]{6})[0-9a-f-]*$/, "$1 $2"); };
  const base = p => String(p || "").split("/").filter(Boolean).pop() || "";
  /* message bodies start with the sender's raw id; the row already names the sender */
  const short = t => String(t).replace(/(?:[\w.~-]*\/)+([\w.-]+)/g, "$1");
  const body = m => short(String(m.body || "").replace(new RegExp("^" + String(m.sender).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s+(is\\s+)?(changing\\s+|started\\s+|done\\s+)?"), "")
    .replace(/\b(run-[0-9a-f]{8}|(?:claude|agy)-[0-9a-f-]{8,})\b/g, id => byId.has(id) ? `"${name(id)}"` : id));
  const claims = (C.claims || []).filter(c => c.status === "active");
  /* a run of edits by one agent is one line: "editing a.py, b.js +3" */
  const msgs = [];
  for (const m of [...(C.messages || [])].reverse()) {
    const last = msgs[msgs.length - 1];
    if (last && m.kind === "change" && last.kind === "change" && last.sender === m.sender) { last.files.push(body(m)); continue; }
    if (msgs.length === 6) break;
    msgs.push({...m, files: m.kind === "change" ? [body(m)] : null});
  }
  const parts = [];
  if (R.text) {
    const [first, ...rest] = String(R.text).split("\n");
    parts.push(`<div class="co-report"><div class="co-h"><span>Last report</span><span class="num faint">${R.at ? J.ago(R.at) : ""}</span></div>
      <p class="co-first">${esc(first)}</p>${rest.filter(Boolean).slice(0, 3).map(l => `<p class="co-line">${esc(l)}</p>`).join("")}</div>`);
  }
  if (claims.length) parts.push(`<h4 class="sect-h">Files held <span class="num">${claims.length}</span></h4>` + claims.map(c => {
    const files = (c.files || []).map(base).filter(Boolean);
    return `<a class="mini-row" href="#/agents/${encodeURIComponent(c.agent_id)}" title="${esc((c.files || []).join("\n") || "No files claimed yet")}">${J.sd("run")}<span class="t">${esc(name(c.agent_id))}</span>
      <span class="num muted">${files.length ? esc(files.slice(0, 2).join(", ")) + (files.length > 2 ? ` +${files.length - 2}` : "") : "no files yet"}</span></a>`;
  }).join(""));
  if (msgs.length) parts.push(`<h4 class="sect-h">Channel</h4><div class="co-msgs">${msgs.map(m => {
    const [k, word] = KIND[m.kind] || ["queue", m.kind || "note"];
    return `<div class="co-m" title="${esc(m.body)}"><span class="num faint">${J.clock(m.ts)}</span>${J.sd(k)}<span class="co-x"><b>${esc(name(m.sender))}</b>${m.recipient ? ` <span class="faint">to</span> <b>${esc(name(m.recipient))}</b>` : ""} <span class="co-k">${esc(word)}</span><span class="co-b">${esc(m.files ? [...new Set(m.files)].slice(0, 3).join(", ") + (new Set(m.files).size > 3 ? ` +${new Set(m.files).size - 3}` : "") : body(m))}</span></span></div>`;
  }).join("")}</div>`);
  J.setHTML(q("#cc-coord"), parts.join("") || `<div class="empty">Nothing to coordinate. When two agents work in one project, their file claims and messages show here.</div>`);
}

/* ---------- quick actions (the workload donut repeated the KPI strip, so it is gone) ---------- */
function workload() {
  const Q = J.S?.queue, T = J.S?.terminal, latest = (J.S?.queue?.plans || [])[0];
  J.setHTML(q("#cc-quick"), `
    <button class="qa" type="button" data-q="plan" data-key="qa-plan"><svg class="i" aria-hidden="true"><use href="#i-split"/></svg>Plan a goal</button>
    <button class="qa" type="button" data-q="pause" data-key="qa-pause" ${Q ? "" : "disabled"}><svg class="i" aria-hidden="true"><use href="#${Q?.paused ? "i-play" : "i-pause"}"/></svg>${Q?.paused ? "Resume queue" : "Pause queue"}</button>
    <button class="qa" type="button" data-q="clear" data-key="qa-clear" ${J.groups().done.length ? "" : "disabled"}><svg class="i" aria-hidden="true"><use href="#i-broom"/></svg>Clear finished</button>
    ${T && T.installed && T.mode === "iterm2" ? `<button class="qa" type="button" data-q="retile" data-key="qa-retile" ${T.enabled ? "" : "disabled"} title="${T.enabled ? "Even out the panes in the Jarviee iTerm2 window" : "Turn on iTerm2 panes in Settings"}"><svg class="i" aria-hidden="true"><use href="#i-retile"/></svg>Re-tile panes</button>` : ""}
    <a class="qa" href="#/workflows${latest ? "/" + encodeURIComponent(latest.id) : ""}" data-key="qa-flow"><svg class="i" aria-hidden="true"><use href="#i-flow"/></svg>Latest workflow</a>`);
}
/* ---------- collapsed sections: each says in one line what is inside ---------- */
const ACC = {routing: true, coord: false};   // the default until the owner opens or closes one
const accOpen = k => { const v = J.saved.get("jarviee.acc." + k); return v == null ? ACC[k] : v === "1"; };
function acc(k, title, body, {cls = "", extra = ""} = {}) {
  return `<details class="panel acc ${cls}" data-acc="${k}"${accOpen(k) ? " open" : ""}>
    <summary class="ph"><svg class="i sm chev" aria-hidden="true"><use href="#i-chev"/></svg><h2 id="h-${k}">${title}</h2><span class="acc-sum" id="sum-${k}"></span>${extra}</summary>
    <div class="acc-b">${body}</div></details>`;
}
function digests() {
  const set = (k, html) => J.setHTML(q("#sum-" + k), html);
  const ts = J.S?.tool_status || {}, out = Object.entries(ts).filter(([, t]) => !t.available).map(([t]) => J.TOOL_SHORT[t] || t);
  const d0 = new Date(); d0.setHours(0, 0, 0, 0);
  const routed = (J.S?.agents || []).filter(a => (a.started || 0) >= d0 / 1000).length;
  set("routing", `${out.length ? `<span class="ink-red">${esc(out.join(", "))} out</span>` : `${J.sd("done")}<span>all lanes ready</span>`}<span class="faint">· ${routed} routed today</span>`);
  const C = J.S?.coordination || {}, held = (C.claims || []).filter(c => c.status === "active").length, msgs = (C.messages || []).length;
  set("coord", held || msgs ? `<span>${held} holding files</span><span class="faint">· ${msgs} messages</span>` : `<span class="faint">nothing to coordinate</span>`);
}
async function quick(e) {
  const b = e.target.closest("[data-q]"); if (!b) return;
  const k = b.dataset.q;
  if (k === "new") return J.composer.focus();
  if (k === "plan") return J.composer.plan();
  b.disabled = true;
  try {
    if (k === "pause") { await J.api(J.S.queue.paused ? "/api/queue/resume" : "/api/queue/pause", {}); J.toast(J.S.queue.paused ? "Queue resumed" : "Queue paused"); }
    if (k === "clear") { await J.api("/api/clear", {}); J.toast("Cleared finished agents"); }
    if (k === "retile") { const r = await J.api("/api/terminal", {action: "retile"}); J.toast(r.message || "Re-tiled"); }
  } catch (err) { J.toast(err.message, true); }
  b.disabled = false; J.poll();
}

J.pages.command = {
  init() {
    el = document.querySelector('[data-page="command"]');
    el.innerHTML = `
      <div id="composer"></div>
      <div class="kpis" id="kpis" aria-label="Today at a glance"></div>
      ${acc("routing", "Routing", `<div class="grid cc-a">
          <div class="fleet" id="fleet" role="group" aria-label="Routing: router, tool lanes and tier models"></div>
          <div class="detail-panel"><div class="pb" id="fleet-detail"></div></div></div>`,
        {cls: "fleet-panel", extra: `<span class="fz" role="group" aria-label="Routing zoom"><button class="btn icon sm ghost" type="button" data-fz="-" aria-label="Zoom out"><svg class="i sm" aria-hidden="true"><use href="#i-minus"/></svg></button><span class="num faint" id="fz-v">100%</span><button class="btn icon sm ghost" type="button" data-fz="+" aria-label="Zoom in"><svg class="i sm" aria-hidden="true"><use href="#i-plus"/></svg></button><button class="btn sm ghost" type="button" data-fz="fit" aria-pressed="false" title="Keep the whole graph, every job card included, in view">Fit</button></span><span class="legend-inline"><span>${J.sd("run")}live</span><span>${J.sd("need")}needs you</span><span>${J.sd("out")}out</span></span>`})}
      <div class="grid cc-main">
        <section class="panel" aria-labelledby="h-active">
          <div class="ph"><h2 id="h-active">Active agents</h2><span class="n" id="n-active"></span>
            <span class="act"><button class="btn sm ghost" type="button" id="cc-done-toggle" aria-pressed="true" title="Show the last finished runs under the live ones">Recent</button><a class="btn sm ghost" href="#/agents">All agents<svg class="i sm" aria-hidden="true"><use href="#i-arrow"/></svg></a></span></div>
          <div class="atable" role="list" id="cc-agents"></div>
          <div class="empty" id="cc-agents-empty" hidden>No agents running. Type a task above, or start one in Antigravity or Claude Code.</div>
          <div class="quick" id="cc-quick" role="group" aria-label="Quick actions"></div>
        </section>
      </div>
      ${acc("coord", "Coordination", `<div class="pb coord" id="cc-coord"></div>`)}
      <section class="panel cc-side" aria-labelledby="h-today"><div class="ph"><h2 id="h-today">For you</h2></div><div class="pb fy" id="cc-today"><span class="muted">Reading…</span></div></section>`;
    J.composer.mount(q("#composer"));
    J.fleet.mount(q("#fleet"), q("#fleet-detail"));
    J.act.bind(q("#cc-agents"));
    q("#cc-quick").addEventListener("click", quick);
    el.addEventListener("toggle", e => {
      const d = e.target.closest?.("details[data-acc]"); if (!d) return;
      J.saved.set("jarviee.acc." + d.dataset.acc, d.open ? "1" : "0");
      if (d.open && d.dataset.acc === "routing") J.fleet.render();
    }, true);
    q("#cc-done-toggle").addEventListener("click", () => { showDone = !showDone; J.saved.set("jarviee.ccdone", showDone ? "1" : "0"); agents(); });
    q("#cc-agents").addEventListener("click", e => {
      const row = e.target.closest(".arow.g-need"); if (!row || e.target.closest("a, button")) return;
      J.focusAgent(row.dataset.id);
    });
    q("#cc-agents").addEventListener("keydown", e => {
      const row = e.target.closest(".arow"); if (!row || e.target !== row) return;
      if (e.key === "Enter") { location.hash = "#/agents/" + encodeURIComponent(row.dataset.id); }
      if (e.key === "s") { e.preventDefault(); J.act.pending.get(row.dataset.id) === "confirm" ? J.act.stop(row.dataset.id) : J.act.ask(row.dataset.id); }
    });
    J.on("escape", () => { for (const [id, v] of J.act.pending) if (v === "confirm") J.act.cancel(id); });
  },
  render(ev) {
    if (!J.S) return;
    if (ev === "activity") return;   // the tool-call feed is gone; the agent's own terminal shows its progress
    J.composer.render(); kpis(); J.fleet.render(); agents(); workload(); coordination(); today(); digests();
  },
  tick() {
    for (const c of el.querySelectorAll(".clock[data-start]")) { const s = +c.dataset.start, e = +c.dataset.end; if (!s) continue; const t = J.dur((e || J.now()) - s); if (c.textContent !== t) c.textContent = t; }
    for (const c of el.querySelectorAll("[data-until]")) { const t = J.span(+c.dataset.until - J.now()); if (c.textContent !== t) c.textContent = t; }
    J.fleet.tick();
  },
  key(e) {
    if (e.key === "j" || e.key === "k") {
      const rows = [...el.querySelectorAll(".arow")]; if (!rows.length) return;
      e.preventDefault();
      const i = rows.indexOf(document.activeElement.closest?.(".arow"));
      const n = Math.max(0, Math.min(rows.length - 1, i < 0 ? 0 : i + (e.key === "j" ? 1 : -1)));
      rows[n].focus(); rows[n].scrollIntoView({block: "nearest"});
    }
  },
  crumb() { const g = J.groups(); return `${g.need.length} need you · ${g.run.length} running`; },
};
})();
