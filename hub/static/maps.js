/* maps.js: Project Maps. Each project's .brain/FLOW.json drawn as a flow diagram you can drag, pan and zoom.
   Views: Full (default), Frontend, Backend; a filtered view keeps the nodes just across the boundary as faded ghosts.
   Named user journeys light their path in arc cyan. Dragged positions are saved per project and view. */
"use strict";
(() => {
const {esc} = J;
const W = 212, H = 54, HD = 88, GX = 40, GY = 96;
const hOf = id => byId.get(id)?.kind === "decision" ? HD : H;   // decisions are diamonds and need the height
const VIEWS = [["full", "Full"], ["frontend", "Frontend"], ["backend", "Backend"]];
const KIND = {actor: "User", screen: "Screen", component: "Component", api: "API", service: "Service", job: "Job",
  store: "Data store", external: "External", config: "Config", decision: "Decision"};
/* standard flowchart shapes, drawn to the node's own size (w x h) */
const SHAPE = {actor: "stadium", screen: "rect", component: "rect", api: "para", service: "sub", job: "hex",
  store: "cyl", external: "ext", config: "doc", decision: "diamond"};
function shapePath(shape, w, h) {
  const r = Math.min(6, h * .25), a = 1, b = w - 1, t = 1, u = h - 1, m = h / 2, k = Math.min(16, w * .12), e = Math.min(7, h * .22),
    dv = Math.min(8, h * .2), si = Math.min(11, w * .14);
  switch (shape) {
    case "stadium": return `M${m} ${t}H${w - m}A${m - 1} ${m - 1} 0 0 1 ${w - m} ${u}H${m}A${m - 1} ${m - 1} 0 0 1 ${m} ${t}Z`;
    case "diamond": return `M${w / 2} ${t}L${b} ${m}L${w / 2} ${u}L${a} ${m}Z`;
    case "para": return `M${a + k} ${t}H${b}L${b - k} ${u}H${a}Z`;
    case "hex": return `M${a + k} ${t}H${b - k}L${b} ${m}L${b - k} ${u}H${a + k}L${a} ${m}Z`;
    case "cyl": return `M${a} ${t + e}V${u - e}A${w / 2 - 1} ${e} 0 0 0 ${b} ${u - e}V${t + e}A${w / 2 - 1} ${e} 0 0 0 ${a} ${t + e}A${w / 2 - 1} ${e} 0 0 1 ${b} ${t + e}`;
    case "doc": return `M${a} ${t}H${b}V${u - dv}Q${w * .75} ${u - dv * 2.1} ${w / 2} ${u - dv}T${a} ${u - dv}Z`;
    case "sub": return `M${a + 3} ${t}H${b - 3}Q${b} ${t} ${b} ${t + 3}V${u - 3}Q${b} ${u} ${b - 3} ${u}H${a + 3}Q${a} ${u} ${a} ${u - 3}V${t + 3}Q${a} ${t} ${a + 3} ${t}ZM${a + si} ${t}V${u}M${b - si} ${t}V${u}`;
    default: return `M${a + r} ${t}H${b - r}Q${b} ${t} ${b} ${t + r}V${u - r}Q${b} ${u} ${b - r} ${u}H${a + r}Q${a} ${u} ${a} ${u - r}V${t + r}Q${a} ${t} ${a + r} ${t}Z`;
  }
}
const shapeSvg = (kind, w, h, cls = "pm-shape") => `<svg class="${cls}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><path d="${shapePath(SHAPE[kind] || "rect", w, h)}"/></svg>`;
const LAYER = {frontend: "Frontend", backend: "Backend", external: "External"};
let el = null, proj = J.saved.get("jarviee.map.proj") || "", view = "full";
let data = null, loadedKey = "", loading = false, sel = null, hover = null, journey = -1, projSig = "";
let pos = {}, core = new Set(), ghosts = new Set(), vis = [], vedges = [], byId = new Map();
let drag = null, pan = null, saveT = 0, raf = 0, generating = null;
const cams = {};
const q = s => el.querySelector(s);
const camKey = () => `${proj}:${view}`;
const cam = () => cams[camKey()] || (cams[camKey()] = {x: 40, y: 40, z: 1});
const ratio = () => { const c = q("#pm-canvas"); return c && c.offsetWidth ? c.getBoundingClientRect().width / c.offsetWidth : 1; };

/* ---------- layout: layered, top-down. Back edges are ignored for ranking; rows ordered by barycentre ---------- */
function autoLayout(ids, edges) {
  const out = new Map(ids.map(i => [i, []])), seen = new Map(), fwd = [];
  for (const e of edges) out.get(e.from)?.push(e);
  const hasIn = new Set(edges.map(e => e.to));
  const dfs = n => {
    seen.set(n, 1);
    for (const e of out.get(n)) {
      const s = seen.get(e.to);
      if (s === 1 || byId.get(e.to)?.kind === "actor") continue;   // loops, and anything pointing back at the user
      fwd.push(e); if (!s) dfs(e.to);
    }
    seen.set(n, 2);
  };
  for (const i of ids) if (byId.get(i)?.kind === "actor" && !seen.has(i)) dfs(i);   // the user starts the flow, even when alerts point back at them
  for (const i of ids) if (!hasIn.has(i) && !seen.has(i)) dfs(i);
  for (const i of ids) if (!seen.has(i)) dfs(i);
  const rank = new Map(ids.map(i => [i, 0])), indeg = new Map(ids.map(i => [i, 0]));
  for (const e of fwd) indeg.set(e.to, indeg.get(e.to) + 1);
  const fout = new Map(ids.map(i => [i, []])), fin = new Map(ids.map(i => [i, []]));
  for (const e of fwd) { fout.get(e.from).push(e.to); fin.get(e.to).push(e.from); }
  const queue = ids.filter(i => !indeg.get(i));
  while (queue.length) {
    const n = queue.shift();
    for (const m of fout.get(n)) { rank.set(m, Math.max(rank.get(m), rank.get(n) + 1)); indeg.set(m, indeg.get(m) - 1); if (!indeg.get(m)) queue.push(m); }
  }
  // a node nothing points to sits just above what it feeds, not in the top row (users stay on top)
  for (const i of [...ids].sort((a, b) => rank.get(b) - rank.get(a)))
    if (!fin.get(i).length && fout.get(i).length && byId.get(i)?.kind !== "actor") rank.set(i, Math.min(...fout.get(i).map(m => rank.get(m))) - 1);
  const rows = [];
  for (const i of ids) (rows[rank.get(i)] ||= []).push(i);
  const at = new Map();
  const index = () => rows.forEach(r => r?.forEach((n, k) => at.set(n, k)));
  index();
  const bary = (n, nb) => { const xs = nb.get(n).map(m => at.get(m)); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : at.get(n); };
  for (let pass = 0; pass < 6; pass++) {
    const down = pass % 2 === 0, order = down ? rows.keys() : [...rows.keys()].reverse();
    for (const r of order) { if (!rows[r]) continue; const b = new Map(rows[r].map(n => [n, bary(n, down ? fin : fout)])); rows[r].sort((a, c) => b.get(a) - b.get(c)); index(); }
  }
  // a row wider than MAXR wraps onto extra lines, so a screen with many siblings stays readable
  const MAXR = 7, lines = [];
  rows.forEach((r, i) => { if (!r) return; for (let k = 0; k < r.length; k += MAXR) lines.push({r: r.slice(k, k + MAXR), wrapped: k > 0, i}); });
  const widest = Math.max(...lines.map(l => l.r.length)), res = {};
  let y = 0, prevH = H;
  lines.forEach((l, k) => {
    const lh = Math.max(...l.r.map(hOf));
    if (k) y += prevH + (l.wrapped ? 44 : GY);
    const off = (widest - l.r.length) * (W + GX) / 2;
    l.r.forEach((n, x) => { res[n] = {x: off + x * (W + GX), y: y + (lh - hOf(n)) / 2}; });   // centred on the line
    prevH = lh;
  });
  return res;
}

/* ---------- what this view shows ---------- */
function computeView(fit) {
  const f = data?.flow;
  sel = hover = null;
  if (!f) { vis = []; vedges = []; return; }
  byId = new Map(f.nodes.map(n => [n.id, n]));
  const inCore = n => view === "full" || (view === "frontend" ? n.layer === "frontend" : n.layer !== "frontend");
  core = new Set(f.nodes.filter(inCore).map(n => n.id));
  ghosts = new Set();
  if (view !== "full") for (const e of f.edges) {
    if (core.has(e.from) && !core.has(e.to)) ghosts.add(e.to);
    if (core.has(e.to) && !core.has(e.from)) ghosts.add(e.from);
  }
  const shown = id => core.has(id) || ghosts.has(id);
  vis = f.nodes.filter(n => shown(n.id));
  vedges = f.edges.filter(e => shown(e.from) && shown(e.to) && (core.has(e.from) || core.has(e.to)));
  const auto = autoLayout(vis.map(n => n.id), vedges), saved = data.layout?.[view] || {};
  pos = {};
  for (const n of vis) if (saved[n.id]) pos[n.id] = {x: saved[n.id][0], y: saved[n.id][1]};
  if (!Object.keys(pos).length) for (const n of vis) pos[n.id] = auto[n.id];
  else placeNew(vis.filter(n => !pos[n.id]).sort((a, b) => auto[a.id].y - auto[b.id].y || auto[a.id].x - auto[b.id].x).map(n => n.id));
  if (journey >= (f.flows || []).length) journey = -1;
  if (fit || !cams[camKey()]) requestAnimationFrame(fitCam);
}

/* parts added after the owner arranged the map (an update added them): put each one next to what it connects to,
   below its sources or above its targets, then nudge sideways and down until it overlaps nothing */
function placeNew(ids) {
  const M = 18, hit = (x, y, h) => Object.entries(pos).some(([id, p]) =>
    x < p.x + W + M && x + W + M > p.x && y < p.y + hOf(id) + M && y + h + M > p.y);
  for (const id of ids) {
    const h = hOf(id), from = vedges.filter(e => e.to === id && pos[e.from]).map(e => pos[e.from]);
    const to = vedges.filter(e => e.from === id && pos[e.to]).map(e => pos[e.to]);
    const avg = ps => ps.reduce((s, p) => s + p.x, 0) / ps.length;
    let x, y;
    if (from.length) { x = avg(from); y = Math.max(...from.map(p => p.y)) + H + GY * .6; }
    else if (to.length) { x = avg(to); y = Math.min(...to.map(p => p.y)) - h - GY * .6; }
    else { const all = Object.values(pos); x = Math.max(...all.map(p => p.x), 0) + W + GX; y = Math.min(...all.map(p => p.y), 0); }
    const step = (W + GX) / 2;
    search: for (let row = 0; row < 12; row++) {
      for (const k of [0, 1, -1, 2, -2, 3, -3, 4, -4]) {
        if (!hit(x + k * step, y + row * (H + 30), h)) { x += k * step; y += row * (H + 30); break search; }
      }
    }
    pos[id] = {x: Math.round(x), y: Math.round(y)};
  }
}

function fitCam() {
  const c = q("#pm-canvas"); if (!c || !vis.length) return;
  const side = q(".pm-side")?.offsetWidth || 0, cw = c.offsetWidth - side - 24, ch = c.offsetHeight;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of vis) { const p = pos[n.id]; x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x + W); y1 = Math.max(y1, p.y + hOf(n.id)); }
  const z = Math.max(.2, Math.min(1.1, (cw - 48) / (x1 - x0), (ch - 48) / (y1 - y0)));
  Object.assign(cam(), {z, x: 24 + (cw - (x1 - x0) * z) / 2 - x0 * z, y: (ch - (y1 - y0) * z) / 2 - y0 * z});
  applyCam();
}
function applyCam() {
  const w = q("#pm-world"); if (!w) return;
  const c = cam();
  w.style.transform = `translate(${c.x.toFixed(1)}px, ${c.y.toFixed(1)}px) scale(${c.z.toFixed(3)})`;
  w.classList.toggle("near", c.z >= .72);
  const z = q("#pm-zoom"); if (z) z.textContent = Math.round(c.z * 100) + "%";
  const bg = q("#pm-canvas"); if (bg) { bg.style.backgroundSize = `${18 * c.z}px ${18 * c.z}px`; bg.style.backgroundPosition = `${c.x}px ${c.y}px`; }
}
function zoomAt(f, cx, cy) {
  const c = cam(), z = Math.max(.2, Math.min(2.2, c.z * f));
  if (cx === undefined) { const k = q("#pm-canvas"); cx = k.offsetWidth / 2; cy = k.offsetHeight / 2; }
  c.x = cx - (cx - c.x) * z / c.z; c.y = cy - (cy - c.y) * z / c.z; c.z = z;
  applyCam();
}

