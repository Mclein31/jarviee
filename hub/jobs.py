"""Jobs module for aggregating active runs, plans, and terminal sessions.

Aggregates jobs by stable (id, job_id) identity, links tasks and steps without mixing
pipelines across jobs, evaluates job complexity based on config thresholds, isolates pane
reuse using ingest.new_job, and excludes jobs closed by the user.
"""
import json
import time

from . import ingest
from .config import (
    DEFAULT_COMPLEX_JOB_DURATION_SECONDS,
    DEFAULT_COMPLEX_JOB_MIN_STEPS,
    DEFAULT_FINISHED_JOB_RETENTION_SECONDS,
)
from .jobstatus import (
    HEADER,
    STATUS_TO_HEADER,
    TAB_COLORS,
    header_kind_of,
    header_text_of,
    job_status,
    tab_color_of,
)
from .titles import make_title

__all__ = [
    "DEFAULT_COMPLEX_JOB_DURATION_SECONDS",
    "DEFAULT_COMPLEX_JOB_MIN_STEPS",
    "DEFAULT_FINISHED_JOB_RETENTION_SECONDS",
    "HEADER",
    "Job",
    "STATUS_TO_HEADER",
    "TAB_COLORS",
    "aggregate_jobs",
    "get_job",
    "get_jobs",
    "header_kind_of",
    "header_text_of",
    "is_complex",
    "job_status",
    "key_of",
    "outcome_of",
    "reset_pane_job",
    "tab_color_of",
]


def key_of(entity):
    """Stable key (id, job_id) as a tuple, and key_str as id#job_id."""
    if isinstance(entity, tuple):
        return entity
    eid = str(entity.get("id") or "")
    jid = str(entity.get("job_id") or "")
    return (eid, jid)


