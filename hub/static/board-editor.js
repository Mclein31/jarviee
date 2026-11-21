/* board-editor.js: accessible modal dialog to create, edit, delete, revert-polish, or start an agent on a board card. */
"use strict";
(() => {
const {esc} = J;
const LABELS = ["feature", "bugfix", "error", "refactor", "chore", "docs", "research"];
const PRIORITIES = [["low", "Low"], ["normal", "Normal"], ["high", "High"]];
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

let dlg = null, curCard = null, curProject = "", opener = null, openerCardId = null, busy = false, confirmingDel = false;
const q = s => dlg?.querySelector(s);

function toolList() {
  const tools = J.S?.tools ? Object.keys(J.S.tools).filter(t => t !== "echo" || J.S.default_tool === "echo") : ["agy", "claude"];
  return tools.length ? tools : ["agy", "claude"];
}
function tierList() {
  return J.TIERS?.length ? J.TIERS : ["basic", "light", "complex", "heavy"];
}
function linkedRun(card) {
  return card?.run_id ? (J.S?.agents || []).find(a => a.id === card.run_id) || {id: card.run_id, status: ""} : null;
}

function focusables() {
  if (!dlg) return [];
  return [...dlg.querySelectorAll(FOCUSABLE)].filter(el =>
    !el.disabled && !el.hidden && !el.closest("[hidden]") && (!el.closest("details:not([open])") || el.tagName === "SUMMARY")
  );
}

function showError(msg) {
  const err = q("#bd-err");
  if (!err) return;
  err.textContent = msg || "";
  err.hidden = !msg;
}

function setBusy(on) {
  busy = on;
  if (!dlg) return;
  for (const el of dlg.querySelectorAll("button, input, select, textarea")) {
    if (el.dataset.bdAct === "close") continue;
    el.disabled = on;
  }
  if (!on) syncStartBtn();
}

function syncStartBtn() {
  const b = q("#bd-start");
  if (!b || !curCard) return;
  const run = linkedRun(curCard);
  const active = run && (run.status === "running" || run.status === "queued");
  b.disabled = !!active || busy;
  b.title = active ? "An agent is already working on this card" : "Launch an agent for this card and move it to Build";
}

function restoreFocus() {
  if (dlg?.open) return;
  const prev = opener, cid = openerCardId;
  opener = null;
  openerCardId = null;
  if (!prev && !cid) return;
  if (prev && prev.isConnected && typeof prev.focus === "function") {
    prev.focus({preventScroll: true});
    return;
  }
  if (cid) {
    const cardEl = document.querySelector(`.bd-card[data-id="${CSS.escape(cid)}"]`);
    if (cardEl) { cardEl.focus({preventScroll: true}); return; }
  }
  document.querySelector("#bd-new")?.focus({preventScroll: true});
}

function close() {
  if (!dlg || !dlg.open) return;
  confirmingDel = false;
  dlg.close();
  restoreFocus();
}

function delRowHTML() {
  if (!curCard) return "";
  if (confirmingDel) {
    return `<span class="bd-del-confirm" role="group" aria-label="Confirm deletion">
      <span class="bd-del-ask">Delete card?</span>
      <button class="btn sm danger" type="button" id="bd-del-yes" data-bd-act="delete">Confirm delete</button>
      <button class="btn sm ghost" type="button" data-bd-act="cancel-delete">Keep</button>
    </span>`;
  }
  return `<button class="btn ghost danger" type="button" id="bd-del" data-bd-act="ask-delete">Delete</button>`;
}

function paintDelete() {
  const slot = q("#bd-del-slot");
  if (slot) slot.innerHTML = delRowHTML();
}

function origHTML(card) {
  if (!card?.original) return "";
  const ot = card.original.title || "", od = card.original.description || "";
  const model = card.polish_model ? ` · ${esc(card.polish_model)}` : "";
  return `<details class="bd-orig" id="bd-orig">
    <summary>Original vs improved${model}</summary>
    <div class="bd-orig-grid">
      <div class="bd-orig-pane">
        <div class="bd-orig-h">Original</div>
        <div class="bd-orig-t">${esc(ot)}</div>
        <div class="bd-orig-d">${od ? esc(od) : `<span class="faint">(empty)</span>`}</div>
      </div>
      <div class="bd-orig-pane">
        <div class="bd-orig-h">Improved</div>
        <div class="bd-orig-t">${esc(card.title || "")}</div>
        <div class="bd-orig-d">${card.description ? esc(card.description) : `<span class="faint">(empty)</span>`}</div>
      </div>
    </div>
    <div class="bd-orig-acts">
      <button class="btn sm" type="button" data-bd-act="revert">Revert to my text</button>
    </div>
  </details>`;
}

function startHTML(card) {
  if (!card) return "";
  const tools = toolList().map(t => `<option value="${esc(t)}">${esc(J.TOOL?.[t] || t)}</option>`).join("");
  const tiers = tierList().map(t => `<option value="${esc(t)}">${esc(J.TIER_WORD?.[t] || t)}</option>`).join("");
  return `<div class="bd-field bd-start-field">
    <span class="bd-start-lbl">Start agent</span>
    <div class="bd-start-row">
      <select class="select sm" id="bd-tool" aria-label="Agent tool"><option value="">Auto tool</option>${tools}</select>
      <select class="select sm" id="bd-tier" aria-label="Agent tier"><option value="">Auto tier</option>${tiers}</select>
      <button class="btn sm" type="button" id="bd-start" data-bd-act="start"><svg class="i sm" aria-hidden="true"><use href="#i-play"/></svg>Start agent</button>
    </div>
  </div>`;
}

function renderForm() {
  const card = curCard, proj = card ? (card.project || "") : curProject;
  const run = linkedRun(card);
  const label = card?.label || "feature";
  const priority = card?.priority || "normal";
  const labelOpts = LABELS.map(l => `<option value="${esc(l)}"${l === label ? " selected" : ""}>${esc(l)}</option>`).join("");
  const priOpts = PRIORITIES.map(([v, t]) => `<option value="${esc(v)}"${v === priority ? " selected" : ""}>${esc(t)}</option>`).join("");

  dlg.innerHTML = `<form class="bd-modal-form" id="bd-form" novalidate>
    <div class="dlg-h">
      <h2 id="bd-dlg-h">${card ? "Edit card" : "New card"}</h2>
      ${proj ? `<span class="chip mono bd-proj-chip">${esc(proj)}</span>` : ""}
      ${run ? `<a class="chip mono bd-run-link" href="#/agents/${encodeURIComponent(run.id)}" title="Open linked agent">${esc(run.status || "linked")}</a>` : ""}
      <button class="btn icon ghost sm" type="button" data-bd-act="close" aria-label="Close"><svg class="i" aria-hidden="true"><use href="#i-close"/></svg></button>
    </div>
    <div class="dlg-b bd-modal-b">
      <div class="bd-err" id="bd-err" role="alert" hidden></div>
      <div class="bd-field">
        <label for="bd-title">Title</label>
        <input class="input" id="bd-title" name="title" type="text" maxlength="120" required value="${esc(card?.title || "")}" placeholder="Short imperative task title" autocomplete="off">
      </div>
      <div class="bd-field">
        <label for="bd-desc">Description</label>
        <textarea class="input bd-desc" id="bd-desc" name="description" rows="5" maxlength="4000" placeholder="Goal, context, or acceptance criteria (optional)">${esc(card?.description || "")}</textarea>
      </div>
      <div class="bd-fields-row">
        <div class="bd-field">
          <label for="bd-label">Label</label>
          <select class="select" id="bd-label" name="label">${labelOpts}</select>
        </div>
        <div class="bd-field">
          <label for="bd-priority">Priority</label>
          <select class="select" id="bd-priority" name="priority">${priOpts}</select>
        </div>
      </div>
      <div class="bd-field bd-polish-field">
        <label class="toggle">
          <input type="checkbox" id="bd-polish" name="polish" checked>
          <span>Improve with AI (haiku)</span>
        </label>
        ${card?.polish === "pending" ? `<span class="faint bd-polish-note" role="status">Currently improving…</span>` : ""}
      </div>
      <div id="bd-orig-slot">${origHTML(card)}</div>
      ${startHTML(card)}
      <div class="bd-modal-acts">
        <span id="bd-del-slot">${delRowHTML()}</span>
        <span class="spacer"></span>
        <button class="btn ghost" type="button" data-bd-act="close">Cancel</button>
        <button class="btn primary" type="submit" id="bd-save">${card ? "Save" : "Create card"}</button>
      </div>
    </div>
  </form>`;
  syncStartBtn();
}

/* ---------- actions ---------- */
async function save() {
  if (busy) return;
  const titleEl = q("#bd-title"), descEl = q("#bd-desc"), labelEl = q("#bd-label"), priEl = q("#bd-priority"), polEl = q("#bd-polish");
  const title = titleEl.value.trim(), description = descEl.value.trim();
  const label = labelEl.value, priority = priEl.value, polish = !!polEl.checked;
  if (!title) {
    showError("A card needs a title.");
    titleEl.focus();
    return;
  }
  if (!curCard && !curProject) {
    showError("Pick a project before creating a card.");
    return;
  }
  showError("");
  setBusy(true);
  try {
    if (curCard) {
      const changed = title !== (curCard.title || "") || description !== (curCard.description || "");
      await J.api("/api/board/update", {id: curCard.id, title, description, label, priority, polish});
      J.toast(polish && changed ? "Card saved · improving with AI" : "Card saved");
      J.announce("Card saved");
    } else {
      await J.api("/api/board/create", {project: curProject, title, description, label, priority, polish});
      J.toast(polish ? "Card created · improving with AI" : "Card created");
      J.announce("Card created");
    }
    setBusy(false);
    close();
    J.board?.reload?.();
  } catch (e) {
    setBusy(false);
    showError(e.message || "Could not save the card.");
  }
}

async function revertPolish() {
  if (busy || !curCard) return;
  showError("");
  setBusy(true);
  try {
    const r = await J.api("/api/board/revert-polish", {id: curCard.id});
    if (r.card) curCard = r.card;
    q("#bd-title").value = curCard.title || "";
    q("#bd-desc").value = curCard.description || "";
    q("#bd-polish").checked = false;
    const orig = q("#bd-orig-slot");
    if (orig) orig.innerHTML = "";
    setBusy(false);
    J.toast("Reverted to your text");
    J.announce("Reverted to your original text");
    J.board?.reload?.();
    q("#bd-title")?.focus();
  } catch (e) {
    setBusy(false);
    showError(e.message || "Could not revert the card.");
  }
}

async function removeCard() {
  if (busy || !curCard) return;
  showError("");
  setBusy(true);
  try {
    await J.api("/api/board/delete", {id: curCard.id});
    setBusy(false);
    opener = document.querySelector("#bd-new");
    openerCardId = null;
    close();
    J.toast("Card deleted");
    J.announce("Card deleted");
    J.board?.reload?.();
  } catch (e) {
    setBusy(false);
    showError(e.message || "Could not delete the card.");
  }
}

async function startAgent() {
  if (busy || !curCard) return;
  const tool = q("#bd-tool")?.value || "", tier = q("#bd-tier")?.value || "";
  const body = {id: curCard.id};
  if (tool) body.tool = tool;
  if (tier) body.tier = tier;
  showError("");
  setBusy(true);
  try {
    await J.api("/api/board/start", body);
    setBusy(false);
    close();
    J.toast("Agent started");
    J.announce("Agent started");
    J.board?.reload?.();
    J.poll?.();
  } catch (e) {
    setBusy(false);
    showError(e.message || "Could not start an agent.");
  }
}

/* ---------- mount & events ---------- */
function mount() {
  dlg = document.createElement("dialog");
  dlg.className = "bd-modal";
  dlg.setAttribute("aria-labelledby", "bd-dlg-h");
  document.body.append(dlg);

  dlg.addEventListener("close", restoreFocus);
  dlg.addEventListener("cancel", e => { e.preventDefault(); close(); });
  dlg.addEventListener("click", e => {
    if (e.target === dlg) { close(); return; }
    if (e.target.closest("a.bd-run-link")) { close(); return; }
    const b = e.target.closest("[data-bd-act]");
    if (!b) return;
    const act = b.dataset.bdAct;
    if (act === "close") close();
    else if (act === "ask-delete") { confirmingDel = true; paintDelete(); q("#bd-del-yes")?.focus(); }
    else if (act === "cancel-delete") { confirmingDel = false; paintDelete(); q("#bd-del")?.focus(); }
    else if (act === "delete") removeCard();
    else if (act === "revert") revertPolish();
    else if (act === "start") startAgent();
  });
  dlg.addEventListener("submit", e => {
    if (e.target.id === "bd-form") { e.preventDefault(); save(); }
  });
  dlg.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.id === "bd-desc") { e.preventDefault(); save(); return; }
    if (e.key !== "Tab") return;
    const list = focusables();
    if (!list.length) { e.preventDefault(); return; }
    const first = list[0], last = list[list.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first || !dlg.contains(document.activeElement)) { e.preventDefault(); last.focus(); }
    } else if (document.activeElement === last || !dlg.contains(document.activeElement)) {
      e.preventDefault(); first.focus();
    }
  });
}

J.boardEditor = {
  open({project, card} = {}) {
    if (!dlg) mount();
    if (!dlg.open) {
      opener = typeof document.activeElement?.focus === "function" ? document.activeElement : null;
      openerCardId = card?.id || null;
    }
    curCard = card ? {...card} : null;
    curProject = card ? (card.project || "") : (project || document.querySelector("#bd-project")?.value || J.saved?.get?.("jarviee.board.project") || "");
    confirmingDel = false;
    busy = false;
    renderForm();
    if (!dlg.open) dlg.showModal();
    q("#bd-title")?.focus();
  },
  close,
};
})();