/* ---------- edges ---------- */
function geom(e) {
  const A = pos[e.from], B = pos[e.to], ha = hOf(e.from), hb = hOf(e.to);
  let p0, p3, c1, c2;
  if (B.y >= A.y + ha + 10) { p0 = [A.x + W / 2, A.y + ha]; p3 = [B.x + W / 2, B.y]; const d = Math.max(28, (p3[1] - p0[1]) / 2); c1 = [p0[0], p0[1] + d]; c2 = [p3[0], p3[1] - d]; }
  else if (B.y + hb + 10 <= A.y) { p0 = [A.x + W / 2, A.y]; p3 = [B.x + W / 2, B.y + hb]; const d = Math.max(28, (p0[1] - p3[1]) / 2); c1 = [p0[0], p0[1] - d]; c2 = [p3[0], p3[1] + d]; }
  else {
    const right = B.x >= A.x, d = Math.max(28, Math.abs(B.x - A.x) / 2) * (right ? 1 : -1);
    p0 = [right ? A.x + W : A.x, A.y + ha / 2]; p3 = [right ? B.x : B.x + W, B.y + hb / 2]; c1 = [p0[0] + d, p0[1]]; c2 = [p3[0] - d, p3[1]];
  }
  const mid = [(p0[0] + 3 * c1[0] + 3 * c2[0] + p3[0]) / 8, (p0[1] + 3 * c1[1] + 3 * c2[1] + p3[1]) / 8];
  const a = Math.atan2(p3[1] - c2[1], p3[0] - c2[0]), s = 6;
  const head = `M${(p3[0] - s * Math.cos(a - .45)).toFixed(1)} ${(p3[1] - s * Math.sin(a - .45)).toFixed(1)}L${p3[0].toFixed(1)} ${p3[1].toFixed(1)}L${(p3[0] - s * Math.cos(a + .45)).toFixed(1)} ${(p3[1] - s * Math.sin(a + .45)).toFixed(1)}`;
  const f = n => n.toFixed(1);
  return {d: `M${f(p0[0])} ${f(p0[1])}C${f(c1[0])} ${f(c1[1])} ${f(c2[0])} ${f(c2[1])} ${f(p3[0])} ${f(p3[1])}`, head, mid, p0, p3};
}
function paintEdges() {
  const svg = q("#pm-wires"), labels = q("#pm-labels"); if (!svg) return;
  let paths = "", chips = "";
  vedges.forEach((e, i) => {
    const g = geom(e), L = byId.get(e.from)?.layer || "backend", gh = ghosts.has(e.from) || ghosts.has(e.to) ? " ghost" : "";
    paths += `<g class="pe L-${L}${gh}" data-e="${i}"><path d="${g.d}"/><path class="hd" d="${g.head}"/><circle cx="${g.p0[0].toFixed(1)}" cy="${g.p0[1].toFixed(1)}" r="2.6"/></g>`;
    if (e.label) chips += `<span class="pe-l L-${L}${gh}" data-e="${i}" style="left:${g.mid[0].toFixed(1)}px;top:${g.mid[1].toFixed(1)}px">${esc(e.label)}</span>`;
  });
  svg.innerHTML = paths; labels.innerHTML = chips;
}

