#!/usr/bin/env python3
"""Screenshots of the iTerm2 8-pane panel at work, the demo dashboard and a notification banner. Zero tokens.

    python3 scripts/demo/panel_demo.py up        # demo server + "Jarviee Demo" window, 8 fake agents running
    python3 scripts/demo/panel_demo.py shots     # panel, banner (and dashboard if Playwright is installed)
    python3 scripts/demo/panel_demo.py down      # close the demo window, stop the server, remove the temp folder
    python3 scripts/demo/panel_demo.py all       # up, wait, shots, down

How it stays away from your real Jarviee: it uses port 7790 (never 7777), its own data folder (/tmp/jarviee-demo), and an
iTerm2 window of its own titled "Jarviee Demo" (its own dynamic profile). Before anything is launched it asks
`demo_server.py --iterm --check` for the settings the server will really use and refuses to go on if they are not the
demo's. It only ever closes the window titled "Jarviee Demo" with the id it recorded. No real agent runs: every tool
runs scripts/demo/fake_agent.py, which only prints.

Needs macOS, iTerm2, and for the screenshots: Screen Recording permission for the app that runs this script (Terminal,
iTerm2 ...) in System Settings > Privacy & Security. Without it macOS gives a blank or wallpaper-only picture: the script
says so instead of saving it. Python 3.9+ standard library only.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PORT = 7790
ROOT = Path("/tmp/jarviee-demo")
STATE = Path("/tmp/jarviee-demo-panel.json")   # pid, port and folder of the detached server (outside ROOT: the server wipes it)
LOG = Path("/tmp/jarviee-demo-panel.log")
DEMO_WINDOW = "Jarviee Demo"
OUT_PANEL = REPO / "docs" / "screenshots" / "14-orchestrate-panel.png"
OUT_DASH = REPO / "docs" / "screenshots" / "15-dashboard-8-agents.png"
OUT_BANNER = REPO / "showcase" / "screenshots" / "notification-banner.png"

# The 8 runs: (project, tool, tier, task). The first is the lead; the others its workers. Tools claude / agy run the fake
# agent here (models: Opus, Sonnet, Haiku, Gemini 4 Hydrogen, Gemini 3.8 Flash, Gemini 4 Neon). Different projects and
# no shared file names, so the file coordinator lets all 8 start at once.
RUNS = [
    ("acme-storefront", "claude", "heavy", "Lead: ship the checkout redesign"),
    ("acme-storefront", "claude", "complex", "Rewrite checkout totals to integer cents and cover rounding with tests"),
    ("acme-storefront", "agy", "light", "Add a quantity stepper to the cart drawer"),
    ("weather-api", "agy", "light", "Add per-key rate limiting to the forecast endpoint"),
    ("weather-api", "claude", "complex", "Retry upstream weather calls with exponential backoff"),
    ("habit-tracker-app", "claude", "complex", "Fix the streak counter when the phone's time zone changes"),
    ("habit-tracker-app", "agy", "heavy", "Build the weekly summary screen with a bar chart"),
    ("portfolio-site", "agy", "basic", "Rename the nav label 'Work' to 'Projects' everywhere"),
]
# the notification: an agent that needs you (hub/alerts.py _fire_alert: title "Jarviee · <section>", subtitle
# "<project folder> · <tool>", message "<run title> — <activity>")
NOTE_TITLE = "Jarviee · Needs your response"
NOTE_SUBTITLE = "acme-storefront · claude"
NOTE_MESSAGE = "Add discount codes — Waiting: may I run `npm run migrate` against the dev database?"


def say(msg):
    print(msg, flush=True)


def die(msg):
    sys.exit(f"panel_demo: {msg}")


# ---------------------------------------------------------------- server and window

def server_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True   # it answered
    except (OSError, ValueError):
        return False


def safety_check(duration):
    """Ask the demo server what it would really use (config overlay and defaults applied) and refuse anything else."""
    r = subprocess.run([sys.executable, str(HERE / "demo_server.py"), "--iterm", "--check", "--port", str(PORT),
                        "--dir", str(ROOT), "--duration", str(duration)], capture_output=True, text=True, timeout=120)
    try:
        c = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        die(f"the safety check gave no answer: {(r.stderr or r.stdout)[-300:]}")
    problems = []
    if c["port"] != PORT or c["port"] == 7777:
        problems.append(f"port is {c['port']}")
    if c["window_name"] != DEMO_WINDOW or "Demo" not in c["profile"]:
        problems.append(f"window is {c['window_name']!r} / profile {c['profile']!r}")
    if (c["mode"], c["layout"], c["grid"]) != ("iterm2", "grid", [4, 2]):
        problems.append(f"terminal is {c['mode']} {c['layout']} {c['grid']}")
    if not str(c["db_path"]).startswith(str(ROOT) + "/"):
        problems.append(f"database is {c['db_path']}")
    if not str(c["profile_file"]).endswith("jarviee-demo.json"):
        problems.append(f"iTerm2 profile file is {c['profile_file']}")
    for tool, cmd in c["tool_commands"].items():
        if c["fake_agent"] not in cmd:
            problems.append(f"tool {tool} does not run the fake agent: {cmd}")
    if problems:
        die("refusing to start, the demo config is not what it should be: " + "; ".join(problems))
    say(f"safety check ok: port {c['port']}, window \"{c['window_name']}\", {c['mode']} {c['layout']} {c['grid']}, "
        f"display {c['display']}, data {c['root']}, every tool runs fake_agent.py")


def api(path, body=None):
    token = (ROOT / "data" / "token").read_text(encoding="utf-8").strip()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Jarviee-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def term_state():
    try:
        return json.loads((ROOT / "data" / "terminal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wait_for(fn, what, timeout):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    die(f"timed out waiting for {what}")


def cmd_up(a):
    if server_up():
        die(f"something already answers on port {PORT}. Run `panel_demo.py down` first (it is never port 7777).")
    safety_check(a.duration)
    LOG.write_text("", encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(HERE / "demo_server.py"), "--iterm", "--port", str(PORT), "--dir", str(ROOT),
                          "--display", a.display, "--font-size", str(a.font_size), "--duration", str(a.duration)],
                         stdout=open(LOG, "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    STATE.write_text(json.dumps({"pid": p.pid, "port": PORT, "root": str(ROOT)}), encoding="utf-8")
    wait_for(server_up, "the demo server", 60)
    say(f"demo server up on http://127.0.0.1:{PORT} (pid {p.pid}, log {LOG})")
    wait_for(lambda: "prefill:" in LOG.read_text(encoding="utf-8") and term_state().get("window"), "the demo window", 60)
    say("window: " + next(x for x in LOG.read_text(encoding="utf-8").splitlines() if x.startswith("prefill:")))
    time.sleep(6)   # every free pane needs its waiter listening before the first run is handed out
    for i, (project, tool, tier, task) in enumerate(RUNS):
        r = api("/api/run", {"task": task, "tool": tool, "tier": tier, "project": project})
        rid = r.get("id")
        if not rid:
            die(f"launch failed: {r}")
        wait_for(lambda: rid in (term_state().get("runs") or {}), f"a pane for run {i + 1}", 40)
        say(f"  pane {i + 1}/8: {project:18} {r.get('tool')}/{r.get('model')}  {task}")
        time.sleep(a.gap)
    st = term_state()
    if len(st.get("tabs") or []) != 1 or len(st.get("runs") or {}) != 8:
        die(f"expected 1 tab with 8 runs, got {len(st.get('tabs') or [])} tab(s) and {len(st.get('runs') or {})} run(s)")
    say(f"8 fake agents working in the \"{DEMO_WINDOW}\" window (about {a.duration:.0f} s each).")


def cmd_down(_a=None):
    info = {}
    try:
        info = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    pid = info.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGINT)   # the server's own cleanup closes the "Jarviee Demo" window, drops its profile
            for _ in range(60):
                os.kill(int(pid), 0)
                time.sleep(0.5)
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    else:
        say("no demo server recorded")
    time.sleep(1)
    subprocess.run(["pkill", "-f", str(HERE / "fake_agent.py")], capture_output=True)   # only the demo's own fake agents
    shutil.rmtree(ROOT, ignore_errors=True)
    for f in (STATE,):
        f.unlink(missing_ok=True)
    if LOG.exists():
        say("server log tail: " + " | ".join(LOG.read_text(encoding="utf-8").splitlines()[-3:]))
    say("demo stopped and cleaned up")


# ---------------------------------------------------------------- screenshots

def png_looks_real(path):
    """No PIL here: a window of 8 busy panes compresses to hundreds of KB, a black or wallpaper-only grab to a few."""
    return Path(path).stat().st_size > 120_000


def iterm_window(wid):
    """"<title>|l,t,r,b" of the iTerm2 window with this id, or "" when there is none."""
    script = ('on run argv\n tell application id "com.googlecode.iterm2"\n repeat with w in windows\n'
              ' if (id of w) is ((item 1 of argv) as integer) then\n set b to bounds of w\n'
              ' return (name of w) & "|" & (item 1 of b) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b)\n'
              ' end if\n end repeat\n end tell\n return ""\nend run\n')
    r = subprocess.run(["osascript", "-", str(wid)], input=script, capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def shot_panel(out):
    wid = term_state().get("window")
    if not wid:
        die("no demo window recorded: run `up` first")
    info = iterm_window(wid)
    if not info.split("|", 1)[0].startswith(DEMO_WINDOW):
        die(f"window {wid} is not titled {DEMO_WINDOW!r} (got {info!r}); not capturing it")
    out.parent.mkdir(parents=True, exist_ok=True)
    # -l: this one window by its id (works even if another window covers it); -o: no shadow; -x: no sound
    r = subprocess.run(["screencapture", "-x", "-o", f"-l{wid}", str(out)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists() or not png_looks_real(out):
        out.unlink(missing_ok=True)
        die("the picture came out empty or black. macOS Screen Recording permission is probably missing for the app that runs "
            "this script (System Settings > Privacy & Security > Screen & System Audio Recording): turn it on, quit and "
            "reopen that app, and run `shots` again. " + (r.stderr or "").strip())
    say(f"saved {out.relative_to(REPO)}  (window {wid}, {info.split('|', 1)[1]})")


def screens():
    sys.path.insert(0, str(REPO))
    from hub import displays
    return displays.read_screens() or []   # already in top-left coordinates (what screencapture and iTerm2 bounds use)


def shot_banner(out):
    """One demo alert through terminal-notifier, then a tight crop of the banner in the top-right corner of the main screen
    (macOS shows banners there). A banner is translucent: a blurred smear of whatever window is behind it shows through,
    so put only demo windows (or nothing) in that corner and LOOK at the picture before using it."""
    notifier = shutil.which("terminal-notifier") or "/opt/homebrew/bin/terminal-notifier"
    if not os.path.isfile(notifier):
        say("terminal-notifier is not installed (brew install terminal-notifier): no banner picture")
        return
    scr = screens()
    if not scr:
        say("no screen found: no banner picture")
        return
    fx, fy, fw, fh = (int(v) for v in scr[0]["frame"])
    # a banner is ~ 350 x 60 points, 12 points from the right edge, just under the menu bar
    region = f"{fx + fw - 366},{fy + 40},{354},{62}"
    r = subprocess.run([notifier, "-title", NOTE_TITLE, "-subtitle", NOTE_SUBTITLE, "-message", NOTE_MESSAGE,
                        "-group", "jarviee-demo-banner"], capture_output=True, text=True)
    if r.returncode != 0 or "turned off" in (r.stdout + r.stderr):
        say("terminal-notifier could not post the banner: " + ((r.stdout + r.stderr).strip().splitlines() or ["?"])[0] +
            "\n  Turn on System Settings > Notifications > terminal-notifier (Allow, style Banners, previews Always), "
            "then run `shots` again. No banner picture saved.")
        return
    time.sleep(2.2)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["screencapture", "-x", "-R", region, str(out)], capture_output=True)
    subprocess.run([notifier, "-remove", "jarviee-demo-banner"], capture_output=True)
    say(f"saved {out.relative_to(REPO)}. LOOK AT IT before using it: nothing real may show in or around the banner.")


def shot_dashboard(out):
    js = HERE / "take_panel_dashboard.js"
    if not shutil.which("node") or not os.environ.get("NODE_PATH"):
        say("dashboard picture skipped: needs node and Playwright (NODE_PATH=<dir>/node_modules, see scripts/demo/README.md). "
            f"Open http://127.0.0.1:{PORT}/ at 1600x1000 and save it as {out.relative_to(REPO)} instead.")
        return
    subprocess.run(["node", str(js), f"http://127.0.0.1:{PORT}", str(out)], check=False)


def cmd_shots(a):
    if not server_up():
        die("the demo server is not running: run `up` first")
    shot_panel(OUT_PANEL)
    shot_banner(OUT_BANNER)
    if not a.no_dashboard:
        shot_dashboard(OUT_DASH)


def cmd_all(a):
    try:
        cmd_up(a)
        time.sleep(a.wait)   # let the agents get properly into their work
        cmd_shots(a)
    finally:
        cmd_down()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["up", "shots", "down", "all"])
    ap.add_argument("--duration", type=float, default=300, help="seconds each fake agent works (default %(default)s)")
    ap.add_argument("--display", default="builtin", help="screen for the window: main | builtin | N (default %(default)s)")
    ap.add_argument("--font-size", type=float, default=11, help="pane font size (default %(default)s)")
    ap.add_argument("--gap", type=float, default=1.0, help="seconds between launches (default %(default)s)")
    ap.add_argument("--wait", type=float, default=60, help="`all`: seconds to wait before the pictures (default %(default)s)")
    ap.add_argument("--no-dashboard", action="store_true", help="`shots`: skip the dashboard picture")
    a = ap.parse_args()
    if sys.platform != "darwin":
        die("macOS only (iTerm2, screencapture)")
    {"up": cmd_up, "shots": cmd_shots, "down": cmd_down, "all": cmd_all}[a.cmd](a)


if __name__ == "__main__":
    main()
