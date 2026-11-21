"""Run tree for the Orchestration view: pure functions, no I/O.

build(data, now) -> nodes (orchestrator -> phase columns -> task agents -> retries/fallbacks) plus a
deterministic layout (x, y per card) and orthogonal edges, so the browser only draws.
"""
import re

PHASES = [("research", "Research"), ("analysis", "Analysis"), ("plan", "Plan"), ("build", "Build"),   # run order
          ("fact_check", "Fact check"), ("test", "Test"), ("review", "Review"), ("fix", "Fix"), ("brain", "Brain sync")]
PHASE_KEYS = {k for k, _ in PHASES}
PHASE_WORDS = [("review", r"\b(review|audit|qa|code[- ]review)\b"),
               ("fact_check", r"\b(fact[- ]check|verify)\b"),
               ("analysis", r"\b(analy[sz]e|analysis|data[- ]analyst)\b"),
               ("brain", r"\b(brain|changelog|todo\.md|decisions|agent_runs|handoff)\b"),
               ("plan", r"\b(plan|design|research|architect|spec|investigate)\b")]
CARD_W, CARD_H, CHILD_H = 232, 92, 66
INDENT, SPINE, GAP_Y, COL_GAP, HEAD = 28, 18, 34, 44, 56   # GAP_Y leaves room for each card's phase label
TASK_STATUS = {"draft": "queued", "queued": "queued", "blocked": "queued", "waiting": "queued",
               "running": "running", "done": "done", "failed": "failed", "cancelled": "failed"}


def phase_of(task):
    """A pipeline task names its phase; ordinary tasks are guessed from their words."""
    if task.get("phase") in PHASE_KEYS:
        return task["phase"]
    text = f"{task.get('title', '')} {task.get('prompt', '')[:300]}".lower()
    for phase, pat in PHASE_WORDS:
        if re.search(pat, text):
            return phase
    return "build"


def model_chip(tool, model):
    m = str(model or "").lower()
    if "orchestrator" in m:
        return "Orchestrator · " + ("Argon" if "g1-pro" in m else "Neon" if "neon" in m else "Carbon")
    for key, label in (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("neon", "Neon"), ("argon", "Argon"),
                       ("hydrogen", "Hydrogen"),
                       ("flash", "3.8 Flash"), ("gpt-oss", "GPT-OSS"), ("fake", "Echo")):
        if key in m:
            return label + (" · agy" if tool == "agy" and key in ("opus", "sonnet") else "")
    return model or (tool or "—")


def run_status(task_status, run):
    if run and run.get("status") == "waiting":
        return "needs_you"
    if run and run.get("status") in ("running", "done", "failed"):
        return run["status"]
    if run and run.get("status") == "stopped":
        return "failed"
    return TASK_STATUS.get(task_status, "queued")


def _dur(start, end, now):
    return round(((end or now) - start), 1) if start else None


def _node(nid, role, name, status, summary, tool, model, start=None, end=None, now=0, **kw):
    return {"id": nid, "role": role, "name": name, "status": status, "summary": (summary or "")[:140],
            "tool": tool or "", "model": model or "", "chip": model_chip(tool, model),
            "start": start, "end": end, "duration": _dur(start, end, now), "cost": kw.get("cost"),
            "tokens": kw.get("tokens"), "run_id": kw.get("run_id"), "phase": kw.get("phase"),
            "kind": kw.get("kind", ""), "children": []}


def _usage(data, rid):
    u = (data.get("usage") or {}).get(rid) or {}
    return u.get("cost"), u.get("tokens")