/* ---------- nodes ---------- */
function paintNodes() {
  const box = q("#pm-nodes"); if (!box) return;
  box.innerHTML = vis.map(n => {
    const p = pos[n.id];
    const h = hOf(n.id);
    return `<div class="pm-node L-${n.layer} k-${n.kind} s-${SHAPE[n.kind] || "rect"}${ghosts.has(n.id) ? " ghost" : ""}" data-id="${esc(n.id)}" tabindex="0" role="button"
      aria-label="${esc(`${n.label}, ${KIND[n.kind]}, ${LAYER[n.layer]}`)}" title="${esc(n.label)}" style="left:${p.x}px;top:${p.y}px;height:${h}px">
      ${shapeSvg(n.kind, W, h)}<span class="pm-t"><small>${KIND[n.kind]}</small><b>${esc(n.label)}</b></span><span class="pm-step"></span></div>`;
  }).join("");
}
function moveNode(id) {
  const n = q(`.pm-node[data-id="${CSS.escape(id)}"]`); if (!n) return;
  n.style.left = pos[id].x + "px"; n.style.top = pos[id].y + "px";
  if (!raf) raf = requestAnimationFrame(() => { raf = 0; paintEdges(); highlight(); });
}

/* ---------- highlight: selection, hover, or a journey ---------- */
function highlight() {
  if (!el) return;
  const f = data?.flow, nodes = new Set(), edges = new Set(), steps = new Map();
  if (journey >= 0 && f?.flows?.[journey]) {
    const st = f.flows[journey].steps;
    st.forEach((s, i) => { nodes.add(s); if (!steps.has(s)) steps.set(s, i + 1); });
    vedges.forEach((e, i) => { for (let k = 0; k < st.length - 1; k++) if ((e.from === st[k] && e.to === st[k + 1]) || (e.to === st[k] && e.from === st[k + 1])) edges.add(i); });
  } else {
    const focus = sel || hover;
    if (focus) { nodes.add(focus); vedges.forEach((e, i) => { if (e.from === focus || e.to === focus) { edges.add(i); nodes.add(e.from); nodes.add(e.to); } }); }
  }
  const on = nodes.size > 0;
  q("#pm-world")?.classList.toggle("focus", on);
  q("#pm-world")?.classList.toggle("journey", journey >= 0);
  for (const n of el.querySelectorAll(".pm-node")) {
    const id = n.dataset.id;
    n.classList.toggle("lit", nodes.has(id)); n.classList.toggle("sel", id === sel);
    n.querySelector(".pm-step").textContent = steps.get(id) || "";
  }
  for (const g of el.querySelectorAll("[data-e]")) g.classList.toggle("lit", edges.has(+g.dataset.e));
}

