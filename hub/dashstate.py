"""Building the /api/state payload, and the cheap parts it is made of."""
import os
import threading
import time
from pathlib import Path

from . import batch, coord, dispatch, fallback, launcher, mapwatch, obsidian, projmap, push, queue_api, subagents, voice
from .ingest import tier_of_model, transcript_model
from .router import TIERS, model_efforts, model_family
from .titles import make_summary, make_title

_FINISHED = ("done", "failed", "stopped")
_synced = {}       # run id -> key of the last progress sync of a finished row
_tails = {}        # run id -> (key, stripped log tail)
_brain_cache = {}  # (workspace, status file) -> (signature, projects, status rows)
_lock = threading.Lock()


def _stat_key(path):
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns
    except (OSError, TypeError, ValueError):
        return None


def _forget_gone(ids):
    """Drop cache entries for rows that left the list, so the caches stay as small as the list."""
    for cache in (_synced, _tails):
        for k in [k for k in cache if k not in ids]:
            cache.pop(k, None)


def _sync_progress(a, store):
    """launcher.sync_run_progress, skipped for a finished row whose log and counters have not changed since the
    last sync: it re-reads and re-parses the whole log, and on a finished run it can no longer find anything new."""
    if a.get("status") not in _FINISHED:
        launcher.sync_run_progress(a, store)
        return
    key = (a.get("status"), _stat_key(a.get("log_path")), a.get("steps"), a.get("activity"))
    if _synced.get(a.get("id")) == key:
        return
    launcher.sync_run_progress(a, store)
    _synced[a.get("id")] = (a.get("status"), _stat_key(a.get("log_path")), a.get("steps"), a.get("activity"))


def _log_tail(a, tasks):
    """The row's stripped log tail, recomputed only when the log file or the row's status text changed."""
    key = (a.get("log_path"), _stat_key(a.get("log_path")), a.get("status"), a.get("activity"))
    hit = _tails.get(a.get("id"))
    if hit and hit[0] == key:
        return hit[1]
    text = launcher.strip_tty(_read_log_bytes(a, tasks).decode("utf-8", errors="replace"))
    _tails[a.get("id")] = (key, text)
    return text


