"""GET /api/jobs and GET /api/jobs/<id>[?job_id=...]: the Workflows carousel's data.

One job = one plan/pipeline or one run/terminal session, keyed by (id, job_id) (hub/jobs.py). Its tasks and steps are
its own: a pane reused for a new job gets a new job_id, so the old job's key no longer resolves. Status, header kind and
colour come from hub/jobstatus.py, the same source the pane headers and the combined report use. A job the owner
closed (closed_by_user) or stopped is never listed and never resolves, so its tab cannot come back.
"""
import time

from . import jobs, jobstatus, runs_api, runview

AGENT_NAMES = {"claude": "Claude", "agy": "agy"}
DONE_TASK = ("done",)
WAITING = ("waiting", "needs_you")


def _agent_label(tools):
    names = [AGENT_NAMES.get(t, t) for t in tools if t]
    return " + ".join(names)


def _current_step(job):
    """What the job is doing now: the running (else waiting, else next queued) task of a plan, or a run's activity."""
    if job.kind in ("plan", "pipeline"):
        tasks = [t for t in job.tasks if isinstance(t, dict)]
        for wanted in (("running",), WAITING, ("queued", "blocked", "draft", "approved")):
            hit = next((t for t in tasks if jobstatus.job_status(t) in wanted or t.get("status") in wanted), None)
            if hit:
                return str(hit.get("title") or hit.get("id") or "")[:90]
        return ""
    row = (job.runs or [{}])[0]
    return str(row.get("activity") or "")[:90]


def _progress(job):
    """(done, total, pct): steps done of a plan's tasks; a run has no known total, so pct is None until it is done."""
    if job.kind in ("plan", "pipeline") and job.tasks:
        total = len(job.tasks)
        done = sum(1 for t in job.tasks if t.get("status") in DONE_TASK)
        return done, total, round(100 * done / total)
    st = job.status
    return (1, 1, 100) if st == "done" else (0, 0, None)


def summary(job):
    done, total, pct = _progress(job)
    kind = jobstatus.header_kind_of({"status": job.status})
    return {"id": job.id, "job_id": job.job_id, "key": job.key_str, "kind": job.kind, "title": job.title,
            "agent": _agent_label(job.tools or [job.tool]), "agents": [AGENT_NAMES.get(t, t) for t in job.tools],
            "tool": job.tool, "status": job.status, "header": kind,
            "color": list(jobstatus.TAB_COLORS.get(kind) or ()),
            "pct": pct, "done": done, "total": total, "steps": job.steps, "started": job.started, "ended": job.ended,
            "duration": round(job.duration, 1), "current_step": _current_step(job), "complex": bool(job.is_complex)}


def _listed(job):
    return not job.closed_by_user and job.status != "stopped"


def listing(cfg, store, tasks, now=None, all_jobs=False):
    """{"jobs": [...], "threshold": {...}}: the carousel's jobs; only the complex ones unless all_jobs."""
    now = now or time.time()
    rows = [j for j in jobs.aggregate_jobs(store, tasks=tasks, cfg=cfg, now=now) if _listed(j)]
    if not all_jobs:
        rows = [j for j in rows if j.is_complex]
    return {"jobs": [summary(j) for j in rows],
            "threshold": {"min_steps": cfg.get("complex_job_min_steps"),
                          "duration_seconds": cfg.get("complex_job_duration_seconds"),
                          "retention_seconds": cfg.get("finished_job_retention_seconds")}}


def detail(cfg, store, tasks, rid, job_id="", sched=None, now=None):
    """(status, payload) for one job's own stages and steps; 404 when unknown or replaced, 410 once closed."""
    if not runs_api.ID_RE.match(rid or ""):
        return 400, {"error": "bad job id"}
    now = now or time.time()
    job = jobs.get_job((rid, job_id or ""), store, tasks=tasks, cfg=cfg, now=now)
    if job is None:
        return 404, {"error": "no such job (or its pane now runs a newer job)"}
    if not _listed(job):
        return 410, {"error": "job closed"}
    data, _ = runs_api.load(cfg, store, tasks, rid, sched, now)
    if not data:
        return 404, {"error": "no such job"}
    tree = runview.build(data, now)
    stages = [{"phase": h["phase"], "label": h["label"],
               "steps": [{"id": n["id"], "name": n["name"], "status": n["status"], "tool": n["tool"], "chip": n["chip"],
                          "summary": n["summary"], "start": n["start"], "end": n["end"], "duration": n["duration"],
                          "run_id": n["run_id"]}
                         for n in tree["nodes"] if n["phase"] == h["phase"] and n["role"] in ("agent", "hub")]}
              for h in tree["columns"]]
    return 200, {"job": summary(job), "stages": stages, "pipeline": tree["pipeline"], "summary": tree["summary"]}


def get(path, query, cfg, store, tasks, sched):
    """(status, payload) for a jobs path, or None when it isn't one of ours."""
    from urllib.parse import parse_qs
    args = parse_qs(query)
    if path == "/api/jobs":
        return 200, listing(cfg, store, tasks, all_jobs=args.get("all", ["0"])[0] == "1")
    if path.startswith("/api/jobs/"):
        return detail(cfg, store, tasks, path[len("/api/jobs/"):], args.get("job_id", [""])[0], sched)
    return None
