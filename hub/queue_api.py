"""HTTP handlers for planning and the task queue (used by server.py)."""
import contextlib
from collections import Counter

from . import launcher, planner
from .respond import opt_str
from .router import TIERS, fit_effort, pick_model

LANE_LABELS = {"agy": "Antigravity: basic & light", "claude": "Claude: complex & heavy"}


def queue_state(cfg, tasks, sched):
    """Lanes with counts, open tasks and the recent plans for the dashboard."""
    plans = tasks.plans(limit=6, visible=True)
    for p in plans:   # the dashboard poll stays small: no internal keys, a short research excerpt
        if p.get("pipeline"):
            p["pipeline"] = {k: v for k, v in p["pipeline"].items() if k not in ("_test", "_fact_check")}
            p["pipeline"]["research"] = (p["pipeline"].get("research") or "")[:500]
    open_tasks = [t for p in plans for t in p["tasks"]]
    running = Counter(t["tool"] for t in open_tasks if t["status"] == "running")
    waiting = Counter(t["tool"] for t in open_tasks if t["status"] in ("queued", "blocked"))
    lanes = {tool: {"label": LANE_LABELS.get(tool, tool), "capacity": cap, "running": running[tool],
                    "queued": waiting[tool]} for tool, cap in cfg.get("lanes", {}).items()}
    return {"paused": bool(sched and sched.paused), "paused_reason": getattr(sched, "paused_reason", ""),
            "lanes": lanes, "plans": plans,
            "quota": dict(sched.quota) if sched else {}}



def make_draft(body, cfg, tasks, sched=None):
    raw_goal = body.get("goal")
    if not isinstance(raw_goal, str):
        raise ValueError("'goal' must be a string")
    goal = raw_goal.strip()
    if not goal:
        raise ValueError("goal is empty")
    project = opt_str(body, "project") or ""
    which_planner = opt_str(body, "planner") or "claude"
    plan = planner.make_plan(goal, cfg, project or None, planner=which_planner,
                             quota=sched.quota if sched else None)
    if body.get("dry_run"):
        return {"dry_run": True, **plan, "table": planner.format_table(plan)}
    pid = tasks.add_plan(goal, project, plan["tasks"], planner=plan["planner"], notes=plan["notes"])
    return {"id": pid, **tasks.plan(pid), "table": planner.format_table(tasks.plan(pid))}


def post(path, body, cfg, tasks, sched):
    """Return (status, payload), or None when the path is not a queue endpoint."""
    tid = pid = opt_str(body, "id") or ""
    if path == "/api/plan":
        return 200, make_draft(body, cfg, tasks, sched=sched)
    if path == "/api/plan/approve":
        if not tasks.plan(pid):
            raise ValueError(f"no plan '{pid}'")
        tasks.approve(pid)
        if sched:
            sched.tick()
        return 200, tasks.plan(pid)
    if path == "/api/plan/prune":
        days = max(1, int(body.get("days") or 7))
        with (sched.lock if sched else contextlib.nullcontext()):
            removed = tasks.prune_plans(max_age_days=days)
        return 200, {"removed": len(removed), "ids": removed, "days": days}
    if path in ("/api/plan/discard", "/api/plan/cancel"):
        plan = tasks.plan(pid)
        if not plan:
            raise ValueError(f"no plan '{pid}'")
        with (sched.lock if sched else contextlib.nullcontext()):   # no tick or advance in between
            tasks.set_plan(pid, status="cancelled")
            for t in tasks.plan(pid)["tasks"]:
                if t["status"] in ("draft", "queued", "blocked", "waiting"):
                    tasks.set(t["id"], status="cancelled")
                elif t["status"] == "running" and sched:
                    sched.cancel(t["id"])
        return 200, tasks.plan(pid)
    if path == "/api/plan/edit":
        plan = tasks.plan(pid)
        if not plan:
            raise ValueError(f"no plan '{pid}'")
        fields = {}
        if "goal" in body:
            fields["goal"] = opt_str(body, "goal") or ""
        if "notes" in body:
            fields["notes"] = body["notes"] if isinstance(body["notes"], list) else (opt_str(body, "notes") or "")
        if fields:
            tasks.set_plan(pid, **fields)
        if "tasks" in body and isinstance(body["tasks"], list):
            if plan["status"] != "draft":
                raise ValueError("only a draft plan's tasks can be edited; cancel or retry tasks instead")
            own = {t["id"]: t for t in plan["tasks"]}
            for t_edit in body["tasks"]:
                if not isinstance(t_edit, dict) or not isinstance(t_edit.get("id"), str) or t_edit.get("id") not in own:
                    raise ValueError(f"no task '{t_edit.get('id') if isinstance(t_edit, dict) else t_edit}' in plan {pid}")
                cur, up = own[t_edit["id"]], {}
                if "title" in t_edit:
                    up["title"] = (opt_str(t_edit, "title") or "").strip()[:200] or cur["title"]
                level = opt_str(t_edit, "level") if "level" in t_edit else opt_str(t_edit, "tier")
                if level is not None:
                    if level not in TIERS:
                        raise ValueError(f"unknown level '{level}'")
                    up["level"] = level
                if "tool" in t_edit:
                    tool_val = opt_str(t_edit, "tool")
                    if tool_val not in cfg["tools"]:
                        raise ValueError(f"unknown tool '{tool_val}'")
                    up["tool"] = tool_val
                if "level" in up or "tool" in up:   # the model follows the tool and level, as a reroute does
                    tool, lvl = up.get("tool", cur["tool"]), up.get("level", cur["level"])
                    up["model"], up["effort"] = fit_effort(tool, pick_model(tool, lvl, cfg),
                                                           launcher.pick_effort(tool, lvl, cfg), cfg)
                    up["route_note"] = "edited by you"
                for k in ("files", "depends_on"):
                    if isinstance(t_edit.get(k), list):
                        up[k] = [str(x) for x in t_edit[k]]
                if t_edit.get("status") == "cancelled":   # dropping a task from the draft
                    up["status"] = "cancelled"
                if up:
                    tasks.set(cur["id"], **up)
        return 200, tasks.plan(pid)
    if not sched:
        return None
    if path == "/api/queue/pause":
        sched.pause()
        return 200, {"paused": True}
    if path == "/api/queue/resume":
        sched.resume()
        sched.tick()
        return 200, {"paused": False}
    if path == "/api/task/cancel":
        return 200, sched.cancel(tid)
    if path == "/api/task/retry":
        t = sched.retry(tid)
        sched.tick()
        return 200, t
    if path == "/api/task/reroute":
        t = sched.reroute(tid, opt_str(body, "tool") or "")
        sched.tick()
        return 200, t
    return None