class Job(dict):
    """Represents an aggregated job with dict and attribute access."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setdefault("id", "")
        self.setdefault("job_id", "")
        self.setdefault("kind", "run")
        self.setdefault("title", "")
        self.setdefault("summary", "")
        self.setdefault("project", "")
        self.setdefault("status", "running")
        self.setdefault("tool", "")
        self.setdefault("tools", [])
        self.setdefault("model", "")
        self.setdefault("effort", "")
        self.setdefault("steps", 0)
        self.setdefault("duration", 0.0)
        self.setdefault("started", 0.0)
        self.setdefault("ended", None)
        self.setdefault("tasks", [])
        self.setdefault("runs", [])
        self.setdefault("closed_by_user", False)
        self.setdefault("is_complex", False)
        self["key"] = (self["id"], self["job_id"])
        self["key_str"] = f"{self['id']}#{self['job_id']}"

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'Job' object has no attribute '{name}'")

    def __setattr__(self, name, value):
        self[name] = value

    def to_dict(self):
        return dict(self)


def _config_thresholds(cfg=None):
    min_steps = DEFAULT_COMPLEX_JOB_MIN_STEPS
    dur_seconds = DEFAULT_COMPLEX_JOB_DURATION_SECONDS
    if isinstance(cfg, dict):
        try:
            if cfg.get("complex_job_min_steps") is not None:
                min_steps = int(cfg["complex_job_min_steps"])
        except (ValueError, TypeError):
            pass
        try:
            if cfg.get("complex_job_duration_seconds") is not None:
                dur_seconds = int(cfg["complex_job_duration_seconds"])
        except (ValueError, TypeError):
            pass
    return min_steps, dur_seconds


def is_complex(
    job=None,
    *,
    steps=None,
    tools=None,
    duration=None,
    cfg=None,
    now=None,
):
    """Evaluates whether a job is complex based on config criteria.

    A job is complex if:
    - steps >= complex_job_min_steps (default 2: more than one step)
    - > 1 agent tool used
    - duration > complex_job_duration_seconds (default 60)
    """
    if job is not None:
        if steps is None:
            steps = job.get("steps", 0)
        if tools is None:
            tools = job.get("tools")
            if not tools and job.get("tool"):
                tools = [job["tool"]]
        if duration is None:
            duration = job.get("duration")
            if duration is None:
                started = job.get("started") or 0.0
                ended = job.get("ended")
                curr_time = now if now is not None else time.time()
                duration = max(0.0, (ended if ended is not None else curr_time) - started)
        if cfg is None and "_cfg" in job:
            cfg = job["_cfg"]

    min_steps, max_duration = _config_thresholds(cfg)

    steps_val = int(steps or 0)
    tool_list = [t for t in (tools or []) if t]
    unique_tools = set(tool_list)
    dur_val = float(duration or 0.0)

    return (
        steps_val >= max(1, min_steps)
        or len(unique_tools) > 1
        or dur_val > max_duration
    )


def outcome_of(row):
    """Outcome status matching hub/batch.py (None while working, else settled string)."""
    if row is None or row.get("cleared") or row.get("closed_by_user"):
        return "closed"
    st = row.get("status")
    if st in ("running", "queued", "draft", "approved", "blocked"):
        return None
    if st == "waiting":
        act = str(row.get("activity") or "").lower()
        if "turn finished" in act or "responded" in act:
            return "replied"
        return "needs"
    if st in ("stopped", "cancelled"):
        return "closed"
    if st in ("done", "failed"):
        return st
    return None


def reset_pane_job(existing_row, launched=False):
    """Reset counters and generate a new job id when a new job starts in the same pane."""
    return ingest.new_job(existing_row, launched)


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


def aggregate_jobs(
    store,
    tasks=None,
    cfg=None,
    now=None,
    active_only=False,
    include_closed=False,
):
    """Aggregate active runs, plans, and terminal sessions by stable (id, job_id).

    - Links each job's tasks and steps without mixing pipelines across jobs.
    - Evaluates is_complex per config criteria.
    - Excludes jobs where closed_by_user is true unless include_closed=True.
    - Respects finished_job_retention_seconds for recently finished jobs.
    """
    now = time.time() if now is None else now
    jobs = []
    claimed_runs = set()

    # 1. Gather plans (including pipeline plans)
    if tasks is not None:
        candidate_plans = []
        seen_plan_ids = set()

        if hasattr(tasks, "open_plans"):
            try:
                for p in tasks.open_plans():
                    if p and p.get("id") and p["id"] not in seen_plan_ids:
                        candidate_plans.append(p)
                        seen_plan_ids.add(p["id"])
            except Exception:
                pass

        if hasattr(tasks, "plans"):
            try:
                for p in tasks.plans(limit=100):
                    if p and p.get("id") and p["id"] not in seen_plan_ids:
                        candidate_plans.append(p)
                        seen_plan_ids.add(p["id"])
            except Exception:
                pass

        for p in candidate_plans:
            pid = p["id"]
            if bool(p.get("closed_by_user")) and not include_closed:
                continue

            # Fetch tasks belonging strictly to this plan
            plan_tasks = []
            if hasattr(tasks, "list"):
                plan_tasks = tasks.list(plan_id=pid) or []
            elif isinstance(p.get("tasks"), list):
                plan_tasks = p["tasks"]

            # Find runs belonging to tasks of this plan
            plan_runs = []
            task_ids = {t["id"] for t in plan_tasks if isinstance(t, dict) and t.get("id")}
            task_run_ids = {t["run_id"] for t in plan_tasks if isinstance(t, dict) and t.get("run_id")}

            all_store_rows = []
            if store is not None:
                if hasattr(store, "all"):
                    all_store_rows = store.all() or []
                elif isinstance(store, list):
                    all_store_rows = store
                elif isinstance(store, dict):
                    all_store_rows = list(store.values())

            for r in all_store_rows:
                rid = r.get("id")
                if not rid:
                    continue
                belongs = False
                if rid in task_run_ids:
                    belongs = True
                else:
                    pt = _plan_task_of(r)
                    if pt and (pt in task_ids or pt.startswith(f"{pid}.")):
                        belongs = True
                if belongs:
                    plan_runs.append(r)
                    claimed_runs.add(rid)

            # Check if all runs in this plan are closed by user
            if plan_runs and all(bool(r.get("closed_by_user")) for r in plan_runs) and not include_closed:
                continue

            # Steps: sum steps across tasks/runs of this plan only
            plan_steps = 0
            counted_run_ids = set()
            for r in plan_runs:
                plan_steps += int(r.get("steps") or 0)
                counted_run_ids.add(r.get("id"))
            for t in plan_tasks:
                if t.get("run_id") not in counted_run_ids:
                    plan_steps += int(t.get("steps") or 0)

            # Tools: collect agent tools used by this plan only
            tools_set = set()
            for t in plan_tasks:
                tool_name = t.get("tool")
                if tool_name:
                    tools_set.add(tool_name)
            for r in plan_runs:
                tool_name = r.get("tool")
                if tool_name:
                    tools_set.add(tool_name)
            tools_list = sorted(tools_set)

            # Timing & Duration
            started = p.get("created") or 0.0
            run_starts = [r.get("started") for r in plan_runs if r.get("started")]
            if run_starts:
                started = min([started] + run_starts) if started else min(run_starts)

            ended = p.get("updated") if p.get("status") in ("done", "failed", "cancelled") else None
            run_ends = [r.get("ended") for r in plan_runs if r.get("ended")]
            if run_ends and p.get("status") in ("done", "failed", "cancelled"):
                ended = max(run_ends)

            duration = max(0.0, (ended if ended is not None else now) - (started or now))

            # Status determination
            st = job_status(p, tasks=plan_tasks, runs=plan_runs)

            if active_only and st in ("done", "failed", "stopped"):
                continue

            retention = DEFAULT_FINISHED_JOB_RETENTION_SECONDS
            if isinstance(cfg, dict):
                try:
                    if cfg.get("finished_job_retention_seconds") is not None:
                        retention = int(cfg["finished_job_retention_seconds"])
                except (ValueError, TypeError):
                    pass
            if st in ("done", "failed", "stopped") and ended:
                if (now - ended) > retention:
                    continue

            job_dict = {
                "id": pid,
                "job_id": p.get("job_id") or "",
                "kind": "pipeline" if p.get("pipeline") else "plan",
                "title": p.get("goal") or p.get("title") or pid,
                "summary": p.get("summary") or (f"Pipeline with {len(plan_tasks)} tasks" if p.get("pipeline") else f"Plan with {len(plan_tasks)} tasks"),
                "project": p.get("project") or "",
                "status": st,
                "tool": tools_list[0] if tools_list else (p.get("planner") or ""),
                "tools": tools_list,
                "model": (p.get("planner_model") or ""),
                "effort": "",
                "steps": plan_steps,
                "duration": duration,
                "started": started or now,
                "ended": ended,
                "tasks": plan_tasks,
                "runs": plan_runs,
                "closed_by_user": bool(p.get("closed_by_user"))
                or (bool(plan_runs) and all(bool(r.get("closed_by_user")) for r in plan_runs)),
                "_cfg": cfg,
            }
            job_dict["is_complex"] = is_complex(
                steps=plan_steps,
                tools=tools_list,
                duration=duration,
                cfg=cfg,
                now=now,
            )
            jobs.append(Job(job_dict))

    # 2. Gather standalone runs and terminal sessions from store
    store_rows = []
    if store is not None:
        if hasattr(store, "all"):
            store_rows = store.all() or []
        elif isinstance(store, list):
            store_rows = store
        elif isinstance(store, dict):
            store_rows = list(store.values())

    for row in store_rows:
        rid = row.get("id")
        if not rid or rid in claimed_runs:
            continue

        # Exclude jobs closed by user
        if bool(row.get("closed_by_user")) and not include_closed:
            continue

        jid = row.get("job_id") or ""
        st = job_status(row)

        if active_only and st in ("done", "failed", "stopped"):
            continue

        started = row.get("started") or row.get("created") or now
        ended = row.get("ended")
        retention = DEFAULT_FINISHED_JOB_RETENTION_SECONDS
        if isinstance(cfg, dict):
            try:
                if cfg.get("finished_job_retention_seconds") is not None:
                    retention = int(cfg["finished_job_retention_seconds"])
            except (ValueError, TypeError):
                pass

        if st in ("done", "failed", "stopped") and ended:
            if (now - ended) > retention:
                continue

        duration = max(0.0, (ended if ended is not None else now) - started)
        steps = int(row.get("steps") or 0)
        tool = row.get("tool") or ""
        tools = [tool] if tool else []

        kind = "terminal" if (row.get("source") == "hook" or rid.startswith(("claude-", "agy-"))) else "run"
        title = row.get("title") or make_title(row.get("task") or "") or rid

        job_dict = {
            "id": rid,
            "job_id": jid,
            "kind": kind,
            "title": title,
            "summary": row.get("summary") or "",
            "project": row.get("project") or "",
            "status": st,
            "tool": tool,
            "tools": tools,
            "model": row.get("model") or "",
            "effort": row.get("effort") or "",
            "steps": steps,
            "duration": duration,
            "started": started,
            "ended": ended,
            "tasks": [{"id": rid, "title": title, "tool": tool, "status": st, "steps": steps}],
            "runs": [row],
            "closed_by_user": bool(row.get("closed_by_user")),
            "_cfg": cfg,
        }
        job_dict["is_complex"] = is_complex(
            steps=steps,
            tools=tools,
            duration=duration,
            cfg=cfg,
            now=now,
        )
        jobs.append(Job(job_dict))

    # Sort: active jobs first, then by started desc
    def _sort_key(j):
        is_active = 0 if j.status in ("running", "waiting", "queued") else 1
        return (is_active, -(j.started or 0.0))

    jobs.sort(key=_sort_key)
    return jobs


# Convenient alias
get_jobs = aggregate_jobs


def get_job(key_or_id, store, tasks=None, cfg=None, now=None):
    """Retrieve a single Job by (id, job_id) tuple or string key."""
    all_jobs = aggregate_jobs(store, tasks=tasks, cfg=cfg, now=now, include_closed=True)
    if isinstance(key_or_id, tuple) and len(key_or_id) == 2:
        target_id, target_job_id = str(key_or_id[0]), str(key_or_id[1] or "")
        for j in all_jobs:
            if j.id == target_id and (j.job_id == target_job_id or not target_job_id):
                return j
        return None

    target = str(key_or_id)
    if "#" in target:
        target_id, target_job_id = target.split("#", 1)
        for j in all_jobs:
            if j.id == target_id and j.job_id == target_job_id:
                return j
        return None

    for j in all_jobs:
        if j.id == target or j.job_id == target or j.key_str == target:
            return j
    return None
