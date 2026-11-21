"""The dashboard's own Safari window, on the secondary screen.

`open <url>` lets Safari put the page in a tab of whatever window is in front (the owner's browsing window). Jarviee
instead keeps a window of its own: a Safari window whose only tab is the dashboard is reused and moved; otherwise a
new window is made. The script is fixed; the URL and bounds travel as osascript arguments.
"""
import subprocess
import sys
import webbrowser

from . import displays
from .config import DASHBOARD_WINDOW_DEFAULTS as DEFAULTS

# argv: dashboard URL, "l,t,r,b" bounds ("" = leave where Safari puts it)
SAFARI = r'''
on rect(txt)
	set AppleScript's text item delimiters to ","
	set p to text items of txt
	set AppleScript's text item delimiters to ""
	return {(item 1 of p) as integer, (item 2 of p) as integer, (item 3 of p) as integer, (item 4 of p) as integer}
end rect

on run argv
	set u to item 1 of argv
	tell application id "com.apple.Safari"
		set target to missing value
		repeat with w in windows
			try
				if (count of tabs of w) is 1 and ((URL of current tab of w) as text) starts with u then
					set target to w
					exit repeat
				end if
			end try
		end repeat
		if target is missing value then
			make new document with properties {URL:u}
			set target to front window
			set made to "new"
		else
			set made to "reused"
		end if
		if (item 2 of argv) is not "" then set bounds of target to my rect(item 2 of argv)
		set index of target to 1
		activate
		return made
	end tell
end run
'''


def settings(cfg):
    return {**DEFAULTS, **(cfg.get("dashboard_window") or {})}


def bounds_for(screens, setting):
    """"l,t,r,b" filling the chosen screen's visible area, or "" when only one screen is connected and the setting
    asks for another one (the window then stays where Safari opens it)."""
    screen, _ = displays.pick_screen(screens, setting)
    if not screen:
        return ""
    if len(screens) == 1 and str(setting).strip().lower() not in ("main", "0"):
        return ""
    x, y, w, h = screen.get("visible") or screen["frame"]
    return f"{int(x)},{int(y)},{int(x + w)},{int(y + h)}"


def _open_default(url):
    if sys.platform == "darwin":
        subprocess.run(["open", url], capture_output=True)
    else:
        webbrowser.open(url)


def open_dashboard(cfg, run=None, screens=None, fallback=None):
    """Open (or bring back) the dashboard window. Returns "new", "reused" or "default" (handed to `open`)."""
    s = settings(cfg)
    url = f"http://127.0.0.1:{int(cfg['port'])}/"
    run = run or _osascript
    fallback = fallback or _open_default
    if sys.platform != "darwin" or str(s.get("browser")).lower() != "safari":
        fallback(url)
        return "default"
    try:
        found = (screens or displays.read_screens)(False) or []
        out = run(SAFARI, [url, bounds_for(found, s.get("display", "secondary"))])
        if out in ("new", "reused"):
            return out
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    fallback(url)   # no Automation permission for Safari, or Safari refused: never lose the dashboard
    return "default"


def _osascript(script, args):
    if sys.platform != "darwin":
        raise OSError("osascript is only available on macOS")
    r = subprocess.run(["osascript", "-", *[str(a) for a in args]], input=script, capture_output=True, text=True,
                       timeout=20)
    if r.returncode != 0:
        raise ValueError((r.stderr or r.stdout).strip()[-200:])
    return r.stdout.strip()
