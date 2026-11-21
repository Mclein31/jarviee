"""Daily standup / evening review (GET /api/standup?date=YYYY-MM-DD, `hub standup`). No LLM: it only
counts what the agents table and each project's .brain/TODO.md already say."""
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import obsidian

OWNER_RE = re.compile(r"^\s*[-*]\s*👤\s*(.+)$")


def day_bounds(date=None):
    d = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def owner_items(cfg, project):
    """Open 👤 lines from <project>/.brain/TODO.md."""
    p = obsidian.find_project(cfg, project)
    f = Path(p) / ".brain" / "TODO.md" if p else None
    try:
        return [m.group(1).strip()[:200] for l in f.read_text(encoding="utf-8").splitlines() if (m := OWNER_RE.match(l))] if f else []
    except (OSError, AttributeError):
        return []


def standup(cfg, store, date=None, now=None):
    lo, hi = day_bounds(date)
    now = now if now is not None else time.time()
    projects = {}
    for r in store.all():
        end = r.get("ended") or r.get("updated") or 0
        started = r.get("started") or 0
        if not (lo <= end < hi or lo <= started < hi):
            continue
        p = projects.setdefault(r.get("project") or "(no project)", {"done": 0, "failed": 0, "running": 0, "seconds": 0, "escalations": 0, "runs": []})
        st = r.get("status")
        if st == "done": p["done"] += 1
        elif st == "failed": p["failed"] += 1
        elif st in ("running", "waiting", "queued"): p["running"] += 1
        if started and (r.get("ended") or st in ("running", "waiting")):
            p["seconds"] += max(0, int((r.get("ended") or min(now, hi)) - started))
        if r.get("fallback_from"): p["escalations"] += 1
        p["runs"].append({"id": r.get("id"), "title": r.get("title") or (r.get("task") or "")[:60], "status": st, "tool": r.get("tool"), "model": r.get("model")})
    for name, p in projects.items():
        p["owner_items"] = owner_items(cfg, name)
    return {"date": datetime.fromtimestamp(lo).strftime("%Y-%m-%d"), "projects": projects,
            "totals": {k: sum(p[k] for p in projects.values()) for k in ("done", "failed", "running", "seconds", "escalations")}}


def render(s):
    t = s["totals"]
    out = [f"Standup {s['date']}: {t['done']} done, {t['failed']} failed, {t['running']} running, {t['seconds'] // 60} min of agent time, {t['escalations']} rerouted"]
    for name, p in sorted(s["projects"].items()):
        out.append(f"\n{name}: {p['done']} done, {p['failed']} failed, {p['running']} running, {p['seconds'] // 60} min")
        out += [f"  [{r['status']}] {r['title']}" for r in p["runs"]]
        out += [f"  👤 {i}" for i in p["owner_items"]]
    return "\n".join(out)