/* ---------- side panel ---------- */
function linkList(ids, verb) {
  if (!ids.length) return "";
  return `<h4 class="sect-h">${verb}</h4><div class="pm-links">${ids.map(([id, label]) => { const n = byId.get(id); return n ? `<button type="button" class="pm-link" data-go="${esc(id)}"><span class="pm-dot L-${n.layer}"></span><b>${esc(n.label)}</b>${label ? `<span class="num">${esc(label)}</span>` : ""}</button>` : ""; }).join("")}</div>`;
}
function side() {
  const f = data?.flow; if (!f) return "";
  if (sel && byId.has(sel)) {
    const n = byId.get(sel);
    const outs = vedges.filter(e => e.from === sel).map(e => [e.to, e.label]), ins = vedges.filter(e => e.to === sel).map(e => [e.from, e.label]);
    return `<div class="pm-sh"><span class="pm-ic L-${n.layer}"><svg class="i" aria-hidden="true"><use href="#k-${n.kind}"/></svg></span><div><small>${KIND[n.kind]} · ${LAYER[n.layer]}</small><h3>${esc(n.label)}</h3></div>
        <button class="btn icon sm ghost" type="button" data-unsel aria-label="Close"><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg></button></div>
      ${n.detail ? `<p>${esc(n.detail)}</p>` : ""}${n.file ? `<div class="chip mono pm-file" title="${esc(n.file)}">${esc(n.file)}</div>` : ""}
      ${linkList(outs, "Calls / leads to")}${linkList(ins, "Called by / comes from")}`;
  }
  if (journey >= 0 && f.flows[journey]) {
    const fl = f.flows[journey];
    return `<div class="pm-sh"><div><small>User journey</small><h3>${esc(fl.name)}</h3></div></div>
      <ol class="pm-steps">${fl.steps.map(s => { const n = byId.get(s); const off = !core.has(s) && !ghosts.has(s); return `<li><button type="button" class="pm-link${off ? " off" : ""}" data-go="${esc(s)}"><span class="pm-dot L-${n.layer}"></span><b>${esc(n.label)}</b><span class="num">${LAYER[n.layer]}</span></button></li>`; }).join("")}</ol>
      ${view !== "full" ? `<p class="faint">Steps outside the ${view} view are greyed. Switch to Full to see the whole journey.</p>` : ""}`;
  }
  const cnt = l => f.nodes.filter(n => n.layer === l).length;
  return `<div class="pm-sh"><div><small>Project map</small><h3>${esc(f.title || data.project)}</h3></div></div>
    ${f.summary ? `<p>${esc(f.summary)}</p>` : ""}
    <div class="pm-legend">${Object.keys(LAYER).map(l => `<span><span class="pm-dot L-${l}"></span>${LAYER[l]}<b class="num">${cnt(l)}</b></span>`).join("")}</div>
    ${mapStatus()}
    <h4 class="sect-h">Shapes</h4><div class="pm-key">${[...new Set(f.nodes.map(n => n.kind))].sort((a, b) => Object.keys(KIND).indexOf(a) - Object.keys(KIND).indexOf(b))
      .map(k => `<span>${shapeSvg(k, 34, k === "decision" ? 22 : 16, "pm-key-s")}${KIND[k]}</span>`).join("")}</div>
    <p class="faint">Drag boxes to arrange them; drag the background or scroll to pan; pinch or use the zoom buttons to zoom. Click a box to see what it connects to.</p>
    ${f.flows.length ? `<h4 class="sect-h">User journeys</h4><div class="pm-links">${f.flows.map((fl, i) => `<button type="button" class="pm-link" data-journey="${i}"><svg class="i sm" aria-hidden="true"><use href="#i-route"/></svg><b>${esc(fl.name)}</b><span class="num">${fl.steps.length}</span></button>`).join("")}</div>` : ""}
    ${data.warnings?.length ? `<h4 class="sect-h">Skipped in FLOW.json</h4><ul class="pm-warn">${data.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}`;
}

/* ---------- is the map current? (hub/mapwatch.py keeps it so) ---------- */
const WAIT = {settling: "updates once the latest commits settle", busy: "updates when the agents working in this project finish",
  cooldown: "next automatic update after the cooldown", "daily cap": "today's automatic update limit is reached"};
function mapStatus() {
  const m = projects().find(p => p.name === proj)?.map || {}, auto = J.S?.maps?.auto_update !== false;
  const run = m.run && (J.S?.agents || []).find(a => a.id === m.run), n = m.changed || 0;
  const ago = t => t ? `${J.span(J.now() - t)} ago` : "";
  let line;
  if (m.state === "updating") line = `${J.sd("run")}<span>Updating now${run ? ` · <a href="#/agents/${encodeURIComponent(run.id)}">watch the agent</a>` : ""}</span>`;
  else if (m.state === "behind") line = `${J.sd("need")}<span><b>${n} changed file${n === 1 ? "" : "s"}</b> since the map. ${auto ? (WAIT[m.waiting] || "It updates automatically shortly.") : "Automatic updates are off (Settings)."}</span>`;
  else if (m.state === "current") line = `${J.sd("done")}<span>Up to date with the code${m.result === "updated" && m.finished ? ` · updated ${ago(m.finished)}` : ""}</span>`;
  else if (m.state === "uncommitted") line = `${J.sd("idle")}<span>The map isn't committed yet, so code changes can't be tracked against it.</span>`;
  else if (m.state === "untracked") line = `${J.sd("idle")}<span>Not a git repo, so changes aren't tracked. Use Update now after changing it.</span>`;
  else if (m.state === "error") line = `${J.sd("fail")}<span>Last update: ${esc(m.note || "failed")}</span>`;
  else line = `${J.sd("idle")}<span>Checking the code for changes…</span>`;
  return `<div class="pm-status">${line}</div>`;
}

