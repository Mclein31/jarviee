"""Trace (waterfall) rows for GET /api/run/<id>/trace. Pure: tree + spans in, rows out.

Rows nest agent -> model turn -> tool call. Times are seconds since the run's start (t0).
A span with no end is still running (end = None); the browser draws it up to the "now" line.
"""


def _rel(ts, t0):
    return None if ts is None else round(ts - t0, 2)


def trace(data, tree, now):
    t0, rows = tree["t0"], []
    usage = data.get("usage") or {}
    for c in tree["nodes"]:
        if c["role"] == "orchestrator":
            continue
        rid = c.get("run_id")
        u = usage.get(rid) or {}
        live = c.get("state", c["status"]) in ("running", "needs_you")
        rows.append({"id": c["id"], "parent": None, "depth": 0, "label": c["name"], "summary": c["summary"],
                     "agent": c["tool"], "tool": "", "args": "", "status": c.get("state", c["status"]),
                     "model": c["chip"],
                     "start": _rel(c["start"], t0), "end": None if live else _rel(c["end"], t0),
                     "tokens": u.get("tokens"), "cost": u.get("cost"), "queued": c["start"] is None})
        if not rid:
            continue
        spans = (data.get("spans") or {}).get(rid, [])
        ids = {s["id"] for s in spans}
        for s in spans:
            parent = s["parent"] if s.get("parent") in ids else c["id"]
            depth = 2 if parent != c["id"] else 1
            end = s["end"]
            if end is None and not live:
                end = c["end"] or now     # the run ended without closing this span
            rows.append({"id": s["id"], "parent": parent, "depth": depth,
                         "label": s["tool"] if s["kind"] != "turn" else "model turn",
                         "summary": s.get("args") or "", "agent": s.get("agent") or c["tool"], "tool": s["tool"],
                         "args": s.get("args") or "", "kind": s["kind"],
                         "status": s["status"] if s["end"] is not None or not live else "running",
                         "model": s.get("model") or c["chip"], "start": _rel(s["start"], t0),
                         "end": _rel(end, t0), "tokens": None, "cost": None, "queued": False})
    return {"id": tree["id"], "t0": t0, "now": _rel(now, t0), "live": tree["live"],
            "end": _rel(now if tree["live"] else t0 + tree["summary"]["elapsed"], t0), "rows": _order(rows)}


def _order(rows):
    """Depth-first: each row followed by its children, keeping start order among siblings."""
    kids = {}
    for r in rows:
        kids.setdefault(r["parent"], []).append(r)
    out = []

    def walk(pid):
        for r in kids.get(pid, []):
            out.append(r)
            walk(r["id"])
    walk(None)
    return out
