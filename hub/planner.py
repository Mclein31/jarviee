"""Split a big goal into small, routed tasks.

The planner is Claude (sonnet) in headless mode, read-only (`--permission-mode plan`), fed the
project's .brain/ files. It must answer with STRICT JSON; the answer is validated, retried once
when invalid, and sanity-checked against the router.
"""
import json
import re
import subprocess
from pathlib import Path

from . import dispatch, obsidian
from .launcher import build_argv, build_bridge_argv, readonly_args
from .router import LEVELS, assess, fit_effort, normalize, pick_effort, pick_model, rank
from .runplan import append_flags

MAX_TASKS = 20
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

INSTRUCTIONS = """You are the planner for Jarviee. Split the GOAL into small tasks that separate
coding agents can do one at a time. Do not edit any files; only plan.

Answer with STRICT JSON only: a JSON array, no prose, no markdown fences. Each item:
{"id": "t1", "title": "short imperative title", "prompt": "full instructions for the agent",
 "level": "basic|light|complex|heavy", "files": ["likely/paths/to/touch"],
 "depends_on": ["ids that must finish first"], "acceptance": "how to verify it is done"}

Levels:
- basic: rename, typo, formatting, copy/text tweaks, single-line config, version bumps
- light: small single-file feature or fix, simple component, docs, straightforward tests
- complex: multi-file feature, non-trivial bug, API/DB changes, refactor of one module
- heavy: architecture, cross-cutting refactor/migration, security/auth, concurrency, hard debugging

Rules: prefer more, smaller tasks over one big task; give the lowest honest level; list the
files each task will likely touch so tasks that share files are not run together; only add
depends_on when a task truly needs another's result; at most 20 tasks."""


class PlanError(ValueError):
    pass


def brain_context(cfg, project):
    p = obsidian.find_project(cfg, project) if project else None
    if not p:
        return ""
    pc = cfg.get("planner", {})
    brain = p / cfg["obsidian"]["project_brain_dir"]
    parts = []
    for name in pc.get("context_files", ["PROJECT_CONTEXT.md", "ARCHITECTURE.md", "TODO.md"]):
        f = brain / name
        if f.is_file():
            _, body = obsidian.parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            parts.append(f"### .brain/{name}\n{body.strip()}")
    text = "\n\n".join(parts)
    limit = int(pc.get("max_context_chars", 12000))
    return text if len(text) <= limit else text[:limit] + "\n…(trimmed)"


def build_prompt(goal, cfg, project, error=""):
    ctx = brain_context(cfg, project)
    out = INSTRUCTIONS + (f"\n\nPROJECT: {project}\n<<<\n{ctx}\n>>>" if ctx else "") + f"\n\nGOAL: {goal}"
    if error:
        out += (f"\n\nYour previous answer was rejected: {error}\n"
                "Reply again with the JSON array only.")
    return out