def task_nodes(data, now):
    out = []
    for t in data.get("tasks") or []:
        runs = [data["runs"][r] for r in (data.get("attempts") or {}).get(t["id"], []) if r in data.get("runs", {})]
        first = runs[0] if runs else None
        last = runs[-1] if runs else None
        status = run_status(t.get("status"), last)
        cost, tok = _usage(data, first["id"]) if first else (None, None)
        n = _node(t["id"], "agent", t.get("name") or f"{'Claude' if t.get('tool') == 'claude' else 'agy'} · {t['id'].split('.')[-1]}",
                  status, t.get("title"), (first or t).get("tool"), (first or t).get("model"),
                  first and first.get("started"), (first or {}).get("ended"), now,
                  cost=cost, tokens=tok, run_id=first and first["id"], phase=phase_of(t))
        prev = first
        for i, r in enumerate(runs[1:], 2):
            kind = "fallback" if prev and r.get("tool") != prev.get("tool") else "retry"
            label = f"Fallback to {r.get('tool')}" if kind == "fallback" else f"Retry #{i}"
            c, k = _usage(data, r["id"])
            note = t.get("route_note") if kind == "fallback" else (prev or {}).get("activity")
            child = _node(r["id"], "attempt", label, kind, note, r.get("tool"), r.get("model"), r.get("started"),
                          r.get("ended"), now, cost=c, tokens=k, run_id=r["id"], phase=n["phase"], kind=kind)
            child["state"] = run_status(t.get("status"), r)     # the chip says retry/fallback; state says how it went
            n["children"].append(child)
            prev = r
        n.update(detail(t, last))
        n["outcome"] = status                                    # the task as a whole (last attempt)
        if len(runs) > 1:   # the card itself shows the first attempt; later attempts are its children
            n["status"] = "failed" if first.get("status") in ("failed", "stopped") else run_status(t.get("status"), first)
        n["state"] = n["status"]
        out.append(n)
    return out


def detail(task, run):
    """What the side panel shows beyond the card: the planner's polished brief and what the agent is doing now."""
    run = run or {}
    files = run.get("files") or []
    if isinstance(files, str):
        files = [files]
    return {"prompt": (task.get("prompt") or "")[:4000], "acceptance": (task.get("acceptance") or "")[:1000],
            "depends_on": list(task.get("depends_on") or []), "level": task.get("level") or "",
            "route_note": task.get("route_note") or "", "activity": (run.get("activity") or "")[:300],
            "steps": int(run.get("steps") or 0), "files": [str(f) for f in files][:40]}


def layout(orch, columns):
    """columns: [(phase, label, [nodes])]. Sets x/y/w/h on every card; returns (edges, size, heads)."""
    col_w = SPINE + INDENT + CARD_W
    width = max(1, len(columns)) * col_w + max(0, len(columns) - 1) * COL_GAP
    orch.update(x=max(0, (width - CARD_W) // 2), y=0, w=CARD_W, h=CARD_H)
    ocx, bus = orch["x"] + CARD_W // 2, CARD_H + HEAD // 2
    edges, heads, bottom = [], [], CARD_H
    for i, (phase, label, nodes) in enumerate(columns):
        x0 = i * (col_w + COL_GAP)
        heads.append({"phase": phase, "label": label, "x": x0, "y": CARD_H + HEAD - 6, "count": len(nodes)})
        y, spine = CARD_H + HEAD + 26, x0 + SPINE // 2
        for j, n in enumerate(nodes):
            n.update(x=x0 + SPINE, y=y, w=CARD_W, h=CARD_H)
            mid = y + CARD_H // 2
            if j == 0:
                top = n["x"] + CARD_W // 2
                pts = [[ocx, CARD_H], [ocx, bus], [top, bus], [top, y]]
            else:
                first = nodes[0]
                pts = [[first["x"], first["y"] + CARD_H // 2], [spine, first["y"] + CARD_H // 2], [spine, mid], [n["x"], mid]]
            edges.append({"to": n["id"], "status": n["status"], "points": pts})
            y += CARD_H + GAP_Y
            for c in n["children"]:
                c.update(x=n["x"] + INDENT, y=y, w=CARD_W - INDENT + SPINE, h=CHILD_H)
                cm = y + CHILD_H // 2
                edges.append({"to": c["id"], "status": c["status"],
                              "points": [[n["x"] + 12, n["y"] + CARD_H], [n["x"] + 12, cm], [c["x"], cm]]})
                y += CHILD_H + GAP_Y
        bottom = max(bottom, y)
    return edges, {"w": width, "h": bottom}, heads


def cards(orch, columns):
    out = [orch]
    for _, _, nodes in columns:
        for n in nodes:
            out.append(n)
            out.extend(n["children"])
    return out
