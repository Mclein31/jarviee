"""The Jarviee iTerm2 window: open, tile, close and select one pane per run.

State (window id, split tree per tab, run → session) lives in ~/.jarviee/terminal.json so a
restarted server finds the same window. All AppleScript goes through `self.run` (injectable).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import displays, iterm, tiling
from .config import TERMINAL_DEFAULTS as DEFAULTS

APPS = ("/Applications/iTerm.app", str(Path.home() / "Applications/iTerm.app"))
SLOT_PREFIX = "@jarviee:"   # what a free grid pane's waiter reads before it starts the run (hooks/pane_runner.py)
SLOT_ARG = "--slot"
TTY_RE = re.compile(r"^/dev/ttys[0-9]{1,4}$")
PERMISSION_HELP = ("macOS didn't let Jarviee control iTerm2, so runs use the background mode. To allow it: "
                   "System Settings › Privacy & Security › Automation → turn on iTerm2 under the app that runs hub.py.")


def settings(cfg):
    return {**DEFAULTS, **(cfg.get("terminal") or {})}


def grid_dims(t):
    """(columns, rows) from the `terminal` settings `t` (default 4×2 = 8 panes: the orchestrator + seven workers)."""
    try:
        c, r = (int(v) for v in (t.get("grid") or (4, 2)))
    except (TypeError, ValueError):
        c, r = 4, 2
    return max(1, min(c, 6)), max(1, min(r, 4))


def slot_waiting(tty):
    """True when a free pane's waiter (pane_runner.py --slot) is what runs on this tty, so typing a run id there is
    safe. Never types into an agent's session."""
    if not TTY_RE.match(str(tty or "")):
        return False
    try:
        r = subprocess.run(["ps", "-o", "command=", "-t", tty], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(f"pane_runner.py {SLOT_ARG}" in line for line in r.stdout.splitlines())


def active(cfg):
    """The Terminal for launched runs, or None (background mode, CLI runs, tests)."""
    t = cfg.get("_terminal")
    return t if (cfg.get("_serving") and t is not None and t.enabled()) else None


class Terminal:
    reload_wait = displays.PROFILE_RELOAD_WAIT
    place_wait = 2.5   # seconds for a new full screen window to land on its screen

    def __init__(self, cfg, run=None, screens=None, problems=None, state_path=None, profile_path=None,
                 installed=None, front=None, activate=None, font=None, waiting=None):
        self.cfg, self.t = cfg, settings(cfg)
        self.run = run or iterm.osascript
        self.front = front or displays.frontmost
        self.activate = activate or displays.activate
        self.read_screens = screens or displays.read_screens
        self.read_problems = problems or displays.read_problems
        self.path = Path(state_path or Path(cfg["db_path"]).parent / "terminal.json")
        self.profile_path = Path(profile_path or displays.PROFILE_PATH)
        self._installed = installed
        self.font = font or displays.default_font
        self.waiting = waiting or slot_waiting
        self.lock = threading.RLock()
        self.notice = None          # {"key", "text"} shown on the dashboard until dismissed
        self.state = self._load()

    # ---- state ----
    def _load(self):
        base = {"enabled": None, "window": None, "fullscreen": False, "screen": None, "rect": None,
                "tabs": [], "runs": {}, "idle": None, "vacant": [], "dismissed": [], "grid_sids": [], "panels": {}}
        try:
            base.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
        return base

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- settings / status ----
    def installed(self):
        return self._installed if self._installed is not None else any(Path(p).is_dir() for p in APPS)

    def enabled(self):
        if self.t["mode"] != "iterm2" or not self.installed():
            return False
        return self.state.get("enabled") is not False

    def set_enabled(self, on):
        with self.lock:
            self.state["enabled"] = bool(on)
            self._save()

    def grid_size(self):
        """(columns, rows) of the /orchestrate panel (default 4×2 = 8 panes)."""
        return grid_dims(self.t)

    def grid(self):
        """(columns, rows) when EVERY run goes to the fixed grid (layout "grid"), else None. The default layout is
        "split": runs auto-tile, `max_panes_per_tab` per tab, and only /orchestrate runs use the grid tab."""
        return self.grid_size() if str(self.t.get("layout")) == "grid" else None

    def _is_grid_tab(self, tree):
        """True for an /orchestrate panel tab (every tab is one in the "grid" layout)."""
        if self.grid():
            return True
        sids = set(self.state.get("grid_sids") or [])
        return any(s in sids for s in tiling.leaves(tree))

    def _grid_tabs(self):
        return [i for i, t in enumerate(self.state["tabs"]) if self._is_grid_tab(t)]

    def has_pane(self, run_id):
        return run_id in self.state.get("runs", {})

    def _note(self, key, text):
        self.notice = {"key": key, "text": text}

    def dismiss(self, key):
        with self.lock:
            if key and key not in self.state["dismissed"]:
                self.state["dismissed"] = (self.state["dismissed"] + [key])[-20:]
                self._save()

    def status(self):
        notices = []
        if self.t["mode"] == "iterm2" and not self.installed():
            notices.append({"key": "missing", "text": "iTerm2 isn't installed, so runs use the background mode."})
        if self.notice:
            notices.append(self.notice)
        if self.enabled() and self.state.get("fullscreen"):
            notices += [{"key": f"req{i}", "text": p} for i, p in enumerate(self._problems())]
        shown = [n for n in notices if n["key"] not in self.state["dismissed"]]
        return {"mode": self.t["mode"], "installed": self.installed(), "enabled": self.enabled(),
                "window": bool(self.state.get("window")), "fullscreen": bool(self.state.get("fullscreen")),
                "screen": (self.state.get("screen") or {}).get("name", ""), "notices": shown,
                "panes": len(self.state.get("runs", {})), "layout": "grid" if self.grid() else "split",
                "free": len(self.state.get("vacant") or []), "orchestrate": bool(self._grid_tabs())}

    def _problems(self):
        if not hasattr(self, "_probs"):
            self._probs = self.read_problems()
        return self._probs

    # ---- window ----
    def _target(self):
        """(screen, fullscreen, why). Full screen only off the main display, so its Space never changes."""
        screens = self.read_screens() or []
        screen, why = displays.pick_screen(screens, self.t["display"])
        fullscreen = bool(self.t["fullscreen"]) and screen is not None and screen.get("index", 0) != 0
        return screen, fullscreen, why, screens

    def _reconcile(self):
        """Drop panes the owner closed by hand. Returns {session: (cols, rows)}; WindowGone if it's gone."""
        out = self.run(iterm.LIST, [self.state["window"]])
        dims = {}
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
                dims[parts[1]] = (int(parts[2]), int(parts[3]))
        if out.strip() and not dims:   # never mistake an unreadable answer for a closed window
            raise iterm.TerminalError(f"couldn't read the Jarviee panes: {out[:80]!r}")
        tabs = []
        for tree in self.state["tabs"]:
            for sid in tiling.leaves(tree):
                if sid not in dims:
                    tree = tiling.remove(tree, sid)
            if tree is not None:
                tabs.append(tree)
        self.state["tabs"] = tabs
        self.state["runs"] = {r: s for r, s in self.state["runs"].items() if s in dims}
        if self.state.get("idle") not in dims:
            self.state["idle"] = None
        self.state["vacant"] = [s for s in self.state.get("vacant") or [] if s in dims]
        self.state["grid_sids"] = [s for s in self.state.get("grid_sids") or [] if s in dims]
        if not tabs:
            raise iterm.WindowGone("no Jarviee panes left")
        return dims

    def _forget_window(self):
        self.state.update(window=None, tabs=[], runs={}, idle=None, vacant=[], grid_sids=[], panels={}, fullscreen=False)

    def _focus(self):
        return "1" if self.t["focus_on_launch"] else "0"

    def _guard(self, script, args):
        """Run a window / tab / split script. iTerm2 activates itself for a new normal window, so with
        focus_on_launch off the app that was in front (Safari, the command center) gets focus back."""
        before = "" if self.t["focus_on_launch"] else (self.front() or "")
        out = self.run(script, args)
        if before and before != displays.ITERM_BUNDLE and self.front() == displays.ITERM_BUNDLE:
            self.activate(before)
        return out

    def _target_text(self):
        """Screen rect handed to keepFocus: only a full screen window owns its screen."""
        if not self.state.get("fullscreen"):
            return "0,0,0,0"
        x, y, w, h = self.state.get("rect") or (0, 0, 0, 0)
        return f"{int(x)},{int(y)},{int(x + w)},{int(y + h)}"

    def _font(self):
        size = self.t.get("font_size")
        return displays.font_spec(size, self.font()) if size else None

    def _install_profile(self, index=None):
        """Write the dynamic profiles (screen for full screen, pane font); wait for iTerm2 to reload a changed file."""
        if index is None:
            keep = displays.current_screen_index(self.profile_path)
            index = keep if keep is not None else displays.SCREEN_NO_PREFERENCE
        if displays.install_profile(displays.profile(index, self._font()), self.profile_path):
            time.sleep(self.reload_wait)   # iTerm2 reloads the folder asynchronously

    def _create_window(self, command, first_free=False, orchestrate=False):
        screen, fullscreen, why, _ = self._target()
        screen = screen or {"index": 0, "name": "", "frame": [0, 0, 1440, 900]}
        frame = screen.get("frame") or [0, 0, 1440, 900]
        visible = screen.get("visible") or frame
        self._install_profile(screen.get("index", 0) if fullscreen else None)
        name = displays.PROFILE_NAME if fullscreen else displays.WINDOW_PROFILE
        bounds = "" if fullscreen else ",".join(
            str(v) for v in displays.window_bounds(visible, self.t["position"], self.t["width_pct"]))
        if fullscreen:
            rect = frame
        else:
            left, top, right, bottom = (int(v) for v in bounds.split(","))
            rect = [left, top, right - left, bottom - top]
        self.state.update(fullscreen=fullscreen, rect=list(rect),
                          screen={"name": screen.get("name", ""), "index": screen.get("index", 0)})
        if self.grid() or orchestrate:
            sid = self._grid_build("window", command, first_free, bounds, name)
        else:
            out = self._guard(iterm.OPEN, [command, self._focus(), bounds, self._target_text(), name])
            wid, sid = out.split()
            self.state.update(window=int(wid), tabs=[sid], idle=None)
        if fullscreen:
            self._check_placement(screen, frame, visible)
        if why:
            self._note("screen", f"Jarviee window: {why}, so it opened as a normal window there.")
        return sid

    def _check_placement(self, screen, frame, visible):
        """A full screen window must end up on the target screen. If iTerm2 put it elsewhere (a stale
        profile, a refused full screen), move it there as a normal window and say so."""
        wid = self.state["window"]
        for _ in range(int(self.place_wait / 0.25) + 1):
            try:
                b = [int(float(v)) for v in self.run(iterm.BOUNDS, [wid]).split(",")]
            except (iterm.TerminalError, ValueError):
                return
            if displays.inside(b, frame):
                return
            time.sleep(0.25)
        x, y, w, h = visible
        try:
            self.run(iterm.BOUNDS, [wid, f"{int(x)},{int(y)},{int(x + w)},{int(y + h)}"])
        except iterm.TerminalError:
            return
        self.state.update(fullscreen=False, rect=list(visible))
        self._note("placement", f"iTerm2 didn't open the Jarviee window in full screen on “{screen.get('name')}”, "
                                "so it was moved there as a normal window. Close it and the next run tries again.")

    # ---- grid layout: a fixed columns × rows window; free panes wait for the next run ----
    def _free_command(self):
        return pane_command(self.cfg, None, slot=True)

    def _grid_build(self, mode, command, first_free=False, bounds="", name=None):
        """A new grid window or tab. The first pane runs `command`, the others wait for runs. Returns the first pane."""
        cols, rows = self.grid_size()
        if name is None:
            name = displays.PROFILE_NAME if self.state.get("fullscreen") else displays.WINDOW_PROFILE
        win = "" if mode == "window" else str(self.state["window"])
        out = self._guard(iterm.GRID, [mode, win, command, self._free_command(), self._focus(), bounds,
                                       self._target_text(), name, cols, rows])
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        cells = []
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and iterm.SID_RE.match(parts[2]):
                cells.append((int(parts[0]), int(parts[1]), parts[2]))
        if not lines or not lines[0].isdigit() or not cells:
            raise iterm.TerminalError(f"couldn't read the new grid: {out[:80]!r}")
        tree = tiling.grid_tree(cells)
        first = min(cells)[2]
        if mode == "window":
            self.state.update(window=int(lines[0]), tabs=[tree], idle=None, vacant=[], grid_sids=[])
        else:
            self.state["tabs"].append(tree)
        self.state["grid_sids"] = (self.state.get("grid_sids") or []) + tiling.leaves(tree)
        self.state["vacant"] = (self.state.get("vacant") or []) + [
            s for s in tiling.leaves(tree) if first_free or s != first]
        return first

    def _fill_free(self, run_id, within=None, skip=()):
        """Hand the run to the first free grid pane (top row first, left to right). Its waiter reads the run id and
        becomes the run's pane runner. Returns the pane, or None when every pane is busy. `within`: only these panes
        (one orchestrate panel); `skip`: never these (a panel's lead pane)."""
        win, vacant = self.state["window"], self.state.get("vacant") or []
        order = [s for t in self.state["tabs"] for s in tiling.reading_order(t)
                 if s in vacant and s not in skip and (within is None or s in within)]
        for sid in order:
            try:
                tty = self.run(iterm.TTY, [win, sid])
            except iterm.WindowGone:
                raise
            except iterm.TerminalError:
                continue
            if not self.waiting(tty):
                continue   # not a waiter (yet): never type into anything else
            self.run(iterm.WRITE, [win, sid, SLOT_PREFIX + run_id])
            vacant.remove(sid)
            if self.t["focus_on_launch"]:
                try:
                    self.run(iterm.SELECT, [win, sid])
                except iterm.TerminalError:
                    pass
            return sid
        return None

    def _top_up(self, k):
        """Grow tab k back to the full grid with free panes (after a pane was closed). Returns how many it added."""
        cols, rows = self.grid_size()
        added = 0
        while self.state["tabs"][k] is not None and len(tiling.leaves(self.state["tabs"][k])) < cols * rows:
            hit = tiling.grid_split(self.state["tabs"][k], cols, rows)
            if not hit:
                break
            at, orient = hit
            sid = self._guard(iterm.SPLIT, [self.state["window"], at, orient, self._free_command(), "0",
                                            self._target_text()])
            self.state["tabs"][k] = tiling.split(self.state["tabs"][k], at, orient, sid)
            self.state["vacant"] = (self.state.get("vacant") or []) + [sid]
            self.state["grid_sids"] = (self.state.get("grid_sids") or []) + [sid]
            added += 1
        return added

    def prefill(self):
        """Grid layout: open the Jarviee window with every pane free (or top an open one back up). A short message."""
        if not self.grid():
            return "The Jarviee window opens with the first run (layout is not grid)."
        if not self.enabled():
            return "iTerm2 panes are off."
        with self.lock:
            try:
                if self.state.get("window"):
                    try:
                        self._reconcile()
                        self._install_profile()
                        self._place_grid_window()
                        added = sum(self._top_up(k) for k in range(len(self.state["tabs"])))
                        self._save()
                        self._even()
                        return f"Jarviee window ready ({added} pane(s) added)." if added else "Jarviee window ready."
                    except iterm.WindowGone:
                        self._forget_window()
                self._create_window(self._free_command(), first_free=True)
                self._save()
                self._even()
                cols, rows = self.grid()
                return f"Opened the Jarviee window: {cols}×{rows} free panes."
            except iterm.PermissionDenied:
                self._note("permission", PERMISSION_HELP)
                self._save()
                return PERMISSION_HELP
            except iterm.TerminalError as e:
                self._note("error", f"Couldn't open the Jarviee window ({e}).")
                self._save()
                return f"Couldn't open the Jarviee window ({e})."

    def _place_grid_window(self):
        """An open normal window (left from an earlier layout, or resized by hand) goes back to filling its target
        screen, so the grid's panes get their full size."""
        screen, fullscreen, _, _ = self._target()
        if fullscreen or not screen:
            return
        visible = screen.get("visible") or screen.get("frame")
        left, top, right, bottom = displays.window_bounds(visible, self.t["position"], self.t["width_pct"])
        try:
            self.run(iterm.BOUNDS, [self.state["window"], f"{left},{top},{right},{bottom}"])
        except iterm.WindowGone:
            raise
        except iterm.TerminalError:
            return
        self.state.update(fullscreen=False, rect=[left, top, right - left, bottom - top],
                          screen={"name": screen.get("name", ""), "index": screen.get("index", 0)})

    def free_pane(self, run_id):
        """A grid pane's run ended and its waiter is back: the pane takes the next run. True if it was a grid pane."""
        with self.lock:
            sid = self.state["runs"].pop(run_id, None)
            known = sid and any(sid in tiling.leaves(t) for t in self.state["tabs"])
            if known and sid not in (self.state.get("vacant") or []):
                self.state["vacant"] = (self.state.get("vacant") or []) + [sid]
            self._save()
            return bool(known)

    def _add_pane(self, command):
        """A pane for a run that found no free grid pane. Auto-tile runs go to the split tabs (`max_panes_per_tab`
        each, a new tab when full) and never into an /orchestrate panel's tab."""
        grid = self.grid()
        tabs = self.state["tabs"]
        maxp = grid[0] * grid[1] if grid else max(1, int(self.t["max_panes_per_tab"]))
        win, focus, target = self.state["window"], self._focus(), self._target_text()
        k = tiling.pick_tab(tabs, maxp, set() if grid else set(self._grid_tabs()))
        if k is None:
            if grid:   # every pane is busy: a new grid tab, the run in its first pane
                return self._grid_build("tab", command)
            sid = self._guard(iterm.TAB, [win, command, focus, target])
            tabs.append(sid)
            return sid
        if grid:   # a pane was closed by hand: the run takes the gap's place
            _, _, w, h = self.state.get("rect") or (0, 0, 16, 10)
            at, orient = tiling.grid_split(tabs[k], *grid) or tiling.next_split(tabs[k], (w / h) if h else 1.6)
            sid = self._guard(iterm.SPLIT, [win, at, orient, command, focus, target])
            tabs[k] = tiling.split(tabs[k], at, orient, sid)
            return sid
        tree, idle = tabs[k], self.state.get("idle")
        if idle and idle in tiling.leaves(tree):   # the run takes the idle pane's place
            sid = self._guard(iterm.SPLIT, [win, idle, "V", command, focus, target])
            self.run(iterm.CLOSE, [win, idle])
            tabs[k] = tiling.remove(tiling.split(tree, idle, "V", sid), idle)
            self.state["idle"] = None
            return sid
        _, _, w, h = self.state.get("rect") or (0, 0, 16, 10)
        at, orient = tiling.next_split(tree, (w / h) if h else 1.6)
        sid = self._guard(iterm.SPLIT, [win, at, orient, command, focus, target])
        tabs[k] = tiling.split(tree, at, orient, sid)
        return sid

    def _check_screen(self):
        if not self.state.get("fullscreen"):
            return
        names = [s.get("name") for s in (self.read_screens(False) or [])]
        want = (self.state.get("screen") or {}).get("name")
        if want and want not in names:
            self._note("display", f"“{want}” isn't connected any more. The Jarviee window stays where macOS "
                                  "put it; reconnect the display or close the window to open a new one.")
        elif self.notice and self.notice["key"] == "display":
            self.notice = None

    worker_wait = 20.0   # seconds a worker waits for a window to free up in its orchestrate panel

    def open_pane(self, run_id, command, orchestrate=None):
        """Open a pane running `command` for run_id; returns its session id. Raises iterm.TerminalError.
        `orchestrate` (the run came from /orchestrate): {"role": "lead"} opens a NEW 4×2 panel tab with the run in
        window 1 and seven free windows; {"role": "worker", "lead": <run id>} takes a free window of that lead's panel.
        Without it the run auto-tiles (max_panes_per_tab per tab)."""
        role = (orchestrate or {}).get("role")
        if role == "worker":
            return self._open_worker_pane(run_id, str(orchestrate.get("lead") or ""))
        with self.lock:
            try:
                sid = None
                if role == "lead" and not self.grid():   # a grid pane frees itself after the run: its runner needs JARVIEE_SLOT
                    command = pane_command(self.cfg, run_id, slot=True)
                if self.state.get("window") and self.state.get("fullscreen") and not self.t["fullscreen"]:
                    self._forget_window()   # full screen was turned off: open a normal window you can see
                elif self.state.get("window") and not self.state.get("fullscreen") and self.t["fullscreen"] \
                        and self._target()[1]:
                    self._forget_window()   # full screen was turned on and the target screen can take it
                if self.state.get("window"):
                    try:
                        self._reconcile()
                        self._check_screen()
                        if role == "lead":
                            sid = self._grid_build("tab", command)   # window 1 = the lead, the rest wait for workers
                        else:
                            if self.grid():
                                sid = self._fill_free(run_id)
                            if sid is None:
                                sid = self._add_pane(command)
                    except iterm.WindowGone:
                        self._forget_window()
                if sid is None:
                    sid = self._create_window(command, orchestrate=role == "lead")
                if not iterm.SID_RE.match(sid or ""):
                    raise iterm.TerminalError(f"unexpected session id {sid!r}")
                self.state["runs"][run_id] = sid
                if role == "lead":
                    self.state["panels"][run_id] = sid
                if self.notice and self.notice["key"] in ("permission", "error"):
                    self.notice = None
                self._save()
                self._even()
                return sid
            except iterm.PermissionDenied:
                self._note("permission", PERMISSION_HELP)
                self._save()
                raise
            except iterm.TerminalError as e:
                self._note("error", f"Couldn't open an iTerm2 pane ({e}). Runs fall back to the background.")
                self._save()
                raise

    def _open_worker_pane(self, run_id, lead):
        """A free window of `lead`'s panel for a worker. A window frees itself a moment after its run ends, so a
        worker that finds none waits (outside the lock) before giving up."""
        end = time.time() + self.worker_wait
        while True:
            sid = self._take_panel_window(run_id, lead)
            if sid:
                return sid
            if time.time() >= end:
                raise iterm.TerminalError("no free window in the orchestrate panel")
            time.sleep(min(1.0, max(0.05, self.worker_wait / 20)))

    def _take_panel_window(self, run_id, lead):
        with self.lock:
            try:
                if not self.state.get("window"):
                    raise iterm.TerminalError("the orchestrate panel isn't open")
                try:
                    self._reconcile()
                except iterm.WindowGone:
                    self._forget_window()
                    raise iterm.TerminalError("the orchestrate panel was closed")
                lead_sid = (self.state.get("panels") or {}).get(lead)
                k = next((i for i, t in enumerate(self.state["tabs"]) if lead_sid and lead_sid in tiling.leaves(t)), None)
                if k is None:
                    raise iterm.TerminalError("the orchestrate panel was closed")
                self._top_up(k)   # a window the owner closed by hand comes back as a free one
                sid = self._fill_free(run_id, within=set(tiling.leaves(self.state["tabs"][k])),
                                      skip=set((self.state.get("panels") or {}).values()))
                if sid:
                    self.state["runs"][run_id] = sid
                self._save()
                return sid
            except iterm.PermissionDenied:
                self._note("permission", PERMISSION_HELP)
                self._save()
                raise

    def _even(self):
        """Fix uneven panes in a normal window. Full screen ignores sizes, but its splits stay even anyway."""
        if self.state.get("fullscreen") or not self.state.get("window"):
            return []
        try:
            dims = self._reconcile()
            fixed = []
            for tree in self.state["tabs"]:
                bad = tiling.uneven(tree, dims)
                if bad:
                    want = tiling.targets(tree, *tiling.size(tree, dims))
                    self.run(iterm.RESIZE, [self.state["window"], *[f"{s},{c},{r}" for s, (c, r) in want.items()]])
                    fixed += bad
            return fixed
        except iterm.TerminalError:
            return []

    def release_last(self, run_id):
        """The run's pane is the only one in a full screen window: forget the window and let the pane end itself (its
        runner exits, iTerm2 closes the pane, the window and its Space). Nothing is left behind, and no scripted close
        of a full screen window (that hung in a hidden Space, iTerm2 3.7). True when released."""
        with self.lock:
            sid = self.state["runs"].get(run_id)
            if not sid or not self.state.get("window") or not self.state.get("fullscreen"):
                return False
            try:
                self._reconcile()
            except iterm.WindowGone:
                self._forget_window()
                self._save()
                return False
            except iterm.TerminalError:
                return False
            tabs = self.state["tabs"]
            if not (len(tabs) == 1 and tiling.leaves(tabs[0]) == [sid]):
                return False
            self._forget_window()
            self._save()
            return True

    def close_pane(self, run_id):
        """Close a run's pane; the others re-tile on their own (siblings scale up evenly)."""
        with self.lock:
            sid = self.state["runs"].pop(run_id, None)
            try:
                if not sid or not self.state.get("window"):
                    return False
                try:
                    self._reconcile()
                except iterm.WindowGone:
                    self._forget_window()
                    return False
                k = next((i for i, t in enumerate(self.state["tabs"]) if sid in tiling.leaves(t)), None)
                if k is None:
                    return False
                win = self.state["window"]
                only = len(self.state["tabs"]) == 1 and tiling.leaves(self.state["tabs"][0]) == [sid]
                if only and self.state.get("fullscreen"):
                    # A scripted close of a full screen window in a hidden Space hung (iTerm2 3.7), and an idle
                    # placeholder pane left a blank terminal on screen (owner, 2026-11-14). The runner exits on its
                    # own after the close (pane_runner.close_now), and iTerm2 then closes the pane and the window.
                    self._forget_window()
                    return True
                self.run(iterm.CLOSE, [win, sid])
                tree = tiling.remove(self.state["tabs"][k], sid)
                if tree is None:
                    del self.state["tabs"][k]
                else:
                    self.state["tabs"][k] = tree
                    if self._is_grid_tab(tree):   # keep the grid whole: a free pane takes the closed one's place
                        self._top_up(k)
                if not self.state["tabs"]:
                    self._forget_window()
                self._even()
                return True
            except iterm.TerminalError:
                return False
            finally:
                self._save()

    def select_pane(self, run_id):
        """Switch to the Jarviee Space and select the run's pane. (ok, reason)."""
        with self.lock:
            sid = self.state["runs"].get(run_id)
            if not sid or not self.state.get("window"):
                return False, "This run has no open pane."
            try:
                self.run(iterm.SELECT, [self.state["window"], sid])
                return True, ""
            except iterm.WindowGone:
                self._forget_window()
                self._save()
                return False, "The Jarviee window was closed."
            except iterm.TerminalError as e:
                return False, str(e)

    def retile(self):
        """Forget panes closed by hand and even out sizes. Returns a short message for the dashboard."""
        with self.lock:
            if not self.state.get("window"):
                return "No Jarviee window is open."
            try:
                dims = self._reconcile()
            except iterm.WindowGone:
                self._forget_window()
                self._save()
                return "The Jarviee window was closed. The next run opens a new one."
            except iterm.TerminalError as e:
                return f"Couldn't read the panes: {e}"
            self._save()
            if self.state.get("fullscreen"):
                bad = [s for t in self.state["tabs"] for s in tiling.uneven(t, dims)]
                if not bad:
                    return "Panes are even."
                return ("Native full screen ignores scripted pane sizes, so iTerm2 can't be re-tiled from here. "
                        "Use Window › Arrange Split Panes Evenly in iTerm2.")
            fixed = self._even()
            return f"Re-tiled {len(fixed)} pane(s)." if fixed else "Panes are even."

    def close_finished(self, live_ids):
        """Close the panes of runs that aren't running any more. Returns how many closed."""
        done = [r for r in list(self.state.get("runs", {})) if r not in set(live_ids)]
        return sum(1 for r in done if self.close_pane(r))


def pane_command(cfg, run_id=None, slot=None):
    """The pane's command line: the runner and the run id (validated), or the placeholder: a free grid pane's waiter
    (grid layout) or the idle pane. In the grid layout the runner frees its pane after the run instead of closing it."""
    import shlex
    import sys
    from . import auth
    from .panes import RUN_RE, RUNNER
    if run_id is not None and not RUN_RE.match(run_id):
        raise ValueError(f"bad run id {run_id!r}")
    # The token's FILE, never the token: this line is osascript's argv and the pane's command, which every local user
    # can read with `ps`. The runner reads the 0600 file itself (hooks/pane_runner.py load_token).
    token_file = os.path.abspath(str(auth.token_path(cfg)))
    grid = (str(settings(cfg).get("layout")) == "grid") if slot is None else bool(slot)   # slot: a grid (/orchestrate) pane
    parts = ["/usr/bin/env", f"JARVIEE_PORT={int(cfg['port'])}", f"JARVIEE_TOKEN_PATH={token_file}",
             *(["JARVIEE_SLOT=1"] if grid else []), sys.executable, str(RUNNER),
             run_id if run_id is not None else SLOT_ARG if grid else "--idle"]
    return " ".join(shlex.quote(p) for p in parts)
