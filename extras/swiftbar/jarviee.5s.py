#!/usr/bin/env python3
# <xbar.title>Jarviee</xbar.title>
# <xbar.desc>Waiting agents and worst state from the local Jarviee hub, with pause / resume / focus actions.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""SwiftBar plugin: the Jarviee menu-bar glance.

Reads GET /api/glance from the local hub and prints SwiftBar's menu format. The menu items call the `jarviee`
CLI (bin/jarviee), which holds the action token, so this file never touches a secret. Standard library only, and it
never raises: when the hub is down it prints a quiet "Jarviee off".

Install: symlink this file into your SwiftBar plugin folder (the `.5s.` in the name is the refresh interval).
Settings (environment, optional): JARVIEE_PORT (default 7777), JARVIEE_BIN (path of the jarviee CLI).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

GRAY = "#8a8f98"
STATE = {"needs_you": "needs you", "failed": "failed", "running": "running", "idle": "idle"}


def fetch(port, timeout=2.0):
    """The glance payload as a dict, or None when the hub is down or answers nonsense."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{int(port)}/api/glance", timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def cli_path():
    env = os.environ.get("JARVIEE_BIN")
    if env:
        return env
    return str(Path(__file__).resolve().parent.parent.parent / "bin" / "jarviee")


def _quote(value):
    """SwiftBar parameter values with spaces (or quotes) must be double-quoted: bash="/My Repo/bin/jarviee"."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _item(label, cli, command, refresh=True):
    return f"{label} | bash={_quote(cli)} param1={command} terminal=false" + (" refresh=true" if refresh else "")


def render(glance, cli, port=7777):
    """SwiftBar menu lines for one glance payload (None = the hub is off)."""
    if not isinstance(glance, dict):
        return [f"Jarviee off | color={GRAY}"]
    counts = glance.get("counts") if isinstance(glance.get("counts"), dict) else {}
    level = glance.get("level") if glance.get("level") in STATE else "idle"
    try:
        waiting = int(counts.get("waiting") or 0)
    except (TypeError, ValueError):
        waiting = 0
    color = glance.get("color") if isinstance(glance.get("color"), str) else GRAY
    paused = bool(glance.get("paused"))
    lines = [f"{'⏸' if paused else '●'} {waiting} · {STATE[level]} | color={color}", "---"]
    label = str(glance.get("label") or "idle").replace("|", "/")
    lines.append(f"{label} | color={color}")
    if paused:
        lines.append(f"Queue paused | color={GRAY}")
    oldest = glance.get("oldest_waiting") if isinstance(glance.get("oldest_waiting"), dict) else None
    if oldest:
        what = str(oldest.get("title") or oldest.get("id") or "an agent").replace("|", "/")[:60]
        where = f" ({str(oldest['project']).replace('|', '/')})" if oldest.get("project") else ""
        try:
            mins = int((oldest.get("since") or 0) // 60)
        except (TypeError, ValueError):
            mins = 0
        lines.append(f"Oldest waiting: {what}{where}, {mins} min | color={GRAY}")
    quota = glance.get("quota") if isinstance(glance.get("quota"), dict) else {}
    wk = quota.get("claude_week_used")
    if isinstance(wk, (int, float)):
        lines.append(f"Claude week {round(wk)}% used | color={GRAY}")
    lines.append("---")
    lines.append(_item("Open dashboard", cli, "open", refresh=False))
    if waiting:
        lines.append(_item("Focus next waiting agent", cli, "next"))
    lines.append(_item("Resume queue" if paused else "Pause queue", cli, "resume" if paused else "pause"))
    return lines


def main():
    try:
        try:
            port = int(os.environ.get("JARVIEE_PORT") or 7777)
        except ValueError:
            port = 7777
        lines = render(fetch(port), cli_path(), port)
    except Exception:
        lines = [f"Jarviee off | color={GRAY}"]
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Jarviee off")
    sys.exit(0)
