/* settings.js: routing rules, alerts, iTerm2 panes, quota re-checks and where things live. */
"use strict";
(() => {
const {esc} = J;
let el = null, omni = null;
const q = s => el.querySelector(s);
const row = (title, desc, control) => `<div class="set-row"><div><b>${title}</b><p>${desc}</p></div><div class="set-c">${control}</div></div>`;
const loadOmni = () => J.api("/api/omniroute/status").then(r => { omni = r; if (el) render(); }).catch(() => {});

function render() {
  const S = J.S, fb = S.fallback || {}, T = S.terminal, al = J.alerts(), ts = S.tool_status || {};
  const tog = (attr, on, label) => `<label class="toggle"><input type="checkbox" ${attr} ${on ? "checked" : ""}><span class="sr-only">${label}</span></label>`;
  const omniNote = !omni ? "Checking gateway…"
    : !omni.enabled ? "Off"
    : !omni.has_key ? `No API key: run <code>python3 hub.py omniroute connect</code>`
    : omni.healthy ? "Ready"
    : `Gateway offline: ${esc(omni.reason || "unavailable")}`;
  J.setHTML(q("#set-routing"), [
    row("Use whichever tool is available", "When Antigravity or Claude Code is out of quota, signed out or missing, tasks go to the other tool at the same level instead of waiting. Applies to new tasks, the queue and retries.",
      tog('data-rule="use_available_tool" data-key="r1"', fb.use_available_tool !== false, "Use whichever tool is available")),
    row("Fall back on quota or rate limits", "Re-run a task on an equivalent model (config <code>fallback.equivalents</code>) when the first one hits a limit.",
      tog('data-rule="enabled" data-key="r2"', fb.enabled !== false, "Automatic fallback")),
    row("Never fall back to Claude Opus", "Fallbacks and the switch above never pick Opus on their own; a heavy task steps down to Sonnet instead.",
      tog('data-rule="never_fallback_to_opus" data-key="r3"', fb.never_fallback_to_opus !== false, "Never fall back to Opus")),
    row("OmniRoute as last resort", `When both Antigravity and Claude Code are unavailable, route through the local OmniRoute gateway. ${omniNote}`,
      tog('data-rule="omniroute" data-key="r4"', omni ? !!omni.enabled : true, "OmniRoute as last resort")),
  ].join(""));
  J.setHTML(q("#set-tools"), Object.entries(ts).map(([tool, t]) => {
    const bad = Object.entries(t.tiers || {}).filter(([, m]) => !m.available);
    return row(`${J.toolMono(tool)} ${esc(J.TOOL[tool] || tool)}`,
      t.available ? (bad.length ? `Ready. Out: ${bad.map(([tr]) => J.TIER_WORD[tr]).join(", ")}.` : "Ready on every tier.") : esc(t.reason || "Unavailable"),
      `${J.st(t.available ? "done" : "out", t.available ? "ready" : t.until ? `resets in ${J.span(t.until - J.now())}` : "out")}<button class="btn sm" type="button" data-probe="${esc(tool)}" data-key="p:${esc(tool)}"><svg class="i sm" aria-hidden="true"><use href="#i-refresh"/></svg>Re-check</button>`);
  }).join("") || `<div class="empty">No tool lanes configured.</div>`);
  const vo = S.voice || {};
  J.setHTML(q("#set-alerts"), [
    al && al.enabled ? row("Alerts", "macOS banner and sound when an agent starts waiting, fails or finishes. Cooldown and quiet hours come from config.",
      tog('data-alerts data-key="al"', !al.muted, "Alerts")) : `<div class="empty">Alerts are off in config (<code>alerts.enabled</code>) or this server doesn't support them.</div>`,
    row("Speak the report", "Jarviee reads you a short summary of what a run did when it finishes, fails or needs you.",
      tog('data-voice data-key="vo"', vo.enabled !== false, "Speak the report")),
    row("Report volume", "While Jarviee speaks, the Mac plays at the first level and music and video drop to the second, both as a share of the Mac's full volume. Afterwards everything goes back to where it was.",
      `<label class="field">Voice <input type="number" min="5" max="100" step="5" value="${esc(String(vo.report_volume ?? 70))}" data-vlevel="report_volume" data-key="vr" aria-label="Voice volume, percent of full" style="width:4.5em">%</label>
       <label class="field">Others <input type="number" min="0" max="100" step="5" value="${esc(String(vo.duck_volume ?? 20))}" data-vlevel="duck_volume" data-key="vd" aria-label="Other audio while Jarviee speaks, percent of full" style="width:4.5em">%</label>`),
  ].join(""));
  const P = S.push || {};
  const pstat = P.error ? `<span class="err">${esc(P.error)}</span>`
    : P.enabled && P.at ? (P.next ? `${P.queued} alert${P.queued === 1 ? "" : "s"} queued on ntfy. Next: <b>${esc(P.next.title)}</b> at ${J.clock(P.next.fire)}.` : "Nothing coming up in the next 36 hours.")
    : P.enabled ? "Reading your calendar…" : "Off.";
  J.setHTML(q("#set-push"), [
    row("Heads-up on your phone", `A push ${esc(String(P.lead_min || 15))} min before each calendar event and timed reminder, and your schedule for the day at ${esc(P.digest_at || "08:00")}. Alerts wait on the ntfy server, so they arrive even while this Mac sleeps.`,
      tog('data-push data-key="ph"', !!P.enabled, "Heads-up on your phone")),
    row("Subscribe on your phone", `Install the free <b style="display:inline">ntfy</b> app, tap <b style="display:inline">+</b>, and subscribe to your topic (<button class="linkish" type="button" data-reveal="topic">show</button><code class="topic-v" hidden>${esc(P.topic || "")}</code>). Anyone who knows this name can read the alerts, so keep it to yourself.`,
      `<button class="btn sm" type="button" data-pact="copy" data-key="pc">Copy topic</button><button class="btn sm" type="button" data-pact="test" data-key="pt">Send test</button>`),
    row("New topic", "If the topic name leaks, make a new one. Queued alerts are taken back and you subscribe again with the new name.",
      `<button class="btn sm" type="button" data-pact="rotate" data-key="pr">New topic</button>`),
    row("Timing", "Minutes of warning before an event, and when the day's summary arrives.",
      `<input type="number" min="1" max="240" value="${esc(String(P.lead_min || 15))}" data-pin="lead_min" aria-label="Minutes before" style="width:5em"> min
       <input type="time" value="${esc(P.digest_at || "08:00")}" data-pin="digest_at" aria-label="Summary time">`),
    row("Claude limit alerts", "One push when the 5-hour limit is all spent, and one when the cooldown is over. The second waits on the ntfy server, so it arrives while this Mac sleeps.",
      tog('data-plimit data-key="pl"', P.limit_alerts !== false, "Claude limit alerts")),
    (P.calendars || []).length ? row("Calendars", "Untick a calendar to stop its alerts, for example Classes on a break.",
      (P.calendars || []).map(c => `<label style="margin-right:12px"><input type="checkbox" data-pcal="${esc(c)}" ${(P.mute_calendars || []).includes(c) ? "" : "checked"}> ${esc(c)}</label>`).join("")) : "",
    `<p class="faint" style="margin:10px 0 0;font-size:var(--fs-sm)">${pstat}</p>`,
  ].join(""));
  J.setHTML(q("#set-maps"), row("Keep project maps up to date", "When a project's code changes in new commits, an agent updates its map (<code>.brain/FLOW.json</code>) once the project is quiet, and Jarviee commits it. At most one update per project every 3 hours.",
    tog('data-maps data-key="mp"', S.maps?.auto_update !== false, "Keep project maps up to date")));
  J.setHTML(q("#set-term"), T && T.installed && T.mode === "iterm2" ? [
    row("Open runs in iTerm2", `Launched and planned runs open in panes of the Jarviee window${T.screen ? ` on ${esc(T.screen)}` : ""}.`, tog('data-term data-key="tm"', !!T.enabled, "Open runs in iTerm2")),
    row("Panes", `${T.panes || 0} open.`, `<button class="btn sm" type="button" data-tact="retile" data-key="rt" ${T.enabled ? "" : "disabled"}><svg class="i sm" aria-hidden="true"><use href="#i-retile"/></svg>Re-tile</button>
      <button class="btn sm" type="button" data-tact="close-finished" data-key="cf" ${T.enabled ? "" : "disabled"}>Close finished panes</button>`),
    ...(T.notices || []).map(n => `<div class="set-note"><span>${esc(n.text)}</span><button class="btn sm ghost" type="button" data-tact="dismiss" data-nkey="${esc(n.key)}" data-key="n:${esc(n.key)}">Dismiss</button></div>`),
  ].join("") : `<div class="empty">iTerm2 isn't installed, or panes are off in config (<code>terminal.mode</code>). Runs stay in the background.</div>`);
  const cur = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--z")) || .85;
  const W = window.JarvieeGlass?.info;
  J.setHTML(q("#set-scale"), row("Interface scale", "How big Jarviee draws everything. Saved in this browser.",
    `<span class="seg" role="group" aria-label="Interface scale">${J.SCALES.map(z => `<button type="button" data-scale="${z}" data-key="sc:${z}" aria-pressed="${Math.abs(z - cur) < .001}">${Math.round(z * 100)}%</button>`).join("")}</span>`)
    + (W ? row("Background", `The photo behind the glass. ${W.choice === "desktop" ? (W.name ? `Following your desktop: <b style="display:inline">${esc(W.name)}</b>.` : "Your desktop has no still picture, so a quiet photo stands in.") : ""} The glass darkens itself over bright pictures so text stays readable.`,
      `<select class="select" data-wall aria-label="Background">${(W.options || []).map(o => `<option value="${esc(o.id)}" ${o.id === W.choice ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`) : ""));
  J.setHTML(q("#set-about"), `<div class="kv"><span>Name</span><b>Jarviee (formerly Agent Hub)</b><span>Server</span><b class="num">127.0.0.1:${esc(location.port || "80")}</b>
    <span>Data folder</span><b class="num">~/.jarviee</b><span>Workspace</span><b class="num" title="${esc(S.brain?.workspace || "")}">${esc(S.brain?.workspace || "—")}</b>
    <span>Command</span><b class="num">jarviee · jarviee glance · jarviee next</b></div>
    <button class="btn sm" type="button" data-keys><svg class="i sm" aria-hidden="true"><use href="#i-key"/></svg>Keyboard shortcuts</button>`);
}

J.pages.settings = {
  init() {
    el = document.querySelector('[data-page="settings"]');
    el.innerHTML = `<div class="grid set-grid">
      <div class="set-col">
        <section class="panel"><div class="ph"><h2>Routing</h2></div><div class="pb" id="set-routing"></div></section>
        <section class="panel"><div class="ph"><h2>Phone</h2></div><div class="pb" id="set-push"></div></section>
        <section class="panel"><div class="ph"><h2>Display</h2></div><div class="pb" id="set-scale"></div></section>
      </div>
      <div class="set-col">
        <section class="panel"><div class="ph"><h2>Notifications</h2></div><div class="pb" id="set-alerts"></div></section>
        <section class="panel"><div class="ph"><h2>Project maps</h2></div><div class="pb" id="set-maps"></div></section>
        <section class="panel"><div class="ph"><h2>Tools</h2><span class="act faint">quota state per lane</span></div><div class="pb" id="set-tools"></div></section>
        <section class="panel"><div class="ph"><h2>iTerm2 panes</h2></div><div class="pb" id="set-term"></div></section>
        <section class="panel"><div class="ph"><h2>About</h2></div><div class="pb col-gap" id="set-about"></div></section>
      </div></div>`;
    el.addEventListener("change", e => {
      if (e.target.dataset.wall !== undefined) {
        return J.api("/api/settings/appearance", {wallpaper: e.target.value})
          .then(r => window.JarvieeGlass.reload(true).then(render).then(() => J.toast(r.warning || "Background changed", !!r.warning)))
          .catch(err => J.toast(err.message, true));
      }
      const r = e.target.closest("[data-rule]"); if (r) return J.saveRule(r.dataset.rule, r.checked).then(() => { if (r.dataset.rule === "omniroute") loadOmni(); });
      if (e.target.closest("[data-alerts]")) return J.setAlerts(!e.target.checked);
      if (e.target.dataset.vlevel) {
        const n = Math.max(0, Math.min(100, Math.round(+e.target.value || 0)));
        return J.api("/api/settings/voice", {[e.target.dataset.vlevel]: n}).then(r => { J.S.voice = r.voice; J.toast(r.warning || "Saved", !!r.warning); })
          .catch(err => J.toast(err.message, true));
      }
      if (e.target.closest("[data-voice]")) {
        const on = e.target.checked;
        return J.api("/api/settings/voice", {enabled: on}).then(r => { J.S.voice = r.voice; J.toast(on ? "Spoken report is on" : "Spoken report is off"); })
          .catch(err => { e.target.checked = !on; J.toast(err.message, true); });
      }
      if (e.target.dataset.pin || e.target.dataset.pcal !== undefined) {
        const body = {};
        if (e.target.dataset.pin) body[e.target.dataset.pin] = e.target.value;
        else body.mute_calendars = [...el.querySelectorAll("[data-pcal]")].filter(x => !x.checked).map(x => x.dataset.pcal);
        return J.api("/api/settings/push", body).then(r => { J.S.push = r.push; render(); J.toast(r.warning || "Saved", !!r.warning); })
          .catch(err => J.toast(err.message, true));
      }
      if (e.target.closest("[data-plimit]")) {
        const on = e.target.checked;
        return J.api("/api/settings/push", {limit_alerts: on}).then(r => { J.S.push = r.push; J.toast(on ? "Limit alerts are on" : "Limit alerts are off"); })
          .catch(err => { e.target.checked = !on; J.toast(err.message, true); });
      }
      if (e.target.closest("[data-push]")) {
        const on = e.target.checked;
        return J.api("/api/settings/push", {enabled: on}).then(r => { J.S.push = r.push; render(); J.toast(r.warning || (on ? "Phone alerts are on" : "Phone alerts are off; queued ones were taken back"), !!r.warning); })
          .catch(err => { e.target.checked = !on; J.toast(err.message, true); });
      }
      if (e.target.closest("[data-maps]")) {
        const on = e.target.checked;
        return J.api("/api/settings/maps", {auto_update: on}).then(r => { J.S.maps = r.maps; J.toast(on ? "Maps update automatically" : "Automatic map updates are off"); })
          .catch(err => { e.target.checked = !on; J.toast(err.message, true); });
      }
      if (e.target.closest("[data-term]")) return term({action: e.target.checked ? "enable" : "disable"});
    });
    el.addEventListener("click", e => {
      const r = e.target.closest("[data-reveal]"); if (!r) return;
      const v = r.nextElementSibling; v.hidden = !v.hidden; r.textContent = v.hidden ? "show" : "hide";
    });
    el.addEventListener("click", async e => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.keys !== undefined) return document.querySelector("#keys-dlg").showModal();
      if (b.dataset.scale) { J.setScale(parseFloat(b.dataset.scale)); render(); J.emit("state", J.S); return; }
      if (b.dataset.pact === "copy") {
        try { await navigator.clipboard.writeText(J.S.push?.topic || ""); J.toast("Topic copied"); } catch (err) { J.toast("Couldn't copy: " + err.message, true); }
        return;
      }
      if (b.dataset.pact === "rotate") {
        if (!b.dataset.armed) {   // second click confirms
          b.dataset.armed = "1"; b.textContent = "Sure? Subscribe again after";
          setTimeout(() => { delete b.dataset.armed; b.textContent = "New topic"; }, 4000); return;
        }
        try { const r = await J.api("/api/push/rotate", {}); J.S.push = r.push; render(); J.toast("New topic made. Subscribe to it on your phone."); } catch (err) { J.toast(err.message, true); }
        return;
      }
      if (b.dataset.pact === "test") {
        b.disabled = true;
        try { await J.api("/api/push/test", {}); J.toast("Test sent. Check your phone."); } catch (err) { J.toast("Not sent: " + err.message, true); }
        b.disabled = false; return;
      }
      if (b.dataset.tact) return term({action: b.dataset.tact, ...(b.dataset.nkey ? {key: b.dataset.nkey} : {})});
      if (b.dataset.probe) {
        b.disabled = true;
        try { const r = await J.api("/api/quota/probe", {tool: b.dataset.probe}); J.toast(r.message || (r.ok ? "Available" : "Still out"), !r.ok); }
        catch (err) { J.toast(err.message, true); }
        b.disabled = false; J.poll();
      }
    });
  },
  render() { if (J.S && el) { render(); if (!omni) loadOmni(); } },
  crumb() { return "routing, alerts and panes"; },
};
async function term(body) {
  try { const r = await J.api("/api/terminal", body); if (r.message || r.reason) J.toast(r.message || r.reason, r.ok === false); } catch (e) { J.toast(e.message, true); }
  J.poll();
}
})();
