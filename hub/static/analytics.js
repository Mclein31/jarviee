/* analytics.js: tokens, cost, quota and throughput from /api/usage, plus every recorded run from /api/runs. */
"use strict";
(() => {
const {esc} = J;
let el = null;
const q = s => el.querySelector(s);

/* stacked hourly bars with an hour axis; series = [{key, cls, label}] over rows [{t, [key]: n}] */
function hourChart(rows, series, unit) {
  if (!rows.length) return `<div class="empty">No data yet.</div>`;
  const W = 720, H = 150, n = rows.length, bw = W / n, max = Math.max(1, ...rows.map(r => series.reduce((s, x) => s + (r[x.key] || 0), 0)));
  const bars = rows.map((r, i) => {
    let y = H, out = "";
    for (const s of series) {
      const v = r[s.key] || 0; if (!v) continue;
      const h = Math.max(1.5, v / max * (H - 8)); y -= h;
      out += `<rect class="${s.cls}" x="${(i * bw + 2).toFixed(1)}" y="${y.toFixed(1)}" width="${(bw - 4).toFixed(1)}" height="${h.toFixed(1)}" rx="2"><title>${new Date(r.t * 1000).getHours()}:00 · ${s.label} ${unit === "tok" ? J.fmtTok(v) : v}</title></rect>`;
    }
    return out || `<rect class="nil" x="${(i * bw + 2).toFixed(1)}" y="${H - 1.5}" width="${(bw - 4).toFixed(1)}" height="1.5"/>`;
  }).join("");
  const grid = [.25, .5, .75, 1].map(f => `<line x1="0" x2="${W}" y1="${(H - f * (H - 8)).toFixed(1)}" y2="${(H - f * (H - 8)).toFixed(1)}"/>`).join("");
  const labels = rows.map((r, i) => i % 3 === 0 ? `<span style="left:${(100 * (i + .5) / n).toFixed(2)}%">${String(new Date(r.t * 1000).getHours()).padStart(2, "0")}</span>` : "").join("");
  return `<div class="hchart"><span class="hmax num">${unit === "tok" ? J.fmtTok(max) : max}</span><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="per hour, last 24 hours"><g class="grid">${grid}</g>${bars}</svg>
    <div class="hx">${labels}</div><div class="hl">${series.map(s => `<span><i class="${s.cls}"></i>${s.label}</span>`).join("")}</div></div>`;
}
function meter(label, used, until, note) {
  const lvl = used == null ? "grey" : used >= 100 ? "red" : "";   // red only when it is used up (out); otherwise the tool's hue
  return `<div class="meter" title="${esc(note || label)}"><span class="ml">${esc(label)}</span><span class="mv num">${used == null ? "—" : Math.round(used) + "% used"}</span>
    <span class="bar ${lvl}"><i style="width:${used == null ? 0 : Math.min(100, used)}%"></i></span>${until && until > J.now() ? `<span class="mw">resets in <span data-until="${until}">${J.span(until - J.now())}</span></span>` : note ? `<span class="mw">${esc(note)}</span>` : ""}</div>`;
}
function render() {
  const U = J.U;
  if (!U) { J.setHTML(q("#an-top"), `<section class="panel"><div class="empty">${J.U_err ? `Couldn't read usage: ${esc(J.U_err)}` : "Reading usage… the first read scans transcripts and takes a few seconds."}</div></section>`); return; }
  const c = U.claude || {}, a = U.agy || {}, act = U.activity || {};
  const tok = (c.tokens_today?.total || 0) + (a.tokens_today?.total || 0), wk = (c.tokens_week || 0) + (a.tokens_week || 0);
  J.setHTML(q("#an-top"), `<div class="kpis an-kpis">
    <div class="kpi k-run"><span class="kl">Tokens today</span><span class="kv num">${J.fmtTok(tok)}</span><span class="ks">in ${J.fmtTok((c.tokens_today?.input || 0) + (a.tokens_today?.input || 0))} · out ${J.fmtTok((c.tokens_today?.output || 0) + (a.tokens_today?.output || 0))}</span></div>
    <div class="kpi k-run"><span class="kl">Cache read today</span><span class="kv num">${J.fmtTok((c.tokens_today?.cache_read || 0) + (a.tokens_today?.cache_read || 0))}</span><span class="ks">cache write ${J.fmtTok((c.tokens_today?.cache_write || 0) + (a.tokens_today?.cache_write || 0))}</span></div>
    <div class="kpi k-cost"><span class="kl">Est. cost today</span><span class="kv num">${J.fmtUsd(c.cost_today)}</span><span class="ks">${c.cost_partial ? "some models unpriced" : "Claude Code at API rates"}</span></div>
    <div class="kpi k-run"><span class="kl">Tokens · 7 days</span><span class="kv num">${J.fmtTok(wk)}</span><span class="ks">Claude ${J.fmtTok(c.tokens_week)} · agy ${J.fmtTok(a.tokens_week)}</span></div>
    <div class="kpi k-done"><span class="kl">Finished · 24 h</span><span class="kv num">${act.finished_24h ?? "—"}</span><span class="ks">${act.finished_today ?? 0} today</span></div>
    <div class="kpi k-neutral"><span class="kl">Avg time to finish</span><span class="kv num">${act.avg_finish_s != null ? J.span(act.avg_finish_s) : "—"}</span><span class="ks">last 24 h</span></div></div>`);
  const hrs = (c.hourly || []).map((v, i) => ({t: (U.generated || J.now()) - (23 - i) * 3600, claude: v, agy: (a.hourly || [])[i] || 0}));
  J.setHTML(q("#an-tok"), hourChart(hrs, [{key: "claude", cls: "s-cl", label: "Claude"}, {key: "agy", cls: "s-ag", label: "agy"}], "tok"));
  J.setHTML(q("#an-fin"), hourChart(act.hours || [], [{key: "claude", cls: "s-cl", label: "Claude"}, {key: "agy", cls: "s-ag", label: "agy"}, {key: "other", cls: "s-ot", label: "other"}], "n"));
  const wins = c.limits?.windows || [];
  /* today's Claude tokens by kind, and the week: real data that fills the pane beside the taller agy quota */
  const tt = c.tokens_today || {}, kinds = [["cache_read", "Cache read"], ["cache_write", "Cache write"], ["input", "Input"], ["output", "Output"]];
  const ttot = kinds.reduce((n, [k]) => n + (tt[k] || 0), 0);
  const mixHtml = ttot ? `<h4 class="sect-h">Today's tokens by kind</h4>
    <div class="tmix" role="img" aria-label="Claude tokens today by kind">${kinds.map(([k], i) => `<i class="tm-${i}" style="width:${Math.max(tt[k] ? .6 : 0, 100 * (tt[k] || 0) / ttot).toFixed(2)}%"></i>`).join("")}</div>
    <div class="tmix-k">${kinds.map(([k, l], i) => `<span><i class="tm-${i}"></i>${l}<b class="num">${J.fmtTok(tt[k] || 0)}</b></span>`).join("")}</div>
    <div class="faint small">7 days: <b class="num">${J.fmtTok(c.tokens_week || 0)}</b> tokens on Claude Code</div>` : "";
  J.setHTML(q("#an-claude"), (wins.length ? wins.map(w => meter(w.label, w.used_pct, w.resets_at, w.note)).join("") : `<div class="empty">${esc(c.limits_reason || "Enable hooks/statusline.py to read Claude's 5-hour and weekly limits.")}</div>`)
    + `<h4 class="sect-h">Tokens by model today</h4>` + ((c.by_model || []).length ? `<table class="tbl"><tbody>${c.by_model.map(m => `<tr><td>${esc(m.label)}</td><td class="r num">${J.fmtTok(m.tokens)}</td><td class="r num">${m.priced ? J.fmtUsd(m.cost) : "—"}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">No Claude Code activity today.</div>`)
    + (c.block ? `<div class="faint small">This 5-hour block: ${J.fmtTok(c.block.tokens)} · ${J.fmtUsd(c.block.cost)} · ends <span data-until="${c.block.resets_at}">${J.span(c.block.resets_at - J.now())}</span></div>` : "") + mixHtml);
  const g = a.quota?.groups || [];
  J.setHTML(q("#an-agy"), g.length ? g.map(gr => `<h4 class="sect-h" title="${esc(gr.models)}">${esc(gr.name)}</h4>${gr.buckets.map(b => meter(b.label, b.used_pct, b.resets_at)).join("")}`).join("")
    : `<div class="empty">${esc(a.quota_reason || "agy /usage isn't readable right now.")}</div>`);
}
function runs() {
  const R = J.RUNS;
  if (!R) { J.setHTML(q("#an-runs"), `<div class="empty">Loading…</div>`); return; }
  if (!R.length) { J.setHTML(q("#an-runs"), `<div class="empty">No runs recorded yet.</div>`); return; }
  const SK = {running: "run", done: "done", failed: "fail", cancelled: "stopped", stopped: "stopped", waiting: "need"};
  J.setHTML(q("#an-runs"), `<div class="tscroll"><table class="tbl"><thead><tr><th>Status</th><th>Run</th><th>Project</th><th>Kind</th><th class="r">Duration</th><th class="r">Tasks</th><th class="r">Tokens</th><th class="r">Cost</th></tr></thead><tbody>
    ${R.slice(0, 60).map(r => `<tr data-href="#/workflows/${encodeURIComponent(r.id)}"><td>${J.st(SK[r.status] || "queue", r.status)}</td><td class="clip"><a href="#/workflows/${encodeURIComponent(r.id)}">${esc(J.makeTitle(r.title) || r.title || r.id)}</a></td>
      <td class="muted">${esc(r.project || "—")}</td><td class="num faint">${esc(r.kind)}</td><td class="r num">${J.dur(r.duration)}</td><td class="r num">${r.done}/${r.total}</td>
      <td class="r num">${r.tokens ? J.fmtTok(r.tokens) : "—"}</td><td class="r num">${r.cost != null ? J.fmtUsd(r.cost) : "—"}</td></tr>`).join("")}</tbody></table></div>`);
}

J.pages.analytics = {
  init() {
    el = document.querySelector('[data-page="analytics"]');
    el.innerHTML = `<div id="an-top"></div>
      <div class="grid an-grid">
        <section class="panel"><div class="ph"><h2>Tokens per hour</h2><span class="n">last 24 h</span></div><div class="pb" id="an-tok"></div></section>
        <section class="panel"><div class="ph"><h2>Runs finished per hour</h2><span class="n">last 24 h</span></div><div class="pb" id="an-fin"></div></section>
        <section class="panel"><div class="ph"><h2>Claude Code limits</h2></div><div class="pb col-gap" id="an-claude"></div></section>
        <section class="panel"><div class="ph"><h2>Antigravity quota</h2></div><div class="pb col-gap" id="an-agy"></div></section>
      </div>
      <section class="panel"><div class="ph"><h2>Run history</h2><span class="act faint">tokens and cost from local transcripts</span></div><div id="an-runs"></div></section>`;
    el.addEventListener("click", e => { const tr = e.target.closest("tr[data-href]"); if (tr && !e.target.closest("a")) location.hash = tr.dataset.href; });
  },
  show() { J.pollRuns(); },
  render(ev) { if (!el) return; if (ev !== "activity") render(); runs(); },
  tick() { for (const c of el.querySelectorAll("[data-until]")) { const t = J.span(+c.dataset.until - J.now()); if (c.textContent !== t) c.textContent = t; } },
  crumb() { return J.U ? `updated ${J.ago(J.U.generated)}` : ""; },
};
})();
