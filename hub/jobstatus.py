"""Job status vocabulary: the one source for run/plan/task status, pane-header kind, badge text and tab colour.

A leaf module (stdlib only, no hub imports) so hooks/pane_runner.py, hub/progress.py and hub/jobs.py can all share it
without pulling in the launcher/ingest chain or forming an import cycle. hub/jobs.py re-exports every name here.
"""
import re

CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

TAB_COLORS = {
    "running": (128, 128, 128),
    "waiting": (255, 190, 0),
    "done": (40, 200, 80),
    "failed": (230, 50, 50),
}

HEADER = {
    "running": ("⚪", "RUNNING"),
    "waiting": ("🟡", "NEEDS YOU"),
    "done": ("🟢", "DONE"),
    "failed": ("🔴", "FAILED"),
    "stopped": ("⚪", "STOPPED"),
}

STATUS_TO_HEADER = {
    "running": "running",
    "queued": "running",
    "draft": "running",
    "approved": "running",
    "blocked": "running",
    "waiting": "waiting",
    "needs_you": "waiting",
    "needs": "waiting",
    "done": "done",
    "failed": "failed",
    "stale": "failed",
    "stopped": "stopped",
    "cancelled": "stopped",
}


def job_status(entity, tasks=None, runs=None):
    """Normalize status for runs, plans, tasks, or sessions.

    Accepts a raw status string or a dict/Job representing a run, plan, or task.
    When evaluating a collection of tasks and runs (e.g. for a plan), resolves
    overall status with consistent precedence:
      stopped (if closed/cancelled) -> waiting/needs you -> running -> failed -> queued -> done
    """
    if entity is None:
        return "stopped"
    if isinstance(entity, str):
        entity = {"status": entity}

    if entity.get("closed_by_user"):
        return "stopped"

    raw = str(entity.get("status") or "").lower()
    if raw in ("cancelled", "stopped"):
        return "stopped"
    if raw in ("stale",):
        return "failed"
    if raw in ("failed", "done") and not tasks and not runs and not entity.get("tasks") and not entity.get("runs"):
        return raw

    all_tasks = (tasks if tasks is not None else entity.get("tasks")) or []
    all_runs = (runs if runs is not None else entity.get("runs")) or []
    if all_tasks or all_runs:
        if all_runs and all(bool(r.get("closed_by_user")) for r in all_runs) and not raw:
            return "stopped"

        def _child_st(c):
            if isinstance(c, dict):
                if c.get("closed_by_user"):
                    return "stopped"
                c_raw = str(c.get("status") or "").lower()
            else:
                c_raw = str(c or "").lower()
            if c_raw in ("stale",):
                return "failed"
            if c_raw in ("stopped", "cancelled"):
                return "stopped"
            if c_raw in ("running", "done", "failed"):
                return c_raw
            if c_raw in ("waiting", "needs_you", "needs"):
                return "waiting"
            if c_raw in ("draft", "approved", "queued", "blocked"):
                return "queued"
            return c_raw or "running"

        task_statuses = [_child_st(t) for t in all_tasks]
        run_statuses = [_child_st(r) for r in all_runs]
        combined = task_statuses + run_statuses
        if any(s == "waiting" for s in combined):
            return "waiting"
        if any(s == "running" for s in combined):
            return "running"
        if any(s == "failed" for s in combined):
            return "failed"
        if any(s == "queued" for s in combined):
            return "queued"
        if combined and all(s == "done" for s in combined):
            return "done"

    if raw in ("failed", "done"):
        return raw
    if raw in ("running", "waiting"):
        return raw
    if raw in ("needs_you", "needs"):
        return "waiting"
    if raw in ("draft", "approved", "queued", "blocked"):
        return "queued"
    return raw or "running"


def header_kind_of(status_or_entity):
    """Maps a status string or entity dict to the header state ('running', 'waiting', 'done', 'failed', 'stopped')."""
    if isinstance(status_or_entity, dict):
        st = job_status(status_or_entity)
    else:
        st = job_status({"status": str(status_or_entity or "")})
    return STATUS_TO_HEADER.get(st, "running")


def tab_color_of(status_or_entity):
    """Returns the RGB tuple for iTerm2 tab color or None for default."""
    kind = header_kind_of(status_or_entity)
    return TAB_COLORS.get(kind, None)


def header_text_of(status_or_entity, title=""):
    """Returns formatted header string with badge dot and label."""
    kind = header_kind_of(status_or_entity)
    dot, label = HEADER[kind]
    clean_title = CTRL_RE.sub(" ", str(title or ""))[:80]
    return f"{dot} {label} · {clean_title}"
