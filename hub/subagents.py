"""Claude Code subagents of a hook session, read from the session's transcript folder.

Claude Code keeps each subagent next to its parent's transcript:
  ~/.claude/projects/<slug>/<session_id>/subagents/agent-<id>.meta.json   {agentType, description, model, finished, ...}
  ~/.claude/projects/<slug>/<session_id>/subagents/agent-<id>.jsonl       the subagent's own transcript
Jarviee shows them as extra rows (source "subagent", parent = the session's agent id) in /api/state and as tasks in
the session's workflow. Nothing is stored: rows are rebuilt from these files. Read-only; never raises.
"""
import json
import re
import threading
import time
from datetime import datetime

from . import runcost, usage

STALE_S = 600            # an unfinished subagent with no transcript write for this long is treated as stopped
TTL_S = 3.0
AGENT_RE = re.compile(r"^agent-([A-Za-z0-9_-]{4,64})\.meta\.json$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
SUB_ID_RE = re.compile(r"^sub-[A-Za-z0-9_-]{4,64}$")
TOOL_RE = re.compile(rb'"type":\s*"tool_use",\s*"id":\s*"[^"]{1,80}",\s*"name":\s*"([A-Za-z0-9_:.-]{1,60})"')
_cache, _dirs, _parent = {}, {}, {}
# SubagentStart / SubagentStop hook events, kept in memory per parent session row: {parent_id: {agent_id: {...}}}.
# They make a subagent's state exact; without one (older sessions, a hub restart) the transcript files decide.
_events = {}
_events_lock = threading.Lock()   # hook threads add events while the state build iterates them
MAX_EVENT_SESSIONS = 100


def _norm_id(agent_id):
    s = str(agent_id or "").strip()
    return s[len("agent-"):] if s.startswith("agent-") else s


def _sid_of(parent_id):
    return parent_id[len("claude-"):] if str(parent_id).startswith("claude-") else ""


def note_event(parent_id, event, data, now=None):
    """Record a SubagentStart / SubagentStop hook event for a session row. Never raises."""
    try:
        aid = _norm_id(data.get("agent_id"))
        if not parent_id or not AGENT_ID_RE.match(aid):
            return
        now = now or time.time()
        with _events_lock:
            if parent_id not in _events and len(_events) >= MAX_EVENT_SESSIONS:
                _events.pop(next(iter(_events)))
            subs = _events.setdefault(parent_id, {})
            e = subs.setdefault(aid, {"type": "", "started": None, "stopped": None, "message": ""})
            e["type"] = str(data.get("agent_type") or e["type"])[:60]
            if event == "SubagentStart":
                e["started"], e["stopped"] = now, None   # a resumed subagent starts again
            elif event == "SubagentStop":
                e["stopped"] = now
                e["message"] = " ".join(str(data.get("last_assistant_message") or "").split())[:200]
                e["started"] = e["started"] or now
        _cache.pop(_sid_of(parent_id), None)
    except Exception:
        pass


def session_of(row):
    """The Claude session id behind a hook row, or ''."""
    aid = str((row or {}).get("id") or "")
    if (row or {}).get("source") != "hook" or not aid.startswith("claude-"):
        return ""
    sid = aid[len("claude-"):]
    return sid if runcost.SESSION_RE.match(sid) else ""


def _dir(sid):
    hit = _dirs.get(sid)
    if hit and hit[0] > time.time():
        return hit[1]
    found = next(iter(sorted(usage.CLAUDE_PROJECTS.glob(f"*/{sid}/subagents"))), None)
    _dirs[sid] = (time.time() + (300 if found else 20), found)   # look again soon while a session has none yet
    return found


def _started(jl, fallback):
    try:
        with open(jl, "rb") as f:
            first = json.loads(f.readline() or b"{}")
        ts = str(first.get("timestamp") or "")
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else fallback
    except (OSError, ValueError, TypeError):
        return fallback


def _last_tool(jl, size):
    try:
        with open(jl, "rb") as f:
            f.seek(max(0, size - 32768))
            hits = TOOL_RE.findall(f.read())
        return hits[-1].decode() if hits else ""
    except OSError:
        return ""


def for_session(row, tier_fn=None, now=None):
    """Subagent rows for one hook session row, oldest first ([] when it has none)."""
    sid = session_of(row)
    if not sid:
        return []
    now = now or time.time()
    hit = _cache.get(sid)
    if hit and hit[0] > now and hit[2] == row.get("status"):
        return hit[1]
    rows = []
    with _events_lock:   # a copy: iterating the live dict while a hook thread adds to it raises
        evs = {k: dict(v) for k, v in (_events.get(row.get("id")) or {}).items()}
    parent_over = row.get("status") in ("done", "failed", "stopped")
    seen = set()
    d = _dir(sid)
    try:
        metas = sorted(d.iterdir()) if d else []
    except OSError:
        metas = []
    for p in metas:
        m = AGENT_RE.match(p.name)
        if not m:
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                continue
        except (OSError, ValueError):
            continue
        aid, jl = m.group(1), p.with_name(f"agent-{m.group(1)}.jsonl")
        seen.add(aid)
        try:
            st = jl.stat()
            updated, size = st.st_mtime, st.st_size
        except OSError:
            updated, size = p.stat().st_mtime, 0
        ev = evs.get(aid)
        if ev and ev["stopped"]:       # exact: the hook said it stopped
            finished, status = True, "done"
            updated = max(updated, ev["stopped"]) if size else ev["stopped"]
        elif ev and ev["started"] and not parent_over:   # exact: started, no stop yet, session still alive
            finished, status = False, "running"
        else:                          # no event seen: infer from the transcript files
            finished = bool(meta.get("finished"))
            status = "done" if finished else ("running" if now - updated < STALE_S else "stopped")
        model = str(meta.get("model") or "")
        if not model or model == "inherit":
            model = row.get("model") or ""
        tool = _last_tool(jl, size) if status == "running" and size else ""
        desc = str(meta.get("description") or meta.get("agentType") or "Subagent")[:200]
        kind = str(meta.get("agentType") or "subagent")[:60]
        sub = {
            "id": f"sub-{aid}", "source": "subagent", "parent": row.get("id"), "parent_title": row.get("title") or "",
            "tool": "claude", "model": model, "tier": (tier_fn(model) if tier_fn else "") or "",
            "status": status, "title": desc, "task": desc, "summary": f"{kind} subagent", "agent_type": kind,
            "background": meta.get("requestShape") == "background",
            "activity": f"using {tool}" if tool else ("finished" if finished else ("working" if status == "running" else "stopped without finishing")),
            "started": _started(jl, updated), "updated": updated, "ended": updated if status != "running" else None,
            "workspace": row.get("workspace") or "", "steps": 0, "stoppable": False, "stop_kind": None,
            "stop_reason": "A subagent runs inside its parent session; stop it from there.",
        }
        rows.append(sub)
        _parent[sub["id"]] = row.get("id")
    for aid, ev in evs.items():    # reported by hooks before (or without) any transcript folder
        if aid in seen or not ev["type"] or not ev["started"]:
            continue
        status = "done" if ev["stopped"] else ("stopped" if parent_over else "running")
        model = row.get("model") or ""
        done_at = ev["stopped"] or ev["started"]
        rows.append({
            "id": f"sub-{aid}", "source": "subagent", "parent": row.get("id"), "parent_title": row.get("title") or "",
            "tool": "claude", "model": model, "tier": (tier_fn(model) if tier_fn else "") or "",
            "status": status, "title": ev["type"], "task": ev["type"], "summary": f"{ev['type']} subagent",
            "agent_type": ev["type"], "background": False,
            "activity": "finished" if ev["stopped"] else ("working" if status == "running" else "stopped without finishing"),
            "started": ev["started"], "updated": done_at, "ended": done_at if status != "running" else None,
            "workspace": row.get("workspace") or "", "steps": 0, "stoppable": False, "stop_kind": None,
            "stop_reason": "A subagent runs inside its parent session; stop it from there."})
        _parent[f"sub-{aid}"] = row.get("id")
    rows.sort(key=lambda r: r["started"] or 0)
    _cache[sid] = (now + TTL_S, rows, row.get("status"))
    return rows


def parent_of(sub_id):
    """The parent session's agent id for a subagent row id seen recently, or ''."""
    return _parent.get(str(sub_id or ""), "") if SUB_ID_RE.match(str(sub_id or "")) else ""
