"""Orchestration view payloads (GET /api/run/<id>/tree and /trace). Pure: data in, JSON-ready dicts out."""
from . import runtree as rt

PLAN_STATUS = {"draft": "queued", "approved": "running", "running": "running", "done": "done",
               "failed": "failed", "cancelled": "failed"}


ENDED = ("failed", "cancelled")


def _synthetic(data, phase, tasks_done, now):
    """A hub card (planner, review, brain sync). Once the plan has failed or been cancelled, a step that never
    finished is 'skipped': it never runs after that, so it must not keep the plan live forever."""
    node = _synthetic_card(data, phase, tasks_done, now)
    if data.get("status") in ENDED and node["status"] in ("running", "queued", "needs_you"):
        node["status"] = "skipped"
    return node


def _synthetic_card(data, phase, tasks_done, now):
    st = data.get("status")
    if phase == "plan":
        pipe = data.get("pipeline") or {}
        tasks = data.get("tasks") or []
        planned = [t for t in tasks if t.get("phase") == "build"] if pipe else tasks   # pipelines: the planner's own tasks
        pst = ("queued" if pipe.get("phase") == "research" else "running" if pipe.get("phase") == "planning" else "failed"
               if pipe.get("phase") == "failed" and not planned else "done") if pipe else ("done" if tasks else "running")
        return rt._node("hub-plan", "planner", "Project-manager" if pipe else "Planner", pst,
                        f"Split the goal into {len(planned)} task{'s' if len(planned) != 1 else ''}", "claude",
                        data.get("planner_model") or "", data.get("created"), data.get("created"), now,
                        phase="plan", kind="hub")
    if phase == "review":
        status = "done" if data.get("review") else ("running" if tasks_done and st != "done" else "queued")
        return rt._node("hub-review", "reviewer", "Review pass", status,
                        (data.get("review") or "One read-only review over the plan's combined changes")[:140],
                        "claude", data.get("review_model") or "sonnet", phase="review", kind="hub")
    status = "done" if st == "done" else ("running" if tasks_done else "queued")
    return rt._node("hub-brain", "brain", "Brain sync", status, "Appends each attempt to .brain/AGENT_RUNS.md",
                    "", "", phase="brain", kind="hub")


def build(data, now):
    tasks = rt.task_nodes(data, now)
    finished = all(n["outcome"] in ("done", "failed") for n in tasks) and bool(tasks)
    columns = []
    for phase, label in rt.PHASES:
        nodes = [n for n in tasks if n["phase"] == phase]
        wanted = phase in ("plan", "brain") if data.get("pipeline") else phase in ("plan", "review", "brain")
        if not nodes and data.get("kind") == "plan" and wanted and (phase != "review" or data.get("review_enabled", True)):
            nodes = [_synthetic(data, phase, finished, now)]
        if nodes:
            columns.append((phase, label, nodes))
    allcards = [c for _, _, ns in columns for n in ns for c in [n] + n["children"]]
    starts = [c["start"] for c in allcards if c.get("start")]
    ends = [c["end"] or now for c in allcards if c.get("start")]
    t0 = min(starts) if starts else (data.get("created") or now)
    live = any(c.get("state", c["status"]) in ("running", "needs_you") for c in allcards)
    t1 = now if live or not ends else max(ends)
    costs = [c["cost"] for c in allcards if c.get("run_id") and c.get("cost") is not None]
    toks = [c["tokens"] for c in allcards if c.get("run_id") and c.get("tokens") is not None]
    is_plan = data.get("kind") == "plan"
    orch = rt._node("orch", "orchestrator", "Orchestrator" if is_plan else "Launcher",
                    PLAN_STATUS.get(data.get("status"), "running" if live else "done") if is_plan
                    else ("running" if live else (tasks[0]["outcome"] if tasks else "done")),
                    data.get("title"), "claude" if is_plan else "", data.get("planner_model") or "",
                    t0, None if live else t1, now, cost=round(sum(costs), 6) if costs else None,
                    tokens=sum(toks) if toks else None)
    pipe = data.get("pipeline") or {}
    running = [n["name"] for n in tasks if n.get("state", n["status"]) in ("running", "needs_you")]
    orch.update(prompt=data.get("title") or "", acceptance="", depends_on=[], level=pipe.get("level") or "",
                route_note="", steps=0, files=[],
                activity=pipe.get("note") or (f"Coordinating {', '.join(running[:3])}" if running else ""))
    edges, size, heads = rt.layout(orch, columns)
    real = [n for n in tasks]
    priced = [c for c in allcards if c.get("run_id")]
    return {
        "id": data["id"], "kind": data.get("kind"), "project": data.get("project") or "",
        "title": data.get("title") or "", "live": live, "paused": bool(data.get("paused")),
        "nodes": rt.cards(orch, columns), "edges": edges, "columns": heads, "size": size,
        "t0": t0, "now": now, "pipeline": data.get("pipeline"),
        "summary": {"done": sum(1 for n in real if n["outcome"] == "done"), "total": len(real),
                    "elapsed": round(t1 - t0, 1), "tokens": sum(toks) if toks else None,
                    "cost": round(sum(costs), 4) if costs else None,
                    "cost_partial": len(costs) < len(priced)},
        "usage": usage_tables(data, allcards),
        "events": events(data, allcards),
    }