def _extract_json(text):
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    if not t.startswith("["):
        a, b = t.find("["), t.rfind("]")
        if a == -1 or b <= a:
            raise PlanError("no JSON array found in the planner's answer")
        t = t[a:b + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise PlanError(f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}") from None


def validate(data):
    """Return a clean task list or raise PlanError naming the first problem."""
    if not isinstance(data, list) or not data:
        raise PlanError("expected a non-empty JSON array of tasks")
    if len(data) > MAX_TASKS:
        raise PlanError(f"too many tasks ({len(data)}); at most {MAX_TASKS}")
    tasks, ids = [], set()
    for i, t in enumerate(data, 1):
        where = f"task {i}"
        if not isinstance(t, dict):
            raise PlanError(f"{where} is not an object")
        tid = str(t.get("id") if t.get("id") is not None else "").strip()
        if not ID_RE.match(tid) or tid in ids:
            raise PlanError(f"{where}: id must be unique and match {ID_RE.pattern}")
        ids.add(tid)
        for key in ("title", "prompt"):
            if not isinstance(t.get(key), str) or not t[key].strip():
                raise PlanError(f"{where} ({tid}): '{key}' must be a non-empty string")
        try:
            level = normalize(t.get("level"))
        except ValueError:
            level = ""
        if not level:
            raise PlanError(f"{where} ({tid}): level must be one of {LEVELS}")
        files = t.get("files", [])
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise PlanError(f"{where} ({tid}): files must be a list of paths")
        raw_deps = t.get("depends_on", [])
        if not isinstance(raw_deps, list) or not all(isinstance(d, str) for d in raw_deps):
            raise PlanError(f"{where} ({tid}): depends_on must be a list of task ids")
        deps = [d.strip() for d in raw_deps if d.strip()]
        acc = t.get("acceptance", "")
        tasks.append({"id": tid, "title": t["title"].strip(), "prompt": t["prompt"].strip(), "level": level,
                      "files": [f.strip() for f in files if f.strip()], "depends_on": deps,
                      "acceptance": ("" if acc is None else str(acc)).strip()})
    for t in tasks:
        bad = [d for d in t["depends_on"] if d not in ids or d == t["id"]]
        if bad:
            raise PlanError(f"task {t['id']}: unknown or self dependency {bad}")
    _check_cycles(tasks)
    return tasks


def _check_cycles(tasks):
    deps = {t["id"]: set(t["depends_on"]) for t in tasks}
    done = set()
    while deps:
        ready = [k for k, v in deps.items() if v <= done]
        if not ready:
            raise PlanError(f"dependency cycle among {sorted(deps)}")
        for k in ready:
            done.add(k)
            deps.pop(k)


def parse(text):
    return validate(_extract_json(text))

def claude_runner(cfg, project, quota=None, state=None, cwd=None):
    """Return run(prompt) -> stdout, calling the planner tool headless and read-only."""
    pc = cfg.get("planner", {})
    tool = pc.get("tool", "claude")
    model = pc.get("model", "sonnet")
    if not dispatch.tool_available(tool, cfg, quota)[0]:
        alt = dispatch.other_tool(tool, cfg)
        if dispatch.tool_available(alt, cfg, quota)[0]:
            tool, model = alt, pick_model(alt, "light", cfg)
    if state is not None:
        state["used"] = f"{tool} · {model}"
    p = obsidian.find_project(cfg, project) if project else (Path(cwd) if cwd else None)

    def _call(cur_tool, cur_model, prompt):
        tool_cfg = cfg["tools"][cur_tool]
        argv = build_argv(tool_cfg, prompt, cur_model, "", read_only=True)
        default_ro = readonly_args(cur_tool)
        extra = pc.get("extra_args", default_ro) if cur_tool == pc.get("tool", "claude") else default_ro
        argv = append_flags(argv, extra)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=pc.get("timeout", 300),
                               cwd=str(p) if p else None, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise PlanError(f"planner tool '{argv[0]}' not found on PATH") from None
        except subprocess.TimeoutExpired:
            raise PlanError("planner timed out") from None
        if cur_tool == "agy" and r.returncode == 0 and not (r.stdout or "").strip():
            bridge = build_bridge_argv(tool_cfg, prompt, cur_model, "", read_only=True)
            if bridge:
                try:
                    r = subprocess.run(append_flags(bridge, extra), capture_output=True, text=True,
                                       timeout=pc.get("timeout", 300), cwd=str(p) if p else None,
                                       stdin=subprocess.DEVNULL)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        return r

    def run(prompt):
        nonlocal tool, model
        r = _call(tool, model, prompt)
        out = f"{r.stdout or ''}\n{r.stderr or ''}".strip()
        hit = dispatch.quota_hit(out, cfg) if r.returncode != 0 else ""
        if hit:
            if isinstance(quota, dict):
                quota[tool] = hit
            alt = dispatch.other_tool(tool, cfg)
            if alt != tool and dispatch.tool_available(alt, cfg, quota)[0]:
                tool, model = alt, pick_model(alt, "light", cfg)
                if state is not None:
                    state["used"] = f"{tool} · {model} (fallback: {hit})"
                r = _call(tool, model, prompt)
                out = f"{r.stdout or ''}\n{r.stderr or ''}".strip()
        if r.returncode != 0:
            raise PlanError(f"planner exited {r.returncode}: {out[-300:].strip()}")
        return r.stdout
    return run


def heuristic_tasks(goal):
    """Offline stand-in for the planner: one task per clause of the goal."""
    parts = [s.strip(" .") for s in re.split(r"\s*(?:;|,|\band then\b|\band\b)\s*", goal) if s.strip(" .")]
    return [{"id": f"t{i}", "title": s[:60], "prompt": s, "level": "light", "files": [],
             "depends_on": [], "acceptance": ""} for i, s in enumerate(parts or [goal], 1)]


def make_plan(goal, cfg, project=None, runner=None, planner="claude", quota=None, cwd=None):
    """Plan, validate (one retry), sanity-check levels and route every task."""
    notes = []
    if planner == "mock":
        raw = validate(heuristic_tasks(goal))
        for t in raw:
            t["level"] = assess(t["prompt"], cfg)["level"]
        used = "mock (split on clauses)"
    else:
        pc = cfg.get("planner", {})
        state = {"used": f"{pc.get('tool', 'claude')} · {pc.get('model', 'sonnet')}"}
        run = runner or claude_runner(cfg, project, quota=quota, state=state, cwd=cwd)
        try:
            raw = parse(run(build_prompt(goal, cfg, project)))
        except PlanError as e:
            notes.append(f"planner answer rejected ({e}); retried once")
            raw = parse(run(build_prompt(goal, cfg, project, error=str(e))))
        used = state["used"]

    tasks = []
    for t in raw:
        a = assess(t["prompt"], cfg, brief=False)   # planner prompts are long by design
        planner_level, router_level = t["level"], a["level"]
        final = planner_level
        if abs(rank(planner_level) - rank(router_level)) >= 2:
            final = max(planner_level, router_level, key=rank)
            notes.append(f"{t['id']}: planner said {planner_level}, router said {router_level}; using {final}")
        r = dispatch.route(t["prompt"], cfg, level=final, quota=quota)
        tool, model, note = r["tool"], r["model"], r["fallback"]
        effort = pick_effort(t["prompt"], tool, final, cfg, brief=False, model=model)[0]   # planner prompts are long by design
        borderline = {planner_level, router_level} == {"light", "complex"} or (a["borderline"] and final == "complex")
        if final == "complex" and borderline and cfg.get("prefer_agy_on_borderline") and not r["fallback"]:
            light = cfg["dispatch"]["light"]
            tool = light["tool"]
            model = pick_model(tool, final, cfg)
            effort = pick_effort(t["prompt"], tool, final, cfg, brief=False, model=model)[0]
            note = "borderline light/complex: agy first, escalates to Claude if it fails"
        model, effort = fit_effort(tool, model, effort, cfg)
        tasks.append({**t, "level": final, "planner_level": planner_level, "router_level": router_level,
                      "tool": tool, "model": model, "effort": effort, "route_note": note})
    return {"goal": goal, "project": project or "", "planner": used, "notes": notes, "tasks": tasks}


def format_table(plan):
    """Plain-text table: id, title, level, tool, model, depends_on."""
    rows = [("id", "title", "level", "tool", "model", "depends_on")]
    for t in plan["tasks"]:
        tid = t["id"].split(".")[-1]
        deps = ", ".join(d.split(".")[-1] for d in t["depends_on"]) or "-"
        lvl = t["level"] + ("*" if t.get("planner_level") and t["planner_level"] != t["level"] else "")
        rows.append((tid, t["title"][:48], lvl, t["tool"], t["model"], deps))
    widths = [max(len(str(r[i])) for r in rows) for i in range(6)]
    lines = ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    extra = [f"  {t['id'].split('.')[-1]}: {t['route_note']}" for t in plan["tasks"] if t.get("route_note")]
    return "\n".join(lines + (["", "Routing notes:"] + extra if extra else [])
                     + (["", "Planner notes:"] + [f"  {n}" for n in plan["notes"]] if plan["notes"] else []))
