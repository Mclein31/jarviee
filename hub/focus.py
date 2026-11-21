"""Bring an agent's own window to the front: POST /api/focus.

- A run Jarviee launched into an iTerm2 pane: select that pane (terminal.show).
- Any other session (hooks): walk the agent's process tree up to the .app hosting it.
  iTerm2 / Terminal → select the exact tab by the agent's TTY. Editors (VS Code, Cursor, Antigravity, Windsurf…)
  → `open -a <app> <workspace>`, which focuses the window that has that folder open.
Every value travels as an argument, never inside script text. Never raises; returns (ok, message).
"""
import os
import re
import subprocess

ITERM = "com.googlecode.iterm2"
TERMINAL = "com.apple.Terminal"
TTY_RE = re.compile(r"^ttys\d{1,4}$")
APP_RE = re.compile(r"^(.+?\.app)(?:/|$)")

ITERM_SELECT = r'''on run argv
	set target to "/dev/" & (item 1 of argv)
	tell application id "com.googlecode.iterm2"
		repeat with w in windows
			repeat with t in tabs of w
				repeat with s in sessions of t
					if tty of s is target then
						select s
						tell t to select
						set index of w to 1
						activate
						return "ok"
					end if
				end repeat
			end repeat
		end repeat
	end tell
	return "missing"
end run'''

TERMINAL_SELECT = r'''on run argv
	set target to "/dev/" & (item 1 of argv)
	tell application id "com.apple.Terminal"
		repeat with w in windows
			repeat with t in tabs of w
				if tty of t is target then
					set selected of t to true
					set index of w to 1
					activate
					return "ok"
				end if
			end repeat
		end repeat
	end tell
	return "missing"
end run'''


def _ps(pid):
    """(ppid, tty, executable path) for one process, or None."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,tty=,comm=", "-p", str(int(pid))], capture_output=True,
                             text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    parts = out.split(None, 2)
    if len(parts) < 3 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1], parts[2]


ITERM_SERVER = "iTerm2 (session server)"   # host_of's answer when the shell hangs off iTermServer, not iTerm.app


def host_of(pid):
    """(app bundle path, tty) for the process tree above pid: the first ancestor running from an .app,
    and the first real terminal device on the way up. ("", "") when nothing matches.
    Skips apps embedded in a .framework (Python.app); iTerm2's session server (iTermServer-x.y.z in
    Application Support, the parent of every shell when session restoration is on) counts as iTerm2."""
    tty, seen = "", 0
    while pid and pid > 1 and seen < 12:
        seen += 1
        row = _ps(pid)
        if not row:
            break
        ppid, t, exe = row
        if not tty and TTY_RE.match(t or ""):
            tty = t
        if os.path.basename(exe).startswith("iTermServer"):
            return ITERM_SERVER, tty
        m = APP_RE.match(exe)
        if m and ".framework/" not in m.group(1):
            return m.group(1), tty
        pid = ppid
    return "", tty


def bundle_id(app_path):
    try:
        return subprocess.run(["mdls", "-raw", "-name", "kMDItemCFBundleIdentifier", app_path], capture_output=True,
                              text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _osa(script, *args):
    try:
        r = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=6)
        return r.stdout.strip() or r.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)


def focus(agent, cfg):
    """(ok, message) after trying to bring the agent's window forward."""
    term = cfg.get("_terminal")
    aid = agent.get("id") or ""
    if term and agent.get("source") == "launcher" and term.has_pane(aid):
        ok, why = term.select_pane(aid)
        return ok, ("Showing its iTerm2 pane." if ok else why)
    if agent.get("source") == "launcher":   # headless run: its parent is the Jarviee server, not a window
        return False, "This run is headless, so it has no window. Its output is in the Log tab."
    pid = agent.get("agent_pid") or agent.get("pid")
    ws = agent.get("workspace") or ""
    if agent.get("agent_host") == "antigravity-app":
        return _open_app("Antigravity", ws)
    if not pid:
        return False, "This session hasn't reported its process yet, so Jarviee can't tell which window it's in."
    app, tty = host_of(int(pid))
    if not app:
        return False, "Couldn't find the app this session runs in (its process may have ended)."
    name = os.path.basename(app).removesuffix(".app")
    bid = ITERM if app == ITERM_SERVER else bundle_id(app)
    if bid == ITERM and tty:
        out = _osa(ITERM_SELECT, tty)
        return (True, f"Showing its iTerm2 tab ({tty}).") if out == "ok" else (False, f"iTerm2 tab not found ({out[:80]}).")
    if bid == TERMINAL and tty:
        out = _osa(TERMINAL_SELECT, tty)
        return (True, f"Showing its Terminal tab ({tty}).") if out == "ok" else (False, f"Terminal tab not found ({out[:80]}).")
    if app == ITERM_SERVER:
        return _open_app("iTerm", "", "iTerm2")
    return _open_app(app, ws, name)


def _open_app(app, workspace, name=None):
    """`open -a App folder` focuses the editor window that has the folder open (or opens one)."""
    folder = workspace if workspace and os.path.isabs(workspace) and os.path.isdir(workspace) else ""
    args = ["open", "-a", app] + ([folder] if folder else [])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Couldn't open {name or app}: {e}"
    if r.returncode != 0:
        return False, f"Couldn't open {name or app}: {r.stderr.strip()[:120]}"
    where = f" ({os.path.basename(workspace.rstrip('/'))})" if workspace else ""
    return True, f"Showing {name or os.path.basename(app).removesuffix('.app')}{where}."
