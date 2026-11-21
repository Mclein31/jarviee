"""Project maps: how each project works, as a graph the dashboard can draw.

A map lives in the project's own brain, `<project>/.brain/FLOW.json`:
  nodes  {id, label, kind, layer, detail?, file?}   layer: frontend | backend | external
  edges  {from, to, label?}
  flows  {name, steps: [node id, ...]}              named user journeys, optional
An agent writes it ("Map this project" launches one with `brief()`); `load()` validates it so a bad file can only
produce a clear error, never a broken page. Positions the owner drags nodes to are Jarviee's, not the project's:
they go to ~/.jarviee/flow-layout.json, keyed by project and view, so moving a box never touches a repo.
"""
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from . import brand, obsidian

FLOW_FILE = "FLOW.json"
MAX_BYTES = 512_000
MAX_NODES, MAX_EDGES, MAX_FLOWS, MAX_STEPS = 300, 900, 40, 60
LAYERS = ("frontend", "backend", "external")
KINDS = ("actor", "screen", "component", "api", "service", "job", "store", "external", "config", "decision")
VIEWS = ("full", "frontend", "backend")
ID_RE = re.compile(r"^[A-Za-z0-9_.:/@#-]{1,80}$")
COORD_MAX = 100_000
_lock = threading.Lock()


def _text(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def flow_path(cfg, project):
    p = obsidian.find_project(cfg, project)
    return (p / obsidian._b(cfg)["project_brain_dir"] / FLOW_FILE) if p else None


def flow_mtime(project_dir, cfg):
    """mtime of a project's FLOW.json or None; for the state poll, so the page knows when to re-fetch."""
    try:
        return (project_dir / obsidian._b(cfg)["project_brain_dir"] / FLOW_FILE).stat().st_mtime
    except OSError:
        return None


def validate(raw):
    """(flow, warnings) from parsed JSON. Drops what it can't use and says why; raises ValueError when nothing is usable."""
    if not isinstance(raw, dict):
        raise ValueError("FLOW.json must be a JSON object with nodes and edges")
    warn, nodes, seen = [], [], set()
    for n in (raw.get("nodes") or [])[:MAX_NODES]:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        if not ID_RE.match(nid) or nid in seen:
            warn.append(f"skipped node with a missing, odd or repeated id: {nid[:40]!r}")
            continue
        seen.add(nid)
        layer = str(n.get("layer") or "").lower()
        kind = str(n.get("kind") or "").lower()
        nodes.append({"id": nid, "label": _text(n.get("label") or nid, 80),
                      "kind": kind if kind in KINDS else "component",
                      "layer": layer if layer in LAYERS else "backend",
                      "detail": _text(n.get("detail"), 400), "file": _text(n.get("file"), 200)})
    if not nodes:
        raise ValueError("FLOW.json has no usable nodes")
    if len(raw.get("nodes") or []) > MAX_NODES:
        warn.append(f"only the first {MAX_NODES} nodes are shown")
    edges, pairs = [], set()
    for e in (raw.get("edges") or [])[:MAX_EDGES]:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("from") or ""), str(e.get("to") or "")
        if a not in seen or b not in seen or a == b:
            warn.append(f"skipped edge {a[:30]} -> {b[:30]}: unknown node")
            continue
        label = _text(e.get("label"), 60)
        if (a, b, label) in pairs:
            continue
        pairs.add((a, b, label))
        edges.append({"from": a, "to": b, "label": label})
    flows, linked = [], {(e["from"], e["to"]) for e in edges}
    for f in (raw.get("flows") or [])[:MAX_FLOWS]:
        if not isinstance(f, dict):
            continue
        steps = [s for s in (f.get("steps") or [])[:MAX_STEPS] if isinstance(s, str) and s in seen]
        if len(steps) >= 2:
            name = _text(f.get("name") or "Journey", 60)
            flows.append({"name": name, "steps": steps})
            gaps = [f"{a} -> {b}" for a, b in zip(steps, steps[1:]) if (a, b) not in linked and (b, a) not in linked]
            if gaps:
                warn.append(f"journey '{name}' has no link for {', '.join(gaps[:3])}")
    return {"title": _text(raw.get("title"), 80), "summary": _text(raw.get("summary"), 400),
            "nodes": nodes, "edges": edges, "flows": flows}, warn[:12]


def _layout_file(cfg):
    if (cfg or {}).get("flow_layout_path"):   # tests point this at a temp file
        return Path(cfg["flow_layout_path"])
    return brand.home() / "flow-layout.json"


