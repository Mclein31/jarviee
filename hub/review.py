"""One read-only review pass after a whole plan finishes. Reports findings; never fixes."""
import os
import subprocess
from pathlib import Path

from . import brand, dispatch, obsidian
from .launcher import build_argv, build_bridge_argv, readonly_args
from .router import pick_model
from .runplan import append_flags

SKILL_DIRS = [Path.home() / ".claude" / "skills", Path.home() / ".claude" / "commands"]
AGY_SKILL_DIRS = [Path.home() / ".gemini" / "config" / "skills"]
_SKILL_ENV = {"claude": "CLAUDE_SKILL_DIRS", "agy": "AGY_SKILL_DIRS"}


def skill_dirs_for(tool="claude", skill_dirs=None):
    """Return the directories searched for `tool`'s installed skills (overridable by arg or env var)."""
    if skill_dirs is not None:
        if isinstance(skill_dirs, dict):
            dirs = skill_dirs.get(tool, ())
        elif isinstance(skill_dirs, (str, Path)):
            dirs = (skill_dirs,)
        else:
            dirs = skill_dirs
        return [Path(d).expanduser() for d in dirs if d]
    env_key = _SKILL_ENV.get(tool)
    if env_key:
        raw = brand.env(env_key)
        if raw is not None:
            return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
    if tool == "claude":
        return list(SKILL_DIRS)
    if tool == "agy":
        return list(AGY_SKILL_DIRS)
    return []


def skill_installed(name, tool="claude", skill_dirs=None):
    """True when `name` is installed in `tool`'s skill directories."""
    name = (name or "").strip()
    if not name or Path(name).name != name or name in (".", ".."):
        return False
    return any((d / name).exists() or (d / f"{name}.md").exists() for d in skill_dirs_for(tool, skill_dirs))


def combined_changes(cfg, plan):
    """The combined diff when the project is a git repo, else the claimed files and summaries."""
    p = obsidian.find_project(cfg, plan["project"]) if plan["project"] else None
    done = [t for t in plan["tasks"] if t["status"] == "done"]
    files = sorted({f for t in done for f in t["files"]})
    base = plan.get("base")   # a pipeline diffs from where it started: every finished task auto-commits
    if p and (p / ".git").exists():
        try:
            spec = [base] if base else ["HEAD", "--", *files]
            r = subprocess.run(["git", "-C", str(p), "diff", "--stat", "--patch", *spec],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                return "git diff:\n" + r.stdout[-30000:]
        except (OSError, subprocess.TimeoutExpired):
            pass
    lines = ["The project is not a git repository, so there is no diff. Read these files:"]
    lines += [f"- {f}" for f in files] or ["- (no files were listed)"]
    lines.append("\nWhat each task reported:")
    for t in done:
        lines.append(f"\n## {t['id']}: {t['title']}\n{t.get('summary') or '(no summary)'}")
    return "\n".join(lines)


def review_prompt(cfg, plan, tool=None, skill_dirs=None):
    rc = cfg.get("review", {})
    target = tool or rc.get("tool", "claude")
    use = f"Use the {rc['skill']} skill. " if skill_installed(rc.get("skill"), tool=target, skill_dirs=skill_dirs) else ""
    return (f"{use}Review the combined changes made by several agents for this goal:\n{plan['goal']}\n\n"
            f"{combined_changes(cfg, plan)}\n\n"
            "Report findings only, most severe first, each with file:line, severity and why it matters. "
            "Do not edit any files and do not fix anything.")


def review_tool(cfg, quota=None):
    """(tool, model) the review runs on: config review.tool, or the other tool when that one is out of quota."""
    rc = cfg.get("review", {})
    tool = rc.get("tool", "claude")
    model = rc.get("model", "sonnet")
    if not dispatch.tool_available(tool, cfg, quota)[0]:
        alt = dispatch.other_tool(tool, cfg)
        if dispatch.tool_available(alt, cfg, quota)[0]:
            tool, model = alt, pick_model(alt, "light", cfg)
    return tool, model


def run_review(cfg, tasks, plan_id, quota=None):
    """Run the review and store its text on the plan. Safe to call from a thread."""
    rc = cfg.get("review", {})
    plan = tasks.plan(plan_id)
    if not plan or not rc.get("enabled", True) or not any(t["status"] == "done" for t in plan["tasks"]):
        return ""
    tool, model = review_tool(cfg, quota)
    prompt = review_prompt(cfg, plan, tool=tool)
    p = obsidian.find_project(cfg, plan["project"]) if plan["project"] else None
    tasks.set_plan(plan_id, review="(review running)")

    def _call(cur_tool, cur_model):
        tool_cfg = cfg["tools"][cur_tool]
        argv = build_argv(tool_cfg, prompt, cur_model, "", read_only=True)
        extra = readonly_args(cur_tool)
        r = subprocess.run(append_flags(argv, extra), capture_output=True, text=True, timeout=rc.get("timeout", 600),
                           cwd=str(p) if p else None, stdin=subprocess.DEVNULL)
        if cur_tool == "agy" and r.returncode == 0 and not (r.stdout or "").strip():
            bridge = build_bridge_argv(tool_cfg, prompt, cur_model, "", read_only=True)
            if bridge:
                r = subprocess.run(append_flags(bridge, extra), capture_output=True, text=True,
                                   timeout=rc.get("timeout", 600), cwd=str(p) if p else None,
                                   stdin=subprocess.DEVNULL)
        return r

    try:
        r = _call(tool, model)
        out = f"{r.stdout or ''}\n{r.stderr or ''}".strip()
        hit = dispatch.quota_hit(out, cfg) if r.returncode != 0 else ""
        if hit:
            if isinstance(quota, dict):
                quota[tool] = hit
            alt = dispatch.other_tool(tool, cfg)
            if alt != tool and dispatch.tool_available(alt, cfg, quota)[0]:
                r = _call(alt, pick_model(alt, "light", cfg))
                out = f"{r.stdout or ''}\n{r.stderr or ''}".strip()
        if r.returncode != 0:
            text = f"(review exited {r.returncode}) {out[-300:]}"
        else:
            text = r.stdout.strip() or f"(review produced no output; exit {r.returncode}) {r.stderr[-300:]}"
    except (OSError, subprocess.TimeoutExpired) as e:
        text = f"(review could not run: {e})"
    tasks.set_plan(plan_id, review=text)
    return text
