/* composer.js: the command bar. Live router preview, tier override, tool/project/folder, Plan mode with inline approval, voice dictation. */
"use strict";
(() => {
const {$, esc} = J;
let addEv = null, skipAdd = "", undoTimer = null;   // addEv: the add-event reading of the text; skipAdd: text the owner sent to an agent instead
let root = null, single = false, tierSel = "", planOn = false, draft = null, tPrev = null, seq = 0, sigOpts = "", launching = false;
const q = s => root.querySelector(s);

const HTML = `
<form class="cmdbar" id="cmd" autocomplete="off">
  <div class="cmd-row">
    <svg class="mark" aria-hidden="true"><use href="#mark"/></svg>
    <label for="task" class="sr-only">New task for an agent</label>
    <textarea id="task" rows="1" placeholder="Tell Jarviee what to do. It picks the tool and the model."></textarea>
    <button class="btn ghost mic" id="mic" type="button" aria-pressed="false"><svg class="i" aria-hidden="true" viewBox="0 0 16 16"><rect x="5.5" y="1.5" width="5" height="8" rx="2.5"/><path d="M3 7.5a5 5 0 0 0 10 0M8 12.5v2M5.5 14.5h5"/></svg></button>
    <select class="select" id="tpl" aria-label="Insert a saved prompt template"><option value="">Templates</option></select>
    <button class="btn ghost" id="tpl-save" type="button" title="Save the text as a template">Save</button>
    <button class="btn ghost" id="tpl-del" type="button" title="Delete the selected template" hidden>Delete</button>
    <span class="kbd hint" aria-hidden="true">/</span>
    <button class="btn primary pill" id="launch" type="submit" disabled><svg class="i" aria-hidden="true"><use href="#i-play"/></svg><span>Launch</span></button>
  </div>
  <div class="mic-note" id="mic-note" role="status" hidden></div>
  <div class="cmd-opts">
    <label class="field" id="f-tool">Tool <select class="select" id="tool"><option value="">Auto</option></select></label>
    <label class="field">Project <select class="select" id="project"><option value="">None</option></select></label>
    <label class="field" id="f-cwd">Folder <input class="input" id="cwd" type="text" placeholder="optional path" spellcheck="false"></label>
    <label class="field" id="f-planner" hidden>Planner <select class="select" id="planner"><option value="claude">Claude · sonnet</option><option value="mock">Offline split</option></select></label>
    <div class="field" role="group" aria-label="Level" id="f-level">Level
      <span class="seg" id="tier">
        <button type="button" data-tier="" aria-pressed="true">Auto</button><button type="button" data-tier="basic" aria-pressed="false">Basic</button><button type="button" data-tier="light" aria-pressed="false">Light</button><button type="button" data-tier="complex" aria-pressed="false">Complex</button><button type="button" data-tier="heavy" aria-pressed="false">Heavy</button>
      </span>
    </div>
    <span class="seg"><button type="button" id="plantoggle" class="em" aria-pressed="false" title="Split the goal into subtasks you approve before anything runs"><svg class="i sm" aria-hidden="true"><use href="#i-split"/></svg> Plan</button></span>
    <span class="kbd launch-kbd" aria-hidden="true">${/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? "⌘ Enter" : "Ctrl Enter"}</span>
  </div>
  <div class="thumbs" id="thumbs" hidden></div>
  <div class="preview" id="preview" aria-live="polite"></div>
</form>`;

const reasonText = r => r.replace(/^simple-change words: /, "simple change: ").replace(/^small-change words: /, "small change: ")
  .replace(/^multi-part words: /, "multi-part: ").replace(/^high-risk words: /, "high risk: ");
const looksLikeSeveral = t => (t.match(/^\s*(?:[-*•]|\d+[.)])\s+/gm) || []).length >= 2 || (t.match(/\b(?:and then|then|and)\b/gi) || []).length >= 2;
const short = id => String(id).split(".").pop();

let shots = [];   // {path, url}: images pasted or dropped, appended to the task as file paths
function paintShots() {
  const t = q("#thumbs"); t.hidden = !shots.length;
  t.innerHTML = shots.map((s, i) => `<span class="thumb"><img src="${s.url}" alt="Attached image ${i + 1}"><button type="button" data-rm="${i}" aria-label="Remove image ${i + 1}"><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg></button></span>`).join("");
}
async function attach(files) {
  for (const f of files) {
    if (!/^image\/(png|jpeg|gif|webp)$/.test(f.type)) continue;
    try {
      const r = await fetch("/api/attach", {method: "POST", headers: {"Content-Type": f.type, "X-Jarviee-Token": window.JARVIEE_TOKEN || (document.querySelector('meta[name="jarviee-token"]') || {}).content || ""}, body: f});
      const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status);
      shots.push({path: j.path, url: URL.createObjectURL(f)}); paintShots(); open(); preview();
    } catch (e) { J.toast("Image not attached: " + e.message, true); }
  }
}
const withShots = text => shots.length ? text + "\n\nScreenshots showing the problem (view these image files):\n" + shots.map(s => "- " + s.path).join("\n") : text;
function open() { q("#cmd").classList.add("open"); }
function autosize() { const t = q("#task"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; t.style.overflowY = t.scrollHeight > 180 ? "auto" : "hidden"; }
function setTier(t) { tierSel = t; for (const b of q("#tier").children) b.setAttribute("aria-pressed", String(b.dataset.tier === t)); }
function sync() {
  q("#f-tool").hidden = planOn; q("#f-level").hidden = planOn; q("#f-planner").hidden = !planOn;
  q("#f-cwd").hidden = planOn || !!q("#project").value;
  q("#plantoggle").setAttribute("aria-pressed", String(planOn));
}
function setPlan(on) { planOn = on; draft = null; q("#task").placeholder = on ? "A bigger goal. It is split into subtasks you approve first." : "Tell Jarviee what to do. It picks the tool and the model."; sync(); preview(); }
function options() {
  const S = J.S; if (!S || !root) return;
  const tools = Object.keys(S.tools).filter(t => t !== "echo" || S.default_tool === "echo"), names = S.projects.map(p => p.name);
  const sig = tools.join("|") + "#" + names.join("|");
  if (sig === sigOpts) return;
  const first = !sigOpts; sigOpts = sig;
  const ts = q("#tool"), ps = q("#project"), tv = ts.value, pv = first ? (J.saved.get("jarviee.project") || "") : ps.value;
  ts.innerHTML = `<option value="">Auto</option>` + tools.map(t => `<option value="${esc(t)}">${esc(J.TOOL[t] || t)}</option>`).join("");
  ts.value = tools.includes(tv) ? tv : "";
  ps.innerHTML = `<option value="">None</option>` + names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  ps.value = names.includes(pv) ? pv : "";
  sync();
  if (!first) preview();
}
/* ---- "add a quiz Friday 3 pm" -> confirm, then add to Apple Calendar (typing alone never writes) ---- */
const CAL_ICON = `<svg class="i sm" aria-hidden="true" viewBox="0 0 16 16"><rect x="2" y="3" width="12" height="11" rx="1.6"/><path d="M2 6.5h12M5.5 1.8v2.4M10.5 1.8v2.4"/></svg>`;
async function calParse(text) {
  if (skipAdd === text) return null;
  try {
    const r = await J.api("/api/calendar/parse", {text});
    if (!r.matched) return null;
    const keep = addEv && addEv.text === text ? addEv.cal : "";
    return {text, ev: r.event, calendars: r.calendars || [], err: r.calendars_error || "", cal: r.event.calendar || keep || ""};
  } catch (e) { return null; }   // the normal task preview takes over
}
const whenText = ev => ev.all_day ? "All day" : `${ev.start_label} to ${ev.end_label}${ev.end_day_label ? " (" + ev.end_day_label + ")" : ""}`;
function renderAdd(a) {
  addEv = a;
  const ev = a.ev, btn = q("#launch");
  const pick = a.calendars.length
    ? `<select class="select" id="calpick" aria-label="Calendar"><option value="">Choose a calendar…</option>${a.calendars.map(c => `<option value="${esc(c)}"${c === a.cal ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>`
    : `<span class="err">${esc(a.err || "No calendars found")}</span>`;
  const warn = [];
  if (ev.calendar_hint && !a.cal) warn.push(`There is no calendar named "${ev.calendar_hint}". Pick one.`);
  else if (!a.cal && a.calendars.length) warn.push("Pick the calendar this goes in.");
  if (ev.note) warn.push(ev.note);
  const row = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  J.setHTML(q("#preview"), `<div class="decision"><span class="route">${CAL_ICON}<b>Add to calendar</b></span><span class="why">Nothing is written until you confirm.</span></div>
    <dl class="calprev">${row("Title", esc(ev.title))}${row("Calendar", pick)}${row("Day", esc(ev.day_label + ", " + ev.year))}${row("Time", esc(whenText(ev)))}${ev.location ? row("Place", esc(ev.location)) : ""}</dl>
    ${warn.map(w => `<div class="note warn">${esc(w)}</div>`).join("")}
    <div class="note">Not an event? <button class="btn sm" type="button" data-act="astask">Send to an agent instead</button></div>`);
  btn.querySelector("span").textContent = "Add to calendar";
  btn.disabled = !a.cal;
  a.shown = true;   /* this reading of exactly this text is on screen: only now may a second action write it */
}
function stopUndo() { clearInterval(undoTimer); undoTimer = null; }
async function addToCalendar(a) {
  if (!a.cal) { J.toast("Pick a calendar first", true); return; }
  const ev = {title: a.ev.title, calendar: a.cal, start: a.ev.start, end: a.ev.end, all_day: a.ev.all_day, location: a.ev.location};
  try {
    const r = await J.api("/api/calendar/add", {event: ev});
    if (!r.ok) { J.setHTML(q("#preview"), `<span class="err">Not added: ${esc(r.error || "the calendar said no")}</span>`); return; }
    q("#task").value = ""; autosize(); addEv = null; stopUndo();
    const what = `<b>${esc(ev.title)}</b> in ${esc(ev.calendar)}, ${esc(a.ev.day_label)}, ${esc(a.ev.all_day ? "all day" : whenText(a.ev))}`;
    const left = r.undo ? Math.min(30, r.undo_s || 30) : 0;
    J.setHTML(q("#preview"), `<div class="note">${CAL_ICON}${r.skipped ? `Already on your calendar: ${what}.` : `Added ${what}.`}
      ${r.undo ? `<button class="btn sm" type="button" id="undo" data-act="undo" data-undo="${esc(r.undo)}">Undo <span class="num" id="undo-s">${left}</span>s</button>` : ""}</div>
      ${r.undo_note ? `<div class="note warn">${esc(r.undo_note)}</div>` : ""}`);
    J.announce(`Added ${ev.title} to ${ev.calendar}`);
    if (r.undo) {
      let t = left;
      undoTimer = setInterval(() => {
        const b = q("#undo-s");
        if (!b) return stopUndo();
        if (--t <= 0) { stopUndo(); q("#undo")?.remove(); return; }
        b.textContent = t;
      }, 1000);
    }
  } catch (e) { J.setHTML(q("#preview"), `<span class="err">Not added: ${esc(e.message)}</span>`); }
}
async function undoAdd(token) {
  stopUndo();
  try {
    const r = await J.api("/api/calendar/undo", {undo: token});
    J.setHTML(q("#preview"), r.ok ? `<div class="note">${CAL_ICON}Removed it from your calendar.</div>` : `<span class="err">Not removed: ${esc(r.error || "")}</span>`);
    if (r.ok) J.announce("Event removed");
  } catch (e) { J.setHTML(q("#preview"), `<span class="err">${esc(e.message)}. Delete it in Calendar.</span>`); }
}
/* ---- slash commands: the list comes from GET /api/commands (hub/slash.py), so the bar never has its own copy ---- */
let cmds = null, cmdsLoad = null;
function loadCmds() {
  if (cmds) return Promise.resolve(cmds);
  cmdsLoad = cmdsLoad || J.api("/api/commands").then(r => (cmds = r.commands || [])).catch(() => { cmdsLoad = null; return []; });
  return cmdsLoad;
}
const slashRe = c => new RegExp("^\\/(?:" + [c.name, ...c.aliases].join("|") + ")(?![\\w-])", "i");
function slashParse(text) {   // {c, rest} when the text starts with a registered command, like slash.parse() on the server
  for (const c of cmds || []) { const m = slashRe(c).exec(text); if (m) return {c, rest: text.slice(m[0].length).replace(/^[\s:,-]+/, "").trim()}; }
  return null;
}
function cmdList(list, head) {
  const rows = list.map(c => `<button class="btn sm" type="button" data-act="slash" data-cmd="${esc(c.name)}" data-args="${c.args ? 1 : 0}">/${esc(c.name)}</button>
    <span><span class="muted">${esc(c.args)}</span> ${esc(c.summary)}${c.launches_agents ? ` <span class="chip">starts an agent</span>` : ""}${c.may_use_many_tokens ? ` <span class="chip ember">many tokens</span>` : ""}</span>`).join("");
  return `<div class="decision"><span class="route"><b>${esc(head)}</b></span><span class="why">click one to put it in the box</span></div>
    <div class="note" style="display:grid;grid-template-columns:auto 1fr;gap:6px 12px;align-items:baseline">${rows}</div>`;
}
const SLASH_NOTE = {
  help: () => "Lists every slash command. Nothing runs.",
  status: () => "Shows what is running, waiting for you, failed in the last hour and queued, per tool. No model call.",
  usage: () => "Shows the Claude Code and agy quota and token use from the last reading. No model call and no waiting for a new reading.",
  startwork: () => "Reads out your briefing for the day and sends it to the phone. Nothing launches.",
  endwork: () => "Reads out today's report. Nothing launches.",
  standup: rest => /^(|today|yesterday|\d{4}-\d{2}-\d{2})$/i.test(rest) ? `Shows what the agents did ${rest && !/^today$/i.test(rest) ? "on " + esc(rest) : "today"}, per project. No model call.`
    : `<span class="err">/standup takes a date as YYYY-MM-DD, or yesterday.</span>`,
  map: rest => {
    if (!rest) return "/map needs a project after it: /map &lt;project&gt;.";
    const ps = (J.S && J.S.projects) || [], p = ps.find(x => x.name.toLowerCase() === rest.toLowerCase());
    if (!p) { const near = ps.filter(x => x.name.toLowerCase().includes(rest.toLowerCase())).map(x => x.name).slice(0, 5); return `<span class="err">No project "${esc(rest)}" with a .brain/ folder.${near.length ? " Did you mean " + esc(near.join(", ")) + "?" : ""}</span>`; }
    return p.flow ? `Brings the ${esc(p.name)} map up to date: an agent reads what changed. The old map is kept if the new one is broken.`
      : `Starts an agent that reads the whole ${esc(p.name)} project and writes its map. <b>A first map can use many tokens.</b>`;
  },
};
/* the preview HTML for slash text, or null when the normal router preview should take over */
async function slashPreview(text) {
  await loadCmds();
  const one = /^\/[\w-]*$/.test(text), hit = slashParse(text);
  if (one && !hit) {
    const low = text.slice(1).toLowerCase(), list = (cmds || []).filter(c => [c.name, ...c.aliases].some(n => n.startsWith(low)));
    return list.length ? cmdList(list, low ? "Commands starting with /" + low : "Slash commands") : null;
  }
  const note = hit && SLASH_NOTE[hit.c.name];
  return note ? `<span class="muted">${note(hit.rest)}</span>` : null;
}
function preview() { clearTimeout(tPrev); tPrev = setTimeout(doPreview, 220); }
async function doPreview() {
  const text = q("#task").value.trim(), btn = q("#launch");
  btn.disabled = !text;
  btn.querySelector("span").textContent = planOn ? "Make plan" : "Launch";
  if (draft) return;
  const pv = q("#preview");
  if (!text) { addEv = null; J.setHTML(pv, `<span class="muted">${planOn ? "Describe a bigger goal. Nothing runs until you approve the plan." : "Type a task to see which tool and model it goes to."}</span>`); return; }
  if (planOn) {
    const who = q("#planner").value === "mock" ? "The offline splitter" : "Claude (sonnet, read-only)";
    J.setHTML(pv, `<span class="muted">${who} splits this into subtasks${q("#project").value ? ` using ${esc(q("#project").value)}/.brain/` : ""}. You approve the plan before anything runs.</span>`);
    return;
  }
  if (/^\/orchestrate(?![\w-])/i.test(text)) { J.setHTML(pv, `<span class="muted">${/^\/orchestrate[\s:,-]*\S/i.test(text) ? "Opus leads in window 1 of a new 4×2 iTerm2 tab and gives the task to up to 7 workers, each on the tool and model its job needs. It checks every result and keeps the windows busy." : "/orchestrate needs a task after it."}</span>`); return; }
  const n = ++seq;
  if (text[0] === "/") { const html = await slashPreview(text); if (n !== seq) return; if (html) { J.setHTML(pv, html); return; } }
  const c = await calParse(text);
  if (n !== seq) return;
  if (c) { renderAdd(c); return; }   // reads as an add-event request: no router preview, nothing launches
  addEv = null;
  try {
    const p = await J.api("/api/classify", {task: withShots(text), tool: q("#tool").value, tier: tierSel, project: q("#project").value, ...(single ? {single: true} : {})});
    if (n !== seq) return;
    const over = J.RANK[p.tier] > J.RANK[p.suggested_tier];
    const why = p.reasons.filter(r => !/^manual|^fallback/.test(r)).map(reasonText).join(", ");
    const route = `<span class="route">${J.tierPill(p.tier, p.suggested_tier, p.reasons, p.effort)}<svg class="i sm" aria-hidden="true"><use href="#i-arrow"/></svg>`
      + `<b>${esc(J.TOOL[p.tool] || p.tool)}</b><span class="num">${esc(p.model)}</span>${p.effort ? `<span class="muted">effort ${esc(p.effort)}</span>` : ""}</span>`;
    const lines = [`<div class="decision">${route}${why ? `<span class="why">${esc(why)}</span>` : ""}</div>`];
    if (p.pipeline || single) lines.push(`<div class="note"><svg class="i sm" aria-hidden="true"><use href="#i-split"/></svg>${p.pipeline ? "pipeline: " + esc((p.phases && p.phases.length ? p.phases : ["research", "plan", "build", "test", "review"]).join(" → ")) : "single agent, no pipeline"}
      <button class="btn sm" type="button" data-act="single" aria-pressed="${single}" title="Skip the pipeline and run one agent">Single agent</button></div>`);
    if (p.fallback) lines.push(`<div class="note ${p.waiting_for_quota ? "bad" : "re"}"><svg class="i sm" aria-hidden="true"><use href="#i-route"/></svg>${esc(p.waiting_for_quota ? "Waits for quota: " + p.fallback : "Rerouted: " + p.fallback)}</div>`);
    if (over) lines.push(`<div class="note warn">A ${esc(p.tier)} model on a task the router rates ${esc(p.suggested_tier)}.</div>`);
    if (!q("#tool").value && !tierSel && p.borderline && p.tool === "agy") lines.push(`<div class="note">Borderline: agy first, Claude if it fails or gets stuck.</div>`);
    if (p.suggested_tier === "heavy" || looksLikeSeveral(text))
      lines.push(`<div class="note">${p.suggested_tier === "heavy" ? "Heavy task." : "This reads like several tasks."} Splitting it may be cheaper. <button class="btn sm" type="button" data-act="useplan">Plan it instead</button></div>`);
    J.setHTML(pv, lines.join(""));
    btn.querySelector("span").textContent = over ? `Launch ${p.tier}` : "Launch";
  } catch (e) { if (n === seq) J.setHTML(pv, `<span class="err">${esc(e.message)}</span>`); }
}
async function launch(opts = {}) {
  const text = q("#task").value.trim();
  if (!text || launching) return;   // not gated on the button: it stays disabled until the debounced preview runs, which swallowed fast Cmd+Enter
  micStop(true); micNote("");
  clearTimeout(tPrev); seq++;   // a preview still pending from typing would land after a fast answer and wipe it
  if (planOn) { launching = true; try { await makePlan(text); } finally { launching = false; } return; }
  launching = true; q("#launch").disabled = true;
  let answered = false, sl = null;
  if (text[0] === "/" && !opts.asTask) {   // a slash command is never an add-event or a schedule question
    await loadCmds(); sl = slashParse(text);
    if (!sl && /^\/[\w-]*$/.test(text)) {   // "/" or a half-typed name: show the commands, launch nothing
      const html = await slashPreview(text);
      J.setHTML(q("#preview"), html || `<span class="err">Unknown command ${esc(text)}.</span>` + cmdList(cmds || [], "Slash commands"));
      launching = false; q("#launch").disabled = false; return;
    }
  }
  if (!opts.asTask && !sl) {   // "add a quiz Friday 3 pm": decide from a fresh reading, not a preview that may be stale
    const seen = !!(addEv && addEv.shown && addEv.text === text);   /* the owner has seen the preview of exactly this text */
    const a = seen ? addEv : await calParse(text);
    if (a) { try { renderAdd(a); if (seen && a.cal) await addToCalendar(a); else if (!seen) J.toast("Check the event, then press Add to calendar", false); } finally { launching = false; q("#launch").disabled = !(q("#task").value.trim() && (!addEv || addEv.cal)); } return; }
  }
  const cwd = !q("#project").value && q("#cwd").value.trim();
  try {
    const a = sl ? {handled: false} : await J.api("/api/ask", {question: text});   // "what's on my schedule today?" is answered from Apple Calendar, not launched as an agent
    if (a.handled) {
      q("#task").value = ""; autosize();
      const al = (a.alerts || []).map(t => `<div class="note warn" style="font-weight:700;display:flex;gap:8px;align-items:center"><svg class="i sm" aria-hidden="true" style="color:var(--ember)"><use href="#i-bell"/></svg>${esc(t)}</div>`).join("");
      J.setHTML(q("#preview"), al + `<div class="note${a.error ? " warn" : ""}" style="white-space:pre-line">${esc(a.schedule || a.answer)}</div>`);
      J.announce(a.answer); answered = true; return;
    }
    const r = await J.api("/api/run", {task: text, tool: q("#tool").value, tier: tierSel, project: q("#project").value, ...(cwd ? {cwd} : {}), ...(single ? {single: true} : {})});
    q("#task").value = ""; shots = []; paintShots(); autosize(); setTier(""); q("#cmd").classList.remove("open"); single = false;
    if (r.slash) { J.setHTML(q("#preview"), slashHTML(r)); J.announce(String(r.text).split("\n")[0]); if (r.launched) { J.toast(r.mode === "update" ? "Map update started" : "Mapping started"); J.poll(); } answered = true; return; }
    if (r.startwork) { J.setHTML(q("#preview"), briefHTML(r)); J.announce(r.text); answered = true; return; }
    if (r.endwork) { J.setHTML(q("#preview"), `<div class="note" style="white-space:pre-line">${esc(r.text)}</div>`); J.announce(r.text); answered = true; return; }
    if (r.orchestrate) { J.toast("Orchestrator started: Opus in window 1"); J.announce("Orchestrator started"); }
    else if (r.pipeline) { J.toast("Pipeline started"); J.announce("Pipeline started"); location.hash = "#/workflows/" + encodeURIComponent(r.plan_id || r.id); }
    else { J.toast(`Launched on ${J.TOOL[r.tool] || r.tool} · ${J.modelLabel(r.model)}`); J.announce("Launched"); }
    J.poll();
  } catch (e) { J.setHTML(q("#preview"), `<span class="err">${sl ? "" : "Not launched: "}${esc(e.message)}</span>`); q("#launch").disabled = false; }
  finally { launching = false; if (!answered) preview(); }
}
/* ---- the answers of /help, /status, /standup, /usage, /map ---- */
function slashHTML(r) {
  if (r.slash === "help" && r.commands) return cmdList(r.commands, "Slash commands");
  return `<div class="note${r.launched === false ? " warn" : ""}" style="white-space:pre-wrap">${esc(r.text)}</div>`
    + (r.warning ? `<div class="note warn">${esc(r.warning)}</div>` : "");
}
/* ---- /startwork: one block per source, its icon and label, then its lines (or why it isn't connected) ---- */
function briefHTML(r) {
  const blocks = (r.order || []).map(k => {
    const s = r.sections[k] || {}, ls = (r.lines || {})[k] || [];
    const cls = s.ok ? "note" : "note warn";
    return `<div class="${cls}"><div style="font-weight:700">${esc((r.icons || {})[k] || "")} ${esc((r.labels || {})[k] || k)}</div>`
      + ls.map(l => `<div>${esc(l)}</div>`).join("") + `</div>`;
  }).join("");
  return `<div class="note" style="white-space:pre-line">${esc(r.text)}</div>` + blocks;
}
/* ---- voice dictation (Web Speech API): fills the box, never launches. The user reviews, then presses Launch. ---- */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const MIC_OFF = !SR ? "Dictation isn't available in this browser. Use Chrome, Edge or Safari."
  : !window.isSecureContext ? "Dictation needs a secure page (https:// or localhost). This page is not one."
  : "";
const MIC_IDLE = "Dictate: hold to talk, or click to start and click again to stop. Chrome and Edge send the audio to their speech service.";
let rec = null, micState = "idle", micBase = "", micHeld = 0, micPress = false, micErr = "";   // idle | starting | listening
function micNote(text, kind) { const n = q("#mic-note"); n.hidden = !text; n.className = "mic-note" + (kind ? " " + kind : ""); n.textContent = text || ""; }
function micPaint() {
  const b = q("#mic"), on = micState !== "idle";
  b.classList.toggle("listening", micState === "listening"); b.classList.toggle("starting", micState === "starting");
  b.setAttribute("aria-pressed", String(on));
  b.setAttribute("aria-label", on ? "Stop dictation" : "Start dictation");
  b.title = on ? "Stop dictation (release or click)" : micErr || (localStt ? `Dictate (${sttEngine === "handy" ? "Handy" : "local Whisper"}, offline): hold to talk, or click to start and click again to stop.${handyKeys ? " Or hold Control+Option anywhere." : ""}` : MIC_IDLE);
  q("#cmd").classList.toggle("dictating", on);
}
function micStop(abort) {
  if (localStt) {
    if (abort) { if (mr) { mr.onstop = null; try { mr.stop(); } catch (_) {} mstream && mstream.getTracks().forEach(x => x.stop()); mr = null; } micState = "idle"; micPress = false; micNote(""); micPaint(); }
    else if (mr) { try { mr.stop(); } catch (_) {} }
    else if (micState === "starting") { micState = "idle"; micNote(""); micPaint(); }
    return;
  }
  if (!rec) return;
  if (!abort) { try { rec.stop(); } catch (_) {} return; }
  const r = rec; rec = null; micState = "idle"; micPress = false; r.onresult = r.onerror = r.onend = null;   // drop it silently (launch, Escape)
  try { r.abort(); } catch (_) {}
  micNote(""); micPaint();
}
/* local path: record with MediaRecorder, transcribe with whisper.cpp on this Mac (far more accurate than the browser's service) */
let localStt = false, sttEngine = "", handyKeys = "", mr = null, mchunks = [], mstream = null;
const TOKEN = () => window.JARVIEE_TOKEN || (document.querySelector('meta[name="jarviee-token"]') || {}).content || "";
async function micStartLocal() {
  const t = q("#task"); micBase = t.value && !/\s$/.test(t.value) ? t.value + " " : t.value;
  micState = "starting"; micNote("Waiting for the microphone… allow access if the browser asks.", "live"); micPaint();
  try { mstream = await navigator.mediaDevices.getUserMedia({audio: true}); }
  catch (e) { micState = "idle"; micFail("Microphone access is blocked. Allow it for this site in the browser's settings, then try again.", true); return; }
  if (micState === "idle") { mstream.getTracks().forEach(x => x.stop()); return; }   // cancelled while the prompt was open
  mchunks = []; mr = new MediaRecorder(mstream);
  mr.ondataavailable = e => { if (e.data.size) mchunks.push(e.data); };
  mr.onstop = async () => {
    mstream.getTracks().forEach(x => x.stop()); const type = mr.mimeType || "audio/webm", chunks = mchunks; mr = null;
    micState = "idle"; micPress = false; micPaint();
    if (!chunks.length) { micNote("Didn't catch anything. Try again, or type the task.", "warn"); return; }
    micNote("Transcribing on this Mac…", "live");
    try {
      const r = await fetch("/api/stt", {method: "POST", headers: {"Content-Type": type, "X-Jarviee-Token": TOKEN()}, body: new Blob(chunks, {type})});
      const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status);
      const text = (j.text || "").replace(/\[BLANK_AUDIO\]/g, "").trim();
      if (!text) { micNote("Didn't catch anything. Try again, or type the task.", "warn"); return; }
      t.value = micBase + text; autosize(); open(); t.focus(); preview();
      micNote("Heard that. Check the text, fix anything wrong, then press Launch.", "ok"); J.announce("Dictation ready. Review the text, then press Launch.");
    } catch (e) { micFail("Transcription failed: " + e.message); }
  };
  mr.start(); micState = "listening"; micErr = "";
  micNote("Recording… speak, then release or click the mic to stop.", "live"); micPaint(); J.announce("Listening");
}
function micStart() {
  if (micState !== "idle") return;
  if (localStt) { micStartLocal(); return; }
  const t = q("#task"); micBase = t.value && !/\s$/.test(t.value) ? t.value + " " : t.value;
  try {
    rec = new SR(); rec.continuous = true; rec.interimResults = true; rec.maxAlternatives = 1; rec.lang = navigator.language || "en-US";
  } catch (e) { micFail("Dictation could not start: " + e.message); return; }
  const mine = rec; let heard = false, err = "";
  rec.onstart = () => { micState = "listening"; micErr = ""; micNote("Listening… speak now. Release or click the mic to stop.", "live"); micPaint(); J.announce("Listening"); };
  rec.onresult = e => {
    let fin = "", interim = "";
    for (let i = 0; i < e.results.length; i++) { const r = e.results[i]; (r.isFinal ? (fin += r[0].transcript) : (interim += r[0].transcript)); }
    heard = !!(fin + interim).trim(); t.value = micBase + (fin + interim).replace(/^\s+/, ""); autosize(); t.scrollTop = t.scrollHeight; open();
  };
  rec.onerror = e => { err = e.error; };
  rec.onend = () => {
    if (rec !== mine) return;
    rec = null; micState = "idle"; micPress = false;
    t.value = t.value.replace(/\s+$/, "");
    if (err === "service-not-allowed") micFail("The browser's speech service is turned off. In Safari, enable Dictation (System Settings → Keyboard → Dictation) and Siri, or open Jarviee in Chrome.", true);
    else if (err === "not-allowed") micFail("Microphone access is blocked. Allow it for this site in the browser's address bar or settings, then try again.", true);
    else if (err === "audio-capture") micFail("No microphone was found. Connect one and try again.");
    else if (err === "network") micFail("The browser's speech service could not be reached. Check your connection and try again.");
    else if (err && err !== "aborted" && err !== "no-speech") micFail("Dictation stopped: " + err + ".");
    else if (!heard || !t.value.trim()) { micNote("Didn't catch anything. Try again, or type the task.", "warn"); micErr = ""; }
    else { micNote("Heard that. Check the text, fix anything wrong, then press Launch.", "ok"); micErr = ""; J.announce("Dictation ready. Review the text, then press Launch."); }
    micPaint(); t.focus(); preview();
  };
  micState = "starting"; micNote("Waiting for the microphone… allow access if the browser asks.", "live"); micPaint();
  try { rec.start(); } catch (e) { rec = null; micState = "idle"; micFail("Dictation could not start: " + e.message); }
}
function micFail(msg, denied) { micErr = denied ? msg : ""; micNote(msg, "bad"); micPaint(); if (denied) J.toast("Microphone access denied", true); }
async function micInit() {
  const b = q("#mic");
  try {
    const j = await (await fetch("/api/stt/status")).json();
    localStt = !!(j.local && window.MediaRecorder && navigator.mediaDevices);
    sttEngine = j.engine || ""; handyKeys = j.handy_installed && /^ctrl\+option$|^option\+ctrl$/.test(j.handy_shortcut || "") ? j.handy_shortcut : "";
  } catch (_) {}
  pttInit();
  if (MIC_OFF && !localStt) { b.setAttribute("aria-disabled", "true"); b.title = MIC_OFF; b.setAttribute("aria-label", "Dictation unavailable. " + MIC_OFF); b.classList.add("off"); return; }
  if (localStt) b.classList.remove("off");
  micPaint();
  /* pointer: press starts, a long press is push-to-talk (release stops), a short press toggles on the next click */
  b.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    if (micState !== "idle") { micStop(); micPress = false; return; }
    micPress = true; micHeld = performance.now(); try { b.setPointerCapture(e.pointerId); } catch (_) {}
    micStart();
  });
  const release = () => { if (micPress && performance.now() - micHeld > 450) micStop(); micPress = false; };
  b.addEventListener("pointerup", release); b.addEventListener("pointercancel", release);
  b.addEventListener("click", e => { if (e.detail === 0) { micState === "idle" ? micStart() : micStop(); } });   // keyboard Enter or Space toggles
  b.addEventListener("contextmenu", e => e.preventDefault());
}

/* Hold Control+Option to talk, release to transcribe. With Handy installed and bound to Control+Option, Handy
   records system-wide and types the text where the cursor is: Jarviee only puts the cursor in the command bar
   first. Without Handy, the same keys drive Jarviee's own mic. A held combo that gains another key is a shortcut,
   not dictation, and is ignored. */
let ptt = 0, pttT = null;
function pttInit() {
  const combo = e => e.ctrlKey && e.altKey && !e.metaKey && !e.shiftKey && (e.key === "Control" || e.key === "Alt");
  document.addEventListener("keydown", e => {
    if (!combo(e)) { if (ptt && e.key !== "Control" && e.key !== "Alt") { ptt = 0; if (!handyKeys && micState !== "idle") micStop(true); micNote(""); } return; }
    if (ptt || e.repeat) return;
    ptt = Date.now();
    if (handyKeys) {
      if (J.shell.current() !== "command") J.newTask(); else { open(); q("#task").focus(); }   // the cursor must be in the bar before Handy types
      micBase = q("#task") ? q("#task").value : "";
      micNote("Listening with Handy… release Control+Option to transcribe.", "live");
      q("#cmd")?.classList.add("dictating");
    } else if (localStt || !MIC_OFF) {
      if (J.shell.current() !== "command") J.newTask();
      if (micState === "idle") micStart();
    }
  }, true);
  const up = e => {
    if (!ptt || (e.key !== "Control" && e.key !== "Alt")) return;
    const held = Date.now() - ptt; ptt = 0;
    if (handyKeys) {
      q("#cmd")?.classList.remove("dictating");
      if (held < 300) { micNote(""); return; }   // a tap: Handy treats it as a toggle; Handy shows its own overlay
      micNote("Transcribing with Handy…", "live");
      const before = micBase; clearTimeout(pttT);
      const until = Date.now() + 15000;
      const watch = () => { const t = q("#task"); if (t && t.value !== before) { micNote("Heard that. Check the text, then press Launch.", "ok"); pttT = setTimeout(() => micNote(""), 4000); preview(); return; }
        if (Date.now() < until) pttT = setTimeout(watch, 250); else micNote("Nothing came through. Is Handy running with Microphone and Accessibility allowed?", "warn"); };
      watch();
    } else if (micState !== "idle") micStop();
  };
  document.addEventListener("keyup", up, true);
  window.addEventListener("blur", () => { if (ptt && !handyKeys && micState !== "idle") micStop(); ptt = 0; });
}

let tpls = [];
function tplPaint(sel) {
  const s = q("#tpl"); s.innerHTML = `<option value="">Templates</option>` + tpls.map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
  s.value = sel && tpls.some(t => t.name === sel) ? sel : ""; q("#tpl-del").hidden = !s.value;
}
async function tplLoad() { try { tpls = (await J.api("/api/templates")).templates || []; tplPaint(); } catch (e) { /* picker stays empty */ } }
function tplInit() {
  q("#tpl").addEventListener("change", e => {
    const t = tpls.find(x => x.name === e.target.value); q("#tpl-del").hidden = !t; if (!t) return;
    const el = q("#task"), body = t.body.replace(/\{project\}/g, q("#project").value || "{project}");
    el.value = el.value.trim() ? el.value.replace(/\s*$/, "\n\n") + body : body;
    autosize(); open(); preview(); el.focus();
    const m = /\{file\}/.exec(el.value); if (m) el.setSelectionRange(m.index, m.index + 6);   // jump to the next blank
  });
  q("#tpl-save").addEventListener("click", async () => {
    const body = q("#task").value.trim(); if (!body) { J.toast("Type a prompt first, then save it", true); return; }
    const name = (window.prompt("Template name (use {project} and {file} as variables)", q("#tpl").value || "") || "").trim(); if (!name) return;
    try { tpls = (await J.api("/api/templates", {name, body})).templates; tplPaint(name); J.toast("Template saved"); } catch (e) { J.toast("Not saved: " + e.message, true); }
  });
  q("#tpl-del").addEventListener("click", async () => {
    const name = q("#tpl").value; if (!name || !window.confirm(`Delete template "${name}"?`)) return;
    try { tpls = (await J.api("/api/templates/delete", {name})).templates; tplPaint(); } catch (e) { J.toast("Not deleted: " + e.message, true); }
  });
  tplLoad();
}

function planTable(p) {
  const rows = p.tasks.map(t => `<tr><td class="num">${esc(short(t.id))}</td><td>${esc(t.title)}</td>
      <td>${J.tierPill(t.level, t.router_level || t.level, [])}</td><td>${esc(J.TOOL_SHORT[t.tool] || t.tool)} <span class="num muted">${esc(t.model)}</span></td>
      <td class="num">${esc((t.depends_on || []).map(short).join(", ") || "–")}</td></tr>`).join("");
  const notes = (p.notes || []).concat(p.tasks.filter(t => t.route_note).map(t => `${short(t.id)}: ${t.route_note}`));
  return `<div class="plan-inline"><table class="tbl"><thead><tr><th>ID</th><th>Task</th><th>Level</th><th>Runs on</th><th>After</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="foot"><span class="muted">${notes.length ? notes.map(esc).join("<br>") : `${p.tasks.length} subtasks from ${esc(p.planner || "the planner")}. Nothing runs until you approve.`}</span>
    <button class="btn" type="button" data-draft="discard">Discard</button><button class="btn primary" type="button" data-draft="approve">Approve and queue</button></div></div>`;
}
async function makePlan(goal) {
  q("#launch").disabled = true;
  J.setHTML(q("#preview"), `<span class="muted">${q("#planner").value === "mock" ? "Splitting…" : "Planning with Claude (sonnet, read-only). This can take a minute."}</span>`);
  try {
    draft = await J.api("/api/plan", {goal: withShots(goal), project: q("#project").value, planner: q("#planner").value});
    J.setHTML(q("#preview"), planTable(draft));
    J.announce(`Plan ready with ${draft.tasks.length} subtasks. Approve to queue them.`);
    q("#preview [data-draft='approve']")?.focus();
    J.poll();
  } catch (e) { J.setHTML(q("#preview"), `<span class="err">Not planned: ${esc(e.message)}</span>`); }
  q("#launch").disabled = !q("#task").value.trim();
}

J.composer = {
  mount(el) {
    root = el;
    el.innerHTML = HTML;
    const task = q("#task");
    task.addEventListener("focus", open);
    task.addEventListener("input", () => { if (micState !== "idle") micStop(); micNote(""); autosize(); if (draft) draft = null; preview(); });
    task.addEventListener("keydown", e => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); launch(); }
      if (e.key === "Escape" && micState !== "idle") { e.preventDefault(); micStop(true); return; }
      if (e.key === "Escape" && !task.value) { task.blur(); q("#cmd").classList.remove("open"); }
    });
    task.addEventListener("paste", e => { const f = [...(e.clipboardData?.files || [])]; if (f.some(x => x.type.startsWith("image/"))) { e.preventDefault(); attach(f); } });
    q("#cmd").addEventListener("dragover", e => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); });
    q("#cmd").addEventListener("drop", e => { if (e.dataTransfer?.files.length) { e.preventDefault(); attach([...e.dataTransfer.files]); } });
    q("#thumbs").addEventListener("click", e => { const b = e.target.closest("[data-rm]"); if (b) { shots.splice(+b.dataset.rm, 1); paintShots(); } });
    q("#cmd").addEventListener("submit", e => { e.preventDefault(); launch(); });
    q("#tier").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { setTier(b.dataset.tier); preview(); } });
    q("#tool").addEventListener("change", preview);
    q("#project").addEventListener("change", () => { J.saved.set("jarviee.project", q("#project").value); sync(); preview(); });
    q("#planner").addEventListener("change", preview);
    q("#plantoggle").addEventListener("click", () => setPlan(!planOn));
    q("#preview").addEventListener("change", e => {
      if (e.target.id !== "calpick" || !addEv) return;
      addEv.cal = e.target.value; renderAdd(addEv);
    });
    q("#preview").addEventListener("click", async e => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.act === "astask") { skipAdd = q("#task").value.trim(); addEv = null; preview(); return; }
      if (b.dataset.act === "undo") { b.disabled = true; undoAdd(b.dataset.undo); return; }
      if (b.dataset.act === "slash") { const t = q("#task"); t.value = "/" + b.dataset.cmd + (b.dataset.args === "1" ? " " : ""); autosize(); t.focus(); preview(); return; }
      if (b.dataset.act === "single") { single = !single; preview(); return; }
      if (b.dataset.act === "useplan") { setPlan(true); task.focus(); return; }
      if (!draft) return;
      const id = draft.id; b.disabled = true;
      try {
        if (b.dataset.draft === "approve") { await J.api("/api/plan/approve", {id}); J.toast(`Queued ${draft.tasks.length} tasks`); draft = null; task.value = ""; autosize(); setPlan(false); }
        else if (b.dataset.draft === "discard") { await J.api("/api/plan/discard", {id}); draft = null; preview(); }
        J.poll();
      } catch (err) { b.disabled = false; J.toast(err.message, true); }
    });
    micInit(); tplInit();
    doPreview();
  },
  render: options,
  focus() { if (!root) return; open(); q("#task").focus(); q("#cmd").scrollIntoView({block: "nearest"}); },
  plan() { if (!root) return; setPlan(true); this.focus(); },
  setText(text) { if (!root) return; const t = q("#task"); t.value = text; t.dispatchEvent(new Event("input", {bubbles: true})); this.focus(); },
  setProject(name) { if (!root) return; q("#project").value = name || ""; J.saved.set("jarviee.project", name || ""); sync(); this.focus(); preview(); },
  /* a brain TODO's Run button: prefill and launch on Auto routing */
  async runTask(project, text) {
    if (!root || launching) return;
    const clean = String(text || "").replace(/\[[^\]]*\]|\([^)]*priority[^)]*\)/gi, "").trim();
    if (!clean) return;
    planOn = false; draft = null; sync();
    q("#tool").value = ""; setTier(""); q("#project").value = project || ""; sync();
    q("#task").value = clean; autosize(); open();
    q("#launch").disabled = false;
    await launch({asTask: true});
  },
};
})();
