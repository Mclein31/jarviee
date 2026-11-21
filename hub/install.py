"""Merge Jarviee's reporter hooks into Antigravity and Claude Code config files.

Safe by design: other hooks and settings are kept, the file is backed up before
any write, the write is atomic, re-running is idempotent, and --uninstall
removes only our own entries.
"""
import copy
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import brand
from .config import ROOT

REPORT = ROOT / "hooks" / "report.py"
HOOK_NAME = "jarviee"
LEGACY_HOOK_NAMES = ("agent-hub",)   # agy hooks.json key before the rename
TIMEOUT = 5
# PreToolUse is left out for Antigravity on purpose: {} unmarshals with
# decision="" and deprecated allow_tool=false, which agy's migrateDeprecatedFields
# rewrites to decision="deny" (blocking every tool call), and no decision value is
# neutral ("allow" skips permission prompts, "ask"/"force_ask" adds prompts).
# PostToolUse gives tool activity instead.
AGY_EVENTS = ["SessionStart", "PreInvocation", "PostToolUse", "PostInvocation", "Stop"]
AGY_GROUPED = {"PreToolUse", "PostToolUse"}
# The extra events (docs: code.claude.com/docs/en/hooks) are observed only: the reporter prints nothing for
# them, and empty output is "no decision" for each (PermissionRequest, SubagentStop and TaskCompleted can
# gate in principle; StopFailure / PostToolUseFailure / SubagentStart cannot).
CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                 "Stop", "Notification", "SessionEnd",
                 "PermissionRequest", "PostToolUseFailure", "StopFailure",
                 "SubagentStart", "SubagentStop", "TaskCompleted"]
CLAUDE_MATCHER = {"PreToolUse": "*", "PostToolUse": "*"}

PATHS = {
    "agy": lambda: Path(brand.env("AGY_HOOKS", "~/.gemini/config/hooks.json")).expanduser(),
    "claude": lambda: Path(brand.env("CLAUDE_SETTINGS", "~/.claude/settings.json")).expanduser(),
}


def _q(s):
    return f'"{s}"' if os.name == "nt" else shlex.quote(str(s))


def hook_command(event, tool):
    return f"{_q(sys.executable)} {_q(REPORT)} {event} {tool}"


def is_ours(handler):
    cmd = str((handler or {}).get("command", "")) if isinstance(handler, dict) else ""
    return "report.py" in cmd and (str(REPORT) in cmd or _q(REPORT) in cmd or "agent-hub" in cmd
                                   or "jarviee" in cmd.lower())


class Malformed(ValueError):
    """The settings file parses, but its hooks are not shaped the way the tool reads them: left untouched."""


def _handler(event, tool):
    return {"type": "command", "command": hook_command(event, tool), "timeout": TIMEOUT}


def merge_agy(data, uninstall=False):
    out = copy.deepcopy(data or {})
    for name in (HOOK_NAME, *LEGACY_HOOK_NAMES):
        out.pop(name, None)
    if not uninstall:
        spec = {}
        for ev in AGY_EVENTS:
            h = _handler(ev, "agy")
            spec[ev] = [{"matcher": "*", "hooks": [h]}] if ev in AGY_GROUPED else [h]
        out[HOOK_NAME] = spec
    return out


def merge_claude(data, uninstall=False):
    """Our hook groups added (or removed), everything else kept exactly as it was: the owner's own hooks, groups we
    don't understand, and empty entries. Raises Malformed instead of replacing a `hooks` value that isn't the
    {event: [group, ...]} shape (it used to be dropped as if it were empty)."""
    out = copy.deepcopy(data or {})
    hooks = out.get("hooks")
    if hooks is None:
        hooks = {}
    elif not isinstance(hooks, dict):
        if uninstall:
            return out   # nothing of ours can be in it: we only ever write an object
        raise Malformed(f"\"hooks\" is a {type(hooks).__name__}, not an object")
    removed_any = False
    for ev in list(hooks):
        if not isinstance(hooks[ev], list):
            if ev in CLAUDE_EVENTS and not uninstall:
                raise Malformed(f"\"hooks.{ev}\" is a {type(hooks[ev]).__name__}, not a list")
            continue   # not ours to judge: kept as is
        groups, removed = [], False
        for g in hooks[ev]:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list) and any(is_ours(h) for h in g["hooks"]):
                removed = True
                g = dict(g, hooks=[h for h in g["hooks"] if not is_ours(h)])
                if not g["hooks"]:
                    continue   # the group held only our handler
            groups.append(g)
        removed_any |= removed
        if groups or not removed:
            hooks[ev] = groups
        else:
            del hooks[ev]
    if not uninstall:
        for ev in CLAUDE_EVENTS:
            group = {"hooks": [_handler(ev, "claude")]}
            if ev in CLAUDE_MATCHER:
                group = {"matcher": CLAUDE_MATCHER[ev], **group}
            hooks.setdefault(ev, []).append(group)
    if hooks:
        out["hooks"] = hooks
    elif removed_any:
        out.pop("hooks", None)
    return out


def _write(path, data):
    """Back up, then replace atomically, keeping the original file mode. A symlinked settings file (dotfiles) is
    written through: its target changes and the link stays (os.replace on the link itself put a plain file there)."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
        stamp = time.strftime('%Y%m%d-%H%M%S')
        backup = path.with_name(f"{path.name}.jarviee-backup-{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = path.with_name(f"{path.name}.jarviee-backup-{stamp}-{n}")
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


def run(target, dry_run=False, uninstall=False, path=None):
    path = Path(path) if path else PATHS[target]()
    label = {"agy": "Antigravity", "claude": "Claude Code"}[target]
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as e:
            print(f"[{label}] {path} is not valid JSON ({str(e)[:120]}); left untouched.")
            return False
        except OSError as e:
            print(f"[{label}] {path} could not be read ({e}); left untouched.")
            return False
        if not isinstance(current, dict):
            print(f"[{label}] {path} is not a JSON object; left untouched.")
            return False
    try:
        merged = (merge_agy if target == "agy" else merge_claude)(current, uninstall)
    except Malformed as e:
        print(f"[{label}] {path}: {e}; left untouched. Fix that entry by hand, then run this again.")
        return False
    if merged == current:
        print(f"[{label}] {path}: already up to date, nothing to write.")
        return True
    verb = "remove" if uninstall else "add"
    events = AGY_EVENTS if target == "agy" else CLAUDE_EVENTS
    if dry_run:
        print(f"[{label}] dry run: would {verb} hooks for {', '.join(events)} in {path}")
        shown = merged.get(HOOK_NAME) if target == "agy" else merged.get("hooks", {})
        print(json.dumps(shown, indent=2))
        return True
    backup = _write(path, merged)
    done = "removed" if uninstall else "added"
    print(f"[{label}] {done} hooks for {', '.join(events)} in {path}"
          + (f"\n    backup: {backup}" if backup else " (new file)"))
    return True
