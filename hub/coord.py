"""Agent coordination: claims and a message channel in SQLite, so agents don't overwrite each other.

Every agent registers its task and the files it plans to touch before it starts (launcher.launch for runs Jarviee
starts; the first prompt of a job for terminal sessions you started yourself). An overlap with another active agent
in the same workspace is resolved one of three ways:

  wait   it shares files with an active agent, or is nearly the same task: the new run waits until that agent
         releases its claim, then starts with that agent's result handed to it (sequential + hand-off).
  split  a similar task on other files: both run, and the new one is told which files the other has claimed.
  go     no overlap.

Terminal sessions you started are never held; they get the same information as a warning instead.

The channel: each agent's claim, changed files (from Edit/Write hook events) and finish are posted as messages.
Claude agents read new messages from other agents through their hook reply (additionalContext); agy only gets them
in its launch prompt. Claims are released when the job finishes, fails or is closed.
"""
import json
import os
import re
import time

from .store import _lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  agent_id TEXT PRIMARY KEY,
  workspace TEXT,       -- resolved working directory: overlaps only count inside the same one
  task TEXT,
  files TEXT,           -- JSON list of paths relative to the workspace (a trailing / claims a folder)
  status TEXT,          -- active | released
  outcome TEXT,         -- done | failed | stopped | ... once released
  created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS agent_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  sender TEXT,          -- agent id ('jarviee' for the hub itself)
  recipient TEXT,       -- NULL: every agent in the workspace
  workspace TEXT,
  kind TEXT,            -- claim | change | finish | wait | split | note
  body TEXT
);
CREATE TABLE IF NOT EXISTS agent_inbox (
  agent_id TEXT PRIMARY KEY,
  last_id INTEGER
);
"""
MAX_NAMED_FILES = 8      # a prompt naming more files than this is listing context (a planner, a repo map), not a plan
WAIT_SIMILARITY = 0.6    # this close to an active task: it waits and gets a hand-off
SPLIT_SIMILARITY = 0.3   # this close: both run, split by files
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "replace_file_content", "write_to_file",
              "edit_file", "create_file", "multi_replace_file_content"}
_STOP = set("""about after again also before being between both could does doing done each from have here into just
like make more most much only other over please same should some such than that them then there these they this
those through under very want what when where which while will with would your task tasks file files code""".split())
PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}|(?:[\w.-]+/)+)(?![\w/])")


def _ws(path):
    try:
        return os.path.realpath(os.path.expanduser(str(path or ""))) if path else ""
    except (OSError, ValueError):
        return str(path or "")


def files_in(text, workspace):
    """Paths named in a task that exist in the workspace, relative to it ('hub/server.py', 'hub/static/')."""
    ws = _ws(workspace)
    if not ws or not text:
        return []
    out = []
    for m in PATH_RE.finditer(str(text)):
        rel = m.group(1).strip("./") if not m.group(1).startswith("../") else ""
        if not rel or rel in out:
            continue
        full = os.path.join(ws, rel)
        if os.path.isfile(full) or os.path.isdir(full):
            out.append(rel + ("/" if os.path.isdir(full) and not rel.endswith("/") else ""))
    # Claiming a whole listing would make every other run wait; files it really changes are claimed as it edits
    return out if len(out) <= MAX_NAMED_FILES else []


def rel_path(path, workspace):
    ws = _ws(workspace)
    p = _ws(path)
    if ws and p.startswith(ws + os.sep):
        return p[len(ws) + 1:]
    return str(path or "")


def overlap(a, b):
    """Paths two claims share; a folder claim covers everything under it."""
    out = set()
    for x in a or []:
        for y in b or []:
            xs, ys = x.rstrip("/"), y.rstrip("/")
            if xs == ys or xs.startswith(ys + "/") or ys.startswith(xs + "/"):
                out.add(x)
    return out


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]{4,}", str(text or "").lower()) if w not in _STOP}


def similarity(a, b):
    """Word overlap (Jaccard) of two task texts, 0..1."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _claim_row(r):
    d = dict(r)
    d["files"] = json.loads(d["files"]) if d.get("files") else []
    return d


def active(store, workspace=None):
    with _lock:
        if workspace is None:
            rows = store.db.execute("SELECT * FROM claims WHERE status='active'").fetchall()
        else:
            rows = store.db.execute("SELECT * FROM claims WHERE status='active' AND workspace=?",
                                    (_ws(workspace),)).fetchall()
    return [_claim_row(r) for r in rows]