/* ---------- frame ---------- */
function projects() { return (J.S?.projects || []); }
function header() {
  const ps = projects(), sig = ps.map(p => p.name + ":" + (p.flow ? 1 : 0)).join("|");
  if (sig !== projSig) {
    projSig = sig;
    q("#pm-proj").innerHTML = ps.map(p => `<option value="${esc(p.name)}"${p.name === proj ? " selected" : ""}>${esc(p.name)}${p.flow ? "" : " (no map yet)"}</option>`).join("");
  }
  q("#pm-proj").value = proj;
  for (const b of q("#pm-view").children) b.setAttribute("aria-pressed", String(b.dataset.view === view));
  const flows = data?.flow?.flows || [];
  const js = q("#pm-journey"), jsig = proj + ":" + flows.map(f => f.name).join("|");
  if (js.dataset.sig !== jsig) { js.dataset.sig = jsig; js.innerHTML = `<option value="-1">All connections</option>` + flows.map((f, i) => `<option value="${i}">${esc(f.name)}</option>`).join(""); }
  js.value = String(journey); js.hidden = !flows.length;
  const has = !!data?.ok;
  for (const id of ["#pm-zctl", "#pm-relayout"]) q(id).hidden = !has;
  q("#pm-gen").innerHTML = `<svg class="i sm" aria-hidden="true"><use href="#i-refresh"/></svg>${has ? "Redraw" : "Map this project"}`;
  q("#pm-gen").title = has ? "Map the project again from scratch" : "";
  const st = projects().find(p => p.name === proj)?.map || {};
  q("#pm-upd").hidden = !has || st.state === "updating";
  q("#pm-gen").hidden = !proj || !!genRun();
}
function genRun() {
  if (!generating || generating.proj !== proj) return null;
  const a = (J.S?.agents || []).find(x => x.id === generating.id);
  return a && ["running", "waiting", "queued"].includes(a.status) ? a : null;
}
function stateCard() {
  const run = genRun();
  if (!projects().length) return `<div class="pm-empty"><b>No projects yet</b><span>Jarviee maps project folders that have a <code>.brain/</code> in the workspace.</span></div>`;
  if (loading && !data) return `<div class="pm-empty"><span>Loading the map…</span></div>`;
  if (run) return `<div class="pm-empty">${J.sd("run")}<b>An agent is mapping ${esc(proj)}</b><span>It reads the code and writes <code>.brain/FLOW.json</code>. The map appears here when it's done.</span><a class="btn sm" href="#/agents/${encodeURIComponent(run.id)}">Watch the agent</a></div>`;
  if (data?.missing) return `<div class="pm-empty"><b>${esc(proj)} has no map yet</b><span>An agent can read the project and draw its full user flow, frontend and backend, into <code>.brain/FLOW.json</code>.</span>
    <button class="btn primary" type="button" data-gen><svg class="i sm" aria-hidden="true"><use href="#i-play"/></svg>Map this project</button></div>`;
  if (data && !data.ok) return `<div class="pm-empty"><b class="ink-red">The map for ${esc(proj)} can't be read</b><span><code>.brain/FLOW.json</code>: ${esc(data.error || "unknown error")}</span>
    <button class="btn" type="button" data-gen><svg class="i sm" aria-hidden="true"><use href="#i-refresh"/></svg>Map it again</button></div>`;
  if (data?.ok && !vis.length) return `<div class="pm-empty"><b>Nothing on the ${view} side</b><span>This map has no ${view} nodes. Switch to Full.</span></div>`;
  return "";
}
function paint() {
  if (!el) return;
  header();
  const card = stateCard();
  q("#pm-state").innerHTML = card; q("#pm-state").hidden = !card;
  q("#pm-world").hidden = !!card;
  q(".pm-side").hidden = !!card;
  if (card) return;
  paintNodes(); paintEdges(); highlight(); applyCam();
  J.setHTML(q(".pm-side"), side());
}

