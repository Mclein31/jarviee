/* tasks.js: the two-lane queue and every plan's subtasks (approve, discard, cancel, retry, reroute). */
"use strict";
(() => {
const {esc} = J;
let el = null, showAll = J.saved.get("jarviee.allplans") === "1";
const q = s => el.querySelector(s);
const OPEN = ["running", "queued", "blocked", "waiting"];
const ENDED = ["failed", "cancelled", "done"];   // a plan that will not run again
/* plans the owner closed with ×: hidden for good, even under "Show finished plans" (kept in this browser) */
const HKEY = "jarviee.hiddenplans";
const hidden = (() => { try { return new Set(JSON.parse(localStorage.getItem(HKEY) || "[]")); } catch (e) { return new Set(); } })();
const saveHidden = () => { try { localStorage.setItem(HKEY, JSON.stringify([...hidden])); } catch (e) { /* storage blocked */ } };
const TST = {running: "run", queued: "queue", blocked: "queue", waiting: "need", done: "done", failed: "fail", cancelled: "stopped", draft: "queue"};
const short = id => String(id).split(".").pop();
const btn = (act, id, label, aria, cls = "") => `<button class="btn sm ${cls}" type="button" data-qact="${act}" data-tid="${esc(id)}" data-key="${act}:${esc(id)}" aria-label="${esc(aria)}">${label}</button>`;

function acts(t) {
  const other = t.tool === "agy" ? "claude" : "agy", a = [];
  if (OPEN.includes(t.status)) a.push(btn("cancel", t.id, "Cancel", `Cancel ${t.title}`, "ghost"));
  if (["failed", "cancelled", "blocked"].includes(t.status)) a.push(btn("retry", t.id, "Retry", `Retry ${t.title}`));
  if (!["running", "done", "draft"].includes(t.status)) a.push(btn("reroute", t.id, `To ${J.TOOL_SHORT[other]}`, `Send ${t.title} to ${other}`, "ghost"));
  return a.join("");
}
/* a draft plan's tasks can be re-levelled, moved to the other tool or dropped before Approve */
const pick = (field, t, opts, aria) => `<select class="select sm" data-edit="${field}" data-tid="${esc(t.id)}" data-key="${field}:${esc(t.id)}" aria-label="${esc(aria)}">${opts.map(([v, l]) => `<option value="${esc(v)}"${v === t[field] ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
/* "After": the tasks this one waits for, as a row of id chips that wraps */
const depChips = t => (t.depends_on || []).length ? `<span class="deps">${t.depends_on.map(d => `<span class="chip mono" title="${esc(d)}">${esc(short(d))}</span>`).join("")}</span>` : `<span class="faint">–</span>`;
function draftRow(t) {
  const tools = Object.keys(J.S.queue.lanes).map(k => [k, J.TOOL_SHORT[k] || k]);
  return `<tr data-tid="${esc(t.id)}"><td class="num faint tid" title="${esc(t.id)}">${esc(short(t.id))}</td>
    <td class="clip"><div class="tt" title="${esc(t.prompt || t.title)}">${esc(t.title)}</div>${t.files?.length ? `<div class="tf num">${esc(t.files.join("  "))}</div>` : ""}</td>
    <td>${pick("level", t, J.TIERS.map(l => [l, J.TIER_WORD[l]]), `Level of ${t.title}`)}</td>
    <td><span class="runs-on">${pick("tool", t, tools, `Tool for ${t.title}`)}<span class="num">${esc(t.model)}</span></span></td>
    <td>${depChips(t)}</td>
    <td>${J.st(TST[t.status] || "queue", t.status)}</td>
    <td class="r"><span class="acts">${t.status === "cancelled" ? "" : btn("drop", t.id, "Drop", `Drop ${t.title} from the plan`, "ghost")}</span></td></tr>`;
}
function taskRow(t) {
  return `<tr data-tid="${esc(t.id)}"><td class="num faint tid" title="${esc(t.id)}">${esc(short(t.id))}</td>
    <td class="clip"><div class="tt" title="${esc(t.prompt || t.title)}">${esc(t.title)}</div>${t.route_note ? `<div class="tn">${esc(t.route_note)}</div>` : ""}${t.files?.length ? `<div class="tf num">${esc(t.files.join("  "))}</div>` : ""}</td>
    <td>${J.tierPill(t.level, t.router_level || t.level, [])}</td>
    <td><span class="runs-on">${J.toolMono(t.tool)}<span class="num">${esc(t.model)}</span></span></td>
    <td>${depChips(t)}</td>
    <td>${J.st(TST[t.status] || "queue", t.status)}${t.escalated ? ` <span class="chip violet">escalated</span>` : ""}</td>
    <td class="r"><span class="acts">${acts(t)}</span></td></tr>`;
}
function lanes() {
  const Q = J.S.queue, ts = J.S.tool_status || {};
  const tasks = Q.plans.filter(p => !ENDED.includes(p.status)).flatMap(p => p.tasks);
  return Object.entries(Q.lanes).map(([tool, l]) => {
    const mine = tasks.filter(t => t.tool === tool && OPEN.includes(t.status)).sort((a, b) => OPEN.indexOf(a.status) - OPEN.indexOf(b.status));
    const st = ts[tool];
    return `<section class="panel lane-p lane-${esc(tool)}"><div class="ph">${J.toolMono(tool)}<h2>${esc(J.TOOL[tool] || tool)}</h2><span class="n">${esc(l.label || "")}</span>
        <span class="act">${st && !st.available ? J.st("out", st.until ? `out · ${J.span(st.until - J.now())}` : "out") : J.st(l.running ? "run" : "done", l.running ? "busy" : "ready")}</span></div>
      <div class="pb lane-load"><span class="bar${st && !st.available ? " grey" : ""}"><i style="width:${J.pct(l.running, l.capacity)}%"></i></span><span class="num">${l.running}/${l.capacity} running · ${l.queued} waiting</span></div>
      ${mine.length ? `<div class="lane-list">${mine.map(t => `<div class="lt">${J.sd(TST[t.status])}<span class="lt-t" title="${esc(t.prompt || t.title)}">${esc(t.title)}</span><span class="num faint">${esc(short(t.id))}</span><span class="acts">${acts(t)}</span></div>`).join("")}</div>`
        : `<div class="empty">Lane idle.${st && !st.available ? ` ${esc(st.reason)}` : ""}</div>`}</section>`;
  }).join("");
}
function plans() {
  const all = J.S.queue.plans.filter(p => !hidden.has(p.id)), list = showAll ? all : all.filter(p => !ENDED.includes(p.status));
  if (!list.length) return `<section class="panel plans-empty"><div class="empty"><svg class="i" aria-hidden="true"><use href="#i-split"/></svg><b>${all.length ? "No open plans" : "No plans yet"}</b><span>Turn on <b>Plan</b> in the command bar to split a bigger goal into subtasks you approve. Each subtask runs on the model its level needs.</span><button class="btn primary pill" type="button" data-plan-goal><svg class="i sm" aria-hidden="true"><use href="#i-split"/></svg>Plan a goal</button></div></section>`;
  return list.map(p => {
    const d = p.tasks.filter(t => t.status === "done").length;
    return `<section class="panel plan-p"><div class="ph"><a class="plan-goal" href="#/workflows/${encodeURIComponent(p.id)}" title="Open the dependency graph">${esc(p.goal)}</a>
        ${J.st(TST[p.status] || (p.status === "approved" ? "queue" : "queue"), p.status)}
        <span class="act"><span class="n">${esc(p.project || "no project")} · ${esc(p.planner || "")}</span>
          <span class="plan-prog"><span class="bar green"><i style="width:${J.pct(d, p.tasks.length)}%"></i></span><span class="num">${d}/${p.tasks.length}</span></span>
          ${p.status === "draft" ? btn("discard", p.id, "Discard", `Discard plan ${p.goal}`, "ghost") + btn("approve", p.id, "Approve and queue", `Approve plan ${p.goal}`, "primary") : ""}
          ${["running", "approved"].includes(p.status) ? btn("cancelplan", p.id, "Cancel plan", `Cancel plan ${p.goal}`, "ghost danger") : ""}
          <a class="btn sm ghost" href="#/workflows/${encodeURIComponent(p.id)}"><svg class="i sm" aria-hidden="true"><use href="#i-flow"/></svg>Graph</a>
          ${ENDED.includes(p.status) ? `<button class="btn icon sm ghost" type="button" data-hide-plan="${esc(p.id)}" data-key="hide:${esc(p.id)}" title="Close: remove this finished plan from the queue" aria-label="Close plan ${esc(p.goal)}"><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg></button>` : ""}</span></div>
      <div class="tscroll"><table class="tbl"><thead><tr><th>ID</th><th>Task</th><th>Level</th><th>Runs on</th><th>After</th><th>Status</th><th></th></tr></thead><tbody>${p.tasks.map(p.status === "draft" ? draftRow : taskRow).join("")}</tbody></table></div>
      ${p.notes?.length ? `<div class="plan-notes">${p.notes.map(esc).join("<br>")}</div>` : ""}
      ${p.review ? `<details class="review"><summary>Review report</summary><pre class="log">${esc(p.review)}</pre></details>` : ""}</section>`;
  }).join("");
}

J.pages.tasks = {
  init() {
    el = document.querySelector('[data-page="tasks"]');
    el.innerHTML = `<div class="toolbar"><span id="tq-state"></span><span class="spacer"></span>
        <label class="toggle"><input type="checkbox" id="tq-all" ${showAll ? "checked" : ""}><span>Show finished and failed plans</span></label>
        <button class="btn sm" type="button" id="tq-pause"></button><button class="btn sm primary" type="button" id="tq-plan"><svg class="i sm" aria-hidden="true"><use href="#i-split"/></svg>Plan a goal</button></div>
      <div class="grid lanes" id="tq-lanes"></div><div class="plans" id="tq-plans"></div>`;
    q("#tq-all").addEventListener("change", e => { showAll = e.target.checked; J.saved.set("jarviee.allplans", showAll ? "1" : "0"); this.render(); });
    q("#tq-plan").addEventListener("click", () => { location.hash = "#/"; requestAnimationFrame(() => J.composer.plan()); });
    q("#tq-plans").addEventListener("click", e => { if (e.target.closest("[data-plan-goal]")) q("#tq-plan").click(); });
    q("#tq-pause").addEventListener("click", async () => { try { await J.api(J.S.queue.paused ? "/api/queue/resume" : "/api/queue/pause", {}); J.poll(); } catch (e) { J.toast(e.message, true); } });
    el.addEventListener("change", async e => {
      const sel = e.target.closest("[data-edit]"); if (!sel) return;
      const tid = sel.dataset.tid, pid = tid.split(".")[0];
      try { await J.api("/api/plan/edit", {id: pid, tasks: [{id: tid, [sel.dataset.edit]: sel.value}]}); J.toast("Plan updated"); }
      catch (err) { J.toast(err.message, true); }
      J.poll();
    });
    el.addEventListener("click", async e => {
      const h = e.target.closest("[data-hide-plan]");
      if (h) { hidden.add(h.dataset.hidePlan); saveHidden(); J.toast("Plan closed"); this.render(); return; }
      const b = e.target.closest("[data-qact]"); if (!b) return;
      const act = b.dataset.qact, id = b.dataset.tid;
      if (act === "drop") {
        b.disabled = true;
        try { await J.api("/api/plan/edit", {id: id.split(".")[0], tasks: [{id, status: "cancelled"}]}); J.toast("Task dropped from the plan"); }
        catch (err) { J.toast(err.message, true); b.disabled = false; }
        J.poll(); return;
      }
      const path = act === "cancelplan" ? "/api/plan/cancel" : ["approve", "discard"].includes(act) ? `/api/plan/${act}` : `/api/task/${act}`;
      const t = act === "reroute" ? J.planTasks().find(x => x.id === id) : null;
      b.disabled = true;
      try { await J.api(path, {id, ...(t ? {tool: t.tool === "agy" ? "claude" : "agy"} : {})}); J.toast({approve: "Plan queued", discard: "Plan discarded", cancelplan: "Plan cancelled", cancel: "Task cancelled", retry: "Task queued again", reroute: "Task rerouted"}[act] || "Done"); }
      catch (err) { J.toast(err.message, true); b.disabled = false; }
      J.poll();
    });
  },
  render() {
    if (!J.S || !el) return;
    const Q = J.S.queue;
    if (!Q) { J.setHTML(q("#tq-plans"), `<section class="panel"><div class="empty">This server runs without the task queue.</div></section>`); return; }
    J.setHTML(q("#tq-state"), Q.paused ? J.st("need", "Queue paused: nothing new starts") : J.st("run", "Queue running"));
    q("#tq-pause").textContent = Q.paused ? "Resume queue" : "Pause queue";
    J.setHTML(q("#tq-lanes"), lanes());
    J.setHTML(q("#tq-plans"), plans());
  },
  crumb() { const Q = J.S?.queue; return Q ? `${Object.values(Q.lanes).reduce((s, l) => s + l.running, 0)} running · ${Object.values(Q.lanes).reduce((s, l) => s + l.queued, 0)} waiting` : ""; },
};
})();