def get(store, agent_id):
    with _lock:
        r = store.db.execute("SELECT * FROM claims WHERE agent_id=?", (agent_id,)).fetchone()
    return _claim_row(r) if r else None


def post(store, sender, kind, body, workspace="", recipient=None):
    with _lock:
        cur = store.db.execute("INSERT INTO agent_messages (ts, sender, recipient, workspace, kind, body) "
                               "VALUES (?,?,?,?,?,?)", (time.time(), sender, recipient, _ws(workspace), kind,
                                                        str(body)[:1000]))
        store.db.commit()
        return cur.lastrowid


def messages(store, workspace=None, limit=50):
    """The channel, newest last (dashboard / debugging)."""
    with _lock:
        if workspace is None:
            rows = store.db.execute("SELECT * FROM agent_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = store.db.execute("SELECT * FROM agent_messages WHERE workspace=? ORDER BY id DESC LIMIT ?",
                                    (_ws(workspace), limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def inbox(store, agent_id, workspace, mark_read=True):
    """Messages from other agents in this workspace (or to this agent) that it hasn't read yet."""
    with _lock:
        r = store.db.execute("SELECT last_id FROM agent_inbox WHERE agent_id=?", (agent_id,)).fetchone()
        if r is None:   # a new reader starts from now: the channel's history isn't news to it
            top = store.db.execute("SELECT COALESCE(MAX(id), 0) FROM agent_messages").fetchone()[0]
            last = top
            store.db.execute("INSERT OR REPLACE INTO agent_inbox (agent_id, last_id) VALUES (?, ?)", (agent_id, top))
            store.db.commit()
            return []
        last = r[0] or 0
        rows = store.db.execute(
            "SELECT * FROM agent_messages WHERE id>? AND sender!=? AND "
            "((recipient IS NULL AND workspace=?) OR recipient=?) ORDER BY id", (last, agent_id, _ws(workspace),
                                                                                agent_id)).fetchall()
        top = store.db.execute("SELECT COALESCE(MAX(id), 0) FROM agent_messages").fetchone()[0]
        if mark_read:
            store.db.execute("INSERT OR REPLACE INTO agent_inbox (agent_id, last_id) VALUES (?, ?)",
                             (agent_id, max(top, last)))
            store.db.commit()
    return [dict(x) for x in rows]


def register(store, agent_id, workspace, task, files=(), title=""):
    """Claim files for an agent's job and decide how it coordinates with the active agents in its workspace.

    Returns {"decision": "go"|"split"|"wait", "with": [claims], "files": [...], "note": str}.
    """
    ws = _ws(workspace)
    files = sorted({f for f in (files or []) if f})
    now = time.time()
    others = [c for c in active(store, ws) if c["agent_id"] != agent_id]
    wait_on, split_with = [], []
    for c in others:
        shared = overlap(files, c["files"])
        sim = similarity(task, c["task"])
        if shared or sim >= WAIT_SIMILARITY:
            wait_on.append(dict(c, shared=sorted(shared), similarity=round(sim, 2)))
        elif sim >= SPLIT_SIMILARITY:
            split_with.append(dict(c, shared=[], similarity=round(sim, 2)))
    with _lock:
        store.db.execute("INSERT OR REPLACE INTO claims (agent_id, workspace, task, files, status, outcome, created, "
                         "updated) VALUES (?,?,?,?, 'active', NULL, ?, ?)",
                         (agent_id, ws, str(task)[:2000], json.dumps(files), now, now))
        # its inbox starts here, so the wait/split notes posted below about it are delivered
        store.db.execute("INSERT OR IGNORE INTO agent_inbox (agent_id, last_id) "
                         "SELECT ?, COALESCE(MAX(id), 0) FROM agent_messages", (agent_id,))
        store.db.commit()
    label = (title or str(task)).strip().splitlines()[0][:80] if (title or task) else agent_id
    post(store, agent_id, "claim", f"{agent_id} started '{label}'" + (f"; it claims {', '.join(files)}" if files else ""),
         ws)
    if wait_on:
        decision, note = "wait", wait_note(wait_on)
        post(store, "jarviee", "wait", f"{agent_id} waits for {', '.join(c['agent_id'] for c in wait_on)}: {note}", ws)
    elif split_with:
        decision, note = "split", split_note(split_with)
        post(store, "jarviee", "split", f"{agent_id} and {', '.join(c['agent_id'] for c in split_with)} split "
                                        f"the work by files", ws)
    else:
        decision, note = "go", ""
    return {"decision": decision, "with": wait_on or split_with, "files": files, "note": note}


def wait_note(claims):
    parts = []
    for c in claims:
        why = f"shares {', '.join(c['shared'])}" if c.get("shared") else "is doing nearly the same task"
        parts.append(f"{c['agent_id']} {why}")
    return "; ".join(parts)


def split_note(claims):
    lines = ["Coordination: other agents are working on related tasks in this project right now. Split the work: "
             "do only your part and do not change the files they claimed."]
    for c in claims:
        files = ", ".join(c["files"]) or "no files claimed yet"
        lines.append(f"- {c['agent_id']}: '{c['task'][:160]}' (claimed: {files})")
    return "\n".join(lines)


def handoff_note(store, agent_ids):
    """For a run that waited: what the agents it waited on did, from their finish messages."""
    lines = []
    for aid in agent_ids:
        with _lock:
            r = store.db.execute("SELECT body FROM agent_messages WHERE sender=? AND kind='finish' "
                                 "ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
        if r:
            lines.append(f"- {r[0]}")
    if not lines:
        return ""
    return ("Hand-off: you waited for overlapping work by other agents, which has now finished. Check the current "
            "state of the files (git status/diff) and continue from it; don't redo or undo their work.\n"
            + "\n".join(lines))


def still_blocking(store, agent_ids):
    """The agents in `agent_ids` that still hold an active claim."""
    live = {c["agent_id"] for c in active(store)}
    return [a for a in agent_ids if a in live]


def touch(store, agent_id, path, workspace=""):
    """An agent changed a file: add it to its claim and tell the others (once per file)."""
    c = get(store, agent_id)
    ws = (c or {}).get("workspace") or _ws(workspace)
    rel = rel_path(path, ws)
    if not rel:
        return False
    if c and c["status"] == "active":
        if rel in c["files"]:
            return False
        files = sorted(set(c["files"]) | {rel})
        with _lock:
            store.db.execute("UPDATE claims SET files=?, updated=? WHERE agent_id=?",
                             (json.dumps(files), time.time(), agent_id))
            store.db.commit()
    elif c is None:   # an agent that never registered (e.g. a session from before this feature): claim now
        with _lock:
            store.db.execute("INSERT OR REPLACE INTO claims (agent_id, workspace, task, files, status, created, "
                             "updated) VALUES (?,?,?,?, 'active', ?, ?)",
                             (agent_id, ws, "", json.dumps([rel]), time.time(), time.time()))
            store.db.commit()
    else:
        return False
    post(store, agent_id, "change", f"{agent_id} is changing {rel}", ws)
    return True


def release(store, agent_id, outcome="done", summary=""):
    """Drop an agent's claims and announce it finished. Idempotent."""
    c = get(store, agent_id)
    if not c or c["status"] != "active":
        return False
    with _lock:
        store.db.execute("UPDATE claims SET status='released', outcome=?, updated=? WHERE agent_id=?",
                         (outcome, time.time(), agent_id))
        store.db.commit()
    files = f"; files: {', '.join(c['files'])}" if c["files"] else ""
    what = f": {summary.strip()[:300]}" if summary and summary.strip() else ""
    post(store, agent_id, "finish", f"{agent_id} {outcome} '{c['task'][:120]}'{files}{what}", c["workspace"])
    return True


def sweep(store):
    """Release claims whose agent is no longer running (a crash, a restart, a row cleared by hand)."""
    n = 0
    for c in active(store):
        row = store.get(c["agent_id"])
        if row is None or row.get("status") not in ("running", "waiting", "queued"):
            n += release(store, c["agent_id"], (row or {}).get("status") or "lost")
    return n


def context_for(store, agent_id, workspace, limit=1500):
    """New messages for an agent as one block of text for its hook reply, or ''."""
    msgs = inbox(store, agent_id, workspace)
    if not msgs:
        return ""
    lines = ["[Jarviee] Messages from other agents working in this project:"]
    lines += [f"- {m['body']}" for m in msgs[-12:]]
    text = "\n".join(lines)
    return text[:limit]