def _read_layouts(cfg):
    try:
        data = json.loads(_layout_file(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(cfg, project):
    """(http code, body) for GET /api/flow?project=."""
    path = flow_path(cfg, project)
    if not path:
        return 404, {"error": f"no project '{project}' with a .brain/ folder"}
    layout = (_read_layouts(cfg).get(path.parent.parent.name) or {})
    if not path.is_file():
        return 200, {"ok": False, "missing": True, "project": path.parent.parent.name, "layout": layout}
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError(f"FLOW.json is over {MAX_BYTES // 1000} KB")
        flow, warn = validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:   # json.JSONDecodeError is a ValueError
        return 200, {"ok": False, "error": str(e)[:300], "project": path.parent.parent.name, "layout": layout}
    return 200, {"ok": True, "project": path.parent.parent.name, "flow": flow, "warnings": warn,
                 "layout": layout, "mtime": path.stat().st_mtime}


def save_layout(cfg, body):
    """(http code, body) for POST /api/flow/layout {project, view, positions: {id: [x, y]}} (positions null resets the view)."""
    p = obsidian.find_project(cfg, str(body.get("project") or ""))
    view = str(body.get("view") or "")
    if not p or view not in VIEWS:
        return 400, {"error": "unknown project or view"}
    pos = body.get("positions")
    clean = {}
    if pos is not None:
        if not isinstance(pos, dict) or len(pos) > MAX_NODES:
            return 400, {"error": "positions must be an object of at most %d nodes" % MAX_NODES}
        for k, v in pos.items():
            if (ID_RE.match(str(k)) and isinstance(v, (list, tuple)) and len(v) == 2
                    and all(isinstance(c, (int, float)) and abs(c) <= COORD_MAX for c in v)):
                clean[str(k)] = [round(float(v[0]), 1), round(float(v[1]), 1)]
    with _lock:
        data = _read_layouts(cfg)
        views = data.setdefault(p.name, {})
        if clean:
            views[view] = clean
        else:
            views.pop(view, None)
        f = _layout_file(cfg)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    return 200, {"ok": True, "saved": len(clean)}


FORMAT = (
    "Write valid JSON only, no comments, with these keys:\n"
    "- title: short name of the system\n"
    "- summary: one or two sentences on what it does\n"
    "- nodes: a list of objects with id (letters, digits, dot, dash, underscore, colon or slash; unique), "
    "label (short display name, at most about 24 characters), kind (one of: " + ", ".join(KINDS) + "), "
    "layer (frontend for anything running in the browser, app or UI, including the user; backend for servers, "
    "APIs, jobs and databases; external for third-party services), detail (one sentence: what it does), "
    "file (main source path, relative to the project root, or empty)\n"
    "- edges: a list of objects with from and to (node ids, in the direction data or control flows) and a short "
    "label naming the call or relation, for example GET /api/state, renders, reads, writes, enqueues\n"
    "- flows: 2 to 6 named user journeys, each an object with name and steps (node ids in order, starting from "
    "the user); every consecutive pair of steps must be joined by an edge\n\n"
    "The map is drawn as a flowchart: actor is a rounded terminator, screen and component are rectangles, api a "
    "parallelogram, service a subroutine box, job a hexagon, store a cylinder, config a document, external a dashed "
    "box, and decision a diamond. Use a decision node (label it as a short question, like Signed in? or Payment ok?) "
    "wherever the flow really branches on a condition in the code, with edges labelled yes and no (or the outcomes), "
    "and include it in the journeys that pass through it.\n\n"
)
RULES = (f"Write only .brain/{FLOW_FILE}. Don't run shell commands or git: Jarviee checks the file and commits it "
         "when you finish.")


def brief(project):
    """The task text for an agent that maps a project. No square brackets: the composer strips them."""
    return (
        f"Map how the {project} project works for the Jarviee Project Maps view, then write it to .brain/{FLOW_FILE}.\n\n"
        "Read the code and the .brain notes first. Map the whole user flow end to end: who uses it, the screens and "
        "components they touch, the API routes those call, the services, background jobs and data stores behind them, "
        "and any outside services. Aim for 12 to 60 nodes: one node per meaningful part, not per file.\n\n"
        + FORMAT + RULES
    )


def update_brief(project, since, changed):
    """Task text for an agent that brings an existing map up to date with the code that changed since `since`."""
    files = "\n".join(f"- {f}" for f in changed[:60]) + (f"\n- and {len(changed) - 60} more" if len(changed) > 60 else "")
    return (
        f"Update the project map of {project} at .brain/{FLOW_FILE} so it matches the code again. "
        f"These files changed since the map was last updated (commit {since[:8]}):\n{files}\n\n"
        "Read the current map first, then those files and whatever they touch. Keep everything that still exists "
        "exactly as it is: same ids, labels and structure, so the owner's saved layout stays. Add nodes and edges for "
        "new features, screens, routes, services, jobs, tables and integrations; remove nodes whose code is gone; fix "
        "details, files and edges that changed; extend or add user journeys the new features create. If nothing in "
        "the map is affected, leave the file unchanged.\n\n" + FORMAT + RULES
    )


def _git(path, *args, timeout=10):
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def commit_flow(project_dir, message):
    """(committed, note) after committing only .brain/FLOW.json in the project's repo. Never raises."""
    rel = f".brain/{FLOW_FILE}"
    if not (project_dir / ".git").exists():
        return False, "not a git repo"
    if _git(project_dir, "check-ignore", "-q", rel)[0] == 0:
        return False, f"{rel} is gitignored"
    if not _git(project_dir, "status", "--porcelain", "--", rel)[1]:
        return False, "no change to commit"
    code, _, err = _git(project_dir, "add", "--", rel)
    if code:
        return False, f"git add failed: {err[:160]}"
    code, out, err = _git(project_dir, "commit", "-q", "-m", message, "--", rel)
    if code:
        return False, f"git commit failed: {(err or out)[:160]}"
    return True, _git(project_dir, "rev-parse", "--short", "HEAD")[1]