def _brain_signature(cfg):
    """What the brain readers depend on: the workspace's project folders, each .brain/*.md's size and mtime, and
    the status table. Cheap (one scandir per project) and exact, so an edit shows on the very next poll."""
    root, bdir = obsidian.workspace(cfg), cfg["obsidian"]["project_brain_dir"]
    sig = [_stat_key(obsidian.status_path(cfg))]
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return None
    for e in entries:
        if e.name.startswith((".", "_")):
            continue
        try:
            if not e.is_dir():
                continue
            files = sorted((f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in os.scandir(os.path.join(e.path, bdir))
                           if f.name.endswith(".md"))
        except OSError:
            continue
        sig.append((e.name, files))
    return sig


def _brain(cfg):
    key = (str(obsidian.workspace(cfg)), str(obsidian.status_path(cfg)))
    sig = _brain_signature(cfg)
    with _lock:
        hit = _brain_cache.get(key)
        if sig is not None and hit and hit[0] == sig:
            return hit
    projects, rows = obsidian.list_projects(cfg), obsidian.status_rows(cfg)
    entry = (sig, projects, rows)
    with _lock:
        _brain_cache[key] = entry
    return entry


def _projects(cfg):
    return _brain(cfg)[1]


def _status_rows(cfg):
    return _brain(cfg)[2]


def tool_status(cfg, store):
    """Per lane tool: can it take work now, and which tier models can. Feeds the Fleet graph and sidebar."""
    out = {}
    for tool in cfg.get("lanes", {}) or ("agy", "claude"):
        if tool not in cfg["tools"]:
            continue
        ok, why = dispatch.tool_available(tool, cfg, store=store, model="*")
        tiers = {}
        for tier, model in (cfg["tools"][tool].get("models") or {}).items():
            m_ok, m_why = fallback.is_available(tool, model, cfg, store=store)
            q = store.get_quota(tool, model) or {}
            tiers[tier] = {"model": model, "available": m_ok, "reason": m_why,
                           "until": q.get("until") if not m_ok else None}
        tq = store.get_quota(tool, "*") or {}
        out[tool] = {"available": ok and any(t["available"] for t in tiers.values()), "reason": why,
                     "until": tq.get("until") if not ok else None, "tiers": tiers,
                     "capacity": int((cfg.get("lanes") or {}).get(tool, 1))}
    return out


def spare_model(tool, cfg):
    """The first model_preference family no tier uses (agy: Gemini 4 Argon, which has its own quota bucket and takes
    over when the shared Gemini bucket is empty), as {model, levels}; None when there is none."""
    t = cfg["tools"].get(tool) or {}
    fams = {model_family(m) for m in (t.get("models") or {}).values()}
    for tmpl in t.get("model_preference") or []:
        fam = model_family(str(tmpl).replace("-{effort}", "").replace("{effort}", ""))
        if fam and fam not in fams:
            levels = model_efforts(tool, fam + "-medium", cfg)
            return {"model": f"{fam}-{levels[-1]}" if levels else fam, "family": fam, "levels": levels}
    return None


def state(cfg, store, tasks=None, sched=None):
    # effort is routed per task, then fitted to the model (hub/router.py fit_effort): the graph shows each tier
    # model's own effort levels and its family, and the job sits under the effort it actually runs at
    tools = {k: {"models": v["models"], "efforts": v.get("efforts", {}), "effort_in_model": bool(v.get("effort_in_model")),
                 "effort_levels": {t: model_efforts(k, m, cfg) for t, m in v["models"].items()},
                 "families": {t: model_family(m) for t, m in v["models"].items()},
                 "spare": spare_model(k, cfg)} for k, v in cfg["tools"].items()}
    agents = store.all()
    _forget_gone({a.get("id") for a in agents})
    term = cfg.get("_terminal")
    now = time.time()
    for a in agents:
        if a.get("source") == "launcher":
            _sync_progress(a, store)
        elif a.get("status") in launcher.LIVE:
            last_event = a.get("updated") or a.get("started") or a.get("created") or 0
            if now - last_event > 7200:
                a["status"] = "stale"
        if a.get("source") == "hook" and a.get("tool") == "claude" and not a.get("model") and a.get("status") in launcher.LIVE:
            m = transcript_model(str(a.get("id", "")).split("-", 1)[-1])
            if m:
                tier = tier_of_model(m, cfg, effort=a.get("effort") or "")
                store.upsert(a["id"], model=m, tier=tier, updated=a.get("updated"))
                a.update(model=m, tier=tier)
        if a.get("source") == "launcher" and a.get("tool") == "claude" and a.get("session_id") and a.get("status") in launcher.LIVE \
                and launcher.is_live(a.get("id"), store):   # a /model switch in the pane: Claude hooks carry no model, the transcript does
            m = transcript_model(str(a["session_id"]))
            if m and m != a.get("model"):
                tier = tier_of_model(m, cfg, effort=a.get("effort") or "")
                store.upsert(a["id"], model=m, tier=tier, updated=a.get("updated"))
                a.update(model=m, tier=tier)
        a["pane"] = bool(term and a.get("source") == "launcher" and term.has_pane(a.get("id")))
        if a.get("log_path"):
            a["log_tail"] = _log_tail(a, tasks)
        if not a.get("title"):   # rows from before titles existed
            a["title"] = make_title(a.get("task") or "")
            a["summary"] = make_summary(a.get("task") or "", a["title"])
        if a.get("status") in launcher.LIVE:
            kind, _, why = launcher.stop_plan(a)
            a["stoppable"], a["stop_kind"], a["stop_reason"] = kind is not None, kind, why
        else:
            a["stoppable"], a["stop_kind"], a["stop_reason"] = False, None, ""
    # Claude Code subagents ride along under their parent session (read from its transcript folder, never stored)
    agents = agents + [s for a in agents if a.get("source") == "hook" and a.get("tool") == "claude"
                       for s in subagents.for_session(a, lambda m: tier_of_model(m, cfg), now)]
    return {
        "agents": agents,
        "projects": [{**p, "flow": projmap.flow_mtime(Path(p["path"]), cfg), "map": mapwatch.status(p["name"])}
                     for p in _projects(cfg)],
        "maps": {"auto_update": mapwatch.settings(cfg)["auto_update"]},
        "voice": {"enabled": bool(voice.get_voice_cfg(cfg)["enabled"]), "tldr": bool(voice.get_voice_cfg(cfg)["tldr"]),
                  "report_volume": int(voice.get_voice_cfg(cfg)["report_volume"]),
                  "duck_volume": int(voice.get_voice_cfg(cfg)["duck_volume"])},
        "push": push.status(cfg),
        "report": dict(batch.LAST),   # the latest combined report (hub/batch.py)
        "coordination": {"claims": coord.active(store), "messages": coord.messages(store, limit=20)},
        "status_rows": _status_rows(cfg),
        "brain": {
            "ok": obsidian.brain_ok(cfg), "workspace": str(obsidian.workspace(cfg)),
            "global_ok": obsidian.global_ok(cfg), "global_brain": str(obsidian.global_brain(cfg)),
            "status_file": str(obsidian.status_path(cfg)),
            "status_ok": obsidian.status_path(cfg).is_file(),
        },
        "tools": tools, "default_tool": cfg["default_tool"], "tiers": TIERS,
        "now": time.time(),
        "queue": queue_api.queue_state(cfg, tasks, sched) if tasks else None,
        "terminal": term.status() if term else None,
        "quota": store.get_all_quota(cfg),
        "fallback": cfg.get("fallback", {"enabled": True, "never_fallback_to_opus": True, "max_hops": 2}),
        "tool_status": tool_status(cfg, store),
        "dispatch": {lvl: {"tool": e.get("tool"), "model": e.get("model") or ""} for lvl, e in cfg.get("dispatch", {}).items()},
        "lanes_cfg": cfg.get("lanes", {}),
    }


def _tail_bytes(path, n):
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(max(0, size - n))
        return f.read()


def _read_log_bytes(a, tasks=None):
    """Read log tail with retry on transient flush/missing states, falling back to plain text."""
    path = Path(a["log_path"])
    for attempt in range(5):
        try:
            if path.exists():
                raw = _tail_bytes(path, 20000)
                if raw.strip():
                    return raw
        except OSError:
            pass
        if attempt < 4:
            time.sleep(0.05)
    if path.exists():
        return b"(Log is currently empty or flushing...)"
    if a.get("status") in ("running", "queued", "waiting"):
        return b"(Log is currently empty or flushing...)"
    lines = [
        f"$ {a.get('tool', 'agent')} --model {a.get('model', '')}  (tier: {a.get('tier', '')})",
        "",
        f"Task: {a.get('task', '')}",
        f"Status: {a.get('status', '')} ({a.get('activity', '')})",
    ]
    if tasks:
        try:
            t = next((x for x in tasks.list() if x.get("run_id") == a.get("id")), None)
            if t and t.get("summary"):
                lines += ["", t["summary"]]
        except Exception:
            pass
    lines += ["", "(Log is currently empty or flushing...)"]
    return "\n".join(lines).encode("utf-8")
