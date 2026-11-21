"""Planning a run: route a task, build the prompt and the command line (no processes are started here)."""
import os
import re
import shutil

from . import dispatch, obsidian, omniroute

PLACEHOLDER = re.compile(r"\{(prompt|model|effort|session)\}")


def env_for(tool_cfg, cfg, base_env=None, runner=None):
    """Child process environment for `tool_cfg`, applying `env_from` integrations (e.g. OmniRoute)."""
    env = dict(os.environ if base_env is None else base_env)
    source = (tool_cfg or {}).get("env_from")
    if not source:
        return env
    if source != "omniroute":
        raise ValueError(f"unknown env_from '{source}'")
    env = omniroute.apply_env(env, cfg) if runner is None else omniroute.apply_env(env, cfg, runner=runner)
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        raise ValueError("OmniRoute has no API key: run python3 hub.py omniroute connect")
    if not env.get("ANTHROPIC_BASE_URL"):
        raise ValueError("OmniRoute has no base_url configured")
    return env


def pick_effort(tool, tier, cfg):
    return (cfg["tools"][tool].get("efforts") or {}).get(tier, "")


def plan(task, cfg, tool=None, tier=None, project=None, model=None, quota=None, store=None, now=None):
    """Route a task: level by the router, tool/model by the dispatch table (overrides win)."""
    r = dispatch.route(task, cfg, level=tier, tool=tool, model=model, quota=quota, store=store, now=now)
    return {
        "tool": r["tool"], "tier": r["level"], "suggested_tier": r["suggested_level"],
        "level": r["level"], "suggested_level": r["suggested_level"],
        "model": r["model"], "effort": r["effort"], "reasons": r["reasons"],
        "borderline": r["borderline"], "fallback": r["fallback"], "project": project or "",
        "waiting_for_quota": r.get("waiting_for_quota", False),
        "earliest_until": r.get("earliest_until"),
        "original_tool": r.get("original_tool"), "original_model": r.get("original_model"),
    }


def readonly_args(tool):
    """Plan-mode flags that keep a headless agent from editing files."""
    return (["--permission-mode", "plan"] if tool in ("claude", "omniroute")
            else (["--mode", "plan"] if tool == "agy" else []))


def append_flags(argv, flags):
    """Append CLI option flags before `--` when a `--` positional separator is present."""
    flags = list(flags or [])
    if not flags:
        return list(argv)
    out = list(argv)
    if "--" in out:
        i = out.index("--")
        return out[:i] + flags + out[i:]
    return out + flags


def build_argv(tool_cfg, prompt, model, effort, read_only=False, session=None, resume=False):
    """Fill {prompt}/{model}/{effort} in one pass so text inside the prompt is never re-substituted.
    `session` pins (or, with resume=True, continues) a conversation for tools whose config has
    session_args / resume_args (Claude Code: --session-id / --resume).

    Prompts starting with `-` are protected from option parsing:
    - `claude` places `--` and the prompt at the end of argv (after all flags).
    - `agy` / `agy-headless-bridge` do not support `--` before `-p`/`-i` values and reject
      flag-like `-p` arguments, so a single leading space is prepended.
    """
    cmd = list(tool_cfg["command"])
    exe = os.path.basename(cmd[0]) if cmd else ""
    prompt_str = str(prompt) if prompt is not None else ""
    safe_prompt = (" " + prompt_str) if exe in ("agy", "agy-headless-bridge") and prompt_str.startswith("-") else prompt
    claude_dash_end = exe == "claude" and "{prompt}" in cmd and (prompt_str.startswith("-") or "--" in cmd)
    values = {"prompt": safe_prompt, "model": model, "effort": effort, "session": session or ""}
    fill = lambda a: PLACEHOLDER.sub(lambda m: values[m.group(1)], a)
    if read_only:
        stripped, i = [], 0
        while i < len(cmd):
            if cmd[i] in ("--mode", "--permission-mode"):
                i += 2
            elif cmd[i].startswith(("--mode=", "--permission-mode=")):
                i += 1
            else:
                stripped.append(cmd[i])
                i += 1
        cmd = stripped
    if claude_dash_end:
        filtered = []
        for idx, tok in enumerate(cmd):
            if tok == "{prompt}":
                continue
            if tok == "--" and idx + 1 < len(cmd) and cmd[idx + 1] == "{prompt}":
                continue
            filtered.append(tok)
        cmd = filtered
    argv = [fill(a) for a in cmd]
    if effort and not re.search(r"-(extra-low|low|medium|high|xhigh|max)$", str(model or "")):
        argv += [fill(a) for a in tool_cfg.get("effort_args", [])]   # not when the slug already carries an effort
    if session:
        argv += [fill(a) for a in tool_cfg.get("resume_args" if resume else "session_args", [])]
    if claude_dash_end:
        argv += ["--", prompt_str]
    return argv


def headless_json_argv(argv, tool, cfg):
    """Claude print-mode argv plus the JSON output flag, when the switch tools.claude.json_output is on (hub/claudejson.py).
    Only for a run that really is headless: not for agy, not for a live session (no -p), not when the command already
    picks a format. Planner and review runs build their own argv and are never touched."""
    from . import claudejson
    if tool != "claude" or "-p" not in argv or not claudejson.enabled(cfg, tool):
        return argv
    if any(a == "--output-format" or a.startswith("--output-format=") for a in argv):
        return argv
    return append_flags(argv, claudejson.FLAGS)


def build_bridge_argv(tool_cfg, prompt, model, effort, read_only=False):
    """Build the fallback argv (e.g. agy-headless-bridge) when configured and available on PATH."""
    bridge_cmd = tool_cfg.get("bridge_command")
    if not bridge_cmd or not shutil.which(bridge_cmd[0]):
        return None
    return build_argv({**tool_cfg, "command": bridge_cmd}, prompt, model, effort, read_only=read_only)


def build_prompt(task, cfg, project):
    if not project:
        return task, {}
    ctx, meta = obsidian.project_context(cfg, project)
    if not ctx or not cfg["obsidian"].get("inject_project_note", True):
        return task, meta
    return (f"Project brain notes for '{project}' (from {project}/.brain/, for context only):\n"
            f"<<<\n{ctx}\n>>>\n\nTask: {task}"), meta


def interactive_argv(tool, argv, unattended=False):
    """The same command as a live CLI session instead of a one-shot print run: claude without -p takes the
    prompt as its first message; agy -i (--prompt-interactive) does the same. None for tools without one.
    `unattended` (queue / pipeline runs): a read-only claude session may not end on the plan-approval dialog,
    which nobody is there to answer; it finishes with its findings as text instead."""
    if tool in ("claude", "omniroute") and "-p" in argv:
        out = list(argv)
        out.remove("-p")
        if "--permission-mode" not in out:   # live sessions start in auto mode, no approval prompts
            out = append_flags(out, ["--permission-mode", "auto"])
        elif unattended and out[out.index("--permission-mode") + 1:][:1] == ["plan"]:
            out = append_flags(out, ["--disallowedTools", "ExitPlanMode"])
        return out
    if tool == "agy" and "-p" in argv:
        out = [("-i" if a == "-p" else a) for a in argv]
        if "--mode" not in out:
            out += ["--mode", "auto"]
        return out
    return None
