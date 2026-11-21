/* fleet.js: the routing graph. Jarviee router → tool lanes → tier models, wired with orthogonal lines.
   Live edges carry a teal pulse, needs-you paths glow ember, a lane that is out is cut and dashed. */
"use strict";
(() => {
const {esc} = J;
/* nothing is selected until the owner clicks a node: the detail under the graph stays hidden (owner, 2026-11-15) */
let box = null, detail = null, sel = J.saved.get("jarviee.fleet.sel") || "", ro = null, lastSig = "";

/* agy reports quota per model group (agy -p /usage); a group at 0% left means its models can't run, even while
   the lane's own circuit breaker still says ready */
function bucketOut(tool, slug) {
  if (tool !== "agy" || !slug) return null;
  const groups = J.U?.agy?.quota?.groups || [], m = slug.toLowerCase();
  const hit = x => {
    const key = (x.name || "").toLowerCase().replace(/[^a-z0-9.]+/g, "-").replace(/^-+|-+$/g, "");
    if (key && m.includes(key)) return true;
    const models = (x.models || "").split(",").map(n => n.toLowerCase().replace(/[^a-z0-9.]+/g, " ").trim().split(/\s+/).filter(Boolean));
    if (models.some(w => w.length && w.every(k => m.includes(k)))) return true;
    return /claude|gpt/i.test(x.name) && /claude|gpt/.test(m);
  };
  const g = groups.find(hit) || groups.find(x => /gemini/i.test(x.name) && /gemini/.test(m) && !/argon|hydrogen/.test(m));
  const b = g && g.buckets.find(x => x.remaining_pct != null && x.remaining_pct <= .5 && (!x.resets_at || x.resets_at > J.now()));
  return b ? {until: b.resets_at, reason: `${g.name} ${b.label.toLowerCase()} quota used up`} : null;
}
/* agy's spare: a model_preference model no tier uses (Gemini 4 Argon: its own quota bucket, so it takes over when the
   shared Gemini bucket is empty). It gets its own row, under the four tiers */
const spareOf = tool => J.S?.tools?.[tool]?.spare || null;
const WORD = tier => tier === "other" ? "Other" : tier === "spare" ? "Spare" : J.TIER_WORD[tier];
function model(tool, tier) {
  if (tier === "other") return {model: "", available: true};
  if (tier === "spare") { const sp = spareOf(tool), out = sp && bucketOut(tool, sp.model); return {model: sp?.model || "", available: !out, ...(out ? {reason: out.reason, until: out.until} : {})}; }
  const m = J.S?.tool_status?.[tool]?.tiers?.[tier] || {model: J.S?.tools?.[tool]?.models?.[tier] || "", available: true};
  const out = m.available !== false && bucketOut(tool, m.model);
  return out ? {...m, available: false, reason: out.reason, until: out.until} : m;
}
/* the four tiers, plus an "Other" slot when a session didn't report its model (hook sessions often don't) */
const tiersFor = (tool, ld) => [...J.TIERS, ...(spareOf(tool) ? ["spare"] : []), ...(ld[tool]?.tiers.other.ids.length ? ["other"] : [])];
/* effort is routed per task and fitted to the model (hub/router.py fit_effort): a job sits under the effort it runs at */
const EFF = ["low", "medium", "high", "xhigh", "max"];
const famOf = m => String(m || "").trim().toLowerCase().replace(/-(low|medium|high|xhigh|max)$/, "");
const levelsOf = (tool, tier) => tier === "spare" ? spareOf(tool)?.levels || EFF : J.S?.tools?.[tool]?.effort_levels?.[tier] || EFF;
function effortOf(a, tool, tier) {
  const sfx = (String(a.model || "").toLowerCase().match(/-(low|medium|high|xhigh|max)$/) || [])[1];
  let e = String(a.effort || sfx || J.S?.tools?.[tool]?.efforts?.[tier] || "").toLowerCase();
  const lv = levelsOf(tool, tier);
  if (!EFF.includes(e) || !lv.length) return lv.length ? (lv.includes("medium") ? "medium" : lv[0]) : "";
  if (lv.includes(e)) return e;
  const i = EFF.indexOf(e);   // a level this model doesn't take runs at its nearest one
  return lv.reduce((b, x) => Math.abs(EFF.indexOf(x) - i) < Math.abs(EFF.indexOf(b) - i) ? x : b, lv[0]);
}
function lanes() { const ts = J.S?.tool_status || {}; return Object.keys(ts).length ? Object.keys(ts) : ["agy", "claude"]; }
/* which lane / tier chip an agent belongs to */
function slot(a) {
  const tiers = J.S?.tools?.[a.tool]?.models || {}, m = String(a.model || "").toLowerCase(), fm = famOf(m);
  /* file the job under the tier whose model family it runs on (gemini-4-hydrogen-xhigh is the Hydrogen row: the effort
     is the slug's last part); a model that isn't any tier's goes to "Other" */
  const hit = Object.keys(tiers).filter(t => { const s = String(tiers[t]).toLowerCase(); return s === m || famOf(s) === fm || (a.tool === "claude" && /^(haiku|sonnet|opus)$/.test(s) && m.includes(s)); });
  if (hit.length) return {tool: a.tool, tier: hit.includes(a.tier) ? a.tier : hit[0]};
  if (m && spareOf(a.tool) && famOf(m) === spareOf(a.tool).family) return {tool: a.tool, tier: "spare"};
  if (m) return {tool: a.tool, tier: ""};
  return {tool: a.tool, tier: J.TIERS.includes(a.tier) ? a.tier : a.tier === "standard" ? "light" : ""};
}
function load() {
  const out = {}, d0 = J.dayStart();
  for (const t of lanes()) { out[t] = {run: 0, need: 0, done: 0, fail: 0, rerouted: 0, tiers: {}}; for (const tr of [...J.TIERS, "spare", "other"]) out[t].tiers[tr] = {run: 0, need: 0, ids: [], eff: {}}; }
  out.router = {routed: 0, rerouted: 0};
  out.jobs = [];
  for (const a of J.S?.agents || []) {
    const s = slot(a), L = out[s.tool];
    if ((a.started || 0) >= d0) out.router.routed++;
    const fb = a.fallback_from || (a.reasons || []).some(r => typeof r === "string" && r.startsWith("fallback:"));
    if (fb && (a.started || 0) >= d0) { out.router.rerouted++; if (L) L.rerouted++; }
    if (!L) continue;
    const tier = s.tier || "other", g = J.groupOf(a), T = L.tiers[tier];
    const eff = g === "run" || g === "need" ? effortOf(a, s.tool, tier) : "";
    if (eff) { const E = T.eff[eff] = T.eff[eff] || {run: 0, need: 0}; E[g]++; }
    if (g === "run") { L.run++; T.run++; T.ids.push(a.id); }
    if (g === "need") { L.need++; T.need++; T.ids.push(a.id); }
    if (g === "run" || g === "need") out.jobs.push({a, tool: s.tool, tier, eff});
    const end = J.endOf(a);
    if (end >= d0) { if (a.status === "done") L.done++; if (a.status === "failed") L.fail++; }
  }
  const lo = lanes(), to = [...J.TIERS, "spare", "other"];
  out.jobs.sort((x, y) => lo.indexOf(x.tool) - lo.indexOf(y.tool) || to.indexOf(x.tier) - to.indexOf(y.tier)
    || (J.groupOf(x.a) === "need" ? -1 : 0) - (J.groupOf(y.a) === "need" ? -1 : 0) || (x.a.started || 0) - (y.a.started || 0));
  return out;
}
const MAX_JOBS = 40;   // every running job gets its card; zoom out (or Fit) to see them all
/* graph zoom: − / + steps, or Fit (keeps the whole graph, every job card included, within the window and follows it as jobs come
   and go). CSS zoom shrinks the layout itself, so the panel gets shorter too; the wires' viewBox keeps them on their nodes. */
let zoom = Math.min(1.2, Math.max(.5, +J.saved.get("jarviee.fleet.zoom") || 1)), autoFit = J.saved.get("jarviee.fleet.fit") !== "0";   // Fit is on until turned off
function applyZoom() {
  if (!box) return;
  if (autoFit) {
    box.style.zoom = "";
    const h = box.getBoundingClientRect().height, room = Math.max(320, innerHeight * .62);
    zoom = Math.min(1, Math.max(.5, Math.floor(room / Math.max(1, h) * 20) / 20));
  }
  box.style.zoom = zoom === 1 ? "" : String(zoom);
  const ctl = box.closest("details")?.querySelector(".fz");
  if (ctl) { ctl.querySelector("#fz-v").textContent = Math.round(zoom * 100) + "%"; ctl.querySelector('[data-fz="fit"]').setAttribute("aria-pressed", String(autoFit)); }
}
const stateOf = (avail, x) => !avail ? "out" : x.need ? "need" : x.run ? "live" : "idle";

function nodeRouter(ld) {
  const paused = J.S?.queue?.paused, fb = J.S?.fallback || {};
  return `<button class="fnode router${sel === "router" ? " sel" : ""}" type="button" data-node="router" data-key="router" aria-pressed="${sel === "router"}">
    <span class="fn-head"><svg class="mark" aria-hidden="true"><use href="#mark"/></svg><span><b>Jarviee Router</b><small>Picks tool and model</small></span></span>
    ${J.st(paused ? "need" : "run", paused ? "queue paused" : "routing")}
    <span class="fn-stats"><span title="Routed today"><b class="num">${ld.router.routed}</b>routed</span><span title="Rerouted today"><b class="num">${ld.router.rerouted}</b>moved</span>
      <span title="Use whichever tool is available"><b class="num">${fb.use_available_tool !== false ? "on" : "off"}</b>switch</span></span>
  </button>`;
}
function nodeLane(tool, ld) {
  const t = J.S?.tool_status?.[tool] || {available: true, capacity: 1}, L = ld[tool], cap = t.capacity || 1;
  const left = t.until ? J.span(t.until - J.now()) : "";
  const other = lanes().find(x => x !== tool), otherOk = J.S?.tool_status?.[other]?.available;
  const out = !t.available;
  const note = out ? (J.S?.fallback?.use_available_tool !== false && otherOk ? `New tasks go to ${J.TOOL[other] || other}` : "Tasks wait for quota") : "";
  return `<button class="fnode lane lane-${esc(tool)}${out ? " out" : ""}${sel === "lane:" + tool ? " sel" : ""}" type="button" data-node="lane:${esc(tool)}" data-key="lane:${esc(tool)}" aria-pressed="${sel === "lane:" + tool}">
    <span class="fn-head">${J.toolMono(tool)}<span><b>${esc(J.TOOL[tool] || tool)}</b><small>${esc(tool)} · ${cap} slot${cap === 1 ? "" : "s"}</small></span></span>
    ${out ? J.st("out", left ? `out · resets in ${left}` : "unavailable") : L.need ? J.st("need", `${L.need} need${L.need === 1 ? "s" : ""} you`) : L.run ? J.st("run", `${L.run} running`) : J.st("done", "ready")}
    <span class="fn-load"><span class="bar${out ? " grey" : ""}"><i style="width:${Math.min(100, J.pct(L.run, cap))}%"></i></span><span class="num">${L.run}/${cap}</span></span>
    <span class="fn-foot">${out ? `<span class="re">${esc(note)}</span>` : `<span>${L.done} done</span><span>${L.fail} failed</span>${L.rerouted ? `<span class="re">${L.rerouted} rerouted in</span>` : ""}`}</span>
  </button>`;
}
function nodeModel(tool, tier, ld) {
  const m = model(tool, tier), T = ld[tool].tiers[tier], laneOk = J.S?.tool_status?.[tool]?.available !== false, other = tier === "other", plain = other || tier === "spare";
  const avail = m.available !== false && laneOk, id = `model:${tool}:${tier}`;
  const st = !avail ? "out" : T.need ? "need" : T.run ? "run" : "idle";
  const badge = T.need ? `<span class="fn-badge need">${T.need}</span>` : T.run ? `<span class="fn-badge run">${T.run}</span>` : "";
  return `<button class="fnode model${!avail ? " out" : ""}${plain ? "" : ` tier tc-${tier}`}${tier === "spare" ? " spare" : ""}${sel === id ? " sel" : ""}" type="button" data-node="${esc(id)}" data-key="${esc(id)}" aria-pressed="${sel === id}"
      title="${esc(other ? `${J.TOOL[tool] || tool} sessions on a model no tier is set to, or that didn't report one` : `${tier === "spare" ? "Spare" : J.TIER_WORD[tier]} on ${J.TOOL[tool] || tool}: ${m.model}${tier === "spare" ? " (takes over when the other models' quota bucket is used up)" : ""}${!avail ? ` (${m.reason || "unavailable"})` : ""}${modelNote(famOf(m.model)) ? "\n" + modelNote(famOf(m.model)) : ""}`)}">
    ${J.sd(st)}<span class="fn-tier">${WORD(tier)}</span>
    <span class="fn-model">${esc(other ? "unlisted or unreported" : (J.modelLabel(J.S?.tools?.[tool]?.effort_in_model ? famOf(m.model) : m.model) || "—"))}</span>${!avail && m.until ? `<span class="fn-reset num" title="${esc(m.reason || "")}">${J.span(m.until - J.now())}</span>` : ""}${effortInline(tool, tier, T)}${badge}
  </button>`;
}
/* narrow (stacked) graph: the effort column folds into the model row: the efforts jobs run at now, else the starting one */
function effortInline(tool, tier, T) {
  const now = Object.entries(T.eff || {}).filter(([, E]) => E.run + E.need).map(([e, E]) => `${EFF_SHORT[e] || e}${E.run + E.need > 1 ? ` ×${E.run + E.need}` : ""}`);
  const b = effortOf({}, tool, tier), base = EFF_SHORT[b] || J.effortLabel(b);   // the starting effort, fitted to what this model takes
  const txt = now.length ? now.join(", ") : base;
  return txt ? `<span class="fn-eff">${esc(txt)}</span>` : "";
}
function nodeEffort(tool, tier, ld) {
  /* the efforts this model takes; the tier's starting point is outlined, the ones jobs run at now are lit with a count */
  const other = tier === "other", plain = other || tier === "spare", T = ld[tool].tiers[tier], base = String(J.S?.tools?.[tool]?.efforts?.[tier] || "").toLowerCase();
  const avail = model(tool, tier).available !== false && J.S?.tool_status?.[tool]?.available !== false;
  const lv = other ? [...new Set(Object.keys(T.eff))] : levelsOf(tool, tier);
  /* a model with no effort control (agy's Orchestrator) says who does the work instead of five empty slots */
  if (!other && !lv.length) {
    const fam = famOf(model(tool, tier).model), plan = /orchestrator/.test(fam) ? (J.modelLabel(fam).match(/\((\w+)\)/) || [])[1] : "";
    return `<div class="fnode effort${!avail ? " out" : ""} tier tc-${tier}" data-node="effort:${esc(tool)}:${esc(tier)}" title="This model takes no effort setting">
      <span class="ech fixed">${esc(plan ? `${plan} plans → Hydrogen builds` : "Effort set by the tool")}</span></div>`;
  }
  /* every row has the same five slots, so Low / Med / High line up down the column; a level this model doesn't take is an empty slot */
  const chips = EFF.map(e => {
    if (!lv.includes(e)) return `<span class="ech gap" aria-hidden="true"></span>`;
    const E = T.eff[e] || {run: 0, need: 0}, n = E.run + E.need;
    const cls = ["ech", e === base ? "base" : "", E.need ? "need" : E.run ? "on" : ""].filter(Boolean).join(" ");
    const tip = `${J.effortLabel(e)}${e === base ? " · starting effort for this tier" : ""}${n ? ` · ${n} job${n === 1 ? "" : "s"} now` : ""}`;
    return `<span class="${cls}" data-eff="${esc(`${tool}:${tier}:${e}`)}" title="${esc(tip)}">${esc(EFF_SHORT[e] || e)}${n ? `<b class="num">${n}</b>` : ""}</span>`;
  }).join("");
  return `<div class="fnode effort${!avail ? " out" : ""}${plain ? "" : ` tier tc-${tier}`}" data-node="effort:${esc(tool)}:${esc(tier)}"
    title="Effort is picked per task, then fitted to what ${esc(plain ? "the model" : J.TIER_WORD[tier] + "'s model")} takes">${chips || `<span class="ech">—</span>`}</div>`;
}
const EFF_SHORT = {low: "Low", medium: "Med", high: "High", xhigh: "XHigh", max: "Max"};
/* what each model family is for and where it sits (research 2026-11-21, .brain/research/2026-11-21-agy-neon-models.md) */
const MODEL_NOTES = [
  [/orchestrator.*neon/, "Gemini 4 Neon plans and reviews; Gemini 4 Hydrogen does the edits, builds and tests. Built for feature work, bug fixes, refactors and multi-file debugging. Effort is fixed by agy."],
  [/orchestrator/, "A planner model plans and reviews; Gemini 4 Hydrogen carries out the steps. Effort is fixed by agy."],
  [/neon/, "agy's strongest model: #1 on the Artificial Analysis Coding Agent Index (58.6), 61 on its Intelligence Index, 65.2% on Terminal-Bench 4.1 (Sonnet 5.5: 63.9%). Repo-wide edits, hard debugging, refactors. The priciest Gemini per token."],
  [/hydrogen/, "Fast, cheap everyday execution: 51–54 on the Intelligence Index, 57% on Terminal-Bench 4.1."],
  [/argon/, "Older Gemini 4 Pro tier (50 on the Intelligence Index), with its own quota bucket, so it stays as the spare when the main Gemini bucket runs out."],
  [/flash/, "Cheapest and quickest; one-line and mechanical edits."],
  [/^opus/, "Claude's top model: the heaviest, most open-ended work."],
  [/^sonnet/, "Claude's workhorse for multi-file features and debugging."],
  [/^haiku/, "Claude's quick, cheap model."],
];
const modelNote = fam => (MODEL_NOTES.find(([re]) => re.test(String(fam || "").toLowerCase())) || [])[1] || "";
function nodeModelLabel(tool, tier, model) {
  const l = J.modelLabel(model) || "—";
  const e = J.effortLabel(J.S?.tools?.[tool]?.efforts?.[tier]);
  return e && !l.includes(" · ") && l !== "—" ? `${l} · ${e}` : l;
}
function jobModelLabel(a) {
  const l = J.modelLabel(a.model) || "", e = J.effortLabel(a.effort);
  return e && l && !l.includes(" · ") ? `${l} · ${e}` : l;
}
function nodeJob({a, tool, tier, eff}) {
  const k = J.statusKey(a), g = J.groupOf(a), sub = g === "need" ? J.waitReason(a.activity) : (a.activity || a.summary || "");
  const known = J.TIERS.includes(tier);
  return `<div class="fjob g-${g}${known ? ` tier tc-${tier}` : ""}" role="link" tabindex="0" title="Go to its window" data-job="${esc(a.id)}" data-from="effort:${esc(tool)}:${esc(tier)}" data-eff="${esc(`${tool}:${tier}:${eff || ""}`)}" data-key="job:${esc(a.id)}">
    <span class="fj-top">${J.sd(k)}<b>${esc(J.titleOf(a))}</b><span class="num clock" data-start="${a.started || ""}"></span>${J.act.html(a, {compact: true, noOpen: true, noFocus: true})}</span>
    <span class="fj-meta">${[known ? `<span class="fj-tier">${J.TIER_WORD[tier]}</span>` : "", a.source === "subagent" ? `<span class="fj-subagent" title="${esc(`Subagent of: ${a.parent_title || a.parent}`)}">Subagent</span>` : "", esc(J.TOOL_SHORT[tool] || tool),
      esc(jobModelLabel(a)), esc(J.place(a) || "")].filter(Boolean).join(" · ")}</span>
    ${sub ? `<span class="fj-sub">${esc(sub)}</span>` : ""}
  </div>`;
}

/* ---------- wires ---------- */
function wirePath(x1, y1, x2, y2, mid) {
  const r = Math.min(8, Math.abs(y2 - y1) / 2, Math.abs(mid - x1), Math.abs(x2 - mid));
  if (Math.abs(y2 - y1) < 1) return `M${x1} ${y1}H${x2}`;
  const dy = y2 > y1 ? 1 : -1;
  return `M${x1} ${y1}H${mid - r}Q${mid} ${y1} ${mid} ${y1 + r * dy}V${y2 - r * dy}Q${mid} ${y2} ${mid + r} ${y2}H${x2}`;
}
function drawWires() {
  if (!box || !box.offsetParent) return;
  const svg = box.querySelector("svg.wires"), base = box.getBoundingClientRect();
  const pt = (el, side) => { const r = el.getBoundingClientRect(); return [side === "r" ? r.right - base.left : r.left - base.left, r.top - base.top + r.height / 2]; };
  const ld = load(), paths = [];
  const router = box.querySelector('[data-node="router"]');
  if (!router) return;
  const stacked = getComputedStyle(box).getPropertyValue("--stack").trim() === "1";
  for (const tool of lanes()) {
    const lane = box.querySelector(`[data-node="lane:${CSS.escape(tool)}"]`);
    if (!lane) continue;
    const ok = J.S?.tool_status?.[tool]?.available !== false;
    const [x1, y1] = pt(router, "r"), [x2, y2] = pt(lane, "l");
    if (!stacked) paths.push({d: wirePath(x1, y1, x2, y2, x1 + (x2 - x1) / 2), s: stateOf(ok, ld[tool]), cut: !ok ? [x1 + (x2 - x1) * .72, y2] : null});
    for (const tier of tiersFor(tool, ld)) {
      const m = box.querySelector(`[data-node="model:${CSS.escape(tool)}:${tier}"]`);
      if (!m) continue;
      const [a1, b1] = pt(lane, "r"), [a2, b2] = pt(m, "l");
      if (a2 <= a1 + 4) continue;
      const mAvail = ok && model(tool, tier).available !== false;
      paths.push({d: wirePath(a1, b1, a2, b2, a1 + Math.min(22, (a2 - a1) / 2)), s: stateOf(mAvail, ld[tool].tiers[tier]), tier});
      const ef = box.querySelector(`[data-node="effort:${CSS.escape(tool)}:${tier}"]`);
      if (ef) { const [c1, d1] = pt(m, "r"), [c2, d2] = pt(ef, "l"); if (c2 > c1 + 4) paths.push({d: wirePath(c1, d1, c2, d2, (c1 + c2) / 2), s: stateOf(mAvail, ld[tool].tiers[tier]), tier}); }
    }
  }
  let below = 0;
  for (const j of box.querySelectorAll("[data-job]")) {
    const m = box.querySelector(`[data-node="${CSS.escape(j.dataset.from)}"]`); if (!m) continue;
    if (stacked || !m.getBoundingClientRect().width) continue;   // stacked graph: efforts are folded into the model rows
    const chip = box.querySelector(`.ech[data-eff="${CSS.escape(j.dataset.eff || "")}"]`) || m;
    const tier = j.dataset.from.split(":")[2], k = Math.max(0, J.TIERS.indexOf(tier)), s = j.classList.contains("g-need") ? "need" : "live";
    const [a1, b1] = pt(m, "r"), [a2, b2] = pt(j, "l");
    if (a2 > a1 + 4) {   // jobs in their own column: the wire leaves the effort row at the lit chip's height
      const [, cy] = pt(chip, "r");
      paths.push({d: wirePath(a1, cy, a2, b2, a1 + Math.min(a2 - a1 - 6, 6 + k * 5)), s, tier});
      continue;
    }
    /* jobs under the graph (narrower windows): out of the lit chip, down the gutter beside the effort column, into the card.
       Not in the stacked graph, where the effort column is folded into the model rows (hidden, so it has no box) */
    const cr = chip.getBoundingClientRect(), mr = m.getBoundingClientRect(), jr = j.getBoundingClientRect();
    if (!cr.width) continue;
    const x1 = cr.right - base.left, y1 = cr.top - base.top + cr.height / 2, gx = mr.right - base.left + 8 + (below++ % 4) * 4;
    /* down the gutter to just above the card, then in over its top edge near its right end */
    const n = below - 1, y2 = jr.top - base.top, ty = y2 - 10 - (n % 4) * 4, x2 = jr.right - base.left - 28 - (n % 4) * 10, r = 6;
    if (ty > y1 + 2 * r && gx - x2 > 2 * r)
      paths.push({d: `M${x1} ${y1}H${gx - r}Q${gx} ${y1} ${gx} ${y1 + r}V${ty - r}Q${gx} ${ty} ${gx - r} ${ty}H${x2 + r}Q${x2} ${ty} ${x2} ${ty + r}V${y2}`, s, tier});
  }
  const order = {out: 0, idle: 1, live: 2, need: 3};
  paths.sort((a, b) => order[a.s] - order[b.s]);
  svg.setAttribute("viewBox", `0 0 ${base.width} ${base.height}`);
  /* live and needs-you wires take their load tier's colour; a needs-you wire keeps the ember glow and pulse */
  const tc = p => (p.s === "live" || p.s === "need") && J.TIERS.includes(p.tier) ? ` tier tc-${p.tier}` : "";
  svg.innerHTML = paths.map(p => `<path class="w ${p.s}${tc(p)}" d="${p.d}"/>` + (p.s === "live" || p.s === "need" ? `<path class="w flow ${p.s}${p.s === "live" ? tc(p) : ""}" d="${p.d}"/>` : "")
    + (p.cut ? `<g class="cut" transform="translate(${p.cut[0].toFixed(1)} ${p.cut[1].toFixed(1)})"><circle r="9"/><path d="M-3.5 -3.5L3.5 3.5M3.5 -3.5L-3.5 3.5"/></g>` : "")).join("");
}

/* ---------- detail panel ---------- */
function meter(label, used, until, tip) {
  const lvl = used == null ? "grey" : used >= 100 ? "red" : "";   // red only when it is used up (out); otherwise the tool's hue
  return `<div class="meter" title="${esc(tip || label)}"><span class="ml">${esc(label)}</span><span class="mv num">${used == null ? "—" : Math.round(used) + "%"}</span>
    <span class="bar ${lvl}"><i style="width:${used == null ? 0 : Math.min(100, used)}%"></i></span>${until && until > J.now() ? `<span class="mw">resets in <span data-until="${until}">${J.span(until - J.now())}</span></span>` : used == null && tip ? `<span class="mw">${esc(tip)}</span>` : ""}</div>`;
}
function quotaMeters(tool) {
  const U = J.U;
  if (!U) return `<div class="empty">Reading quota…</div>`;
  if (tool === "agy") {
    const q = U.agy?.quota;
    if (!q) return `<div class="empty">${esc(U.agy?.quota_reason || "agy /usage isn't readable right now.")}</div>`;
    return q.groups.map(g => g.buckets.map(b => meter(`${g.name} · ${b.label}`, b.used_pct, b.resets_at, g.models)).join("")).join("");
  }
  if (tool === "claude") {
    const c = U.claude || {}, wins = c.limits?.windows || [];
    if (!wins.length) return `<div class="empty">${esc(c.limits_reason || "Claude limits come from the status line script (hooks/statusline.py).")}</div>`;
    return wins.map(w => meter(w.label, w.used_pct, w.resets_at, w.note || w.label)).join("");
  }
  return "";
}
function agentList(ids) {
  const list = (J.S?.agents || []).filter(a => ids.includes(a.id));
  if (!list.length) return `<div class="empty">Nothing running here.</div>`;
  return `<div class="dl-list">${list.map(a => `<a class="mini-row" href="#/agents/${encodeURIComponent(a.id)}">${J.sd(J.statusKey(a))}<span class="t">${esc(J.titleOf(a))}</span><span class="num muted clock" data-start="${a.started || ""}">${J.dur(J.now() - (a.started || J.now()))}</span></a>`).join("")}</div>`;
}
function detailHtml() {
  const S = J.S; if (!S) return "";
  const ld = load();
  if (sel.startsWith("lane:")) {
    const tool = sel.slice(5), t = S.tool_status?.[tool] || {}, L = ld[tool]; if (!L) return "";
    const fin = L.done + L.fail, by = J.today().byTool[tool] || {};
    const ids = Object.values(L.tiers).flatMap(x => x.ids);
    return `<div class="dt-head">${J.toolMono(tool)}<div><h3>${esc(J.TOOL[tool] || tool)}</h3><p>${esc(tool)} CLI lane · ${t.capacity || 1} parallel slot${t.capacity === 1 ? "" : "s"}</p></div></div>
      ${!t.available ? `<div class="dt-alert">${J.sd("out")}<span>${esc(t.reason || "Unavailable")}</span></div>` : ""}
      <div class="dt-stats"><div><b class="num">${L.run}</b><span>running</span></div><div><b class="num">${L.done}</b><span>done today</span></div>
        <div><b class="num">${fin ? J.pct(L.done, fin) + "%" : "—"}</b><span>success</span></div><div><b class="num">${by.n ? J.span(by.secs / by.n) : "—"}</b><span>avg run</span></div></div>
      <h4>Quota</h4>${quotaMeters(tool)}
      <h4>Models</h4><div class="kv">${J.TIERS.map(tr => { const m = model(tool, tr); return `<span>${J.TIER_WORD[tr]}</span><b class="num${m.available === false ? " ink-red" : ""}">${esc(m.model || "—")}</b>`; }).join("")}${spareOf(tool) ? `<span>Spare</span><b class="num${model(tool, "spare").available === false ? " ink-red" : ""}">${esc(spareOf(tool).model)}</b>` : ""}</div>
      <h4>Running here</h4>${agentList(ids)}`;
  }
  if (sel.startsWith("model:")) {
    const [, tool, tier] = sel.split(":"), m = model(tool, tier), T = ld[tool]?.tiers[tier]; if (!T) return "";
    if (tier === "other") return `<div class="dt-head">${J.toolMono(tool)}<div><h3>Model not reported</h3><p>${esc(J.TOOL[tool] || tool)} sessions started in the app whose hooks didn't say which model they use.</p></div></div><h4>Running here</h4>${agentList(T.ids)}`;
    /* in plain words: which tasks land here, how its effort moves, what takes over when it's out */
    const tc = S.tools?.[tool] || {}, fam = tc.effort_in_model ? famOf(m.model) : m.model, name = J.modelLabel(fam) || fam;
    const lv = levelsOf(tool, tier), start = effortOf({}, tool, tier), word = WORD(tier), toolName = J.TOOL[tool] || tool, spare = tier === "spare";
    const eq = (S.fallback?.equivalents || {})[`${tool}:${m.model}`] || [];
    const fbTxt = eq.length ? eq.map(x => { const [t, mm] = String(x).split(":"); return `${J.TOOL_SHORT[t] || t} ${J.modelLabel(mm) || mm}`; }).join(", then ")
      : S.fallback?.use_available_tool !== false ? `the other tool's ${word.toLowerCase()} model` : "nothing (it waits)";
    const live = Object.entries(T.eff || {}).filter(([, E]) => E.run + E.need).map(([e, E]) => `${EFF_SHORT[e] || e} ×${E.run + E.need}`);
    const chips = EFF.map(e => !lv.includes(e) ? "" : `<span class="ech${e === start ? " base" : ""}${T.eff?.[e] ? (T.eff[e].need ? " need" : " on") : ""}">${EFF_SHORT[e]}</span>`).join("");
    return `<div class="dt-head">${J.toolMono(tool)}<div><h3>${esc(name)}</h3><p>${spare ? `Spare on ${esc(toolName)}: takes over when the shared quota is used up` : `Runs ${esc(word)} tasks on ${esc(toolName)}`}</p></div></div>
      ${m.available === false ? `<div class="dt-alert">${J.sd("out")}<span>${esc(m.reason || "Unavailable")}</span></div>` : ""}
      ${modelNote(fam) ? `<p class="dt-note"><b>Best at:</b> ${esc(modelNote(fam))}</p>` : ""}
      <p class="dt-note">${spare ? `When the quota bucket the ${esc(toolName)} tier models share is used up, their tasks move to ${esc(name)}, which has a bucket of its own.` : `${esc(word)} tasks sent to ${esc(toolName)} run on ${esc(name)}.`}${lv.length ? ` Each task starts at <b>${esc(EFF_SHORT[start] || start)}</b> effort and moves one step
        up or down with what it asks (debugging and design go up, renames and typos go down)${tc.effort_in_model ? `. The effort is part of the model name, e.g. <code>${esc(fam)}-${esc(start)}</code>` : ""}.` : " It takes no effort setting, so every task runs as agy sets it."}</p>
      <h4>Effort</h4><div class="kv"><span>Range</span><b>${chips ? `<span class="dt-ech">${chips}</span>` : "set by the tool"}</b><span>Starts at</span><b>${esc(EFF_SHORT[start] || start || "—")}</b>
        <span>Running now</span><b>${live.length ? esc(live.join(", ")) : "nothing"}</b></div>
      <h4>If it's out</h4><div class="kv"><span>Falls back to</span><b>${esc(fbTxt)}</b><span>Quota</span><b>${m.available === false ? `<span class="ink-red">out${m.until ? ` · back in ${esc(J.span(m.until - J.now()))}` : ""}</span>` : "available"}</b></div>
      <h4>Jobs on this model</h4>${agentList(T.ids)}`;
  }
  const fb = S.fallback || {}, d = S.dispatch || {};
  const rer = (S.agents || []).filter(a => a.fallback_from || (a.reasons || []).some(r => typeof r === "string" && r.startsWith("fallback:"))).slice(0, 5);
  return `<div class="dt-head"><svg class="mark" aria-hidden="true"><use href="#mark"/></svg><div><h3>Jarviee Router</h3><p>Rates each task basic, light, complex or heavy, then routes it.</p></div></div>
    <h4>Dispatch table</h4><div class="kv">${J.TIERS.map(tr => `<span>${J.TIER_WORD[tr]}</span><b class="num">${esc(J.TOOL_SHORT[d[tr]?.tool] || d[tr]?.tool || "—")} · ${esc(d[tr]?.model || S.tools?.[d[tr]?.tool]?.models?.[tr] || "")}</b>`).join("")}</div>
    <h4>Routing rules</h4>
    <div class="rules">
      <label class="toggle"><input type="checkbox" data-rule="use_available_tool" ${fb.use_available_tool !== false ? "checked" : ""}><span>Use whichever tool is available</span></label>
      <label class="toggle"><input type="checkbox" data-rule="enabled" ${fb.enabled !== false ? "checked" : ""}><span>Fall back on quota or rate limits</span></label>
      <label class="toggle"><input type="checkbox" data-rule="never_fallback_to_opus" ${fb.never_fallback_to_opus !== false ? "checked" : ""}><span>Never fall back to Claude Opus</span></label>
    </div>
    <h4>Recent reroutes</h4>${rer.length ? `<div class="dl-list">${rer.map(a => `<a class="mini-row" href="#/agents/${encodeURIComponent(a.id)}">${J.sd("fb")}<span class="t">${esc(J.titleOf(a))}</span><span class="num muted">${esc(a.fallback_from ? a.fallback_from.split(":")[0] + " to " + a.tool : a.tool)}</span></a>`).join("")}</div>` : `<div class="empty">No reroutes yet.</div>`}`;
}
/* each h4 starts a section, so the panel can lay its sections out in columns when it sits under the graph */
const sections = html => html.split("<h4>").map((part, i) => `<div class="dsec">${i ? "<h4>" + part : part}</div>`).join("");
J.saveRule = async (key, value) => {
  try { await J.api("/api/settings/fallback", {[key]: value}); J.toast(`${{use_available_tool: "Use whichever tool is available", enabled: "Automatic fallback", never_fallback_to_opus: "Never fall back to Opus", omniroute: "OmniRoute as last resort"}[key] || key}: ${value ? "on" : "off"}`); J.poll(); }
  catch (e) { J.toast(`Not saved: ${e.message}`, true); J.poll(); }
};

J.fleet = {
  mount(el, det) {
    box = el; detail = det;
    box.addEventListener("click", e => {
      const b = e.target.closest("[data-act]"), j = e.target.closest("[data-job]");
      if (b && j) {   // Stop / confirm buttons on a job card
        e.preventDefault(); e.stopPropagation();
        const id = j.dataset.job, act = b.dataset.act;
        if (act === "stop") J.act.ask(id); else if (act === "stop-yes") J.act.stop(id); else if (act === "stop-no") J.act.cancel(id);
        return;
      }
      if (j) { e.preventDefault(); J.focusAgent(j.dataset.job); return; }   // a job card jumps to its window
      const n = e.target.closest("[data-node]"); if (!n) return;
      sel = sel === n.dataset.node ? "" : n.dataset.node;   // clicking the open node again closes its detail
      J.saved.set("jarviee.fleet.sel", sel); lastSig = ""; J.fleet.render();
    });
    box.addEventListener("keydown", e => { const j = e.target.matches?.("[data-job]") && e.target; if (j && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); J.focusAgent(j.dataset.job); } });
    det.addEventListener("change", e => { const r = e.target.closest("[data-rule]"); if (r) J.saveRule(r.dataset.rule, r.checked); });
    det.addEventListener("click", e => { if (e.target.closest("[data-dt-close]")) { sel = ""; J.saved.set("jarviee.fleet.sel", ""); lastSig = ""; J.fleet.render(); } });
    box.closest("details")?.querySelector(".fz")?.addEventListener("click", e => {
      const b = e.target.closest("[data-fz]"); if (!b) return;
      e.preventDefault(); e.stopPropagation();   // inside the panel's summary: don't fold the panel
      const k = b.dataset.fz;
      if (k === "fit") autoFit = !autoFit; else { autoFit = false; zoom = Math.min(1.2, Math.max(.5, Math.round((zoom + (k === "+" ? .1 : -.1)) * 10) / 10)); }
      J.saved.set("jarviee.fleet.zoom", String(zoom)); J.saved.set("jarviee.fleet.fit", autoFit ? "1" : "0");
      applyZoom(); drawWires();
    });
    ro = new ResizeObserver(() => drawWires());
    ro.observe(box);
  },
  render() {
    if (!box || !J.S) return;
    const ld = load();
    if (sel && sel !== "router" && !box.querySelector(`[data-node="${CSS.escape(sel)}"]`) && J.firstLoad === false && !sel.match(/^(lane|model):/)) sel = "";
    const L = lanes(), jobs = ld.jobs.slice(0, MAX_JOBS), more = ld.jobs.length - jobs.length;
    const html = `<svg class="wires" aria-hidden="true"></svg>
      <div class="fcol c-router" style="grid-row:1 / span ${L.length}">${nodeRouter(ld)}</div>
      ${L.map((t, i) => `<div class="fcol c-lane" style="grid-row:${i + 1}">${nodeLane(t, ld)}</div>
        <div class="mgroup" data-lane="${esc(t)}" style="grid-row:${i + 1}">${tiersFor(t, ld).map(tr => nodeModel(t, tr, ld)).join("")}</div>
        <div class="egroup" data-lane="${esc(t)}" style="grid-row:${i + 1}">${tiersFor(t, ld).map(tr => nodeEffort(t, tr, ld)).join("")}</div>`).join("")}
      <div class="fcol c-jobs${jobs.length > 4 ? " dense" : ""}" style="grid-row:1 / span ${L.length}" aria-label="Jobs running now">
        <span class="fj-h">Jobs now <span class="num">${ld.jobs.length}</span></span>
        ${jobs.length ? jobs.map(nodeJob).join("") : `<div class="fjob empty-job"><b>No jobs running</b><span class="fj-sub">Type a task in the command bar; it shows up here, wired to the model it runs on.</span></div>`}
        ${more > 0 ? `<a class="fj-more" href="#/agents?f=run">+${more} more in Agents</a>` : ""}
      </div>`;
    const sig = html.replace(/<svg class="wires"[^>]*><\/svg>/, "");
    if (sig !== lastSig) { lastSig = sig; J.setHTML(box, html); this.tick(); }
    applyZoom();
    drawWires();
    const wrap = detail.closest(".detail-panel"), body = sel ? detailHtml() : "";
    if (wrap) wrap.hidden = !body;
    if (!detail.contains(document.activeElement) || !document.activeElement.matches("input"))
      J.setHTML(detail, body ? `<button class="btn icon sm ghost dt-close" type="button" data-dt-close aria-label="Close details"><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg></button>` + sections(body) : "");
  },
  tick() {
    if (!detail) return;
    for (const c of box.querySelectorAll(".clock[data-start]")) { const st = +c.dataset.start; const t = st ? J.dur(J.now() - st) : ""; if (c.textContent !== t) c.textContent = t; }
    for (const el of detail.querySelectorAll("[data-until]")) { const t = J.span(+el.dataset.until - J.now()); if (el.textContent !== t) el.textContent = t; }
  },
};
})();