async function load() {
  if (!proj) return;
  const key = proj, seen = projects().find(p => p.name === proj)?.flow || null;
  loading = true;
  try { data = await J.api("/api/flow?project=" + encodeURIComponent(key)); }
  catch (e) { data = {ok: false, error: e.message}; }
  if (key !== proj) return;
  data._seen = seen; loading = false; loadedKey = key;
  computeView(true); paint();
}
function save() {
  clearTimeout(saveT);
  saveT = setTimeout(async () => {
    const positions = Object.fromEntries(vis.map(n => [n.id, [Math.round(pos[n.id].x), Math.round(pos[n.id].y)]]));
    data.layout = {...(data.layout || {}), [view]: positions};
    try { await J.api("/api/flow/layout", {project: proj, view, positions}); } catch (e) { J.toast("Couldn't save the layout: " + e.message, true); }
  }, 350);
}
async function generate() {
  try {
    const r = await J.api("/api/flow/generate", {project: proj});
    generating = {proj, id: r.id};
    J.toast(`Mapping ${proj}: an agent is reading the project.`);
  } catch (e) { J.toast(e.message, true); }
  paint();
}
function select(id, center) {
  sel = id; journey = -1; highlight(); header();
  J.setHTML(q(".pm-side"), side());
  if (center && pos[id]) {
    const c = cam(), k = q("#pm-canvas"), sw = q(".pm-side").offsetWidth;
    c.x = (k.offsetWidth - sw) / 2 - (pos[id].x + W / 2) * c.z; c.y = k.offsetHeight / 2 - (pos[id].y + hOf(id) / 2) * c.z; applyCam();
  }
}
function setProject(name) {
  if (!name || name === proj) return;
  proj = name; J.saved.set("jarviee.map.proj", proj); data = null; journey = -1; sel = null;
  history.replaceState(null, "", "#/maps/" + encodeURIComponent(proj));
  paint(); load();
}

