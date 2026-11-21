/* review.js: the review inbox. Finished runs that changed files wait here until the owner decides:
   accept, revert (only when the server proves it safe), run the project's own checks, or open the agent. */
"use strict";
(() => {
const {esc} = J;
let el = null, items = [], loaded = false, selId = null, confirmId = null, busy = null;
let diffs = new Map(), lastSig = "", lastFetch = 0, fetching = false;
const q = s => el.querySelector(s);
const MAX_DIFF_LINES = 2500;

/* ---------- data ---------- */
async function load(force) {
  if (fetching || (!force && Date.now() - lastFetch < 1500)) return;
  fetching = true; lastFetch = Date.now();
  try {
    const {data, changed} = await J.fresh("/api/inbox" + (selId ? "?id=" + encodeURIComponent(selId) : ""));
    if (changed || !loaded) { items = data.items || []; loaded = true; for (const k of diffs.keys()) if (!items.some(i => i.id === k)) diffs.delete(k); J.emit("inbox", items); }
  } catch (e) { /* older server, or offline: the inbox stays as it was */ }
  fetching = false;
  paintCount(); if (el && !el.hidden) J.pages.review.render();
}
function paintCount() {
  const n = loaded ? items.length : 0, c = document.querySelector("#c-review");
  if (c && c.textContent !== (n ? String(n) : "")) c.textContent = n ? String(n) : "";
}
/* a change in the runs that could be in the inbox (from the 2 s state poll) refetches it at once */
function signature() {
  return (J.S?.agents || []).filter(a => a.source === "launcher" && (a.status === "done" || a.status === "failed") && (a.files || []).length)
    .map(a => `${a.id}:${a.status}:${a.reviewed || 0}:${a.check_status || ""}`).join("|");
}
J.review = {count: () => (loaded ? items.length : 0)};

/* ---------- list ---------- */
function reviewWord(it) { return it.status === "failed" ? "failed" : "done"; }
function row(it) {
  const k = it.status === "failed" ? "fail" : "done";
  const chk = it.check.status === "pass" ? ` · <span class="ink-green">checks passed</span>` : it.check.status === "fail" ? ` · <span class="ink-red">checks failed</span>` : "";
  return `${J.toolMono(it.tool)}
    <span class="rv-main"><span class="rv-t">${esc(it.title)}</span>
      <span class="rv-s">${esc(it.project || "no project")} · ${it.files.length} file${it.files.length === 1 ? "" : "s"}${chk}</span></span>
    <span class="rv-st">${J.st(k, reviewWord(it))}</span>
    <span class="rv-d num"><b class="da">+${it.added}</b> <b class="dd">−${it.removed}</b></span>
    <span class="rv-ago num faint">${it.ended ? esc(J.ago(it.ended)) : ""}</span>`;
}

/* ---------- detail ---------- */
function diffHtml(text) {
  const lines = String(text).split("\n");
  const cut = lines.length > MAX_DIFF_LINES;
  const blocks = []; let cur = null;
  for (const l of lines.slice(0, MAX_DIFF_LINES)) {
    if (l.startsWith("diff --git ")) { cur = {head: l, lines: []}; blocks.push(cur); continue; }
    if (cur) cur.lines.push(l);
  }
  if (!blocks.length) return `<div class="empty">${esc(String(text).trim() || "(no changes recorded for this run)")}</div>`;
  const span = l => {
    const c = /^(\+\+\+|---|index |new file|deleted file|similarity|rename |old mode|new mode)/.test(l) ? "dh" : l[0] === "+" ? "da" : l[0] === "-" ? "dd" : l.startsWith("@@") ? "dk" : "";
    return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
  };
  return blocks.map(b => {
    const m = b.head.match(/ b\/(.+)$/);
    return `<div class="rv-file" data-path="${esc(m ? m[1] : "")}"><pre class="log diff" tabindex="0" aria-label="Diff of ${esc(m ? m[1] : "a file")}"><span class="dh">${esc(b.head)}</span>\n${b.lines.map(span).join("\n")}</pre></div>`;
  }).join("") + (cut ? `<div class="empty">The diff is long; the first ${MAX_DIFF_LINES} lines are shown.</div>` : "");
}
async function loadDiff(it, force) {
  const hit = diffs.get(it.id);
  if (hit && !force && Date.now() - hit.at < 30000) return;
  if (hit?.pending) return;
  diffs.set(it.id, {...(hit || {}), pending: true, at: Date.now()});
  let t;
  try { t = await J.api("/api/diff?id=" + encodeURIComponent(it.id)); } catch (e) { t = "Could not load the diff: " + e.message; }
  diffs.set(it.id, {html: diffHtml(t), at: Date.now()});
  if (selId === it.id) { const box = q("#rv-diff"); if (box) J.setHTML(box, diffs.get(it.id).html); }
}
function filesHtml(it) {
  return `<ul class="rv-files" aria-label="Changed files">${it.files.map(f => `<li><button type="button" class="rv-fbtn" data-file="${esc(f.path)}" data-key="f:${esc(f.path)}" title="Jump to ${esc(f.path)} in the diff">
    <span class="rv-fp num"><bdi dir="ltr">${esc(f.path)}</bdi></span><span class="rv-fd num">${f.binary ? "binary" : `<b class="da">+${f.added}</b> <b class="dd">−${f.removed}</b>`}</span></button></li>`).join("")}</ul>`;
}
function checkHtml(it) {
  const c = it.check;
  if (!c.configured) return `<div class="rv-note" role="note">${J.sd("idle")}<span>No test command is configured for <b>${esc(it.project || "this project")}</b>, so checks can't run. Add one line to <code>.brain/CHECK_COMMAND</code> (or <code>checks.${esc(it.project || "project")}</code> in the Jarviee config) to turn them on.</span></div>`;
  const run = busy === "check:" + it.id || c.status === "running";
  const st = run ? J.st("run", "checking") : c.status === "pass" ? J.st("done", "checks passed") : c.status === "fail" ? J.st("fail", "checks failed") : `<span class="muted">Not run yet</span>`;
  const warn = c.trusted === false ? `<div class="rv-note" role="note"><span>This command comes from <code>.brain/CHECK_COMMAND</code>, a file any run can write. It has not run. Read it, then press <b>Trust and run</b> to confirm exactly this command for ${esc(it.project || "this project")}.</span></div>` : "";
  return `${warn}<div class="rv-check"><div class="rv-check-h">${st}<span class="num faint" title="The command Jarviee runs in the project folder">${esc(c.command)}</span></div>
    ${c.output ? `<details class="rv-out"><summary>Output</summary><pre class="log" tabindex="0">${esc(c.output)}</pre></details>` : ""}</div>`;
}
function actionsHtml(it) {
  const rev = it.revert, confirming = confirmId === it.id, b = busy && busy.endsWith(":" + it.id) ? busy.split(":")[0] : "";
  const dis = b ? " disabled" : "";
  let revert;
  if (rev.ok) revert = confirming
    ? `<button class="btn sm danger" type="button" data-do="revert" data-key="revert"${dis}>Confirm revert</button><button class="btn sm ghost" type="button" data-do="cancel" data-key="cancel">Cancel</button>`
    : `<button class="btn sm" type="button" data-do="ask-revert" data-key="revert"${dis}>Revert</button>`;
  else revert = "";
  const run = it.check.configured ? `<button class="btn sm" type="button" data-do="check" data-key="check"${dis}>${b === "check" ? "Running checks" : it.check.trusted === false ? "Trust and run" : "Run checks"}</button>` : "";
  return `<div class="rv-acts" data-id="${esc(it.id)}">
      <button class="btn sm primary" type="button" data-do="accept" data-key="accept"${dis}><svg class="i sm" aria-hidden="true"><use href="#i-check"/></svg>Accept</button>
      ${revert}${run}
      <a class="btn sm" href="#/agents/${encodeURIComponent(it.id)}" data-key="open" title="Open the agent: reply, run again, log, trace"><svg class="i sm" aria-hidden="true"><use href="#i-agents"/></svg>Open agent</a>
      <span class="spacer"></span>
      <button class="btn sm ghost" type="button" data-do="dismiss" data-key="dismiss"${dis} title="Nothing to judge here">Dismiss</button>
    </div>
    ${rev.ok ? "" : `<div class="rv-note" role="note">${J.sd("stopped")}<span><b>Can't revert.</b> ${esc(rev.reason.replace(/^Cannot revert:\s*/, "").replace(/^Nothing to revert:\s*/, "Nothing to revert: "))}</span></div>`}
    ${confirming ? `<div class="rv-note warn" role="note">${J.sd("need")}<span>This adds one new commit that undoes <b class="num">${esc(it.commits[0]?.short || "")}</b>. Nothing is reset or deleted.</span></div>` : ""}`;
}
function detailHtml(it) {
  const when = it.ended ? new Date(it.ended * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}) : "";
  return `<div class="ad-head">${J.toolMono(it.tool)}<div class="ad-t"><h2>${esc(it.title)}</h2>
        <div class="ad-m">${J.st(it.status === "failed" ? "fail" : "done", reviewWord(it))}<span class="muted">${esc(it.project || "no project")}</span></div></div>
      <a class="btn icon sm ghost rv-close" href="#/review" aria-label="Back to the list"><svg class="i sm" aria-hidden="true"><use href="#i-close"/></svg></a></div>
    <div class="rv-meta chips"><span class="chip mono">${esc(J.TOOL_SHORT?.[it.tool] || it.tool)}${it.model ? " · " + esc(J.modelLabel(it.model)) : ""}</span>
      ${it.level ? `<span class="chip">${esc(it.level)}${it.effort ? " · " + esc(it.effort) : ""}</span>` : ""}
      ${it.duration != null ? `<span class="chip mono">${esc(J.dur(it.duration))}</span>` : ""}${when ? `<span class="chip mono">${esc(when)}</span>` : ""}</div>
    ${it.status === "failed" ? `<div class="rv-note" role="note">${J.sd("fail")}<span>This run failed${it.activity ? `: ${esc(it.activity)}` : ""}. Its changes were not committed.</span></div>` : ""}
    <div class="rv-body">
      <div id="rv-actions">${actionsHtml(it)}</div>
      ${it.commits.length ? `<div class="kv"><span>Committed</span><b class="num" title="${esc(it.commits.map(c => c.sha).join(" "))}">${it.commits.map(c => `${esc(c.short)} ${esc(c.subject)}`).join(" · ")}</b></div>` : ""}
      <h4 class="sect-h">Checks</h4><div id="rv-check">${checkHtml(it)}</div>
      <h4 class="sect-h">Changed files<span class="num">${it.files.length} · +${it.added} −${it.removed}</span></h4>${filesHtml(it)}
      <h4 class="sect-h">Diff</h4><div id="rv-diff" class="rv-diff"><div class="empty">Loading…</div></div>
    </div>`;
}
function selected() { return items.find(i => i.id === selId) || null; }
function detail() {
  const box = q("#rv-detail"), it = selected();
  el.classList.toggle("has-sel", !!it);
  if (!it) {
    const msg = !loaded ? "Loading…" : selId ? "That run is not waiting for review any more (accepted, reverted or dismissed)." : items.length ? "Select a run to see what it changed." : "";
    box._shell = null; J.setHTML(box, `<div class="empty pad">${esc(msg)}</div>`);
    return;
  }
  /* the shell (header, files, commits) is redrawn only when the run's data changed; the action row and the checks
     patch on their own and the diff is kept apart, so a poll never resets the diff's scroll or the focused button */
  const shellKey = JSON.stringify([it.id, it.title, it.status, it.activity, it.files, it.commits, it.duration]);
  if (box._shell !== shellKey) { box._shell = shellKey; box._key = null; J.setHTML(box, detailHtml(it)); }
  const key = JSON.stringify([it.revert, it.check, it.commits, confirmId === it.id, busy]);
  if (box._key !== key) { box._key = key; J.setHTML(q("#rv-actions"), actionsHtml(it)); J.setHTML(q("#rv-check"), checkHtml(it)); }
  const d = diffs.get(it.id);
  if (d?.html) J.setHTML(q("#rv-diff"), d.html);
  loadDiff(it);
}

/* ---------- actions ---------- */
function advance(id) {   /* after a decision the next run in the list is selected */
  const i = items.findIndex(x => x.id === id);
  const next = items[i + 1] || items[i - 1];
  return next && next.id !== id ? next.id : null;
}
async function act(kind) {
  const it = selected(); if (!it || busy) return;
  const id = it.id, nextId = advance(id);
  busy = kind + ":" + id; confirmId = null; detail();
  try {
    if (kind === "check") {
      const r = await J.api("/api/inbox/check", it.check.trusted === false ? {id, trust_command: it.check.command} : {id});
      J.toast(r.check.status === "pass" ? "Checks passed" : "Checks failed. See the output.", r.check.status !== "pass");
    } else {
      const r = await J.api("/api/inbox/" + kind, {id});
      J.toast(kind === "accept" ? "Accepted" : kind === "dismiss" ? "Dismissed" : `Reverted. New commit ${String(r.commit || "").slice(0, 8)}`);
    }
  } catch (e) { J.toast(e.message, true); }
  busy = null;
  await load(true); J.poll();
  if (kind !== "check" && !items.some(i => i.id === id) && selId === id) location.hash = "#/review" + (nextId ? "/" + encodeURIComponent(nextId) : "");
}
function select(id) { location.hash = "#/review/" + encodeURIComponent(id); }

J.pages.review = {
  init() {
    el = document.querySelector('[data-page="review"]');
    el.innerHTML = `
      <div class="grid rv-grid">
        <section class="panel rv-listp" aria-label="Runs to review"><div class="ph"><h2>To review</h2><span class="n num" id="rv-n"></span></div>
          <div class="rv-list" id="rv-list" role="list"></div>
          <div class="empty" id="rv-empty" hidden>Nothing to review. Finished runs that changed files will wait here.</div></section>
        <section class="panel rv-detail" id="rv-detail" aria-label="Run detail"></section>
      </div>`;
    const list = q("#rv-list");
    list.addEventListener("click", e => { const r = e.target.closest("[data-id]"); if (r) select(r.dataset.id); });
    list.addEventListener("keydown", e => {
      const r = e.target.closest("[data-id]");
      if (r && e.key === "Enter") { e.preventDefault(); e.stopPropagation(); r.dataset.id === selId ? (location.hash = "#/agents/" + encodeURIComponent(selId)) : select(r.dataset.id); }
      if (r && e.key === " ") { e.preventDefault(); select(r.dataset.id); }
    });
    const det = q("#rv-detail");
    det.addEventListener("click", e => {
      const b = e.target.closest("[data-do]");
      if (b) {
        const d = b.dataset.do;
        if (d === "ask-revert") { confirmId = selId; detail(); q('#rv-actions [data-key="revert"]')?.focus(); }
        else if (d === "cancel") { confirmId = null; detail(); q('#rv-actions [data-key="revert"]')?.focus(); }
        else act(d);
        return;
      }
      const f = e.target.closest("[data-file]");
      if (f) {
        const blk = [...det.querySelectorAll(".rv-file")].find(x => x.dataset.path === f.dataset.file || x.dataset.path.endsWith("/" + f.dataset.file));
        if (blk) { blk.scrollIntoView({block: "start", behavior: "smooth"}); blk.querySelector("pre")?.focus({preventScroll: true}); }
      }
    });
    J.on("escape", () => { if (J.shell.current() !== "review") return; if (confirmId) { confirmId = null; detail(); } else if (selId) location.hash = "#/review"; });
    J.on("state", () => { const s = signature(); if (s !== lastSig) { lastSig = s; load(true); } });
    load(true);
  },
  show(params) {
    const next = params[0] || null;
    if (next !== selId) confirmId = null;
    selId = next;
    load(true);
  },
  render() {
    if (!el) return;
    if (!selId && items.length && J.wide?.() && J.shell.current() === "review") { location.replace("#/review/" + encodeURIComponent(items[0].id)); return; }
    q("#rv-n").textContent = items.length ? String(items.length) : "";
    J.patch(q("#rv-list"), items, {key: i => i.id, html: row, cls: i => `rv-row${i.id === selId ? " sel" : ""}`,
      sig: i => JSON.stringify([i.status, i.title, i.files.length, i.added, i.removed, i.check.status, i.id === selId, i.ended && Math.floor(i.ended / 60)]),
      attrs: i => ({role: "listitem", tabindex: "0", "aria-current": i.id === selId ? "true" : null})});
    q("#rv-empty").hidden = !loaded || items.length > 0;
    detail();
  },
  tick() { if (Date.now() - lastFetch > 20000 && !document.hidden) load(true); },
  key(e) {
    if (e.key === "j" || e.key === "k") {
      if (!items.length) return;
      e.preventDefault();
      const i = items.findIndex(x => x.id === selId), n = Math.max(0, Math.min(items.length - 1, i < 0 ? 0 : i + (e.key === "j" ? 1 : -1)));
      select(items[n].id);
      requestAnimationFrame(() => q(`#rv-list [data-id="${CSS.escape(items[n].id)}"]`)?.scrollIntoView({block: "nearest"}));
    } else if (e.key === "a" && selId && selected()) { e.preventDefault(); act("accept"); }
    else if (e.key === "Enter" && selId && !e.target.closest?.("button, a, summary, [role=listitem]")) { e.preventDefault(); location.hash = "#/agents/" + encodeURIComponent(selId); }
  },
  crumb() { const it = selected(); return it ? it.title : loaded ? `${items.length} to review` : ""; },
};
})();
