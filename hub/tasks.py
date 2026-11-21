"""Plans and queued tasks (SQLite), plus per-task lock files in <project>/.brain/.locks/."""
import json
import time
import uuid
from pathlib import Path

from . import obsidian
from .store import _lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  goal TEXT, project TEXT,
  status TEXT,          -- draft | approved | running | done | failed | cancelled
  planner TEXT,         -- which tool/model planned it, or 'mock'
  notes TEXT,           -- JSON list of planner/validation notes
  review TEXT,          -- final review output
  created REAL, updated REAL,
  pipeline TEXT         -- JSON state of an automatic pipeline, NULL for ordinary plans
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  plan_id TEXT, project TEXT, title TEXT, prompt TEXT, acceptance TEXT,
  level TEXT, planner_level TEXT, router_level TEXT,
  tool TEXT, model TEXT, effort TEXT,
  status TEXT,          -- queued | blocked | running | done | failed | escalated | cancelled
  depends_on TEXT,      -- JSON list of task ids
  files TEXT,           -- JSON list of paths
  attempts INTEGER DEFAULT 0,
  escalated INTEGER DEFAULT 0,
  route_note TEXT,      -- fallback / borderline / reroute / escalation reason
  run_id TEXT, summary TEXT,
  created REAL, updated REAL, started REAL, ended REAL,
  phase TEXT DEFAULT '',          -- pipeline phase: research | build | test | review | fix ('' for plain plans)
  read_only INTEGER DEFAULT 0
);
"""
JSON_COLS = ("depends_on", "files")
OPEN = ("queued", "blocked", "running")


class Tasks:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Add the pipeline columns to a database created before they existed (idempotent)."""
        for table, col, ddl in (("plans", "pipeline", "TEXT"), ("tasks", "phase", "TEXT DEFAULT ''"),
                                ("tasks", "read_only", "INTEGER DEFAULT 0"),
                                ("plans", "cleared", "INTEGER DEFAULT 0")):   # hidden by "Clear finished"
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self.db.commit()

    # ---- plans ----
    def add_plan(self, goal, project, tasks, planner="", notes=(), status="draft", pipeline=None):
        pid = "plan-" + uuid.uuid4().hex[:6]
        now = time.time()
        with _lock:
            self.db.execute("INSERT INTO plans (id, goal, project, status, planner, notes, review, created, updated,"
                            " pipeline) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (pid, goal, project, status, planner, json.dumps(list(notes)), "", now, now,
                             json.dumps(pipeline) if pipeline is not None else None))
            for t in tasks:
                self._insert(pid, project, t, now)
            self.db.commit()
        return pid

    def add_tasks(self, pid, project, tasks, status="queued"):
        """Append tasks to an existing plan (ids {pid}.{id}; deps are plan-local ids like add_plan)."""
        now = time.time()
        with _lock:
            for t in tasks:
                self._insert(pid, project, t, now, status)
            self.db.execute("UPDATE plans SET updated=? WHERE id=?", (now, pid))
            self.db.commit()

    def _insert(self, pid, project, t, now, status="draft"):
        tid = f"{pid}.{t['id']}"
        deps = [f"{pid}.{d}" for d in t.get("depends_on", [])]
        self.db.execute(
            "INSERT INTO tasks (id, plan_id, project, title, prompt, acceptance, level, planner_level, router_level,"
            " tool, model, effort, status, depends_on, files, route_note, created, updated, phase, read_only)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, pid, project, t["title"], t["prompt"], t.get("acceptance", ""), t["level"],
             t.get("planner_level", t["level"]), t.get("router_level", ""), t["tool"], t["model"],
             t.get("effort", ""), status, json.dumps(deps), json.dumps(t.get("files", [])),
             t.get("route_note", ""), now, now, t.get("phase", ""), int(bool(t.get("read_only")))))

    def plan(self, pid):
        with _lock:
            r = self.db.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["notes"] = json.loads(d["notes"] or "[]")
        d["pipeline"] = json.loads(d["pipeline"]) if d.get("pipeline") else None
        d["tasks"] = self.list(plan_id=pid)
        return d

    def plans(self, limit=10, visible=False):
        """Newest plans first; visible=True leaves out finished plans the owner cleared (the dashboard lists)."""
        where = ("WHERE COALESCE(cleared, 0)=0 OR status NOT IN ('done', 'failed', 'cancelled') "   # a retried plan comes back
                 if visible else "")
        with _lock:
            rows = self.db.execute(f"SELECT id FROM plans {where}ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [self.plan(r["id"]) for r in rows]

    def clear_finished(self):
        """Hide done, failed and cancelled plans from the dashboard lists; they stay openable by id."""
        with _lock:
            self.db.execute("UPDATE plans SET cleared=1 WHERE status IN ('done', 'failed', 'cancelled')")
            self.db.commit()

    def open_plans(self):
        """Every plan still in flight (approved or running), oldest first, however old."""
        with _lock:
            rows = self.db.execute("SELECT id FROM plans WHERE status IN ('approved', 'running')"
                                   " ORDER BY created").fetchall()
        return [self.plan(r["id"]) for r in rows]

    def plan_state(self, pid):
        """(status, is_pipeline) of a plan, or ('', False) when it does not exist."""
        with _lock:
            r = self.db.execute("SELECT status, pipeline FROM plans WHERE id=?", (pid,)).fetchone()
        return (r["status"], bool(r["pipeline"])) if r else ("", False)

    def set_plan(self, pid, **fields):
        self._set("plans", pid, fields)

    def approve(self, pid):
        """Move a draft plan's tasks into the queue."""
        with _lock:
            self.db.execute("UPDATE tasks SET status='queued', updated=? WHERE plan_id=? AND status='draft'",
                            (time.time(), pid))
            self.db.execute("UPDATE plans SET status='approved', updated=? WHERE id=?", (time.time(), pid))
            self.db.commit()

    def prune_plans(self, max_age_days=7, now=None):
        """Delete finished/failed/cancelled plans (and their tasks) older than max_age_days."""
        cutoff = (now if now is not None else time.time()) - max(1, int(max_age_days)) * 86400
        with _lock:
            rows = self.db.execute(
                "SELECT id FROM plans WHERE status IN ('done', 'failed', 'cancelled')"
                " AND COALESCE(NULLIF(updated, 0), created) < ?",
                (cutoff,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if not ids:
                return []
            ph = ",".join("?" * len(ids))
            self.db.execute(f"DELETE FROM tasks WHERE plan_id IN ({ph})", ids)
            self.db.execute(f"DELETE FROM plans WHERE id IN ({ph})", ids)
            self.db.commit()
        return ids

    # ---- tasks ----
    def get(self, tid):
        with _lock:
            r = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return self._row(r) if r else None

    def list(self, plan_id=None, statuses=None):
        q, args = "SELECT * FROM tasks WHERE 1=1", []
        if plan_id:
            q += " AND plan_id=?"
            args.append(plan_id)
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        with _lock:
            rows = self.db.execute(q + " ORDER BY created, id", args).fetchall()
        return [self._row(r) for r in rows]

    def set(self, tid, **fields):
        self._set("tasks", tid, fields)

    def projects(self):
        """Names of the projects that have tasks."""
        with _lock:
            rows = self.db.execute("SELECT DISTINCT project FROM tasks WHERE COALESCE(project,'') != ''").fetchall()
        return [r[0] for r in rows]

    def idle_ids(self, project):
        """Ids of a project's tasks that are not running (their lock files, if any, are leftovers)."""
        with _lock:
            rows = self.db.execute("SELECT id FROM tasks WHERE project=? AND status != 'running'", (project,)).fetchall()
        return {r[0] for r in rows}

    def _set(self, table, rid, fields):
        fields["updated"] = time.time()
        if isinstance(fields.get("pipeline"), dict):
            fields["pipeline"] = json.dumps(fields["pipeline"])
        for k in JSON_COLS:
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        with _lock:
            self.db.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                            (*fields.values(), rid))
            self.db.commit()

    @staticmethod
    def _row(r):
        d = dict(r)
        for k in JSON_COLS:
            d[k] = json.loads(d[k] or "[]")
        d["escalated"] = bool(d["escalated"])
        d["read_only"] = bool(d.get("read_only"))
        d["phase"] = d.get("phase") or ""
        return d


# ---- lock files: <project>/.brain/.locks/<task-id>.lock ----
def _lock_dir(cfg, project):
    p = obsidian.find_project(cfg, project)
    return p / cfg["obsidian"]["project_brain_dir"] / ".locks" if p else None


def claim(cfg, project, task_id, files):
    d = _lock_dir(cfg, project)
    if not d:
        return None
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{task_id}.lock"
    f.write_text("\n".join(files) + ("\n" if files else ""), encoding="utf-8")
    return str(f)


def release(cfg, project, task_id):
    d = _lock_dir(cfg, project)
    if d:
        Path(d / f"{task_id}.lock").unlink(missing_ok=True)


def sweep(cfg, project, idle_ids):
    """Delete lock files left behind by tasks that are no longer running (a crash or a restart skipped the
    release). Only files named after a known task id are touched. Returns the task ids whose lock was removed."""
    d = _lock_dir(cfg, project)
    removed = []
    if d and d.is_dir():
        for f in d.glob("*.lock"):
            if f.stem in idle_ids:
                f.unlink(missing_ok=True)
                removed.append(f.stem)
    return removed