/* ---------- pointer: drag a node, pan the background, wheel to pan, pinch to zoom ---------- */
function bindCanvas(c) {
  c.addEventListener("pointerdown", e => {
    if (e.button !== 0 || e.target.closest(".pm-side, .pm-empty, button, a, select")) return;
    const n = e.target.closest(".pm-node");
    if (n) { const p = pos[n.dataset.id]; drag = {id: n.dataset.id, sx: e.clientX, sy: e.clientY, ox: p.x, oy: p.y, moved: false}; n.setPointerCapture(e.pointerId); n.classList.add("dragging"); }
    else { const k = cam(); pan = {sx: e.clientX, sy: e.clientY, ox: k.x, oy: k.y, moved: false}; c.setPointerCapture(e.pointerId); c.classList.add("panning"); }
  });
  c.addEventListener("pointermove", e => {
    if (drag) {
      const r = ratio(), dx = (e.clientX - drag.sx) / r, dy = (e.clientY - drag.sy) / r;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      drag.moved = true; const z = cam().z;
      pos[drag.id] = {x: Math.round(drag.ox + dx / z), y: Math.round(drag.oy + dy / z)}; moveNode(drag.id);
    } else if (pan) {
      const r = ratio(), k = cam(); pan.moved = true;
      k.x = pan.ox + (e.clientX - pan.sx) / r; k.y = pan.oy + (e.clientY - pan.sy) / r; applyCam();
    } else {
      const n = e.target.closest(".pm-node"), id = n?.dataset.id || null;
      if (id !== hover) { hover = id; if (!sel && journey < 0) highlight(); }
    }
  });
  const end = () => {
    if (drag) { q(".pm-node.dragging")?.classList.remove("dragging"); if (drag.moved) save(); else select(drag.id === sel ? null : drag.id); drag = null; }
    if (pan) { c.classList.remove("panning"); if (!pan.moved && (sel || journey >= 0)) { sel = null; journey = -1; highlight(); header(); J.setHTML(q(".pm-side"), side()); } pan = null; }
  };
  c.addEventListener("pointerup", end); c.addEventListener("pointercancel", end);
  c.addEventListener("pointerleave", () => { if (hover && !drag) { hover = null; if (!sel && journey < 0) highlight(); } });
  c.addEventListener("wheel", e => {
    if (e.target.closest(".pm-side")) return;
    e.preventDefault();
    const r = ratio(), b = c.getBoundingClientRect();
    if (e.ctrlKey || e.metaKey) zoomAt(Math.exp(-e.deltaY * .01), (e.clientX - b.left) / r, (e.clientY - b.top) / r);
    else { const k = cam(); k.x -= e.deltaX / r; k.y -= e.deltaY / r; applyCam(); }
  }, {passive: false});
  c.addEventListener("keydown", e => {
    const n = e.target.closest(".pm-node"); if (!n) return;
    const id = n.dataset.id, step = e.shiftKey ? 40 : 10, mv = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step]}[e.key];
    if (mv) { e.preventDefault(); pos[id] = {x: pos[id].x + mv[0], y: pos[id].y + mv[1]}; moveNode(id); save(); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(id); }
  });
}

