"""`hub.py doctor`: read-only health check. ✓ fine, ! worth a look, ✗ broken (exit code 1)."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import auth, brand, install
from . import config as cfgmod


def _version(binary, runner=subprocess.run):
    try:
        r = runner([binary, "--version"], capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr or "").strip().splitlines()[0] if (r.stdout or r.stderr).strip() else "(no output)"
    except Exception as e:
        return f"--version failed: {e.__class__.__name__}"


def hooks_installed(target):
    """True / False / None (file unreadable) for our handlers in that tool's config."""
    p = install.PATHS[target]()
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    if target == "agy":
        spec = data.get(install.HOOK_NAME) if isinstance(data, dict) else None
        return bool(spec)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    for groups in (hooks or {}).values():
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") if isinstance(g, dict) else None) or []:
                if install.is_ours(h):
                    return True
    return False


def _perm(path):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def checks(which=shutil.which, runner=subprocess.run, health=None, py=None):
    """List of (level, text, hint); level is ok | warn | fail."""
    res = []
    add = lambda lvl, text, hint="": res.append((lvl, text, hint))
    py = py or sys.version_info
    if tuple(py[:2]) >= (3, 9):
        add("ok", f"Python {py[0]}.{py[1]}")
    else:
        add("fail", f"Python {py[0]}.{py[1]} is too old", "Install Python 3.9 or newer.")
    up = cfgmod.user_config_path()
    try:
        cfg = cfgmod.load_config()
        add("ok", "Config loads (base config.json + overlay)")
        if up is None:
            add("warn", "User overlay disabled (JARVIEE_USER_CONFIG is empty)")
        elif up.exists():
            add("ok", f"User overlay: {up}")
        else:
            add("warn", f"No user overlay at {up}", "Run: python3 hub.py setup")
    except Exception as e:
        add("fail", f"Config does not load: {e}", f"Fix or delete {up or 'config.json'}; or run python3 hub.py setup")
        return res
    root = Path(cfg["obsidian"]["workspace_root"])
    if root.is_dir():
        brain = cfg["obsidian"].get("project_brain_dir", ".brain")
        try:
            n = sum(1 for p in root.iterdir() if p.is_dir() and (p / brain).is_dir())
        except OSError:
            n = 0
        add("ok" if n else "warn", f"Workspace {root}: {n} project(s) with a {brain}/ folder",
            "" if n else "Create one with: python3 hub.py brain init <project-dir>")
    else:
        add("fail", f"Workspace folder {root} does not exist", "Run: python3 hub.py setup (or create the folder)")
    for t in ("claude", "agy"):
        argv0 = (cfg.get("tools", {}).get(t, {}).get("command") or [t])[0]
        if which(argv0):
            add("ok", f"{argv0} on PATH: {_version(argv0, runner)}")
        else:
            add("warn", f"{argv0} not on PATH", f"Install {argv0} if you want to use it; setup routes around a missing tool.")
        h = hooks_installed(t)
        if h:
            add("ok", f"Hooks installed for {t}")
        elif h is None:
            add("warn", f"Cannot read the {t} hook config", f"Check {install.PATHS[t]()}")
        else:
            add("warn", f"Hooks not installed for {t}", f"Run: python3 hub.py install-hooks --{t}")
    if health is None:
        from .server_health import check_health
        health = check_health
    if health(cfg):
        add("ok", f"Server reachable on port {cfg['port']}")
    else:
        add("warn", f"Server not reachable on port {cfg['port']}", "Start it: python3 hub.py serve")
    if cfg["terminal"].get("mode") == "iterm2":
        if sys.platform == "darwin" and Path("/Applications/iTerm.app").exists():
            add("ok", "iTerm2 present")
        else:
            add("warn", "terminal.mode is iterm2 but iTerm2 is not installed (runs fall back to background)",
                'Set {"terminal": {"mode": "background"}} in the overlay, or run setup.')
    home = brand.home()
    if home.is_dir():
        if os.name == "nt":
            add("ok", f"Data folder {home} present (POSIX permission checks skipped on Windows)")
        else:
            m = _perm(home)
            if m is not None and m & 0o077:
                add("warn", f"{home} is {oct(m)}; others can list it", f"Run: chmod 700 {home}")
            else:
                add("ok", f"Data folder {home} is private")
            tok = auth.token_path(cfg)
            tm = _perm(tok)
            if tm is None:
                add("ok", "No token file yet (created when the server first starts)")
            elif tm & 0o077:
                add("fail", f"Token file {tok} is {oct(tm)}", f"Run: chmod 600 {tok}")
            else:
                add("ok", "Token file is 0600")
            if up is not None and up.exists():
                um = _perm(up)
                if um is not None and um & 0o077:
                    add("warn", f"{up} is {oct(um)}", f"Run: chmod 600 {up}")
    else:
        add("warn", f"Data folder {home} does not exist yet", "It is created on first run.")
    return res


def run(out=print, **kw):
    res = checks(**kw)
    sym = {"ok": "✓", "warn": "!", "fail": "✗"}
    for lvl, text, hint in res:
        out(f"{sym[lvl]} {text}")
        if hint and lvl != "ok":
            out(f"    fix: {hint}")
    bad = sum(1 for r in res if r[0] == "fail")
    warn = sum(1 for r in res if r[0] == "warn")
    out(f"\n{len(res)} checks: {bad} problem(s), {warn} warning(s)")
    return bad == 0
