"""`hub.py setup`: first-run wizard. Detects what is installed, asks a few questions and writes ONLY the user
overlay (~/.jarviee/config.json). Hooks go through hub/install.py (which backs files up); the service and the
app are only offered on macOS and are never installed without a yes."""
import json
import os
import platform
import shutil
import sys
from pathlib import Path

from . import config as cfgmod

DISPATCH_LEVELS = ("basic", "light", "complex", "heavy")
CLAUDE_DISPATCH = {"basic": "haiku", "light": "sonnet", "complex": "sonnet", "heavy": "opus"}
ITERM = "/Applications/iTerm.app"


def detect(which=shutil.which, system=None, exists=os.path.exists, py=None):
    system = system or platform.system()
    py = py or sys.version_info
    mac = system == "Darwin"
    return {"os": system, "mac": mac, "python": f"{py[0]}.{py[1]}", "python_ok": tuple(py[:2]) >= (3, 9),
            "claude": bool(which("claude")), "agy": bool(which("agy")),
            "codex": bool(which("codex")), "gemini": bool(which("gemini")),
            "iterm2": mac and bool(exists(ITERM)),
            "terminal_notifier": mac and bool(which("terminal-notifier"))}


def default_workspace(home=None, cwd=None):
    home = Path(home or Path.home())
    if (home / "Projects").is_dir():
        return str(home / "Projects")
    return str(Path(cwd or os.getcwd()).resolve().parent)


def _base_config():
    with open(cfgmod.CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_overlay(det, tools, workspace_root, base=None):
    """The overlay to write. `tools` is the list of wired tools among claude / agy."""
    base = base or _base_config()
    o = {"obsidian": {"workspace_root": str(workspace_root)}}
    if not det.get("iterm2"):
        o["terminal"] = {"mode": "background"}
    have = set(tools)
    if have == {"claude"}:
        o["dispatch"] = {lv: {"tool": "claude", "model": CLAUDE_DISPATCH[lv]} for lv in DISPATCH_LEVELS}
        o["default_tool"] = "claude"
    elif have == {"agy"}:
        models = base["tools"]["agy"]["models"]
        o["dispatch"] = {lv: {"tool": "agy", "model": models[lv]} for lv in DISPATCH_LEVELS}
        o["default_tool"] = "agy"
    if have and have != {"claude", "agy"}:
        o["lanes"] = {t: (base.get("lanes", {}).get(t, 1) if t in have else 0) for t in ("agy", "claude")}
    return o


def _port():
    try:
        return cfgmod.load_config()["port"]
    except Exception:
        return int(os.environ.get("JARVIEE_PORT") or 7777)


def _ask(prompt, default, yes, inp=input):
    if yes:
        return default
    try:
        ans = inp(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return ans or default


def _ask_yn(prompt, default, yes, inp=input):
    d = "Y/n" if default else "y/N"
    if yes:
        return default
    try:
        ans = inp(f"{prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    return default if not ans else ans.startswith("y")


def _mark(ok, warn=False):
    return "✓" if ok else ("!" if warn else "✗")


def run(yes=False, dry_run=False, inp=input, det=None, workspace=None, out=print):
    det = det or detect()
    out("Jarviee setup\n")
    out("Detected:")
    out(f"  {_mark(det['python_ok'])} Python {det['python']} (needs 3.9+)")
    out(f"  {_mark(True)} OS {det['os']}")
    for t, label in (("claude", "Claude Code (claude)"), ("agy", "Antigravity (agy)")):
        out(f"  {_mark(det[t], True)} {label}: {'found' if det[t] else 'not on PATH'}")
    for t in ("codex", "gemini"):
        if det[t]:
            out(f"  ✓ {t}: found, not wired by default")
    if det["mac"]:
        out(f"  {_mark(det['iterm2'], True)} iTerm2: {'found' if det['iterm2'] else 'not installed (runs will use background mode)'}")
        out(f"  {_mark(det['terminal_notifier'], True)} terminal-notifier: {'found' if det['terminal_notifier'] else 'not installed (optional)'}")
    out("")
    if not det["python_ok"]:
        out("Python 3.9 or newer is required. Install it, then run setup again.")
        return False
    found = [t for t in ("claude", "agy") if det[t]]
    if not found:
        out("No agent CLI found: install Claude Code or Antigravity, then run setup again.")
    current = (cfgmod.read_user_config().get("obsidian") or {}).get("workspace_root")
    root = _ask("Folder that holds your projects", workspace or current or default_workspace(), yes, inp)
    root = str(cfgmod.expand(root))
    if not Path(root).is_dir():
        out(f"  ! {root} does not exist yet; create it or edit {cfgmod.user_config_path()} later.")
    tools = list(found)
    if len(found) > 1 and not yes:
        ans = _ask("Tools to use (claude, agy, or both)", "both", yes, inp).lower()
        tools = ["claude"] if ans == "claude" else ["agy"] if ans == "agy" else list(found)
    elif len(found) == 1:
        out(f"Using {found[0]} only: every task level will be routed to it.")
    overlay = build_overlay(det, tools, root)
    path = cfgmod.user_config_path()
    out("")
    if path is None:
        out("The user config is disabled (JARVIEE_USER_CONFIG is empty); nothing can be written.")
        return False
    if dry_run:
        out(f"Dry run: would merge this into {path} (mode 0600):")
        out(json.dumps(overlay, indent=2))
    else:
        cfgmod.write_user_config(overlay)
        out(f"Wrote {path} (mode 0600):")
        out(json.dumps(overlay, indent=2))
    if len(tools) < 2:
        out("Fallback chains are unchanged: a missing binary already falls through to the next one, so nothing breaks.")
    # hooks
    hook_targets = [t for t in tools if t in ("claude", "agy")]
    if hook_targets:
        from . import install
        if _ask_yn(f"Install the status hooks for {', '.join(hook_targets)}? (backs the files up first)", True, yes, inp):
            for t in hook_targets:
                install.run(t, dry_run=dry_run)
        else:
            out("Skipped hooks. Later: python3 hub.py install-hooks")
    if det["mac"]:
        for what, cmd in (("background service (starts at login)", "service install"),
                          ("Jarviee.app launcher", "app install")):
            if yes:
                out(f"To add the {what}, run: python3 hub.py {cmd}")
            elif _ask_yn(f"Install the {what}?", False, yes, inp):
                from . import service, app_launcher
                if dry_run:
                    out(f"  dry run: would run python3 hub.py {cmd}")
                elif cmd.startswith("service"):
                    service.install(cfgmod.load_config())
                else:
                    app_launcher.install(cfgmod.load_config())
    out("\nSummary")
    out(f"  Config overlay: {path}" + (" (not written: dry run)" if dry_run else ""))
    out("  Cost note: the first project-map run reads a whole project and can use many tokens.")
    out("  Automatic map updates are off by default; turn them on in the dashboard settings if you want them.")
    out("\nNext steps")
    out(f"  python3 hub.py serve          # then open http://127.0.0.1:{_port()}")
    out('  python3 hub.py classify "fix a typo"')
    out("  python3 hub.py doctor         # check everything")
    out("  python3 hub.py brain init <project-dir>   # starter notes for a project")
    return True
