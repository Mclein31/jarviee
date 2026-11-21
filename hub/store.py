import json
import re
import sqlite3
import threading
import time

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  source TEXT,          -- launcher | hook
  tool TEXT,
  task TEXT,
  project TEXT,
  tier TEXT,
  suggested_tier TEXT,
  model TEXT,
  effort TEXT,
  reasons TEXT,
  status TEXT,          -- queued | running | waiting | done | failed | stopped
  activity TEXT,
  workspace TEXT,
  pid INTEGER,
  log_path TEXT,
  steps INTEGER DEFAULT 0,
  started REAL,
  updated REAL,
  ended REAL,
  title TEXT,           -- short one-line title (hub/titles.py)
  summary TEXT,         -- optional muted second line
  agent_pid INTEGER,    -- verified agent process for hook sessions (claude / agy CLI)
  agent_host TEXT,      -- 'antigravity-app' when the session lives inside the app
  cleared INTEGER DEFAULT 0, -- hidden by "Clear finished", kept for today's stats
  pane_id TEXT          -- iTerm2 session id when the run opened in the Jarviee window
);

CREATE TABLE IF NOT EXISTS check_trust (   -- the one .brain/CHECK_COMMAND text the owner confirmed per project
  project TEXT PRIMARY KEY,
  command TEXT,
  at REAL
);
CREATE TABLE IF NOT EXISTS quota_state (
  tool TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,     -- ok | exhausted | transient
  until REAL,               -- timestamp when cooldown expires
  last_error TEXT,
  updated REAL,
  PRIMARY KEY (tool, model)
);
"""
NEW_COLUMNS = {"effort": "TEXT", "title": "TEXT", "summary": "TEXT", "agent_pid": "INTEGER",
               "agent_host": "TEXT", "cleared": "INTEGER DEFAULT 0", "pane_id": "TEXT",
               "parent_run_id": "TEXT", "fallback_from": "TEXT", "hops": "INTEGER DEFAULT 0",
               "files": "TEXT", "verified": "INTEGER DEFAULT 0", "base_head": "TEXT", "session_id": "TEXT",
               "closed_by_user": "INTEGER DEFAULT 0",
               "interactive": "INTEGER DEFAULT 0",   # a live CLI session in a pane (launcher.is_live)   # force-closed: never revived or restarted except by hand
               "pid_start": "TEXT",   # `ps -o lstart=` of `pid` at launch: tells a recycled pid from the run (hub/orphans.py)
               "job_id": "TEXT", "job_no": "INTEGER DEFAULT 0", "job_log_offset": "INTEGER DEFAULT 0",
               # review inbox (hub/inbox.py): reviewed 0 waiting | 1 accepted | 2 dismissed | 3 reverted
               "reviewed": "INTEGER DEFAULT 0", "reviewed_at": "REAL", "revert_sha": "TEXT",
               "check_status": "TEXT", "check_output": "TEXT", "check_at": "REAL",
               # opt-in worktree run (hub/worktrees.py): folder, branch, bring-back outcome and the reason
               "worktree_path": "TEXT", "worktree_branch": "TEXT", "worktree_result": "TEXT", "worktree_note": "TEXT"}   # files: JSON list the run changed (hub/snapshot.py)


# Indexes for the queries that actually run: the dashboard list (not cleared, newest first) and usage history.
# Created after the column migration, so an existing database gets them in place.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_agents_list ON agents(COALESCE(cleared, 0), updated DESC);
CREATE INDEX IF NOT EXISTS idx_agents_ended ON agents(ended);
CREATE INDEX IF NOT EXISTS idx_agents_updated ON agents(updated);
CREATE INDEX IF NOT EXISTS idx_agents_started ON agents(started);
"""
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with _lock:
            self.db.execute("PRAGMA journal_mode=WAL;")
            self.db.execute("PRAGMA busy_timeout=5000;")
            self.db.execute("PRAGMA synchronous=NORMAL;")   # WAL: a commit no longer waits for an fsync
            self.db.executescript(SCHEMA)
            from .coord import SCHEMA as COORD_SCHEMA   # claims + agent messages (hub/coord.py)
            self.db.executescript(COORD_SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(agents)").fetchall()}
            for col, kind in NEW_COLUMNS.items():
                if col not in cols:
                    self.db.execute(f"ALTER TABLE agents ADD COLUMN {col} {kind}")
            # 2026-11: three tiers became four levels; 'standard' is now 'light'.
            self.db.execute("UPDATE agents SET tier='light' WHERE tier='standard'")
            self.db.execute("UPDATE agents SET suggested_tier='light' WHERE suggested_tier='standard'")
            self.db.executescript(INDEXES)
            self.db.commit()

    def version(self):
        """Changes whenever any row changes on this connection (agents, tasks, claims, spans, quota...)."""
        return self.db.total_changes

    def close(self):
        with _lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    def __del__(self):
        try:
            self.db.close()
        except Exception:
            pass

    def upsert(self, id, **fields):
        fields.setdefault("updated", time.time())
        if isinstance(fields.get("reasons"), (list, dict)):
            fields["reasons"] = json.dumps(fields["reasons"])
        if isinstance(fields.get("files"), (list, tuple)):
            fields["files"] = json.dumps(list(fields["files"]))
        if fields.get("status") in ("running", "waiting", "queued"):
            fields["cleared"] = 0   # a cleared session that comes back to life is listed again
        if not all(_COLUMN_RE.match(k) for k in fields):
            raise ValueError("bad column name")
        # one statement: insert, or update only the given fields (the started default applies to inserts only)
        insert = {"started": time.time(), **fields}
        cols = ", ".join(["id", *insert])
        qs = ", ".join("?" * (len(insert) + 1))
        sets = ", ".join(f"{k}=excluded.{k}" for k in fields)
        with _lock:
            self.db.execute(f"INSERT INTO agents ({cols}) VALUES ({qs}) ON CONFLICT(id) DO UPDATE SET {sets}",
                            (id, *insert.values()))
            self.db.commit()

    def trusted_check(self, project):
        with _lock:
            r = self.db.execute("SELECT command FROM check_trust WHERE project=?", (project or "",)).fetchone()
        return r[0] if r else None

    def trust_check(self, project, command):
        with _lock:
            self.db.execute("INSERT INTO check_trust (project, command, at) VALUES (?,?,?) "
                            "ON CONFLICT(project) DO UPDATE SET command=excluded.command, at=excluded.at",
                            (project or "", command, time.time()))
            self.db.commit()

    def bump_steps(self, id):
        with _lock:
            self.db.execute("UPDATE agents SET steps=COALESCE(steps,0)+1 WHERE id=?", (id,))
            self.db.commit()

    def get(self, id):
        with _lock:
            r = self.db.execute("SELECT * FROM agents WHERE id=?", (id,)).fetchone()
        return self._row(r) if r else None

    def all(self, limit=200):
        """Rows the dashboard lists: everything except finished runs the owner cleared."""
        with _lock:
            rows = self.db.execute("SELECT * FROM agents WHERE COALESCE(cleared,0)=0 "
                                   "ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def finished_launched(self, limit=400):
        """Finished launched runs with a recorded file list, cleared ones included: the review inbox's candidates."""
        with _lock:
            rows = self.db.execute("SELECT * FROM agents WHERE source='launcher' AND status IN ('done','failed') "
                                   "AND COALESCE(files,'') NOT IN ('', '[]') AND COALESCE(reviewed,0)=0 "
                                   "ORDER BY COALESCE(ended, updated) DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def history(self, since):
        """Every row touched since `since` (epoch seconds), cleared ones included, for usage stats."""
        with _lock:
            rows = self.db.execute(   # COALESCE(ended, updated, started) >= ? written so the indexes apply
                "SELECT * FROM agents WHERE ended >= ? OR (ended IS NULL AND updated >= ?) "
                "OR (ended IS NULL AND updated IS NULL AND started >= ?) OR started >= ?",
                (since, since, since, since)).fetchall()
        return [self._row(r) for r in rows]

    def clear_finished(self):
        """Hide finished runs from the lists. Rows older than 8 days are deleted outright."""
        with _lock:
            self.db.execute("UPDATE agents SET cleared=1 WHERE status IN ('done','failed','stopped')")
            self.db.execute("DELETE FROM agents WHERE status IN ('done','failed','stopped') "
                            "AND COALESCE(ended, updated) < ?", (time.time() - 8 * 86400,))
            self.db.commit()

    # ---- quota_state circuit breaker ----
    def set_quota(self, tool, model, status, until=None, last_error=""):
        now = time.time()
        with _lock:
            self.db.execute(
                "INSERT INTO quota_state (tool, model, status, until, last_error, updated) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tool, model) DO UPDATE SET "
                "status=excluded.status, until=excluded.until, last_error=excluded.last_error, updated=excluded.updated",
                (tool, model or "*", status, until, str(last_error or ""), now)
            )
            self.db.commit()

    def set_tool_quota(self, tool, status, until=None, last_error="", cfg=None):
        """Mark an entire tool exhausted/transient across all its configured models."""
        self.set_quota(tool, "*", status, until, last_error)
        if cfg and tool in cfg.get("tools", {}):
            for m in cfg["tools"][tool].get("models", {}).values():
                self.set_quota(tool, m, status, until, last_error)

    def get_quota(self, tool, model=None, now=None):
        """Return dict with {tool, model, status, until, last_error, updated}. Auto-restores to 'ok' when expired."""
        ref = now if now is not None else time.time()
        with _lock:
            # Check model-specific row first, then tool-wide wildcard '*'
            r = None
            if model and model != "*":
                r = self.db.execute("SELECT * FROM quota_state WHERE tool=? AND model=?", (tool, model)).fetchone()
            if not r:
                r = self.db.execute("SELECT * FROM quota_state WHERE tool=? AND model='*'", (tool,)).fetchone()
        if not r:
            return {"tool": tool, "model": model or "*", "status": "ok", "until": None, "last_error": "", "updated": 0}
        d = dict(r)
        if d.get("until") and d["until"] <= ref and d["status"] != "ok":
            self.set_quota(tool, d["model"], "ok", None, "")
            d["status"] = "ok"
            d["until"] = None
        return d

    def get_all_quota(self, cfg=None, now=None):
        """Return all quota rows, auto-restoring expired ones, ensuring all configured tool models exist."""
        ref = now if now is not None else time.time()
        with _lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM quota_state").fetchall()]
        out = {}
        for r in rows:
            if r.get("until") and r["until"] <= ref and r["status"] != "ok":
                self.set_quota(r["tool"], r["model"], "ok", None, "")
                r["status"] = "ok"
                r["until"] = None
            key = f"{r['tool']}:{r['model']}"
            out[key] = r

        # Ensure tools/models from config are represented
        if cfg and "tools" in cfg:
            for t_name, t_info in cfg["tools"].items():
                for m_slug in t_info.get("models", {}).values():
                    k = f"{t_name}:{m_slug}"
                    if k not in out:
                        # Check wildcard
                        wc = out.get(f"{t_name}:*")
                        if wc and wc["status"] != "ok":
                            out[k] = {"tool": t_name, "model": m_slug, "status": wc["status"],
                                      "until": wc["until"], "last_error": wc["last_error"], "updated": wc["updated"]}
                        else:
                            out[k] = {"tool": t_name, "model": m_slug, "status": "ok", "until": None, "last_error": "", "updated": 0}
        return out

    def clear_quota(self, tool, model=None):
        """Reset quota status to 'ok'."""
        with _lock:
            if model and model != "*":
                self.db.execute("UPDATE quota_state SET status='ok', until=NULL, last_error='' WHERE tool=? AND model=?", (tool, model))
            else:
                self.db.execute("UPDATE quota_state SET status='ok', until=NULL, last_error='' WHERE tool=?", (tool,))
            self.db.commit()

    @staticmethod
    def _row(r):
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"]) if d.get("reasons") else []
        try:
            d["files"] = json.loads(d["files"]) if d.get("files") else []
        except ValueError:
            d["files"] = []
        return d