J.pages.maps = {
  init() {
    el = document.querySelector('[data-page="maps"]');
    el.innerHTML = `<section class="panel pm" aria-labelledby="pm-h">
      <div class="ph pm-bar"><h2 id="pm-h">Project map</h2>
        <select class="select" id="pm-proj" aria-label="Project"></select>
        <span class="seg" id="pm-view" role="group" aria-label="Show">${VIEWS.map(([k, l]) => `<button type="button" data-view="${k}">${l}</button>`).join("")}</span>
        <select class="select" id="pm-journey" aria-label="User journey" hidden></select>
        <span class="act">
          <button class="btn sm ghost" type="button" id="pm-relayout" title="Forget dragged positions in this view">Auto layout</button>
          <span class="pm-z" id="pm-zctl"><button class="btn icon sm ghost" type="button" data-z="-" aria-label="Zoom out">−</button><span class="num faint" id="pm-zoom">100%</span>
            <button class="btn icon sm ghost" type="button" data-z="+" aria-label="Zoom in"><svg class="i sm" aria-hidden="true"><use href="#i-plus"/></svg></button><button class="btn sm ghost" type="button" data-z="fit">Fit</button></span>
          <button class="btn sm" type="button" id="pm-upd" title="Have an agent bring this map up to date with the code" hidden><svg class="i sm" aria-hidden="true"><use href="#i-refresh"/></svg>Update now</button>
          <button class="btn sm ghost" type="button" id="pm-gen" data-gen></button></span></div>
      <div class="pm-canvas" id="pm-canvas">
        <div class="pm-world" id="pm-world"><svg class="pm-wires" id="pm-wires" aria-hidden="true"></svg><div id="pm-labels"></div><div id="pm-nodes"></div></div>
        <div class="pm-state" id="pm-state" hidden></div>
        <aside class="pm-side" aria-label="Details"></aside>
      </div></section>`;
    bindCanvas(q("#pm-canvas"));
    q("#pm-proj").addEventListener("change", e => setProject(e.target.value));
    q("#pm-journey").addEventListener("change", e => { journey = +e.target.value; sel = null; highlight(); J.setHTML(q(".pm-side"), side()); });
    el.addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.view) { if (b.dataset.view !== view) { view = b.dataset.view; computeView(false); paint(); } return; }
      if (b.dataset.z) { b.dataset.z === "fit" ? fitCam() : zoomAt(b.dataset.z === "+" ? 1.2 : 1 / 1.2); return; }
      if (b.dataset.gen !== undefined) { generate(); return; }
      if (b.id === "pm-upd") {
        b.disabled = true;
        J.api("/api/flow/update", {project: proj, force: true}).then(r => { J.toast(r.message, !r.ok); J.poll(); })
          .catch(err => J.toast(err.message, true)).finally(() => { b.disabled = false; });
        return;
      }
      if (b.id === "pm-relayout") {
        if (data?.layout) delete data.layout[view];
        J.api("/api/flow/layout", {project: proj, view, positions: null}).catch(err => J.toast(err.message, true));
        computeView(true); paint(); return;
      }
      if (b.dataset.go) { select(b.dataset.go, true); return; }
      if (b.dataset.journey) { journey = +b.dataset.journey; sel = null; highlight(); header(); J.setHTML(q(".pm-side"), side()); return; }
      if (b.dataset.unsel !== undefined) { sel = null; highlight(); J.setHTML(q(".pm-side"), side()); }
    });
    J.on("escape", () => { if (J.shell.current() === "maps" && (sel || journey >= 0)) { sel = null; journey = -1; highlight(); header(); J.setHTML(q(".pm-side"), side()); } });
  },
  show(params) {
    if (params[0] && params[0] !== proj) { proj = params[0]; J.saved.set("jarviee.map.proj", proj); data = null; journey = -1; }
  },
  render() {
    if (!J.S || !el) return;
    const ps = projects();
    if (!ps.some(p => p.name === proj)) {
      const pick = ps.find(p => p.flow) || ps[0];
      if (pick) { proj = pick.name; data = null; }
    }
    const p = ps.find(x => x.name === proj);
    if (p && !loading && (loadedKey !== proj || !data || (p.flow || null) !== (data._seen || null))) { paint(); load(); return; }
    header();
    if (!data?.ok) { const card = stateCard(); if (q("#pm-state").innerHTML !== card) paint(); }
    else if (!sel && journey < 0) J.setHTML(q(".pm-side"), side());   // keep the up-to-date line live
  },
  crumb() { const f = data?.flow; return f ? `${proj} · ${f.nodes.length} parts · ${f.edges.length} links` : proj; },
};
})();
