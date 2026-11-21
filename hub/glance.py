"""Glance summary for hotkeys, menu bars, Stream Deck and status lights (GET /api/glance).

Read-only and cheap: it reads the agents table and the already-cached usage data. It never
starts `agy -p /usage`, reads transcripts or spawns a process.
"""
import os
import time

from .usage import local_midnight

RECENT_FAIL_S = 30 * 60
COLORS = {"needs_you": "#f5a524", "failed": "#e5484d", "running": "#3b82f6", "idle": "#6b7280"}


def _quota(usage_data):
    out = {"claude_5h_used": None, "claude_week_used": None, "agy": []}
    if not isinstance(usage_data, dict):
        return out
    claude = usage_data.get("claude")
    limits = claude.get("limits") if isinstance(claude, dict) else None
    for w in (limits or {}).get("windows") or []:
        if w.get("id") == "five_hour":
            out["claude_5h_used"] = w.get("used_pct")
        elif w.get("id") == "seven_day":
            out["claude_week_used"] = w.get("used_pct")
    agy = usage_data.get("agy")
    quota = agy.get("quota") if isinstance(agy, dict) else None
    for g in (quota or {}).get("groups") or []:
        for b in g.get("buckets") or []:
            out["agy"].append({"group": g.get("name") or "", "window": b.get("window") or "",
                               "remaining_pct": b.get("remaining_pct")})
    return out


def _label(c):
    parts = []
    if c["waiting"]:
        parts.append(f"{c['waiting']} need you")
    if c["failed_recent"] and not c["waiting"]:
        parts.append(f"{c['failed_recent']} failed")
    if c["running"]:
        parts.append(f"{c['running']} running")
    return " · ".join(parts) or "idle"


def glance(cfg, store, sched=None, usage_data=None, now=None):
    now = now if now is not None else time.time()
    midnight = local_midnight(now)
    rows = [r for r in store.all() if not r.get("cleared")]
    c = {"waiting": 0, "running": 0, "failed_recent": 0, "done_today": 0, "queued": 0}
    waiting = []
    for r in rows:
        st = r.get("status")
        end = r.get("ended") or r.get("updated") or 0
        if st == "waiting":
            c["waiting"] += 1
            waiting.append(r)
        elif st == "running":
            c["running"] += 1
        elif st == "failed" and now - end <= RECENT_FAIL_S:
            c["failed_recent"] += 1
        elif st == "done" and end >= midnight:
            c["done_today"] += 1
    tasks = getattr(sched, "tasks", None)
    if tasks is not None:
        try:
            c["queued"] = len(tasks.list(statuses=["queued", "blocked"]))
        except Exception:
            pass
    level = ("needs_you" if c["waiting"] else "failed" if c["failed_recent"]
             else "running" if c["running"] else "idle")
    oldest = None
    if waiting:
        w = min(waiting, key=lambda r: r.get("updated") or r.get("started") or now)
        since = w.get("updated") or w.get("started") or now
        oldest = {"id": w["id"], "title": w.get("title") or w.get("task") or "",
                  "project": os.path.basename((w.get("workspace") or "").rstrip("/")) or w.get("project") or "",
                  "since": max(0, round(now - since)), "activity": w.get("activity") or ""}
    return {"counts": c, "level": level, "color": COLORS[level], "label": _label(c),
            "oldest_waiting": oldest, "quota": _quota(usage_data),
            "paused": bool(sched and sched.paused), "updated": now}