def usage_tables(data, cards):
    agents, models = [], {}
    for c in cards:
        if not c.get("run_id"):
            continue
        u = (data.get("usage") or {}).get(c["run_id"]) or {}
        agents.append({"node": c["id"], "name": c["name"], "chip": c["chip"], "tokens": u.get("tokens"),
                       "cost": u.get("cost"), "note": u.get("note", "no usage data"), "models": u.get("models") or {}})
        for m, t in (u.get("models") or {}).items():
            agg = models.setdefault(m, {"model": m, "chip": rt.model_chip(c["tool"], m), "input": 0, "output": 0,
                                        "cache_read": 0, "cache_write": 0, "total": 0, "cost": 0.0, "priced": True})
            for k in ("input", "output", "cache_read", "cache_write", "total"):
                agg[k] += t.get(k) or 0
            if t.get("cost") is None:
                agg["priced"] = False
            else:
                agg["cost"] += t["cost"]
    for m in models.values():
        m["cost"] = round(m["cost"], 6) if m["priced"] else None
    known = [a["tokens"] for a in agents if a["tokens"] is not None]
    costs = [a["cost"] for a in agents if a["cost"] is not None]
    return {"agents": agents, "models": sorted(models.values(), key=lambda m: -m["total"]),
            "totals": {"tokens": sum(known) if known else None, "cost": round(sum(costs), 6) if costs else None,
                       "unpriced": sum(1 for a in agents if a["cost"] is None)}}


def events(data, cards):
    out = []
    if data.get("kind") == "plan" and data.get("created"):
        out.append({"ts": data["created"], "kind": "plan", "node": "orch",
                    "text": f"Plan created with {len(data.get('tasks') or [])} tasks"})
    for c in cards:
        rid = c.get("run_id")
        if not rid or c["start"] is None:
            continue
        verb = {"fallback": "Fell back to", "retry": "Retried on"}.get(c.get("kind"), "Started on")
        text = f"{c['name']}: {verb} {c['tool']} · {c['chip']}"
        if c.get("kind") == "fallback" and c.get("summary"):
            text += f" ({c['summary'][:90]})"
        out.append({"ts": c["start"], "kind": c.get("kind") or "start", "node": c["id"], "text": text})
        run = (data.get("runs") or {}).get(rid) or {}
        if c["end"] and run.get("status") in ("done", "failed", "stopped"):
            out.append({"ts": c["end"], "kind": run["status"], "node": c["id"],
                        "text": f"{c['name']}: {run['status']}" + (f" · {run['activity'][:90]}" if run.get("activity") else "")})
        for s in (data.get("spans") or {}).get(rid, []):
            if s["kind"] == "event":
                label = {"needs_you": "Needs you", "failed": "Failed", "denied": "Permission denied"}.get(s["tool"], s["tool"])
                out.append({"ts": s["start"], "kind": s["tool"], "node": c["id"], "text": f"{c['name']}: {label} · {s['args']}"})
    return sorted(out, key=lambda e: e["ts"])
