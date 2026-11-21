"""GET /api/runs, /api/run/<id>/tree, /api/run/<id>/trace and /static/<file> for the Orchestration view."""
import json
import re
import time
from pathlib import Path

from . import rundemo, runcost, runtrace, runview, spans, subagents
from .store import _lock

ID_RE = re.compile(r"^(plan-[a-f0-9]{4,12}|run-[a-f0-9]{4,16}|(claude|agy)-[A-Za-z0-9_-]{4,80}|sub-[A-Za-z0-9_-]{4,64}|demo)$")
STATIC_DIR = Path(__file__).with_name("static")
STATIC_RE = re.compile(r"^[a-z0-9-]{1,40}\.(js|css|png|svg)$")
# svg can carry script: server.py sends it with nosniff and a script-free, sandboxed Content-Security-Policy
TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8", "png": "image/png",
         "svg": "image/svg+xml"}
RUN_STATUS = {"running": "running", "waiting": "running", "done": "done", "failed": "failed", "stopped": "cancelled"}


def _plan_task_of(row):
    reasons = (row or {}).get("reasons") or []
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except (ValueError, TypeError):
            return ""
    if not isinstance(reasons, list):
        return ""
    hit = next((r for r in reasons if isinstance(r, str) and r.startswith("plan task ")), "")
    return hit[len("plan task "):]


def _rows(store, where, args):
    with _lock:
        fn = getattr(store, "_row", dict)
        return [fn(r) for r in store.db.execute(f"SELECT * FROM agents WHERE {where} ORDER BY started", args)]


def _usage(cfg, store, row):
    sessions = {s.get("session") for s in spans.for_run(store, row["id"])}
    if row["id"].startswith(("claude-", "agy-")):
        sessions.add(row["id"].split("-", 1)[1])
    return runcost.run_usage(cfg, row.get("tool") or "", sessions, row.get("model") or "")


def plan_data(cfg, store, tasks, pid, sched=None):
    plan = tasks.plan(pid)
    if not plan:
        return None
    runs, attempts = {}, {}
    for t in plan["tasks"]:
        found = [r for r in _rows(store, "reasons LIKE ?", (f'%plan task {t["id"]}%',))
                 if _plan_task_of(r) == t["id"]]
        attempts[t["id"]] = [r["id"] for r in found]
        runs.update({r["id"]: r for r in found})
    return {"kind": "plan", "id": pid, "project": plan.get("project"), "title": plan.get("goal"),
            "status": plan.get("status"), "created": plan.get("created"), "review": plan.get("review"),
            "pipeline": plan.get("pipeline"),
            "planner_model": "mock" if plan.get("planner") == "mock" else (cfg.get("planner") or {}).get("model", ""),
            "review_model": (cfg.get("review") or {}).get("model", ""),
            "review_enabled": (cfg.get("review") or {}).get("enabled", True),
            "tasks": plan["tasks"], "runs": runs, "attempts": attempts,
            "spans": {rid: spans.for_run(store, rid) for rid in runs},
            "usage": {rid: _usage(cfg, store, r) for rid, r in runs.items()},
            "paused": bool(sched and sched.paused)}


def run_data(cfg, store, row):
    task = {"id": row["id"], "title": row.get("title") or row.get("task") or row["id"], "prompt": "",
            "tool": row.get("tool"), "model": row.get("model"), "status": RUN_STATUS.get(row.get("status"), "queued")}
    if row.get("source") == "hook":
        task["name"] = f"{'Claude' if row.get('tool') == 'claude' else 'agy'} · main session"
    data = {"kind": "run", "id": row["id"], "project": Path(row.get("workspace") or "").name,
            "title": task["title"], "status": task["status"], "created": row.get("started"),
            "tasks": [task], "runs": {row["id"]: row}, "attempts": {row["id"]: [row["id"]]},
            "spans": {row["id"]: spans.for_run(store, row["id"])}, "usage": {row["id"]: _usage(cfg, store, row)}}
    for sub in subagents.for_session(row):   # a Claude session's subagents are tasks of its workflow
        data["tasks"].append({"id": sub["id"], "title": sub["title"], "prompt": "", "tool": "claude", "model": sub["model"],
                              "status": RUN_STATUS.get(sub["status"], "queued"), "name": f"Subagent · {sub['agent_type']}"})
        data["runs"][sub["id"]] = sub
        data["attempts"][sub["id"]] = [sub["id"]]
        data["spans"][sub["id"]] = []
        data["usage"][sub["id"]] = {}
    return data


def load(cfg, store, tasks, rid, sched=None, now=None):
    """(data, focus) for a plan or run id, or (None, None)."""
    now = now or time.time()
    if rid == "demo":
        return rundemo.data(now), None
    if rid.startswith("plan-"):
        return (plan_data(cfg, store, tasks, rid, sched) if tasks else None), None
    parent = subagents.parent_of(rid)
    if parent:   # a subagent opens its parent session's workflow, focused on its card
        prow = store.get(parent)
        return (run_data(cfg, store, prow), rid) if prow else (None, None)
    row = store.get(rid)
    if not row:
        return None, None
    tid = _plan_task_of(row)
    if tid and tasks:
        data = plan_data(cfg, store, tasks, tid.rsplit(".", 1)[0], sched)
        if data:
            return data, rid
    return run_data(cfg, store, row), None


def recent(cfg, store, tasks, limit=40, now=None):
    now = now or time.time()
    out = []
    for p in (tasks.plans(limit=15, visible=True) if tasks else []):
        data = plan_data(cfg, store, tasks, p["id"])
        tree = runview.build(data, now)
        out.append(_item(tree, p.get("created"), "plan"))
    for row in _rows(store, "COALESCE(cleared, 0)=0", ())[-limit:]:   # "Clear finished" hides runs here too
        if _plan_task_of(row):
            continue
        tree = runview.build(run_data(cfg, store, row), now)
        out.append(_item(tree, row.get("started"), "run"))
    return sorted(out, key=lambda r: -(r["started"] or 0))[:limit]


def _item(tree, started, kind):
    s = tree["summary"]
    status = "running" if tree["live"] else tree["nodes"][0]["status"]
    return {"id": tree["id"], "kind": kind, "title": tree["title"][:90], "project": tree["project"], "status": status,
            "started": started, "duration": s["elapsed"], "cost": s["cost"], "tokens": s["tokens"],
            "done": s["done"], "total": s["total"]}


def get(path, query, cfg, store, tasks, sched):
    """(status, payload) for a runs path, or None when the path isn't one of ours."""
    if path == "/api/runs":
        return 200, {"runs": recent(cfg, store, tasks)}
    if path == "/api/run-status":   # hooks/pane_runner.py header: the run's current job and its status, read together
        from urllib.parse import parse_qs
        rid = parse_qs(query).get("id", [""])[0]
        if not ID_RE.match(rid):
            return 400, {"error": "bad run id"}
        row = store.get(rid)
        if not row:
            return 404, {"error": "no such run"}
        from . import jobstatus
        return 200, {"id": rid, "job_id": row.get("job_id") or "", "status": jobstatus.job_status(row),
                     "title": row.get("title") or "", "activity": row.get("activity") or "",
                     "steps": int(row.get("steps") or 0)}
    m = re.match(r"^/api/run/([^/]+)/(tree|trace)$", path)
    if not m:
        return None
    rid, what = m.group(1), m.group(2)
    if not ID_RE.match(rid):
        return 400, {"error": "bad run id"}
    now = time.time()
    data, focus = load(cfg, store, tasks, rid, sched, now)
    if not data:
        return 404, {"error": f"no plan or run '{rid}'"}
    tree = runview.build(data, now)
    tree["focus"] = focus
    return 200, (tree if what == "tree" else runtrace.trace(data, tree, now))


def static_path(name):
    """The file for /static/<name>, or None: only files directly in hub/static/ with a plain name."""
    if not STATIC_RE.match(name or ""):
        return None
    p = STATIC_DIR / name
    if not p.is_file() or p.resolve().parent != STATIC_DIR.resolve():
        return None
    return p


def static(name):
    """(status, body bytes, content type) for /static/<name>; only files in hub/static/ with a plain name."""
    p = static_path(name)
    if p is None:
        return 404, b"not found", "text/plain"
    return 200, p.read_bytes(), TYPES[name.rsplit(".", 1)[1]]
